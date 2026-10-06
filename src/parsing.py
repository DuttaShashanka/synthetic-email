"""Email header parsing and header-block utilities."""

import re
from email.utils import getaddresses
from .models import ParsedEmail

HEADER_RE = re.compile(r"^(X-From|X-To|X-Cc|X-Bcc|From|To|Cc|Bcc|Date|Subject):[ \t]*(.*)$", re.IGNORECASE | re.MULTILINE)
PROVIDER_HEADERS = {"from", "to", "cc", "date", "subject"}
# Legacy Enron-style headers carry real recipient/sender identities
# and must survive sanitization (renamed to their standard form) so
# the rewrite model does not have to invent recipients.
LEGACY_HEADER_ALIASES = {
    "x-from": "From",
    "x-to": "To",
    "x-cc": "Cc",
    "x-bcc": "Bcc",
}


def _display_name(value: str) -> str:
    """Normalize a raw display name into "Given Family" form."""
    display_name = value.split("<", 1)[0].strip().strip('"')
    display_name = re.sub(r"\([^)]*\)", "", display_name).strip().strip('"')
    if "," in display_name:
        family, given = (part.strip() for part in display_name.split(",", 1))
        if given:
            display_name = f"{given} {family}"
    return re.sub(r"\s+", " ", display_name)


def _recipient_names(value: str) -> list[str]:
    """Extract display names from a recipient header value."""
    if not value:
        return []
    entries = re.split(r"(?<=>)\s*,\s*(?=[^<>]*<)", value) if "<" in value else value.split(",")
    names = []
    for entry in entries:
        name = _display_name(entry)
        if not name:
            addresses = getaddresses([entry])
            name = addresses[0][1] if addresses else ""
        if name:
            names.append(name)
    return names


def _recipient_identity(value: str) -> str:
    """Return a normalized identity key for a recipient value."""
    identity = value.rsplit("@", 1)[0] if "@" in value else value
    return re.sub(r"[^a-z0-9]", "", identity.casefold())


def parse_email(raw: str) -> ParsedEmail:
    """Parse the top header block and body of a raw email message."""
    separator = re.search(r"\r?\n\r?\n", raw)
    # Only the top header block is authoritative: forwarded messages embedded
    # in the body carry their own From/To lines that must not clobber the
    # real headers (later matches would otherwise overwrite earlier ones).
    header_block = raw[: separator.start()] if separator else raw
    headers: dict[str, str] = {}
    for match in HEADER_RE.finditer(header_block):
        headers.setdefault(match.group(1).lower(), match.group(2).strip())
    body = raw[separator.end():].strip() if separator else raw.strip()
    recipients = []
    for standard, legacy in (("to", "x-to"), ("cc", "x-cc"), ("bcc", "x-bcc")):
        recipients.extend(_recipient_names(headers.get(legacy) or headers.get(standard, "")))
    unique_recipients = []
    seen_recipients = set()
    for recipient in recipients:
        identity = _recipient_identity(recipient)
        if identity and identity not in seen_recipients:
            seen_recipients.add(identity)
            unique_recipients.append(recipient)
    sender = _display_name(headers.get("x-from", "")) or headers.get("from", "")
    return ParsedEmail(
        sender=sender,
        recipients=unique_recipients,
        date=headers.get("date", ""),
        subject=headers.get("subject", ""),
        body=body,
    )


def strip_nonessential_headers(raw: str) -> str:
    """Keep only provider headers (and legacy X- aliases) from the header block."""
    separator = re.search(r"\r?\n\r?\n", raw)
    if separator is None:
        return raw

    retained = []
    retained_canonical: set[str] = set()
    keep_continuation = False
    for line in raw[:separator.start()].splitlines():
        if line[:1].isspace():
            if keep_continuation:
                retained.append(line)
            continue
        header_name, delimiter, _ = line.partition(":")
        key = header_name.strip().casefold()
        canonical = LEGACY_HEADER_ALIASES.get(key)
        is_provider = key in PROVIDER_HEADERS or canonical is not None
        keep_continuation = bool(delimiter) and is_provider
        if keep_continuation and is_provider:
            canonical_key = (canonical or header_name).casefold()
            if canonical_key in retained_canonical:
                continue
            retained_canonical.add(canonical_key)
            if canonical:
                line = f"{canonical}:{line.partition(':')[2]}"
            retained.append(line)

    return "\n".join(retained) + "\n\n" + raw[separator.end():]
