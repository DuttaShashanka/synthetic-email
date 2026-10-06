"""Paired comparison of OpenRouter generation models on a seeded sample."""

import argparse
import json
import os
import random
import sys
from pathlib import Path
from statistics import mean
from typing import Any

import requests
from dotenv import load_dotenv

from .evaluate import SCORE_DIMENSIONS, _load_sample, judge_quality
from .pipeline import synthesize


DEFAULT_BENCHMARK_MODELS = (
    ("meta-llama/llama-3.2-1b-instruct", 1.0),
    ("meta-llama/llama-3.2-3b-instruct", 3.0),
    ("meta-llama/llama-3.1-8b-instruct", 8.0),
    ("google/gemma-4-31b-it", 31.0),
)
DEFAULT_JUDGE_MODEL = "openai/gpt-4o-mini"


def load_model_prices() -> dict[str, dict[str, float | None]]:
    """Fetch current per-token pricing for the OpenRouter model catalogue."""
    response = requests.get("https://openrouter.ai/api/v1/models", timeout=30)
    response.raise_for_status()
    catalogue = {}
    for entry in response.json().get("data", []):
        pricing = entry.get("pricing") or {}
        catalogue[entry.get("id", "")] = {
            "prompt": float(pricing["prompt"]) if pricing.get("prompt") is not None else None,
            "completion": float(pricing["completion"]) if pricing.get("completion") is not None else None,
        }
    return catalogue


def calculate_cost_usd(
    usage: list[dict[str, Any]],
    pricing: dict[str, float | None] | None,
) -> float | None:
    """Compute the total USD cost from provider-reported usage records."""
    if not usage:
        return 0.0
    total = 0.0
    had_cost = False
    for call in usage:
        reported = call.get("reported_cost_usd")
        if isinstance(reported, (int, float)):
            total += float(reported)
            had_cost = True
            continue
        prompt_tokens = int(call.get("prompt_tokens") or 0)
        completion_tokens = int(call.get("completion_tokens") or 0)
        if not prompt_tokens and not completion_tokens:
            continue
        if not pricing or pricing.get("prompt") is None or pricing.get("completion") is None:
            continue
        total += prompt_tokens * float(pricing["prompt"])
        total += completion_tokens * float(pricing["completion"])
        had_cost = True
    return round(total, 10) if had_cost else None


def _mean_score(rows: list[dict[str, Any]]) -> float | None:
    """Return the mean judge score across scored rows."""
    scored = [row for row in rows if row.get("judge_scores")]
    if not scored:
        return None
    return round(
        mean(mean(row["judge_scores"][dimension] for dimension in SCORE_DIMENSIONS) for row in scored),
        3,
    )


def choose_value_model(
    model_results: list[dict[str, Any]],
    score_tolerance: float = 0.25,
    minimum_coverage: float = 0.9,
) -> dict[str, Any] | None:
    """Select the lowest-cost model within tolerance of the best judge score."""
    eligible = [
        result
        for result in model_results
        if result.get("mean_judge_score") is not None
        and result.get("generation_acceptance_rate", 0) >= minimum_coverage
        and result.get("judge_scoring_rate", 0) >= minimum_coverage
    ]
    if not eligible:
        return None
    best_score = max(result["mean_judge_score"] for result in eligible)
    close_quality = [
        result
        for result in eligible
        if result["mean_judge_score"] >= best_score - score_tolerance
        and result.get("mean_generation_cost_usd") is not None
    ]
    if not close_quality:
        return None
    selected = min(close_quality, key=lambda result: result["mean_generation_cost_usd"])
    return {
        "model": selected["model"],
        "parameter_billions": selected["parameter_billions"],
        "mean_judge_score": selected["mean_judge_score"],
        "mean_generation_cost_usd": selected["mean_generation_cost_usd"],
        "mean_total_eval_cost_usd": selected["mean_total_eval_cost_usd"],
        "best_mean_judge_score": best_score,
        "quality_tolerance": score_tolerance,
        "minimum_generation_and_judge_coverage": minimum_coverage,
        "selection_rule": "lowest mean generation cost per requested email among models with sufficient coverage and within tolerance of the highest mean judge score; judge cost is reported separately",
    }


def compare_models(
    csv_path: Path,
    sample_size: int = 5,
    seed: int = 417,
    industry: str = "professional services",
    models: tuple[tuple[str, float], ...] = DEFAULT_BENCHMARK_MODELS,
    judge_model: str = DEFAULT_JUDGE_MODEL,
    max_attempts: int = 2,
    score_tolerance: float = 0.25,
) -> dict[str, Any]:
    """Compare generation models on a reproducible, paired email sample."""
    load_dotenv()
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required for model comparison")

    catalogue_prices = load_model_prices()
    judge_prices = catalogue_prices.get(judge_model)
    eligible_rows, selected = _load_sample(csv_path, sample_size, seed)
    model_results: list[dict[str, Any]] = []

    for model, parameter_billions in models:
        model_rows = []
        model_usage = []
        model_pricing = catalogue_prices.get(model)

        for sample_number, (_, source) in enumerate(selected, start=1):
            generation_usage: list[dict[str, Any]] = []
            record: dict[str, Any] = {"sample_number": sample_number, "status": "generation_failed"}
            try:
                candidate, validation, sanitized, attempts = synthesize(
                    source,
                    industry,
                    model=model,
                    max_attempts=max_attempts,
                    usage_sink=generation_usage,
                )
            except Exception as error:
                message = str(error).casefold()
                record["failure_category"] = (
                    "validation_blocked" if "fail-closed" in message else "provider_or_generation_failed"
                )
                record["generation_attempts"] = max_attempts if "fail-closed" in message else 0
                record["generation_cost_usd"] = calculate_cost_usd(generation_usage, model_pricing)
                model_usage.extend(generation_usage)
                model_rows.append(record)
                continue

            generation_cost = calculate_cost_usd(generation_usage, model_pricing)
            judge_usage: list[dict[str, Any]] = []
            try:
                scores = judge_quality(sanitized, candidate, judge_model, api_key, judge_usage)
                status = "scored"
            except Exception as error:
                scores = None
                status = "judge_failed"
                record["judge_failure_category"] = (
                    "provider_failed" if isinstance(error, requests.RequestException) else "invalid_response"
                )

            judge_cost = calculate_cost_usd(judge_usage, judge_prices)
            model_usage.extend(generation_usage)
            model_usage.extend(judge_usage)
            total_cost = (
                generation_cost + judge_cost
                if generation_cost is not None and judge_cost is not None
                else None
            )
            record.update(
                status=status,
                generation_attempts=attempts,
                validator_risk=validation.risk_score,
                ngram_overlap=validation.metrics.get("ngram_overlap", 0.0),
                candidate_source_length_ratio=round(len(candidate) / max(1, len(source)), 4),
                generation_cost_usd=generation_cost,
                judge_cost_usd=judge_cost,
                total_cost_usd=total_cost,
                generation_prompt_tokens=sum(item["prompt_tokens"] for item in generation_usage),
                generation_completion_tokens=sum(item["completion_tokens"] for item in generation_usage),
                judge_scores=scores,
            )
            model_rows.append(record)

        generated = [row for row in model_rows if row.get("validator_risk") is not None]
        scored = [row for row in model_rows if row["status"] == "scored"]
        generation_costs = [
            row["generation_cost_usd"]
            for row in model_rows
            if row.get("generation_cost_usd") is not None
        ]
        judge_costs = [
            row["judge_cost_usd"]
            for row in model_rows
            if row.get("judge_cost_usd") is not None
        ]
        total_generation_cost = sum(generation_costs)
        total_judge_cost = sum(judge_costs)
        total_eval_cost = total_generation_cost + total_judge_cost
        model_results.append(
            {
                "model": model,
                "parameter_billions": parameter_billions,
                "pricing_per_million_tokens": {
                    "input_usd": round(float(model_pricing["prompt"]) * 1_000_000, 6)
                    if model_pricing and model_pricing.get("prompt") is not None
                    else None,
                    "output_usd": round(float(model_pricing["completion"]) * 1_000_000, 6)
                    if model_pricing and model_pricing.get("completion") is not None
                    else None,
                },
                "generation_acceptance_rate": round(len(generated) / max(1, len(model_rows)), 3),
                "judge_scoring_rate": round(len(scored) / max(1, len(model_rows)), 3),
                "mean_judge_score": _mean_score(model_rows),
                "mean_validator_risk": round(mean(row["validator_risk"] for row in generated), 4) if generated else None,
                "mean_5gram_overlap": round(mean(row["ngram_overlap"] for row in generated), 4) if generated else None,
                "mean_candidate_source_length_ratio": round(mean(row["candidate_source_length_ratio"] for row in generated), 4) if generated else None,
                "mean_generation_cost_usd": round(total_generation_cost / max(1, len(model_rows)), 8)
                if generation_costs
                else None,
                "total_generation_cost_usd": round(total_generation_cost, 8)
                if generation_costs
                else None,
                "mean_judge_cost_usd": round(total_judge_cost / max(1, len(model_rows)), 8)
                if judge_costs
                else None,
                "total_judge_cost_usd": round(total_judge_cost, 8) if judge_costs else None,
                "mean_total_eval_cost_usd": round(total_eval_cost / max(1, len(model_rows)), 8)
                if generation_costs or judge_costs
                else None,
                "total_eval_cost_usd": round(total_eval_cost, 8)
                if generation_costs or judge_costs
                else None,
                "usage_pricing_basis": "provider-reported cost when available; otherwise provider token counts multiplied by current catalogue rates",
                "sample_results": model_rows,
            }
        )

    return {
        "method": "paired deterministic sample; deterministic privacy validator; fixed independent LLM judge",
        "sample_seed": seed,
        "eligible_dataset_rows": eligible_rows,
        "sample_size": len(selected),
        "industry": industry,
        "judge_model": judge_model,
        "judge_pricing_per_million_tokens": {
            "input_usd": round(float(judge_prices["prompt"]) * 1_000_000, 6)
            if judge_prices and judge_prices.get("prompt") is not None
            else None,
            "output_usd": round(float(judge_prices["completion"]) * 1_000_000, 6)
            if judge_prices and judge_prices.get("completion") is not None
            else None,
        },
        "privacy_note": "Only sanitized source emails and synthetic candidates were sent to OpenRouter. Raw message text and generated content are not included in this report.",
        "unavailable_parameter_tiers": [
            {"parameter_billions": 0.6, "reason": "No clearly identified 0.6B text-generation model was found in the live OpenRouter catalogue; the 1B model is the nearest available small tier."}
        ],
        "value_recommendation": choose_value_model(model_results, score_tolerance),
        "model_results": model_results,
        "limitations": [
            f"{len(selected)} unstratified examples are a pilot, not a statistically representative comparison.",
            "Parameter counts and catalogue prices are provider metadata and can change; MoE active parameter counts may differ from total size.",
            "LLM-judge scores are subjective utility measures, not privacy guarantees or human evaluation.",
            "Model family, instruction tuning, and provider routing vary along with parameter count, so this is not a controlled parameter-only experiment.",
        ],
    }


def main() -> None:
    """Run the model comparison CLI."""
    parser = argparse.ArgumentParser(description="Compare OpenRouter models on a reproducible email sample")
    parser.add_argument("--input", type=Path, default=Path("data/raw/emails_sample.csv"))
    parser.add_argument("--sample-size", type=int, default=5)
    parser.add_argument("--seed", type=int, default=417)
    parser.add_argument("--industry", default="professional services")
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--score-tolerance", type=float, default=0.25)
    parser.add_argument("--report", type=Path, default=Path("reports/model_comparison.json"))
    args = parser.parse_args()

    report = compare_models(
        args.input,
        sample_size=args.sample_size,
        seed=args.seed,
        industry=args.industry,
        judge_model=args.judge_model,
        max_attempts=args.max_attempts,
        score_tolerance=args.score_tolerance,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report_path": str(args.report), "value_recommendation": report["value_recommendation"], "models": [{"model": row["model"], "size_b": row["parameter_billions"], "acceptance": row["generation_acceptance_rate"], "judge_score": row["mean_judge_score"], "generation_cost_per_email": row["mean_generation_cost_usd"], "eval_cost_per_email": row["mean_total_eval_cost_usd"]} for row in report["model_results"]]}, indent=2))


if __name__ == "__main__":
    main()