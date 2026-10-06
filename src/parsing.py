import re
from email.utils import getaddresses
from .models import ParsedEmail

HEADER_RE = re.compile(r"^(X-From|X-To|X-Cc|X-Bcc|From|To|Cc|Bcc|Date|Subject):[ \t]*(.*)$", re.IGNORECASE | re.MULTILINE)
PROVIDER_HEADERS = {"from", "to", "cc", "date", "subject"}


def _display_name(value: str) -> str:
    display_name = value.split("<", 1)[0].strip().strip('"')
    display_name = re.sub(r"\([^)]*\)", "", display_name).strip().strip('"')
    if "," in display_name:
        family, given = (part.strip() for part in display_name.split(",", 1))
        if given:
            display_name = f"{given} {family}"
    return re.sub(r"\s+", " ", display_name)


def _recipient_names(value: str) -> list[str]:
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
    identity = value.rsplit("@", 1)[0] if "@" in value else value
    return re.sub(r"[^a-z0-9]", "", identity.casefold())


def parse_email(raw: str) -> ParsedEmail:
    headers = {m.group(1).lower(): m.group(2).strip() for m in HEADER_RE.finditer(raw)}
    first_blank = re.search(r"\r?\n\r?\n", raw)
    body = raw[first_blank.end():].strip() if first_blank else raw.strip()
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
    separator = re.search(r"\r?\n\r?\n", raw)
    if separator is None:
        return raw

    retained = []
    keep_continuation = False
    for line in raw[:separator.start()].splitlines():
        if line[:1].isspace():
            if keep_continuation:
                retained.append(line)
            continue
        header_name, delimiter, _ = line.partition(":")
        keep_continuation = bool(delimiter) and header_name.casefold() in PROVIDER_HEADERS
        if keep_continuation:
            retained.append(line)

    return "\n".join(retained) + "\n\n" + raw[separator.end():]
