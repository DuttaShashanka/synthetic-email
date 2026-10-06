"""Post-generation privacy, structure, and overlap validation."""

import re
from typing import Iterable, Set
from .models import ValidationReport
from .privacy import EMAIL_RE, FICTITIOUS_DOMAINS, PHONE_RE, PLACEHOLDER_RE

HEADER_RE = re.compile(r"^(From|To|Date|Subject):\s*.+$", re.I | re.M)
FROM_EMAIL_RE = re.compile(r"^From:\s*(?:[^<\n]*<)?([^\s<>]+@[^\s<>]+)", re.I | re.M)
SIGN_OFF_RE = re.compile(
    r"(?im)^\s*(?:best regards|kind regards|regards|sincerely|thank you|thanks),?\s*\n"
    r"\s*([A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+){0,2})\s*$"
)
TOKEN_RE = re.compile(r"[a-z0-9]{3,}", re.I)


def normalized_ngrams(text: str, n: int = 5) -> Set[str]:
    """Return the set of normalized word n-grams in a text."""
    tokens = TOKEN_RE.findall(text.lower())
    return {" ".join(tokens[i:i+n]) for i in range(max(0, len(tokens) - n + 1))}


def validate_output(
    source: str,
    candidate: str,
    deny_terms: Iterable[str],
    require_fictional_domains: bool = True,
    allowed_domains: Iterable[str] = (),
) -> ValidationReport:
    """Check a synthetic candidate for leakage, placeholders, and structure."""
    errors, warnings = [], []
    low = candidate.lower()
    source_low = source.lower()

    for term in sorted({t.lower().strip() for t in deny_terms if t}, key=len, reverse=True):
        term_pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)")
        if len(term) >= 3 and term_pattern.search(low):
            errors.append(f"Source-derived term leaked: {term}")

    for value in EMAIL_RE.findall(source_low):
        if value in low:
            errors.append(f"Source email leaked: {value}")
    for value in PHONE_RE.findall(source):
        if value.lower() in low:
            errors.append(f"Source phone leaked: {value}")

    if "enron" in low or "@enron." in low:
        errors.append("Source organization/domain reference detected.")
    if PLACEHOLDER_RE.search(candidate) or "[redacted_source_term]" in low:
        errors.append("Unresolved sanitization placeholder detected.")

    addresses = EMAIL_RE.findall(candidate)
    if not addresses:
        errors.append("No email address present in output.")
    elif require_fictional_domains and any(
        address.rsplit("@", 1)[-1].casefold()
        not in ({domain.casefold() for domain in FICTITIOUS_DOMAINS} | {domain.casefold() for domain in allowed_domains})
        for address in addresses
    ):
        errors.append("Non-fictional email domain present.")

    sender = FROM_EMAIL_RE.search(candidate)
    signature = SIGN_OFF_RE.search(candidate)
    if sender and signature:
        sender_local = sender.group(1).rsplit("@", 1)[0].casefold()
        signature_parts = TOKEN_RE.findall(signature.group(1).casefold())
        expected_local = ".".join(signature_parts[:2])
        if len(signature_parts) >= 2 and sender_local != expected_local:
            errors.append("Sender email and sign-off name do not match.")

    headers = {m.group(1).lower() for m in HEADER_RE.finditer(candidate)}
    missing = {"from", "to", "date", "subject"} - headers
    if missing:
        errors.append("Missing required headers: " + ", ".join(sorted(missing)))
    if len(candidate.strip()) < 160:
        errors.append("Output is too short to be a plausible enterprise email.")

    source_grams = normalized_ngrams(source)
    out_grams = normalized_ngrams(candidate)
    overlap = len(source_grams & out_grams) / max(1, len(out_grams))
    if overlap > 0.08:
        errors.append(f"Excessive 5-gram overlap with source: {overlap:.3f}")
    elif overlap > 0.03:
        warnings.append(f"Moderate 5-gram overlap with source: {overlap:.3f}")

    risk = min(1.0, 0.2 * len(errors) + 0.05 * len(warnings) + overlap)
    return ValidationReport(
        valid=not errors,
        risk_score=round(risk, 3),
        errors=errors,
        warnings=warnings,
        metrics={"ngram_overlap": round(overlap, 4), "candidate_chars": float(len(candidate))},
    )
