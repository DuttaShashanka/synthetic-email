import hashlib
import re
from datetime import date, timedelta
from typing import Iterable, Set
from faker import Faker
from .entity_graph import EntityGraph
from .llm import generate_pseudonym
from .models import TransformContext

fake = Faker("en_US")
FICTITIOUS_DOMAINS = [
    "northstarfieldservices.com",
    "harborpeakconsulting.com",
    "verdantbridgesolutions.com",
]

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\(?\d[\d .()/-]{6,}\d\)?)(?!\d)")
MONEY_RE = re.compile(r"(?<!\w)(?:[$€£₹]\s?[\d,]+(?:\.\d+)?|\b[\d,]+(?:\.\d+)?\s?(?:million|billion|thousand|crore|lakh)\b)(?!\w)", re.I)
DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}|"
    r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+)?\d{1,2}\s+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}"
    r"(?:\s+\d{2}:\d{2}(?::\d{2})?(?:\s+[+-]\d{4})?(?:\s+\([A-Za-z]{2,5}\))?)?)\b",
    re.I,
)
URL_RE = re.compile(r"https?://[^\s<>()]+", re.I)
PLACEHOLDER_RE = re.compile(r"\[\[[A-Z_]+_\d+\]\]")

# Conservative patterns for common source-specific terminology that should be abstracted before an external call.
PROJECT_RE = re.compile(r"\b[A-Z]{2,}(?:[- ][A-Z0-9]{2,})*\b")
ORG_SUFFIX_RE = re.compile(r"\b[A-Z][A-Za-z&.,' -]{2,}?\s+(?:Inc\.?|LLC|L\.L\.C\.?|Ltd\.?|Limited|Corporation|Corp\.?|Company|Co\.?|LP|LLP|PLC|Pvt\.?\s*Ltd\.?)\b")
HEADER_PERSON_RE = re.compile(r"(?im)^(?:X-)?(?:From|To|Cc|Bcc):\s*([^<\n]+?)\s*<([^>\s]+)>")
SIMPLE_HEADER_RE = re.compile(r"(?im)^(?:X-)?(?:From|To|Cc|Bcc):\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+([^\s<>@]+@[^\s<>@>]+)")
SALUTATION_RE = re.compile(r"\bDear[ \t]+([A-Z][a-z]+(?:[-'][A-Za-z]+)?(?:[ \t]+[A-Z][a-z]+(?:[-'][A-Za-z]+)?)?)")
SIGNATURE_NAME_RE = re.compile(r"(?im)^\s*([A-Z][a-z]+(?:[-'][A-Za-z]+)?(?:\s+[A-Z][a-z]+(?:[-'][A-Za-z]+)?){1,2})\s*$")
PERSON_NAME_RE = re.compile(r"\b[A-Z][a-z]+(?:[-'][A-Za-z]+)?(?:[ \t]+[A-Z][a-z]+(?:[-'][A-Za-z]+)?){1,2}\b")
COMMON_ACRONYMS = {
    "LLC", "LLP", "PLC", "CEO", "CFO", "COO", "CTO", "HR", "II",
    "PST", "PDT", "MST", "MDT", "CST", "CDT", "EST", "EDT", "UTC", "AM", "PM",
}
NON_IDENTIFYING_TERMS = {
    "from", "to", "date", "subject", "dear", "best", "regards", "attached",
    "this", "that", "please", "the", "both", "in", "if", "others", "thank",
    "there", "these", "those", "when", "while", "after",
}


def stable_index(value: str, count: int) -> int:
    return int(hashlib.sha256(value.lower().encode()).hexdigest(), 16) % count


def fake_name(value: str, industry: str = "general business") -> str:
    return generate_pseudonym("person", value, industry)


def record_replacement(
    ctx: TransformContext,
    entity_type: str,
    source_value: str,
    replacement: str,
    person_source_value: str = "",
) -> None:
    if not source_value or not replacement or source_value == replacement:
        return
    for record in ctx.replacements:
        if record["entity_type"] == entity_type and record["source_value"].casefold() == source_value.casefold():
            record["replacement"] = replacement
            if person_source_value:
                record["person_source_value"] = person_source_value
            return
    record = {
        "entity_type": entity_type,
        "source_value": source_value,
        "replacement": replacement,
    }
    if person_source_value:
        record["person_source_value"] = person_source_value
    ctx.replacements.append(record)


def normalize_person_name(value: str) -> str:
    name = re.sub(r"\([^)]*\)", "", value).strip().strip('"\' ')
    if "," in name:
        surname, given_names = (part.strip() for part in name.split(",", 1))
        name = f"{given_names} {surname}"
    name = re.sub(r"\b[A-Z]\.\s*", "", name)
    name = re.sub(r"\s+", " ", name).strip(" ,")
    return name if len(name.split()) >= 2 else ""


def email_matches_person(source_email: str, person_name: str) -> bool:
    local_part = re.sub(r"[^a-z0-9]", "", source_email.rsplit("@", 1)[0].casefold())
    name_parts = re.findall(r"[a-z]+", normalize_person_name(person_name).casefold())
    if not name_parts:
        raw_parts = re.findall(r"[a-z]+", person_name.casefold())
        if len(raw_parts) < 1 or len(raw_parts[0]) < 3:
            return False
        given_name = raw_parts[0]
        return local_part.startswith(given_name) and len(given_name) >= 3

    given_name, surname = name_parts[0], name_parts[-1]
    possible_local_parts = {
        given_name + surname,
        given_name[0] + surname,
        surname + given_name,
        surname + given_name[0],
    }
    return local_part in possible_local_parts or local_part.endswith(surname)


def fake_email(
    source_email: str,
    ctx: TransformContext,
    graph: EntityGraph,
    person_name: str | None = None,
    industry: str = "general business",
) -> str:
    if source_email not in ctx.email_map:
        person_key = person_name or source_email
        name = graph.resolve("person", person_key, lambda: fake_name(person_key, industry))
        parts = re.sub(r"[^a-zA-Z ]", "", name).lower().split()
        local = ".".join(parts[:2]) if len(parts) > 1 else parts[0]
        source_domain = source_email.rsplit("@", 1)[-1].lower()
        domain = graph.resolve(
            "synthetic_domain_v3",
            source_domain,
            lambda: generate_pseudonym("domain", source_domain, industry),
        )
        candidate = f"{local}@{domain}"
        ctx.email_map[source_email] = graph.resolve(
            "synthetic_email_v3", source_email, lambda: candidate
        )
        record_replacement(
            ctx,
            "synthetic_email_v3",
            source_email,
            ctx.email_map[source_email],
            person_key if person_name else "",
        )
        if person_name:
            record_replacement(ctx, "person", person_key, name)
        graph.relate("email", source_email, "belongs_to", "person", person_key)
        graph.relate("email", source_email, "uses_domain", "domain", source_domain)
    return ctx.email_map[source_email]


def replacement_date(value: str, ctx: TransformContext) -> str:
    if value not in ctx.date_map:
        offset = 365 + stable_index(value, 1095)
        ctx.date_map[value] = (date(2024, 1, 1) + timedelta(days=offset)).isoformat()
    return ctx.date_map[value]


def replacement_money(value: str, ctx: TransformContext) -> str:
    if value not in ctx.money_map:
        n = 25 + stable_index(value, 850)
        ctx.money_map[value] = f"${n:,},000"
    return ctx.money_map[value]


def replacement_phone(value: str, ctx: TransformContext) -> str:
    if value not in ctx.phone_map:
        n = 1000000 + stable_index(value, 8999999)
        ctx.phone_map[value] = f"+1-202-555-{n % 10000:04d}"
    return ctx.phone_map[value]


def redact_direct_identifiers(
    text: str, ctx: TransformContext, graph: EntityGraph | None = None, industry: str = "general business"
) -> str:
    graph = graph or EntityGraph()
    known_people: dict[str, tuple[str, str]] = {}
    email_people: dict[str, str] = {}
    replacement_dates: list[str] = []

    for match in HEADER_PERSON_RE.finditer(text):
        raw_name = match.group(1).strip()
        name = normalize_person_name(raw_name)
        address = match.group(2)
        if name:
            replacement = graph.resolve("person", name, lambda: fake_name(name, industry))
            record_replacement(ctx, "person", name, replacement)
            known_people[raw_name] = (replacement, address)
            known_people[name] = (replacement, address)
            email_people[address.casefold()] = name
            graph.resolve("email", address, lambda: fake_email(address, ctx, graph, name, industry))
            graph.relate("person", name, "has_email", "email", address)

    for match in SIMPLE_HEADER_RE.finditer(text):
        raw_name = match.group(1).strip()
        name = raw_name
        address = match.group(2)
        if address.casefold() in email_people:
            continue
        if any(word.lower() in NON_IDENTIFYING_TERMS for word in name.split()):
            continue
        replacement = graph.resolve("person", name, lambda: fake_name(name, industry))
        record_replacement(ctx, "person", name, replacement)
        known_people[raw_name] = (replacement, address)
        known_people[name] = (replacement, address)
        email_people[address.casefold()] = name
        graph.resolve("email", address, lambda: fake_email(address, ctx, graph, name, industry))
        graph.relate("person", name, "has_email", "email", address)

    for pattern in (SALUTATION_RE, SIGNATURE_NAME_RE):
        for match in pattern.finditer(text):
            name = match.group(1).strip()
            if name not in known_people:
                matched = False
                for known_name, (replacement, address) in known_people.items():
                    known_normalized = normalize_person_name(known_name)
                    if known_normalized and known_normalized.split()[0].lower() == name.lower():
                        short = replacement.split()[0] if replacement else replacement
                        known_people[name] = (short, address)
                        matched = True
                        break
                if not matched:
                    known_people[name] = (graph.resolve("person", name, lambda: fake_name(name, industry)), None)

    candidate_people = set(person_names(text))
    candidate_people.update(
        normalize_person_name(match.group(1))
        for match in HEADER_PERSON_RE.finditer(text)
        if normalize_person_name(match.group(1))
    )
    addresses = {address.casefold() for address in EMAIL_RE.findall(text)}
    for address in addresses:
        if address in email_people:
            continue
        matches = [name for name in candidate_people if email_matches_person(address, name)]
        if len(matches) == 1:
            name = matches[0]
            replacement = graph.resolve("person", name, lambda: fake_name(name, industry))
            record_replacement(ctx, "person", name, replacement)
            known_people[name] = (replacement, address)
            email_people[address] = name
            graph.relate("person", name, "has_email", "email", address)
        elif not matches:
            for known_name, (replacement, _) in known_people.items():
                if email_matches_person(address, known_name) and known_name not in candidate_people:
                    known_people[known_name] = (replacement, address)
                    email_people[address] = known_name
                    graph.relate("person", known_name, "has_email", "email", address)
                    break

    email_placeholders: dict[str, str] = {}

    def _protect_email(match: re.Match[str]) -> str:
        key = f"[[EMAIL_{len(email_placeholders)}]]"
        email_placeholders[key] = match.group(0)
        return key

    text = EMAIL_RE.sub(_protect_email, text)

    for name, (replacement, address) in sorted(
        known_people.items(), key=lambda item: len(item[0]), reverse=True
    ):
        text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", replacement, text, flags=re.I)
        if address:
            graph.relate("person", name, "has_email", "email", address)

    for placeholder, email in email_placeholders.items():
        replacement = fake_email(
            email, ctx, graph, email_people.get(email.casefold()), industry
        )
        text = text.replace(placeholder, replacement)

    def redact_url(match: re.Match[str]) -> str:
        replacement = "https://portal.northstar.example"
        record_replacement(ctx, "url", match.group(0), replacement)
        return replacement

    text = URL_RE.sub(redact_url, text)

    def protect_date(match: re.Match[str]) -> str:
        replacement = graph.resolve(
            "date", match.group(0), lambda: replacement_date(match.group(0), ctx)
        )
        record_replacement(ctx, "date", match.group(0), replacement)
        replacement_dates.append(replacement)
        return f"[[DATE_{len(replacement_dates) - 1}]]"

    text = DATE_RE.sub(protect_date, text)
    def redact_phone(match: re.Match[str]) -> str:
        replacement = graph.resolve(
            "phone", match.group(0), lambda: replacement_phone(match.group(0), ctx)
        )
        record_replacement(ctx, "phone", match.group(0), replacement)
        return replacement

    def redact_money(match: re.Match[str]) -> str:
        replacement = graph.resolve(
            "money", match.group(0), lambda: replacement_money(match.group(0), ctx)
        )
        record_replacement(ctx, "money", match.group(0), replacement)
        return replacement

    text = PHONE_RE.sub(redact_phone, text)
    text = MONEY_RE.sub(redact_money, text)
    for index, replacement in enumerate(replacement_dates):
        text = text.replace(f"[[DATE_{index}]]", replacement)
    def redact_organization(match: re.Match[str]) -> str:
        replacement = graph.resolve(
            "organization", match.group(0), lambda: fake_company(match.group(0), industry)
        )
        record_replacement(ctx, "organization", match.group(0), replacement)
        return replacement

    def redact_project(match: re.Match[str]) -> str:
        source_value = match.group(0)
        if source_value in COMMON_ACRONYMS:
            return source_value
        replacement = graph.resolve("project", source_value, lambda: fake_project(source_value, industry))
        record_replacement(ctx, "project", source_value, replacement)
        return replacement

    text = ORG_SUFFIX_RE.sub(redact_organization, text)
    text = PROJECT_RE.sub(redact_project, text)
    text = re.sub(r"\bEnron\b", "Northstar Field Systems", text, flags=re.I)
    return text


def fake_company(value: str, industry: str = "general business") -> str:
    return generate_pseudonym("organization", value, industry)


def fake_project(value: str, industry: str = "general business") -> str:
    return generate_pseudonym("project", value, industry)


def llm_extract_pii(text: str, industry: str = "general business", model: str | None = None) -> tuple[set[str], set[str]]:
    """Use the LLM to extract person names and organization names not caught by regex heuristics."""
    from .llm import extract_pii_entities
    entities = extract_pii_entities(text, industry, model)
    people = {value for entity_type, value in entities if entity_type == "person"}
    orgs = {value for entity_type, value in entities if entity_type == "organization"}
    return people, orgs


def source_terms(raw: str, extra_terms: Iterable[str] = ()) -> Set[str]:
    terms = set()
    for email in EMAIL_RE.findall(raw):
        terms.add(email.lower())
        domain = email.rsplit("@", 1)[-1].lower()
        terms.add(domain)
    for phone in PHONE_RE.findall(raw):
        terms.add(phone.lower())
    terms.update(value.lower() for value in DATE_RE.findall(raw))
    for org in ORG_SUFFIX_RE.findall(raw):
        if len(org.strip()) >= 4:
            terms.add(org.lower())
    terms.update(name.lower() for name in person_names(raw))
    terms.update(person_aliases(raw))
    terms.add("enron")
    terms.update(t.lower() for t in extra_terms if t and len(t.strip()) >= 3)
    return terms


def person_names(raw: str) -> Set[str]:
    names = {
        match.group(0)
        for match in PERSON_NAME_RE.finditer(raw)
        if match.group(0).split()[0].casefold() not in NON_IDENTIFYING_TERMS
        and not match.group(0).split()[0].casefold().endswith(("'s", "’s"))
    }
    names.update(
        name
        for match in HEADER_PERSON_RE.finditer(raw)
        if (name := normalize_person_name(match.group(1)))
    )
    return names


def person_aliases(raw: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for name in person_names(raw):
        first_name = name.split()[0]
        standalone_or_label = re.search(
            rf"(?im)^\s*{re.escape(first_name)}\s*(?::|$)", raw
        )
        if standalone_or_label:
            aliases[first_name.casefold()] = name
    return aliases


def redact_source_terms(
    text: str,
    terms: Iterable[str],
    graph: EntityGraph | None = None,
    known_people: Iterable[str] = (),
    known_aliases: dict[str, str] | None = None,
    ctx: TransformContext | None = None,
    industry: str = "general business",
) -> str:
    graph = graph or EntityGraph()
    person_keys = {re.sub(r"\s+", " ", name.strip().casefold()) for name in known_people}
    person_keys.update((known_aliases or {}).keys())
    ordered = sorted({t for t in terms if t}, key=len, reverse=True)
    for term in ordered:
        normalized_term = re.sub(r"\s+", " ", term.strip().casefold())
        if normalized_term in person_keys:
            person_name = (known_aliases or {}).get(normalized_term, term)
            replacement = graph.resolve("person", person_name, lambda: fake_name(person_name, industry))
            if ctx is not None:
                record_replacement(ctx, "person", person_name, replacement)
        else:
            replacement = "[REDACTED_SOURCE_TERM]"
        text = re.sub(re.escape(term), lambda _: replacement, text, flags=re.I)
    return text
