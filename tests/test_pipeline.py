"""Tests for sanitization, pseudonym persistence, and validation."""

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from src.entity_graph import EntityGraph
from src.entity_graph import entity_key

from src.pipeline import apply_replacement_edits, preserve_source_identities, retry_guidance, synthesize, SIGNOFFS
from src.privacy import EMAIL_RE, fake_email, llm_extract_pii, person_aliases, person_names, redact_direct_identifiers, redact_source_terms, source_terms
from src.parsing import parse_email, strip_nonessential_headers
from src.validate import validate_output
from src.models import TransformContext


SOURCE = """From: jane.doe@enron.com
To: john.smith@enron.com
Date: 2001-06-07
Subject: DPR approval

Dear John,
Please approve the DPR transaction for $11 million. Call (412) 490-9048.
Regards,
Jane Doe
"""


_STABLE_NAMES = ["Jordan Smith", "Taylor Reed", "Morgan Lee", "Casey Kim", "Riley Chen"]
_name_counter = {"n": 0}


_STABLE_DOMAINS = ["northstarfieldservices.com", "harborpeakconsulting.com", "verdantbridgesolutions.com"]
_domain_counter = {"n": 0}

_SIGNOFF_PHRASES = "|".join(sorted((p for p, _ in SIGNOFFS), key=len, reverse=True))


def _fake_generate_pseudonym(entity_type, source_value, industry, model=None):
    if entity_type == "person":
        _name_counter["n"] += 1
        return _STABLE_NAMES[(_name_counter["n"] - 1) % len(_STABLE_NAMES)]
    if entity_type == "organization":
        return "Acme Solutions"
    if entity_type == "project":
        return "Project Phoenix"
    if entity_type == "domain":
        _domain_counter["n"] += 1
        return _STABLE_DOMAINS[(_domain_counter["n"] - 1) % len(_STABLE_DOMAINS)]
    raise ValueError(f"Unexpected entity type: {entity_type}")


@pytest.fixture(autouse=True)
def _patch_llm():
    """Patch generate_pseudonym and extract_pii_entities so tests run without an OpenRouter API key."""
    _name_counter["n"] = 0
    _domain_counter["n"] = 0

    def _fake_extract_pii(text, industry, model=None):
        """Mock PII extraction: return empty sets to mimic regex-only fallback."""
        return [], []

    with patch(
        "src.privacy.generate_pseudonym", side_effect=_fake_generate_pseudonym
    ), patch("src.llm.extract_pii_entities", side_effect=_fake_extract_pii):
        yield

def test_redacts_direct_identifiers():
    from src.models import TransformContext
    from src.entity_graph import EntityGraph

    graph = EntityGraph()
    sanitized = redact_direct_identifiers(SOURCE, TransformContext(), graph)
    assert "@enron.com" not in sanitized.lower()
    assert "(412) 490-9048" not in sanitized
    assert "$11 million" not in sanitized.lower()


def test_validator_blocks_source_leakage():
    unsafe = """From: jane.doe@enron.com
To: person@northstarfieldservices.com
Date: 2026-01-01
Subject: DPR approval

Dear Team, this is an Enron message from Jane Doe with enough body text to look plausible in a test fixture. Regards."""
    report = validate_output(SOURCE, unsafe, source_terms(SOURCE))
    assert not report.valid
    assert report.errors


def test_validator_ignores_generic_email_words_and_substrings():
    source = """From: original.person@source.test
To: review.team@source.test
Date: 2025-04-01
Subject: Please review the process

Please follow the process for routing the request.
"""
    candidate = """From: Morgan Lee <morgan@northstarfieldservices.com>
To: Priya Shah <priya@northstarfieldservices.com>
Date: 2026-02-12
Subject: Annual operations update

Please review the annual operations plan before the next planning meeting. The
team has prepared a revised schedule that gives each department enough time to
confirm staffing, review dependencies, and share questions with the coordinator.
We will discuss progress during the regular weekly meeting and send a concise
summary afterward so everyone can track the next steps.
"""

    terms = source_terms(source)
    report = validate_output(source, candidate, terms | {"ann"})

    assert "please" not in terms
    assert "the" not in terms
    assert "let" not in source_terms(source.replace("Please follow", "Let me"))
    assert "Source-derived term leaked: ann" not in report.errors
    assert not any(error in report.errors for error in (
        "Source-derived term leaked: please",
        "Source-derived term leaked: the",
    ))


def test_example_sanitization_removes_identifiers(tmp_path):
    from src.models import TransformContext

    source = (Path(__file__).parent.parent / "examples" / "input_email.txt").read_text(encoding="utf-8")
    graph = EntityGraph(tmp_path / "example-graph.sqlite")
    ctx = TransformContext()
    sanitized = redact_direct_identifiers(source, ctx, graph)
    sanitized = strip_nonessential_headers(sanitized)
    deny_terms = source_terms(source)
    sanitized = redact_source_terms(
        sanitized,
        deny_terms,
        graph,
        person_names(source),
        person_aliases(source),
        ctx,
    )
    replacement_records = [
        dict(record)
        for record in ctx.replacements
        if record["source_value"] in strip_nonessential_headers(source)
    ]

    assert "enron" not in sanitized.lower()
    assert {record["entity_type"] for record in replacement_records} >= {
        "person",
        "synthetic_email_v3",
        "date",
        "phone",
        "money",
    }


def test_entity_graph_persists_pseudonyms_and_links_across_documents(tmp_path):
    graph_path = tmp_path / "entity-graph.sqlite"
    first_source = """From: Jane Doe <jane.doe@enron.com>
To: John Smith <john.smith@enron.com>
Date: 2001-06-07
Subject: DPR approval

Please call (412) 490-9048 about Acme Holdings LLC and DPR. Jane Doe will review it.
Regards,
Jane Doe
"""
    second_source = """From: John Smith <john.smith@enron.com>
To: Jane Doe <jane.doe@enron.com>
Date: 2001-06-07
Subject: DPR update

Please call (412) 490-9048 about Acme Holdings LLC and DPR. Jane Doe will review it.
Regards,
John Smith
"""

    first_graph = EntityGraph(graph_path)
    first = redact_direct_identifiers(first_source, TransformContext(), first_graph)
    first = redact_source_terms(first, source_terms(first_source), first_graph, person_names(first_source))
    second_graph = EntityGraph(graph_path)
    second = redact_direct_identifiers(second_source, TransformContext(), second_graph)
    second = redact_source_terms(second, source_terms(second_source), second_graph, person_names(second_source))

    assert "jane.doe@enron.com" not in first.lower()
    assert "jane.doe@enron.com" not in second.lower()
    assert "(412) 490-9048" not in first
    assert "(412) 490-9048" not in second
    assert "Acme Holdings LLC" not in first
    assert "Acme Holdings LLC" not in second
    assert "DPR" not in first
    assert "DPR" not in second
    first_lines = first.splitlines()
    second_lines = second.splitlines()
    jane_header = first_lines[0].split(": ", 1)[1].split(" <", 1)[0]
    jane_body_mention = first_lines[5].split(" will review it.", 1)[0].rsplit(" ", 2)
    jane_signature = first_lines[-1]
    jane_recipient = second_lines[1].split(": ", 1)[1].split(" <", 1)[0]
    assert jane_header == jane_body_mention[-2] + " " + jane_body_mention[-1]
    assert jane_header == jane_signature == jane_recipient
    assert first_graph.relationship_count() >= 4


def test_body_person_mentions_use_persistent_pseudonym(tmp_path):
    graph_path = tmp_path / "body-mentions.sqlite"
    first_source = "Subject: Review update\n\nMark Haedicke will review the revised terms."
    second_source = "Subject: Approval status\n\nWe asked Mark Haedicke to review the revised terms."
    first_graph = EntityGraph(graph_path)
    second_graph = EntityGraph(graph_path)

    first = redact_source_terms(
        first_source,
        source_terms(first_source),
        first_graph,
        person_names(first_source),
    )
    second = redact_source_terms(
        second_source,
        source_terms(second_source),
        second_graph,
        person_names(second_source),
    )
    replacement = first_graph.resolve("person", "Mark Haedicke", lambda: "")

    assert replacement in first
    assert replacement in second
    assert "Mark Haedicke" not in first
    assert "Mark Haedicke" not in second


def test_registry_default_path_does_not_depend_on_working_directory(tmp_path, monkeypatch):
    from src.entity_graph import __file__ as entity_graph_file

    expected_path = Path(entity_graph_file).resolve().parent.parent / "data/processed/entity_graph.sqlite"
    monkeypatch.chdir(tmp_path)

    graph = EntityGraph()

    assert graph.path == expected_path


def test_sender_email_and_signature_share_pseudonym_across_runs(tmp_path):
    source = """From: ccarver@alfers-carver.com
To: gerald.nemec@enron.com
Subject: Review memo

Please review the attached memo.

Regards,
Craig Carver
"""
    graph_path = tmp_path / "sender-identity.sqlite"
    outputs = []
    for _ in range(2):
        graph = EntityGraph(graph_path)
        sanitized = redact_direct_identifiers(source, TransformContext(), graph)
        sanitized = strip_nonessential_headers(sanitized)
        sanitized = redact_source_terms(
            sanitized,
            source_terms(source),
            graph,
            person_names(source),
            person_aliases(source),
        )
        outputs.append(sanitized)

    sender_local = outputs[0].split("From: ", 1)[1].split("@", 1)[0]
    sign_off = outputs[0].rstrip().splitlines()[-1].casefold()

    assert outputs[0] == outputs[1]
    assert sender_local == sign_off.replace(" ", ".")


def test_email_domains_look_real_but_use_reserved_example_com(tmp_path):
    graph = EntityGraph(tmp_path / "email-domain.sqlite")
    source_email = "person@source.test"
    graph.resolve("synthetic_email_v2", source_email, lambda: "old@northstar.example.com")
    ctx = TransformContext()

    first = fake_email(source_email, ctx, graph, "Alex Morgan")
    second = fake_email(source_email, TransformContext(), EntityGraph(graph.path), "Alex Morgan")

    assert first.endswith((
        "@northstarfieldservices.com",
        "@harborpeakconsulting.com",
        "@verdantbridgesolutions.com",
    ))
    assert second == first


def test_replacement_review_excludes_transport_metadata_matches(tmp_path):
    from src.models import TransformContext

    source = """Message-ID: <3252276.1075842650293.JavaMail.evans@thyme>
From: sender.person@enron.com
To: recipient.person@enron.com
Date: Thu, 14 Sep 2000 02:52:00 -0700 (PDT)
Subject: Scheduling update
X-Folder: \\Private_User\\Notes inbox 555-014-2288
X-FileName: mailbox_2021.nsf

Please call (412) 490-9048 to confirm the revised meeting schedule. The team
will send an updated agenda before the next planning session.
"""
    graph = EntityGraph(tmp_path / "review-ledger.sqlite")
    ctx = TransformContext()
    sanitized = redact_direct_identifiers(source, ctx, graph)
    sanitized = strip_nonessential_headers(sanitized)
    redact_source_terms(
        sanitized,
        source_terms(source),
        graph,
        person_names(source),
        person_aliases(source),
        ctx,
    )

    reviewable_source = strip_nonessential_headers(source)
    records = [
        dict(record)
        for record in ctx.replacements
        if record["source_value"] in reviewable_source
    ]
    source_values = [record["source_value"] for record in records]
    assert not any("3252276" in value for value in source_values)
    assert not any("Private_User" in value for value in source_values)
    assert not any("mailbox_2021" in value for value in source_values)
    assert "(412) 490-9048" in source_values


def test_graph_mapping_can_be_updated_and_resolved_in_a_new_connection(tmp_path):
    graph_path = tmp_path / "editable-graph.sqlite"
    first_graph = EntityGraph(graph_path)
    first_graph.resolve("person", "Alice Source", lambda: "Old Name")

    first_graph.update_replacement("person", "Alice Source", "Alex Morgan")

    assert EntityGraph(graph_path).resolve("person", "Alice Source", lambda: "Unexpected") == "Alex Morgan"


def test_editing_person_replacement_updates_email_output_and_graph(tmp_path):
    graph_path = tmp_path / "edited-person.sqlite"
    graph = EntityGraph(graph_path)
    source = """From: alice.source@enron.com
To: bob.team@enron.com
Date: 2001-06-07
Subject: Source planning update

Please review the planning summary and send your response before the scheduled
meeting. The team prepared a complete outline with timelines, review steps, and
responsibility assignments for the next phase of work.
"""
    sanitized = """From: old.name@northstarfieldservices.com
To: bob.team@harborpeakconsulting.com
Date: 2024-08-14
Subject: Fictional planning update

Please review the fictional planning summary and share feedback ahead of the
scheduled meeting. The project group prepared an outline of timelines, review
steps, and responsibilities for the next phase of work.
"""
    candidate = sanitized.rstrip() + "\n\nRegards,\nOld Name\n"
    replacements = [
        {"entity_type": "person", "source_value": "Alice Source", "replacement": "Old Name"},
        {
            "entity_type": "synthetic_email_v3",
            "source_value": "alice.source@enron.com",
            "replacement": "old.name@northstarfieldservices.com",
            "person_source_value": "Alice Source",
        },
    ]
    graph.resolve("person", "Alice Source", lambda: "Old Name")
    graph.resolve(
        "synthetic_email_v3",
        "alice.source@enron.com",
        lambda: "old.name@northstarfieldservices.com",
    )

    updated_sanitized, updated_candidate, updated_records, report = apply_replacement_edits(
        source,
        sanitized,
        candidate,
        replacements,
        {1: "alex.morgan@customfictional.com"},
        graph,
    )

    assert report.valid
    assert "alex.morgan@customfictional.com" in updated_sanitized
    assert "alex.morgan@customfictional.com" in updated_candidate
    assert re.search(rf"(?:{_SIGNOFF_PHRASES}),\n(?:Alex Morgan|Alex)$", updated_candidate.rstrip())
    assert updated_records[1]["replacement"] == "alex.morgan@customfictional.com"
    assert graph.resolve("person", "Alice Source", lambda: "") == "Alex Morgan"
    assert graph.resolve(
        "synthetic_email_v3", "alice.source@enron.com", lambda: ""
    ) == "alex.morgan@customfictional.com"


def test_generated_candidates_still_require_configured_domain():
    candidate = """From: alex.lee@customfictional.com
To: morgan.chen@northstarfieldservices.com
Date: 2026-03-10
Subject: Weekly coordination update

Please review the revised project schedule and share any required changes with
the team before our regular planning meeting. The updated timeline gives each
group enough time to review dependencies, confirm staffing, and raise concerns.
We will circulate a summary after all reviewers have responded.
"""

    report = validate_output("Unrelated original source", candidate, ())

    assert "Non-fictional email domain present." in report.errors


def test_mapping_can_be_saved_when_generation_has_no_accepted_candidate(tmp_path):
    source = """From: jane.doe@enron.com
To: john.smith@enron.com
Date: 2001-06-07
Subject: Schedule update

Please call (412) 490-9048 to confirm the revised schedule. The team has
prepared a new plan for the next planning meeting.
"""
    graph = EntityGraph(tmp_path / "blocked-output.sqlite")
    source_phone = "(412) 490-9048"
    graph.resolve("phone", source_phone, lambda: "+1-202-555-1234")
    sanitized = source.replace(source_phone, "+1-202-555-1234")
    records = [
        {"entity_type": "phone", "source_value": source_phone, "replacement": "+1-202-555-1234"}
    ]

    updated_sanitized, updated_candidate, updated_records, report = apply_replacement_edits(
        source,
        sanitized,
        None,
        records,
        {0: "+1-202-555-9876"},
        graph,
    )

    assert updated_candidate is None
    assert report is None
    assert "+1-202-555-9876" in updated_sanitized
    assert updated_records[0]["replacement"] == "+1-202-555-9876"
    assert graph.resolve("phone", source_phone, lambda: "") == "+1-202-555-9876"


def test_saved_custom_email_domain_is_persisted_in_graph(tmp_path):
    from src.models import TransformContext

    source = (Path(__file__).parent.parent / "examples" / "input_email.txt").read_text(encoding="utf-8")
    graph_path = tmp_path / "custom-domain-reuse.sqlite"

    graph = EntityGraph(graph_path)
    ctx = TransformContext()
    sanitized = redact_direct_identifiers(source, ctx, graph)
    sanitized = strip_nonessential_headers(sanitized)
    redact_source_terms(
        sanitized,
        source_terms(source),
        graph,
        person_names(source),
        person_aliases(source),
        ctx,
    )

    email_index = next(
        index for index, record in enumerate(ctx.replacements)
        if record["entity_type"] == "synthetic_email_v3"
    )
    prior_email = ctx.replacements[email_index]["replacement"]
    custom_email = prior_email.rsplit("@", 1)[0] + "@userfictional.com"
    graph.update_replacement("synthetic_email_v3", ctx.replacements[email_index]["source_value"], custom_email)

    second_graph = EntityGraph(graph_path)
    ctx2 = TransformContext()
    second_sanitized = redact_direct_identifiers(source, ctx2, second_graph)
    second_sanitized = strip_nonessential_headers(second_sanitized)
    redact_source_terms(
        second_sanitized,
        source_terms(source),
        second_graph,
        person_names(source),
        person_aliases(source),
        ctx2,
    )

    assert custom_email in second_sanitized
    assert "userfictional.com" in second_graph.synthetic_email_domains()


def test_generated_variation_keeps_pseudonymized_identity_headers_stable():
    sanitized = """From: alex.morgan@northstarfieldservices.com
To: sam.lee@harborpeakconsulting.com
Date: 2024-08-14
Subject: Stable synthetic subject

Sanitized message body.
"""
    first_candidate = """From: Olivia Chen <olivia.chen@verdantbridgesolutions.com>
To: Peter Hall <peter.hall@northstarfieldservices.com>
Date: 2025-01-02
Subject: Re: A different generated subject

Different generated body.

Best regards,
Olivia Chen
"""
    second_candidate = """From: Taylor Reed <taylor.reed@northstarfieldservices.com>
To: Quinn Park <quinn.park@harborpeakconsulting.com>
Date: 2025-09-10
Subject: Another model subject

Another generated body.

Thanks,
Taylor Reed
"""

    first = preserve_source_identities(first_candidate, sanitized)
    second = preserve_source_identities(second_candidate, sanitized)

    stable_identity_headers = ("From: alex.morgan@northstarfieldservices.com", "To: sam.lee@harborpeakconsulting.com", "Date: 2024-08-14")
    assert all(header in first and header in second for header in stable_identity_headers)
    assert re.search(rf"(?:{_SIGNOFF_PHRASES}),\n(?:Alex Morgan|Alex)", first)
    assert re.search(rf"(?:{_SIGNOFF_PHRASES}),\n(?:Alex Morgan|Alex)", second)
    assert "Olivia Chen" not in first
    assert "Taylor Reed" not in second


def test_model_preamble_and_duplicate_closings_are_removed():
    sanitized = """From: alex.morgan@northstarfieldservices.com
To: sam.lee@harborpeakconsulting.com
Date: 2024-08-14
Subject: Stable synthetic subject

Sanitized message body.
"""
    candidate = """Here is a rewritten email:
---
From: Maria Lewis <maria.lewis@harborpeakconsulting.com>
To: Sam Lee <sam.lee@northstarfieldservices.com>
Date: 2026-10-06
Subject: A revised coordination note

Please review the attached update and send any comments before the next meeting.
Best, maria.lewis Regards, Maria Lewis
---
"""

    output = preserve_source_identities(candidate, sanitized)

    assert output.startswith("From: alex.morgan@northstarfieldservices.com")
    assert "Here is a rewritten email" not in output
    assert "---" not in output
    assert "maria.lewis" not in output
    assert "Maria Lewis" not in output
    assert re.search(rf"(?:{_SIGNOFF_PHRASES}),\n(?:Alex Morgan|Alex)", output)
    assert re.search(rf"(?:{_SIGNOFF_PHRASES}),\n(?:Alex Morgan|Alex)$", output.rstrip())


def test_rfc_email_date_is_replaced_deterministically(tmp_path):
    source = "From: sender@enron.com\nTo: recipient@enron.com\nDate: Thu, 14 Sep 2000 02:52:00 -0700 (PDT)\n\nBody."
    graph = EntityGraph(tmp_path / "date-graph.sqlite")
    sanitized = redact_direct_identifiers(source, TransformContext(), graph)

    assert "14 Sep 2000" not in sanitized
    assert "02:52:00" not in sanitized
    assert re.search(r"Date: 202[4-7]-\d{2}-\d{2}", sanitized)


def test_validator_rejects_sender_signature_mismatch():
    candidate = """From: alex.lee@northstarfieldservices.com
To: morgan.chen@northstarfieldservices.com
Date: 2026-03-10
Subject: Weekly coordination update

Please review the updated schedule and share any required changes with the
team before our regular planning meeting. The revised timeline includes enough
time for each group to check dependencies, confirm staffing, and raise concerns.
We will circulate the final version once all reviewers have responded.

Regards,
Morgan Chen
"""

    report = validate_output("Original source", candidate, ())

    assert "Sender email and sign-off name do not match." in report.errors


def test_nonessential_headers_are_removed_before_provider_input():
    source = """Message-ID: <unique-id@internal-host>
From: sender@source.example
To: recipient@source.example
Date: Thu, 14 Sep 2000 02:52:00 -0700
Subject: Confidential memo
X-Folder: /private/folder
X-FileName: source-mailbox.nsf

Email body remains available for rewriting.
"""

    provider_input = strip_nonessential_headers(source)

    assert "Message-ID" not in provider_input
    assert "X-Folder" not in provider_input
    assert "X-FileName" not in provider_input
    assert "From:" in provider_input
    assert "Subject:" in provider_input
    assert "Email body remains available" in provider_input


def test_parse_email_prefers_legacy_display_names_and_keeps_date_subject():
    source = """Message-ID: <id@host>
Date: Thu, 14 Sep 2000 02:52:00 -0700 (PDT)
From: ccarver@alfers-carver.com
To: gerald.nemec@enron.com
Subject: Memo re good faith
X-From: "Craig R. Carver" <CCarver@alfers-carver.com>
X-To: "Nemec, Gerald (E-mail)" <gerald.nemec@enron.com>
Cc: guido.caranti@enron.com
X-Cc: "Caranti, Guido" <guido.caranti@enron.com>
X-Bcc:
Bcc: guido.caranti@enron.com
X-Bcc:
X-Folder: \\Private_User\\Notes inbox

Message body.
"""

    parsed = parse_email(source)

    assert parsed.sender == "Craig R. Carver"
    assert parsed.recipients == ["Gerald Nemec", "Guido Caranti"]
    assert parsed.date == "Thu, 14 Sep 2000 02:52:00 -0700 (PDT)"
    assert parsed.subject == "Memo re good faith"
    assert not any("Folder" in recipient for recipient in parsed.recipients)
    assert parsed.recipients.count("Guido Caranti") == 1
    assert not any("@enron.com" in recipient for recipient in parsed.recipients)


def test_legacy_header_name_aliases_are_pseudonymized_in_body_and_signature(tmp_path):
    source = """From: ccarver@alfers-carver.com
To: gerald.nemec@enron.com
X-From: \"Craig R. Carver\" <ccarver@alfers-carver.com>
X-To: \"Nemec, Gerald (E-mail)\" <gerald.nemec@enron.com>
Subject: Review memo

Gerald: Please review the memo.

Craig
"""
    graph = EntityGraph(tmp_path / "legacy-headers.sqlite")
    terms = source_terms(source)
    sanitized = redact_direct_identifiers(source, TransformContext(), graph)
    sanitized = strip_nonessential_headers(sanitized)
    sanitized = redact_source_terms(
        sanitized,
        terms,
        graph,
        person_names(source),
        person_aliases(source),
    )

    assert "Gerald" not in sanitized
    assert "Craig" not in sanitized
    assert "@enron.com" not in sanitized
    assert "@alfers-carver.com" not in sanitized


def test_retry_guidance_is_category_based_and_does_not_echo_leaked_terms():
    from src.models import ValidationReport

    report = ValidationReport(
        valid=False,
        risk_score=1.0,
        errors=["Source-derived term leaked: let", "Excessive 5-gram overlap with source: 0.326"],
        warnings=[],
        metrics={},
    )

    guidance = retry_guidance(report)

    assert "substantially different wording" in guidance
    assert "source-specific names" in guidance
    assert "let" not in guidance


def test_llm_extracted_pii_from_body_is_pseudonymized(tmp_path):
    """Verify that person names found by LLM extraction (e.g. in body text) get pseudonymized."""
    from src.llm import extract_pii_entities

    source = """From: bill.giuliani@enron.com
To: andrew.fastow@enron.com
Date: 2001-06-07
Subject: DASH approval

Dear Andrew,

The DASH has been approved and signed by RAC and JEDI II and is now
awaiting Mark Haedicke's review and approval. Please call me at
(412) 490-9048 if you have questions.

Best regards,
Bill Giuliani
"""

    def _mock_extract(text, industry, model=None):
        return [("person", "Mark Haedicke")]

    _name_counter["n"] = 0
    _domain_counter["n"] = 0

    with patch(
        "src.privacy.generate_pseudonym", side_effect=_fake_generate_pseudonym
    ), patch("src.llm.extract_pii_entities", side_effect=_mock_extract):
        graph = EntityGraph(tmp_path / "llm-pii.sqlite")
        ctx = TransformContext()
        deny_terms = source_terms(source)

        llm_people, llm_orgs = llm_extract_pii(source, "energy")
        augmented_people = person_names(source) | llm_people
        deny_terms = deny_terms | llm_orgs

        sanitized = redact_direct_identifiers(source, ctx, graph, "energy")
        sanitized = strip_nonessential_headers(sanitized)
        sanitized = redact_source_terms(
            sanitized,
            deny_terms,
            graph,
            augmented_people,
            person_aliases(source),
            ctx,
            "energy",
        )

        assert "mark haedicke" not in sanitized.lower()
        assert "haedicke" not in sanitized.lower()


def test_salutation_uses_same_pseudonym_as_known_person(tmp_path):
    """Verify 'Dear Andrew' uses the first name of the pseudonym for 'Andrew Fastow'
    from the To header, not a separate generated name."""
    from src.models import TransformContext

    source = """From: Bill Giuliani <bill.giuliani@enron.com>
To: Andrew Fastow <andrew.fastow@enron.com>
Date: 2001-06-07
Subject: Test

Dear Andrew, please review the DPR transaction for $11 million.
Regards,
Bill Giuliani
"""
    graph = EntityGraph(tmp_path / "salutation.sqlite")
    ctx = TransformContext()
    sanitized = redact_direct_identifiers(source, ctx, graph, "energy")

    andrew_replacement = graph.resolve("person", "Andrew Fastow", lambda: "")
    expected_salutation_name = andrew_replacement.split()[0] if andrew_replacement else ""

    assert "Dear Andrew" not in sanitized
    assert expected_salutation_name, "Expected a non-empty pseudonym for Andrew Fastow"
    assert f"Dear {expected_salutation_name}" in sanitized


def test_closing_phrase_is_not_always_regards():
    """Verify that preserve_source_identities uses varied closings across runs."""
    sanitized = """From: alex.morgan@northstarfieldservices.com
To: sam.lee@harborpeakconsulting.com
Date: 2024-08-14
Subject: Stable synthetic subject

Sanitized message body.
"""
    candidate = """From: Olivia Chen <olivia.chen@verdantbridgesolutions.com>
To: Peter Hall <peter.hall@northstarfieldservices.com>
Date: 2025-01-02
Subject: Re: A different generated subject

Different generated body.

Best regards,
Olivia Chen
"""

    closings = set()
    for _ in range(20):
        output = preserve_source_identities(candidate, sanitized)
        lines = output.rstrip().splitlines()
        closing_phrase = lines[-2].rstrip(",")
        closings.add(closing_phrase)

    assert len(closings) > 1, f"Expected variety in closings, got {closings}"
    valid_closings = {"Thanks", "Best", "Thank you", "Regards", "Best regards", "Kind regards", "Best wishes"}
    assert closings.issubset(valid_closings)


def test_single_name_signoff_is_stripped_not_duplicated():
    """Verify a model sign-off using just the sender's first name is stripped, not duplicated."""
    sanitized = """From: jensen.rutledge@northstarfieldservices.com
To: morgan.chen@harborpeakconsulting.com
Date: 2024-08-14
Subject: Stable synthetic subject

Sanitized message body.
"""
    candidate = """From: Jensen Rutledge <jensen.rutledge@verdantbridgesolutions.com>
To: Morgan Chen <morgan.chen@northstarfieldservices.com>
Date: 2025-01-02
Subject: Re: A different generated subject

Different generated body.

Thanks,
Jensen
"""
    output = preserve_source_identities(candidate, sanitized)

    signoff_count = len(re.findall(r"(?i)\b(?:thanks|regards|best|kind regards|thank you)\b\s*,?", output))
    assert signoff_count <= 1, f"Expected at most one sign-off phrase, got {signoff_count}:\n{output}"


def test_to_header_name_before_email_links_to_salutation(tmp_path):
    """Verify 'To: Sylvan cameron.rutledge@...' links Sylvan to the email, not a separate identity."""
    from src.models import TransformContext

    source = """From: reed.fenton@norconcorp.com
To: Sylvan cameron.rutledge@northshorecapitalgroup.com
Date: 2025-03-06 07:48:00
Subject: Update on the Aurora Initiative Investment

Dear Sylvan,
Please review the attached materials.
"""

    graph = EntityGraph(tmp_path / "simple-header.sqlite")
    ctx = TransformContext()
    sanitized = redact_direct_identifiers(source, ctx, graph, "energy")

    sylvan_replacement = graph.resolve("person", "Sylvan", lambda: "")
    assert sylvan_replacement, "Expected Sylvan to be in the entity graph"

    lines = sanitized.splitlines()
    to_line = next(line for line in lines if line.lower().startswith("to:"))
    salutation_line = next(line for line in lines if line.lower().startswith("dear"))

    to_email_match = EMAIL_RE.search(to_line)
    assert to_email_match, f"Expected an email in To header: {to_line}"
    to_local = to_email_match.group(0).rsplit("@", 1)[0]
    to_first = to_local.split(".")[0] if "." in to_local else to_local

    salutation_name = salutation_line.split("Dear ", 1)[1].rstrip(",.")

    sylvan_parts = sylvan_replacement.split()
    sylvan_first = sylvan_parts[0].lower()

    assert to_first.lower() == sylvan_first, (
        f"To header local part '{to_first}' doesn't match salutation name '{sylvan_first}' from pseudonym '{sylvan_replacement}'"
    )


def test_bare_email_with_salutation_single_name_links_to_email(tmp_path):
    """Verify 'To: snhka.harper@nron.com' with 'Dear Snhka' links the name to the email."""
    from src.models import TransformContext

    source = """From: willian.giuliani@enron.com
To: snhka.harper@nron.com
Date: 2001-06-07 07:48:00
Subject: Approval of the DPR transaction

Dear Snhka,
Please review the transaction details.
"""

    graph = EntityGraph(tmp_path / "bare-email.sqlite")
    ctx = TransformContext()
    sanitized = redact_direct_identifiers(source, ctx, graph, "energy")

    snhka_replacement = graph.resolve("person", "Snhka", lambda: "")
    assert snhka_replacement, "Expected Snhka to be in the entity graph"

    lines = sanitized.splitlines()
    to_line = next(line for line in lines if line.lower().startswith("to:"))

    to_email_match = EMAIL_RE.search(to_line)
    assert to_email_match, f"Expected an email in To header: {to_line}"
    to_local = to_email_match.group(0).rsplit("@", 1)[0]
    to_first = to_local.split(".")[0] if "." in to_local else to_local

    snhka_parts = snhka_replacement.split()
    snhka_first = snhka_parts[0].lower()

    assert to_first.lower() == snhka_first, (
        f"To header local part '{to_first}' doesn't match salutation name '{snhka_first}' from pseudonym '{snhka_replacement}'"
    )
