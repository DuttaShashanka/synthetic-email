from dataclasses import asdict
from pathlib import Path
import random
import re
from typing import Any
from .entity_graph import EntityGraph
from .llm import rewrite_with_openrouter
from .models import TransformContext, ValidationReport
from .parsing import parse_email, strip_nonessential_headers
from .privacy import (
    EMAIL_RE,
    FICTITIOUS_DOMAINS,
    PHONE_RE,
    person_aliases,
    person_names,
    redact_direct_identifiers,
    redact_source_terms,
    source_terms,
    stable_index,
)
from .validate import FROM_EMAIL_RE, SIGN_OFF_RE, validate_output

RESPONSE_PREAMBLE_RE = re.compile(
    r"(?i)^\s*(?:here(?:'s| is)\s+(?:(?:a|the)\s+)?(?:rewritten|synthetic)\s+email|rewritten email)\s*:\s*"
)
CLOSING_PHRASE_RE = re.compile(
    r"(?i)\b(?:best\s+regards|kind\s+regards|regards|sincerely|thank\s+you|thanks|best)\s*,?"
)

_OFFLINE_PHRASE_REPLACEMENTS = [
    (re.compile(r"\bI wanted to follow up on\b", re.I), "Following up on"),
    (re.compile(r"\bI wanted to\b", re.I), "I'd like to"),
    (re.compile(r"\bI was wondering\b", re.I), "I'm curious whether"),
    (re.compile(r"\bPlease let me know\b", re.I), "Please advise"),
    (re.compile(r"\bThanks for\b", re.I), "Thank you for"),
    (re.compile(r"\bThanks,\b", re.I), "Thank you,"),
    (re.compile(r"\bBest regards\b", re.I), "Kind regards"),
    (re.compile(r"\bare there any\b", re.I), "could you share any"),
    (re.compile(r"\bif you have\b", re.I), "if you could share"),
    (re.compile(r"\bI will\b", re.I), "We'll"),
    (re.compile(r"\bI am\b", re.I), "I'm"),
    (re.compile(r"\bPlease review\b", re.I), "Please take a look at"),
    (re.compile(r"\bPlease find\b", re.I), "Please see"),
    (re.compile(r"\bPlease call\b", re.I), "Please contact"),
    (re.compile(r"\bhas been approved\b", re.I), "has received approval"),
    (re.compile(r"\bhas been signed\b", re.I), "has been executed"),
    (re.compile(r"\babout the\b", re.I), "regarding the"),
    (re.compile(r"\babout our\b", re.I), "regarding our"),
    (re.compile(r"\blooking forward to\b", re.I), "anticipating"),
    (re.compile(r"\blet me know if\b", re.I), "please confirm if"),
    (re.compile(r"\bwaiting for\b", re.I), "awaiting"),
    (re.compile(r"\bthe meeting\b", re.I), "this meeting"),
    (re.compile(r"\bthe project\b", re.I), "this project"),
    (re.compile(r"\bthe team\b", re.I), "our group"),
    (re.compile(r"\bthe quarterly\b", re.I), "this quarter's"),
    (re.compile(r"\bthe report\b", re.I), "this report"),
    (re.compile(r"\bthe document\b", re.I), "this document"),
    (re.compile(r"\bthe deadline\b", re.I), "this deadline"),
    (re.compile(r"\bthe timeline\b", re.I), "this timeline"),
    (re.compile(r"\bthe agenda\b", re.I), "this agenda"),
    (re.compile(r"\bthe budget\b", re.I), "this budget"),
    (re.compile(r"\bthe file\b", re.I), "this file"),
    (re.compile(r"\bwe need\b", re.I), "we require"),
    (re.compile(r"\bwe should\b", re.I), "we ought to"),
    (re.compile(r"\bI have\b", re.I), "I've"),
    (re.compile(r"\bplease\b", re.I), "kindly"),
]

_OFFLINE_TRANSITIONS = [
    "Following up,",
    "As a reminder,",
    "Regarding this,",
    "Per our conversation,",
    "To circle back,",
]
_OFFLINE_CLOSINGS = [
    "Please let me know your thoughts.",
    "Let me know if you need anything else.",
    "Thanks in advance.",
    "Feel free to reach out.",
    "Looking forward to your response.",
]

_OFFLINE_WORD_SUBS = [
    (re.compile(r"\bresearch and summarize\b", re.I), "look into and briefly review"),
    (re.compile(r"\bthe doctrine offers\b", re.I), "this legal principle provides"),
    (re.compile(r"\bass you can see\b", re.I), "as is evident"),
    (re.compile(r"\bdistinct limitations\b", re.I), "specific constraints"),
    (re.compile(r"\bpotential for use\b", re.I), "possibility for application"),
    (re.compile(r"\bthe duty to act in\s+good faith\b", re.I), "the fiduciary duty"),
    (re.compile(r"\byour request\b", re.I), "your inquiry"),
    (re.compile(r"\byou can see\b", re.I), "you will note"),
    (re.compile(r"\bparticularly\b", re.I), "especially"),
    (re.compile(r"\boffers\b", re.I), "provides"),
    (re.compile(r"\bquarterly budget\b", re.I), "Q3 budget allocation"),
    (re.compile(r"\battached document\b", re.I), "attached file"),
    (re.compile(r"\bfinal numbers\b", re.I), "final figures"),
    (re.compile(r"\bproject timeline\b", re.I), "project schedule"),
    (re.compile(r"\bthe\s+project\b", re.I), "this initiative"),
    (re.compile(r"\bthe\s+team\b", re.I), "our group"),
    (re.compile(r"\bour meeting\b", re.I), "our discussion"),
    (re.compile(r"\blast week\b", re.I), "earlier"),
    (re.compile(r"\breview the attached\b", re.I), "examine the attached"),
    (re.compile(r"\bwe have\b", re.I), "we've"),
    (re.compile(r"\bour strategy\b", re.I), "our approach"),
    (re.compile(r"\bgoing forward\b", re.I), "moving forward"),
    (re.compile(r"\bthe results\b", re.I), "these results"),
    (re.compile(r"\bthe current approach\b", re.I), "this approach"),
    (re.compile(r"\bresearch and analysis\b", re.I), "analysis and review"),
    (re.compile(r"\bfollow-up email\b", re.I), "subsequent message"),
    (re.compile(r"\bnext steps\b", re.I), "next actions"),
    (re.compile(r"\bby end of day\b", re.I), "by close of business"),
    (re.compile(r"\bviable for implementation\b", re.I), "deployable"),
    (re.compile(r"\bidentified several key findings\b", re.I), "uncovered several main insights"),
    (re.compile(r"\bcompleted the initial phase\b", re.I), "finished the initial stage"),
    (re.compile(r"\bprovided an update\b", re.I), "provided an update"),
    (re.compile(r"\binform our strategy\b", re.I), "guide our approach"),
    (re.compile(r"\bthe initial phase\b", re.I), "the first stage"),
    (re.compile(r"\bcirculate\b", re.I), "send out"),
    (re.compile(r"\bI am writing to\b", re.I), "This email serves to"),
    (re.compile(r"\bto inform you that\b", re.I), "to notify you that"),
    (re.compile(r"\bhas been updated\b", re.I), "has been revised"),
    (re.compile(r"\bthe new policy\b", re.I), "the revised policy"),
    (re.compile(r"\beffective immediately\b", re.I), "immediately"),
    (re.compile(r"\ball employees must\b", re.I), "staff must"),
    (re.compile(r"\bin the office\b", re.I), "on-site"),
    (re.compile(r"\bat least three days\b", re.I), "for a minimum of three days"),
    (re.compile(r"\bwe will be enforcing\b", re.I), "we'll enforce"),
    (re.compile(r"\bstarting next Monday\b", re.I), "beginning next week"),
    (re.compile(r"\bIf you have any questions\b", re.I), "Should you have questions"),
    (re.compile(r"\bplease contact\b", re.I), "please reach out to"),
    (re.compile(r"\bplease directly\b", re.I), "directly"),
    (re.compile(r"\bthe updated documentation\b", re.I), "the revised documents"),
    (re.compile(r"\bhas been posted\b", re.I), "is now available"),
    (re.compile(r"\bon the internal portal\b", re.I), "through the company intranet"),
    (re.compile(r"\bthe company policy\b", re.I), "our corporate policy"),
    (re.compile(r"\babout these changes\b", re.I), "about these updates"),
    (re.compile(r"\bplease approve\b", re.I), "please sign off on"),
    (re.compile(r"\bmove forward\b", re.I), "proceed"),
    (re.compile(r"\bsign-off\b", re.I), "approval"),
    (re.compile(r"\bmarketing team\b", re.I), "marketing department"),
    (re.compile(r"\bcampaign launch\b", re.I), "campaign rollout"),
    (re.compile(r"\bdigital advertising\b", re.I), "online advertising"),
    (re.compile(r"\bevent sponsorships\b", re.I), "sponsorship deals"),
    (re.compile(r"\bany additional details\b", re.I), "any further information"),
    (re.compile(r"\bany concerns about\b", re.I), "any issues with"),
    (re.compile(r"\bhave reviewed\b", re.I), "have examined"),
    (re.compile(r"\bsent over\b", re.I), "shared"),
    (re.compile(r"\bready to move forward\b", re.I), "ready to proceed"),
    (re.compile(r"\bagreed-upon changes\b", re.I), "negotiated revisions"),
    (re.compile(r"\bthe final version\b", re.I), "the final draft"),
    (re.compile(r"\bhas been signed\b", re.I), "has been executed"),
    (re.compile(r"\babout the allocation\b", re.I), "about the distribution"),
]

_OFFLINE_FILLERS = [
    "for your review,",
    "as discussed,",
    "for context,",
    "as mentioned,",
    "to recap,",
    "per our conversation,",
    "going forward,",
    "as noted,",
    "just to clarify,",
    "in this context,",
]

_FILLER_SPLIT_RE = re.compile(r"\s+(?:that|which|but|so|however|therefore|moreover|additionally)\s+", re.I)


def _insert_filler(sentence: str, rng: random.Random) -> str:
    words = sentence.split()
    if len(words) < 12:
        return sentence
    comma_idx = [i for i, w in enumerate(words) if w.rstrip().endswith(",")]
    if comma_idx:
        idx = rng.choice(comma_idx)
        filler = rng.choice(_OFFLINE_FILLERS)
        words.insert(idx + 1, filler)
        return " ".join(words)
    split_matches = list(_FILLER_SPLIT_RE.finditer(sentence))
    if split_matches:
        m = rng.choice(split_matches)
        conjunction = m.group().strip()
        filler = rng.choice(_OFFLINE_FILLERS)
        return sentence[: m.start()] + " " + conjunction + " " + filler + " " + sentence[m.end() :]
    if len(words) >= 20:
        idx = rng.randrange(3, len(words) - 3)
        filler = rng.choice(_OFFLINE_FILLERS)
        words.insert(idx, filler)
        return " ".join(words)
    return sentence


_COMMA_FOLLOWED_BY_CONJ = re.compile(r",\s+(?:but|and|or)\s+", re.I)
_COMMA_SPLIT = re.compile(r",\s+")


def _split_long_sentence(sentence: str, rng: random.Random) -> list[str]:
    words = sentence.split()
    if len(words) <= 25:
        return [sentence]
    comma_pos = [m.start() for m in _COMMA_SPLIT.finditer(sentence) if not _COMMA_FOLLOWED_BY_CONJ.match(sentence, m.start())]
    for pos in reversed(comma_pos):
        left = sentence[:pos].strip().rstrip(",")
        right = sentence[pos + 2 :].strip()
        if len(left.split()) >= 8 and len(right.split()) >= 5:
            right = re.sub(r"^([a-z])", lambda m: m.group(1).upper(), right)
            return [left + ".", right]
    return [sentence]


def _rephrase_offline_body(text: str) -> str:
    separator = re.search(r"\r?\n\r?\n", text)
    if separator is None:
        return text
    headers = text[: separator.end()]
    body = text[separator.end() :]

    paragraphs = re.split(r"\n\s*\n", body.strip())
    reformatted: list[str] = []

    for paragraph in paragraphs:
        if re.match(r"^\s*(?:Dear |Hi|Hello|Hey)\s", paragraph, re.I):
            reformatted.append(paragraph)
            continue

        if SIGN_OFF_RE.search(paragraph):
            reformatted.append(paragraph)
            continue

        normalized = re.sub(r"[ \t]*\n[ \t]*", " ", paragraph)

        for pattern, replacement in _OFFLINE_PHRASE_REPLACEMENTS:
            normalized = pattern.sub(replacement, normalized)
        for pattern, replacement in _OFFLINE_WORD_SUBS:
            normalized = pattern.sub(replacement, normalized)

        sentences = re.split(r"(?<=[.!?])\s+", normalized.strip())
        if not sentences or not sentences[0].strip():
            reformatted.append(paragraph)
            continue

        seed = stable_index(paragraph, 10_000_000)
        rng = random.Random(seed)

        expanded: list[str] = []
        for s in sentences:
            s = _insert_filler(s, rng)
            expanded.extend(_split_long_sentence(s, rng))
        sentences = expanded

        if len(sentences) >= 2:
            rng.shuffle(sentences)
            paragraph = " ".join(sentences)
        elif len(sentences) == 1:
            transition = _OFFLINE_TRANSITIONS[seed % len(_OFFLINE_TRANSITIONS)]
            closing = _OFFLINE_CLOSINGS[seed // len(_OFFLINE_TRANSITIONS) % len(_OFFLINE_CLOSINGS)]
            paragraph = f"{transition} {sentences[0]} {closing}"

        reformatted.append(paragraph)

    return headers + "\n\n".join(reformatted)


def offline_rewrite(sanitized: str, industry: str) -> str:
    rewritten = sanitized.replace("[REDACTED_SOURCE_TERM]", "business matter")
    rewritten = rewritten.replace(
        "Northstar Field Systems",
        f"Northstar {industry.title()} Services",
    )
    rewritten = _rephrase_offline_body(rewritten)
    return rewritten


def retry_guidance(report: ValidationReport) -> str:
    errors = " ".join(report.errors).casefold()
    guidance = []
    if "source-derived term leaked" in errors or "source email leaked" in errors or "source phone leaked" in errors:
        guidance.append("Replace all source-specific names, organizations, domains, and contact details with unrelated fictional values.")
    if "excessive 5-gram overlap" in errors:
        guidance.append("Rewrite the subject and body with substantially different wording and sentence structure; retain only the broad communication purpose and approximate length.")
    if "placeholder" in errors:
        guidance.append("Remove all unresolved placeholders and provide complete fictional text.")
    if "sender email and sign-off name do not match" in errors:
        guidance.append("Make the From email local part match the lowercase dot-separated sender name in the sign-off.")
    if "missing required headers" in errors or "no email address" in errors or "non-fictional email domain" in errors:
        guidance.append(
            "Include From, To, Date, and Subject headers and use only these synthetic domains: "
            + ", ".join(FICTITIOUS_DOMAINS)
            + "."
        )
    return " ".join(guidance)


def clean_model_response(candidate: str) -> str:
    candidate = RESPONSE_PREAMBLE_RE.sub("", candidate.strip(), count=1)
    lines = candidate.splitlines()
    if lines and re.fullmatch(r"\s*```(?:email|text)?\s*", lines[0], flags=re.I):
        lines.pop(0)
    if lines and re.fullmatch(r"\s*```\s*", lines[-1]):
        lines.pop()
    while lines and re.fullmatch(r"\s*---+\s*", lines[0]):
        lines.pop(0)
    while lines and re.fullmatch(r"\s*---+\s*", lines[-1]):
        lines.pop()
    candidate = "\n".join(lines).strip()
    candidate = re.sub(r"^---+\s*", "", candidate)
    return candidate.strip()


def strip_model_signoff(candidate: str) -> str:
    sender = FROM_EMAIL_RE.search(candidate)
    if sender is None:
        return candidate.rstrip()

    local_part = sender.group(1).rsplit("@", 1)[0]
    sender_name = re.sub(r"[._-]+", " ", local_part).strip()
    variants = {local_part, sender_name}
    closings = list(CLOSING_PHRASE_RE.finditer(candidate))
    if not closings:
        return candidate.rstrip()

    for closing in closings:
        tail = candidate[closing.start():]
        if len(tail) > max(240, int(len(candidate) * 0.35)):
            continue
        cleaned_tail = CLOSING_PHRASE_RE.sub("", tail)
        for variant in sorted(variants, key=len, reverse=True):
            if variant:
                cleaned_tail = re.sub(
                    rf"(?<!\w){re.escape(variant)}(?!\w)",
                    "",
                    cleaned_tail,
                    flags=re.I,
                )
        if not cleaned_tail.strip(" \t\r\n,.;:-"):
            return candidate[:closing.start()].rstrip()

    signature = SIGN_OFF_RE.search(candidate)
    if signature:
        return candidate[:signature.start()].rstrip()
    return candidate.rstrip()


def preserve_source_identities(candidate: str, sanitized: str) -> str:
    candidate = clean_model_response(candidate)
    candidate = strip_model_signoff(candidate)
    headers = parse_email(sanitized)
    stable_headers = {
        "From": headers.sender,
        "To": ", ".join(headers.recipients),
        "Date": headers.date,
    }
    missing_headers = []
    for header, value in stable_headers.items():
        if not value:
            continue
        pattern = re.compile(rf"(?im)^{header}:\s*.*$")
        replacement = f"{header}: {value}"
        if pattern.search(candidate):
            candidate = pattern.sub(lambda _: replacement, candidate, count=1)
        else:
            missing_headers.append(replacement)
    if missing_headers:
        candidate = "\n".join(missing_headers) + "\n" + candidate.lstrip()

    sender_match = re.search(r"(?im)^From:\s*([^\s<>]+@[^\s<>]+)", candidate)
    if sender_match:
        local_parts = sender_match.group(1).rsplit("@", 1)[0].split(".")
        stable_sender_name = " ".join(part.title() for part in local_parts if part)
        if stable_sender_name:
            candidate = candidate.rstrip() + f"\n\nRegards,\n{stable_sender_name}\n"
    return candidate


def apply_replacement_edits(
    source: str,
    sanitized: str,
    candidate: str | None,
    replacements: list[dict[str, str]],
    edits: dict[int, str],
    graph: EntityGraph,
) -> tuple[str, str, list[dict[str, str]], ValidationReport]:
    updated = [dict(record) for record in replacements]
    changed_indices = set()
    for index, replacement in edits.items():
        if index < 0 or index >= len(updated):
            raise ValueError("Unknown replacement row")
        replacement = replacement.strip()
        if not replacement:
            raise ValueError("Synthetic replacements cannot be blank")
        if replacement != updated[index]["replacement"]:
            updated[index]["replacement"] = replacement
            changed_indices.add(index)

    edited_emails = {
        updated[index].get("person_source_value", "").casefold(): index
        for index in changed_indices
        if updated[index]["entity_type"] == "synthetic_email_v3"
        and updated[index].get("person_source_value")
    }
    for index in list(changed_indices):
        record = updated[index]
        if record["entity_type"] != "synthetic_email_v3":
            continue
        person_source = record.get("person_source_value", "")
        person_index = next(
            (
                person_index
                for person_index, person_record in enumerate(updated)
                if person_record["entity_type"] == "person"
                and person_record["source_value"].casefold() == person_source.casefold()
            ),
            None,
        )
        if person_index is not None:
            local = record["replacement"].rsplit("@", 1)[0]
            updated[person_index]["replacement"] = " ".join(
                part.title() for part in re.split(r"[._-]+", local) if part
            )
            changed_indices.add(person_index)

    for index in list(changed_indices):
        record = updated[index]
        if record["entity_type"] == "person":
            linked_index = edited_emails.get(record["source_value"].casefold())
            if linked_index is not None:
                local = updated[linked_index]["replacement"].rsplit("@", 1)[0]
                record["replacement"] = " ".join(
                    part.title() for part in re.split(r"[._-]+", local) if part
                )
                changed_indices.add(linked_index)
                continue

            replacement_name = re.sub(r"\s+", " ", record["replacement"].strip())
            name_parts = replacement_name.split()
            if len(name_parts) >= 2:
                for linked_index, email_record in enumerate(updated):
                    if (
                        email_record["entity_type"] == "synthetic_email_v3"
                        and email_record.get("person_source_value", "").casefold()
                        == record["source_value"].casefold()
                        and linked_index not in changed_indices
                    ):
                        domain = email_record["replacement"].rsplit("@", 1)[-1]
                        email_record["replacement"] = (
                            f"{name_parts[0].casefold()}.{name_parts[-1].casefold()}@{domain}"
                        )
                        changed_indices.add(linked_index)

    changes = [
        (replacements[index]["replacement"], updated[index]["replacement"])
        for index in sorted(changed_indices)
        if replacements[index]["replacement"] != updated[index]["replacement"]
    ]
    for old_value, new_value in sorted(changes, key=lambda pair: len(pair[0]), reverse=True):
        sanitized = sanitized.replace(old_value, new_value)
        if candidate is not None:
            candidate = candidate.replace(old_value, new_value)

    if candidate is not None:
        candidate = preserve_source_identities(candidate, sanitized)
        report = validate_output(
            source,
            candidate,
            source_terms(source),
            require_fictional_domains=False,
        )
        if not report.valid:
            raise ValueError("Edits were not saved because validation failed: " + "; ".join(report.errors))
    else:
        errors = []
        deny_terms = source_terms(source)
        for index in changed_indices:
            record = updated[index]
            replacement = record["replacement"]
            if record["entity_type"] == "synthetic_email_v3":
                if not EMAIL_RE.fullmatch(replacement):
                    errors.append("Email replacements must be valid email addresses.")
            if PHONE_RE.search(replacement) and record["entity_type"] != "phone":
                errors.append("Replacement contains a phone-like value in the wrong entity field.")
            for term in deny_terms:
                if len(term) >= 3 and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", replacement, re.I):
                    errors.append("A replacement contains a source-derived term.")
                    break
        if errors:
            raise ValueError("Edits were not saved because mapping validation failed: " + "; ".join(sorted(set(errors))))
        report = None

    graph.update_replacements(
        [
            {
                "entity_type": updated[index]["entity_type"],
                "source_value": updated[index]["source_value"],
                "replacement": updated[index]["replacement"],
            }
            for index in sorted(changed_indices)
            if replacements[index]["replacement"] != updated[index]["replacement"]
        ]
    )
    return sanitized, candidate, updated, report


def synthesize(
    source: str,
    industry: str,
    model: str | None = None,
    offline: bool = False,
    max_attempts: int = 3,
    entity_graph_path: str | Path | None = None,
    usage_sink: list[dict[str, Any]] | None = None,
    replacement_sink: list[dict[str, str]] | None = None,
):
    ctx = TransformContext()
    graph = EntityGraph(entity_graph_path)
    deny_terms = source_terms(source)
    sanitized = redact_direct_identifiers(source, ctx, graph)
    sanitized = strip_nonessential_headers(sanitized)
    sanitized = redact_source_terms(
        sanitized,
        deny_terms,
        graph,
        person_names(source),
        person_aliases(source),
        ctx,
    )
    if replacement_sink is not None:
        reviewable_source = strip_nonessential_headers(source)
        replacement_sink.extend(
            dict(record)
            for record in ctx.replacements
            if record["source_value"] in reviewable_source
        )

    last_report = None
    guidance = ""
    for attempt in range(1, max_attempts + 1):
        candidate = (
            offline_rewrite(sanitized, industry)
            if offline
            else rewrite_with_openrouter(sanitized, industry, model, guidance, usage_sink)
        )
        candidate = preserve_source_identities(candidate, sanitized)
        report = validate_output(
            source,
            candidate,
            deny_terms,
            allowed_domains=graph.synthetic_email_domains(),
        )
        if report.valid:
            return candidate, report, sanitized, attempt
        last_report = report
        if offline:
            break
        guidance = retry_guidance(report)

    summary = "; ".join(last_report.errors) if last_report else "unknown validation failure"
    raise RuntimeError(f"Fail-closed: no safe candidate after {max_attempts} attempt(s): {summary}")
