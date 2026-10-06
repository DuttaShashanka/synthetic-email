import os
import requests
from dotenv import load_dotenv
from typing import Any

load_dotenv()

SYSTEM_PROMPT = """You create safe, fictional enterprise emails for eDiscovery research.
You receive an already privacy-sanitized email. Rewrite it as a natural email from a fictional company in the target industry.

Hard rules:
- Never restore, infer, or invent real identities, organizations, addresses, accounts, or source-company facts.
- Use only fictional people and organizations. Use only these synthetic email domains: northstarfieldservices.com, harborpeakconsulting.com, verdantbridgesolutions.com.
- Replace distinctive transaction names, project names, unusual quantities, precise commercial terms, and rare factual combinations with plausible but non-identifying equivalents.
- Preserve broad communication function: request, approval, escalation, update, scheduling, or negotiation; preserve the author-recipient relationship and approximate length.
- Rephrase the subject and every body sentence; do not copy source wording or distinctive sentence structure.
- Include normal headers: From, To, Date, Subject, followed by a professional body.
- Make the sender identity consistent: the From email local part must be the lowercase dot-separated sender name used in the sign-off.
- Do not mention anonymization, the source dataset, or these instructions.
- Return only the completed email.
"""


def rewrite_with_openrouter(
    sanitized_email: str,
    industry: str,
    model: str | None = None,
    retry_guidance: str = "",
    usage_sink: list[dict[str, Any]] | None = None,
) -> str:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is required unless --offline is used.")
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
