"""OpenRouter client helpers for rewriting, pseudonyms, and PII extraction."""

import json
import os
import requests
from dotenv import load_dotenv
from typing import Any

load_dotenv()

SYSTEM_PROMPT = """You create safe, fictional enterprise emails for eDiscovery research.
You receive an already privacy-sanitized email. Rewrite it as a natural email from a fictional company in the target industry.

Hard rules:
- Never restore, infer, or invent real identities, organizations, addresses, accounts, or source-company facts.
- Use only fictional people and organizations.
- Replace distinctive transaction names, project names, unusual quantities, precise commercial terms, and rare factual combinations with plausible but non-identifying equivalents.
- Preserve broad communication function: request, approval, escalation, update, scheduling, or negotiation; preserve the author-recipient relationship and approximate length.
- Rephrase the subject and every body sentence; do not copy source wording or distinctive sentence structure.
- Include normal headers: From, To, Date, Subject, followed by a professional body.
- Make the sender identity consistent: the From email local part must be the lowercase dot-separated sender name used in the sign-off.
- Vary your sign-off phrase naturally (e.g., "Thanks," "Best regards," "Kind regards," "Best," "Thank you,") and use either the full name or just the first name.
- Do not mention anonymization, the source dataset, or these instructions.
- Return only the completed email.
"""

PERSONA_SYSTEM_PROMPT = """You generate realistic, fictional person names for synthetic enterprise email data.

Hard rules:
- Generate a single name that sounds like a real person's name in a Western business context.
- The name must NOT match any real person you know.
- Return ONLY the name (e.g., "Jordan Smith"), nothing else — no labels, no JSON, no extra text.
"""

ORGANIZATION_SYSTEM_PROMPT = """You generate realistic, fictional organization names for synthetic enterprise email data.

Hard rules:
- Generate a single company or organization name that sounds like a real business.
- The name must NOT match any real organization you know.
- Return ONLY the name, nothing else.
"""

PROJECT_SYSTEM_PROMPT = """You generate realistic, fictional project or initiative names for synthetic enterprise email data.

Hard rules:
- Generate a single short project name (2-4 words) that sounds like a real internal project.
- Return ONLY the name (e.g., "Project Phoenix"), nothing else.
"""

DOMAIN_SYSTEM_PROMPT = """You generate realistic, fictional email domain names for synthetic enterprise email data.

Hard rules:
- Generate a single domain name (e.g., "northstarfieldservices.com") that resembles a real corporate domain but is clearly fictional.
- The domain must NOT be a real company domain you know.
- Return ONLY the domain (e.g., "northstarfieldservices.com"), nothing else — no labels, no JSON, no extra text.
"""

PII_EXTRACTION_SYSTEM_PROMPT = """You extract named entities from a business email for privacy redaction.

Extract person names (full names like "Mark Haedicke") and organization names (like "Acme Holdings LLC") that appear in the text. 

Return ONLY extracted entities, one per line, prefixed with their type:
PERSON: full name
ORGANIZATION: full org name

Rules:
- Only extract distinct, full person names (first and last name or full name as written).
- Only extract organization names that include a legal suffix (Inc, LLC, Ltd, Corporation, Company, etc.) or are clearly org names.
- Do NOT extract emails, phone numbers, dates, project acronyms, or job titles without a name.
- Do NOT extract generic terms or common words.
- If no entities are found, return nothing."""


def _openrouter_request(
    system_prompt: str,
    user_prompt: str,
    model: str | None = None,
    max_tokens: int = 100,
    temperature: float = 0.7,
) -> str:
    """Send a chat-completion request and return the model's text response."""
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is required.")
    model = model or os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct")
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def generate_pseudonym(
    entity_type: str,
    source_value: str,
    industry: str,
    model: str | None = None,
) -> str:
    """Generate a realistic fictional pseudonym using the OpenRouter LLM."""
    if entity_type == "person":
        prompt = f"Generate a fictional person name appropriate for the {industry} industry. The source name appears to be from a business email context like '{source_value}'. Make it sound natural and professional."
        return _openrouter_request(PERSONA_SYSTEM_PROMPT, prompt, model)
    elif entity_type == "organization":
        prompt = f"Generate a fictional company or organization name appropriate for the {industry} industry. The source value is '{source_value}'. Make it sound like a real business."
        return _openrouter_request(ORGANIZATION_SYSTEM_PROMPT, prompt, model)
    elif entity_type == "project":
        prompt = f"Generate a short fictional project or initiative name appropriate for the {industry} industry. The source value is '{source_value}'."
        return _openrouter_request(PROJECT_SYSTEM_PROMPT, prompt, model)
    elif entity_type == "domain":
        prompt = f"Generate a fictional email domain appropriate for the {industry} industry. The source domain is '{source_value}'. Make it sound like a real business domain but ensure it is not a real company."
        return _openrouter_request(DOMAIN_SYSTEM_PROMPT, prompt, model)
    else:
        raise ValueError(f"Unsupported entity type for LLM generation: {entity_type}")


def extract_pii_entities(text: str, industry: str, model: str | None = None) -> list[tuple[str, str]]:
    """Use the LLM to extract person and organization names from text that regex may miss."""
    prompt = (
        f"Extract person names and organization names from the following business email. "
        f"Target industry: {industry}.\n\n"
        f"Text:\n{text}"
    )
    response = _openrouter_request(PII_EXTRACTION_SYSTEM_PROMPT, prompt, model, max_tokens=500, temperature=0)
    entities: list[tuple[str, str]] = []
    for line in response.strip().splitlines():
        line = line.strip()
        if line.startswith("PERSON:"):
            name = line[len("PERSON:"):].strip()
            if name:
                entities.append(("person", name))
        elif line.startswith("ORGANIZATION:"):
            name = line[len("ORGANIZATION:"):].strip()
            if name:
                entities.append(("organization", name))
    return entities


def rewrite_with_openrouter(
    sanitized_email: str,
    industry: str,
    model: str | None = None,
    retry_guidance: str = "",
    usage_sink: list[dict[str, Any]] | None = None,
) -> str:
    """Rewrite a sanitized email into a fictional email via OpenRouter."""
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is required.")
    model = model or os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct")
    user = f"Target industry: {industry}\n\nSanitized input:\n---\n{sanitized_email}\n---"
    if retry_guidance:
        user += f"\n\nRevision requirements from the privacy validator:\n{retry_guidance}"
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0.25,
            "max_tokens": 1800,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
        },
        timeout=90,
    )
    response.raise_for_status()
    data = response.json()
    if usage_sink is not None:
        usage = data.get("usage") or {}
        usage_sink.append(
            {
                "model": data.get("model", model),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "reported_cost_usd": usage.get("cost"),
            }
        )
    return data["choices"][0]["message"]["content"].strip()
