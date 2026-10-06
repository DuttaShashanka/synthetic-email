from src.compare_models import calculate_cost_usd, choose_value_model


def test_calculate_cost_uses_reported_provider_cost_when_present():
    usage = [
        {"prompt_tokens": 100, "completion_tokens": 20, "reported_cost_usd": 0.0002},
        {"prompt_tokens": 30, "completion_tokens": 10, "reported_cost_usd": 0.0001},
    ]

    assert calculate_cost_usd(usage, {"prompt": 1.0, "completion": 2.0}) == 0.0003


def test_calculate_cost_falls_back_to_catalogue_rates():
    usage = [{"prompt_tokens": 100, "completion_tokens": 50, "reported_cost_usd": None}]

    assert calculate_cost_usd(usage, {"prompt": 0.000001, "completion": 0.000002}) == 0.0002


def test_value_model_is_cheapest_within_score_tolerance():
    results = [
        {"model": "small", "parameter_billions": 1, "mean_judge_score": 4.8, "generation_acceptance_rate": 1, "judge_scoring_rate": 1, "mean_generation_cost_usd": 0.001, "mean_total_eval_cost_usd": 0.02},
        {"model": "large", "parameter_billions": 31, "mean_judge_score": 5.0, "generation_acceptance_rate": 1, "judge_scoring_rate": 1, "mean_generation_cost_usd": 0.01, "mean_total_eval_cost_usd": 0.011},
    ]

    recommendation = choose_value_model(results, score_tolerance=0.25)

    assert recommendation["model"] == "small"
    assert recommendation["best_mean_judge_score"] == 5.0


def test_value_model_is_none_when_no_model_was_scored():
    assert choose_value_model([], score_tolerance=0.25) is None


def test_value_model_excludes_insufficient_generation_coverage():
    results = [
        {"model": "partial", "parameter_billions": 31, "mean_judge_score": 5.0, "generation_acceptance_rate": 0.8, "judge_scoring_rate": 0.8, "mean_generation_cost_usd": 0.01, "mean_total_eval_cost_usd": 0.012},
        {"model": "reliable", "parameter_billions": 8, "mean_judge_score": 4.8, "generation_acceptance_rate": 1.0, "judge_scoring_rate": 1.0, "mean_generation_cost_usd": 0.001, "mean_total_eval_cost_usd": 0.003},
    ]

    recommendation = choose_value_model(results, score_tolerance=0.25)

    assert recommendation["model"] == "reliable"