from dataclasses import asdict
from pathlib import Path
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
)
from .validate import FROM_EMAIL_RE, SIGN_OFF_RE, validate_output

RESPONSE_PREAMBLE_RE = re.compile(
    r"(?i)^\s*(?:here(?:'s| is)\s+(?:(?:a|the)\s+)?(?:rewritten|synthetic)\s+email|rewritten email)\s*:\s*"
)
CLOSING_PHRASE_RE = re.compile(
    r"(?i)\b(?:best\s+regards|kind\s+regards|regards|sincerely|thank\s+you|thanks|best)\s*,?"
)


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
        candidate = rewrite_with_openrouter(sanitized, industry, model, guidance, usage_sink)
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
        guidance = retry_guidance(report)

    summary = "; ".join(last_report.errors) if last_report else "unknown validation failure"
    raise RuntimeError(f"Fail-closed: no safe candidate after {max_attempts} attempt(s): {summary}")
