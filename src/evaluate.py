"""Sample evaluation with deterministic checks and LLM-as-judge scoring."""

import argparse
import csv
import json
import math
import os
import random
import re
import sys
from pathlib import Path
from statistics import mean, median
from typing import Any

import requests
from dotenv import load_dotenv

from .parsing import parse_email
from .pipeline import synthesize


SCORE_DIMENSIONS = (
    "purpose_preservation",
    "relationship_preservation",
    "email_realism",
    "structure_quality",
    "training_utility",
)
JUDGE_SYSTEM_PROMPT = """You are a strict evaluator of synthetic enterprise emails.
Compare the sanitized source with the synthetic candidate. Score only broad
communication purpose, sender-recipient relationship, realistic email style,
email structure, and usefulness for training an eDiscovery-related classifier.
Do not score privacy; that is measured separately by deterministic checks.
Use integer scores from 1 (poor) to 5 (excellent). Do not quote either email,
repeat names, addresses, organizations, or other identifiers, or include prose.
Return exactly one JSON object with these integer keys: purpose_preservation,
relationship_preservation, email_realism, structure_quality, training_utility.
"""


def parse_judge_scores(content: str) -> dict[str, int]:
    """Parse a judge response into the five required integer scores."""
    match = re.search(r"\{.*\}", content, flags=re.S)
    if match is None:
        raise ValueError("Judge response did not contain a JSON object")
    payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("Judge response must be a JSON object")

    scores = {}
    for dimension in SCORE_DIMENSIONS:
        value = payload.get(dimension)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Judge score '{dimension}' must be numeric")
        if not math.isfinite(value) or not float(value).is_integer() or not 1 <= value <= 5:
            raise ValueError(f"Judge score '{dimension}' must be an integer from 1 to 5")
        scores[dimension] = int(value)
    return scores


def judge_quality(
    sanitized_source: str,
    candidate: str,
    model: str,
    api_key: str,
    usage_sink: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Score a candidate with an LLM judge on five utility dimensions."""
    base_user_prompt = (
        "Sanitized source email:\n---\n"
        + sanitized_source
        + "\n---\nSynthetic candidate:\n---\n"
        + candidate
        + "\n---"
    )
    last_error: Exception | None = None
    for attempt in range(2):
        user_prompt = base_user_prompt
        if attempt:
            user_prompt += "\nReturn only a valid JSON object with all five required integer scores."
        payload = {
            "model": model,
            "temperature": 0,
            "max_tokens": 240,
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        }
        if attempt == 0:
            payload["response_format"] = {"type": "json_object"}
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=90,
            )
            response.raise_for_status()
            data = response.json()
            usage = data.get("usage") or {}
            if usage_sink is not None:
                usage_sink.append(
                    {
                        "model": data.get("model", model),
                        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                        "completion_tokens": int(usage.get("completion_tokens") or 0),
                        "reported_cost_usd": usage.get("cost"),
                    }
                )
            content = data["choices"][0]["message"]["content"]
            return parse_judge_scores(content)
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as error:
            last_error = error

    raise ValueError("Judge failed to return all required scores") from last_error


def _percentile(values: list[int], percentile: float) -> float:
    """Return the given percentile of a list of values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1)
    return float(ordered[index])


def _load_sample(path: Path, sample_size: int, seed: int) -> tuple[int, list[tuple[int, str]]]:
    """Select a reproducible random sample of eligible messages."""
    csv.field_size_limit(sys.maxsize)
    eligible: list[tuple[int, str]] = []
    with path.open(newline="", encoding="utf-8", errors="replace") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row")
        message_column = next(
            (field for field in reader.fieldnames if field and field.strip().casefold() == "message"),
            None,
        )
        if message_column is None:
            raise ValueError("CSV must contain a 'message' column")
        for row_number, row in enumerate(reader, start=1):
            message = row.get(message_column) or ""
            if len(message.strip()) >= 160:
                eligible.append((row_number, message))

    selected = random.Random(seed).sample(eligible, min(sample_size, len(eligible)))
    return len(eligible), selected


def evaluate_sample(
    csv_path: Path,
    sample_size: int = 5,
    seed: int = 417,
    industry: str = "professional services",
    generation_model: str | None = None,
    judge_model: str | None = None,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """Run the sample evaluation pipeline and return aggregate metrics."""
    load_dotenv()
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required for generation and judge evaluation")

    configured_generation_model = generation_model or os.getenv(
        "OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct"
    )
    configured_judge_model = judge_model or os.getenv(
        "OPENROUTER_JUDGE_MODEL", "openai/gpt-4o-mini"
    )
    eligible_count, selected = _load_sample(csv_path, sample_size, seed)
    rows = []

    for sample_number, (_, source) in enumerate(selected, start=1):
        source_parsed = parse_email(source)
        source_chars = len(source)
        source_headers_present = sum(
            bool(value)
            for value in (source_parsed.sender, source_parsed.recipients, source_parsed.date, source_parsed.subject)
        )
        record: dict[str, Any] = {
            "sample_number": sample_number,
            "source_chars": source_chars,
            "source_required_headers": source_headers_present,
            "status": "blocked",
        }

        try:
            candidate, report, sanitized, attempts = synthesize(
                source,
                industry,
                configured_generation_model,
                max_attempts=max_attempts,
            )
        except Exception as error:
            message = str(error).casefold()
            if "fail-closed" in message:
                reason = "validation_blocked"
            elif "api_key" in message:
                reason = "provider_configuration"
            elif isinstance(error, requests.RequestException):
                reason = "provider_request_failed"
            else:
                reason = "generation_failed"
            record.update(status="blocked", failure_category=reason)
            rows.append(record)
            continue

        record.update(
            status="generated",
            generation_attempts=attempts,
            validator_risk=report.risk_score,
            ngram_overlap=report.metrics.get("ngram_overlap", 0.0),
            warning_count=len(report.warnings),
            candidate_chars=len(candidate),
            candidate_source_length_ratio=round(len(candidate) / max(1, source_chars), 4),
        )
        try:
            scores = judge_quality(sanitized, candidate, configured_judge_model, api_key)
            record.update(status="scored", judge_scores=scores)
        except Exception as error:
            failure_category = (
                "judge_provider_failed"
                if isinstance(error, requests.RequestException)
                else "judge_response_invalid"
            )
            record.update(status="judge_failed", judge_failure_category=failure_category)
        rows.append(record)

    generated = [row for row in rows if row["status"] in {"generated", "scored", "judge_failed"}]
    scored = [row for row in rows if row["status"] == "scored"]
    source_lengths = [row["source_chars"] for row in rows]
    candidate_lengths = [row["candidate_chars"] for row in generated]
    judge_means = {
        dimension: round(mean(row["judge_scores"][dimension] for row in scored), 3)
        if scored
        else None
        for dimension in SCORE_DIMENSIONS
    }

    return {
        "method": "deterministic privacy/overlap checks plus LLM-as-judge utility scoring",
        "sample_seed": seed,
        "eligible_dataset_rows": eligible_count,
        "requested_sample_size": sample_size,
        "actual_sample_size": len(selected),
        "industry": industry,
        "generation_model": configured_generation_model,
        "judge_model": configured_judge_model,
        "privacy_note": "Only sanitized source text and generated candidates were sent to OpenRouter. Raw source messages and text outputs are not included in this report.",
        "metrics": {
            "generation_acceptance_rate": round(len(generated) / max(1, len(rows)), 3),
            "judge_scoring_rate": round(len(scored) / max(1, len(rows)), 3),
            "mean_validator_risk": round(mean(row["validator_risk"] for row in generated), 3) if generated else None,
            "mean_5gram_overlap": round(mean(row["ngram_overlap"] for row in generated), 4) if generated else None,
            "source_message_chars_mean": round(mean(source_lengths), 1) if source_lengths else None,
            "source_message_chars_median": median(source_lengths) if source_lengths else None,
            "source_message_chars_p90": _percentile(source_lengths, 0.9),
            "candidate_message_chars_mean": round(mean(candidate_lengths), 1) if candidate_lengths else None,
            "candidate_to_source_length_ratio_mean": round(mean(row["candidate_source_length_ratio"] for row in generated), 4) if generated else None,
            "llm_judge_score_means_1_to_5": judge_means,
        },
        "sample_results": rows,
        "limitations": [
            "The sample is reproducible but not stratified; results are not estimates for the full Enron corpus.",
            "LLM-judge scores are subjective utility signals, not privacy guarantees or human evaluation.",
            "The report intentionally omits source messages, generated text, file paths, and identifiers.",
        ],
    }


def main() -> None:
    """Run the sample evaluation CLI."""
    parser = argparse.ArgumentParser(description="Evaluate synthetic email generation on a small CSV sample")
    parser.add_argument("--input", type=Path, default=Path("data/raw/emails_sample.csv"))
    parser.add_argument("--sample-size", type=int, default=5)
    parser.add_argument("--seed", type=int, default=417)
    parser.add_argument("--industry", default="professional services")
    parser.add_argument("--generation-model", default=None)
    parser.add_argument("--judge-model", default=None)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--report", type=Path, default=Path("reports/sample_evaluation.json"))
    args = parser.parse_args()

    report = evaluate_sample(
        args.input,
        sample_size=args.sample_size,
        seed=args.seed,
        industry=args.industry,
        generation_model=args.generation_model,
        judge_model=args.judge_model,
        max_attempts=args.max_attempts,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report_path": str(args.report), "metrics": report["metrics"]}, indent=2))


if __name__ == "__main__":
    main()