"""Tests for LLM-judge score parsing."""

import pytest

from src.evaluate import SCORE_DIMENSIONS, parse_judge_scores


def test_parse_judge_scores_accepts_complete_integer_json():
    scores = {dimension: 4 for dimension in SCORE_DIMENSIONS}

    assert parse_judge_scores(__import__("json").dumps(scores)) == scores


def test_parse_judge_scores_extracts_json_from_fenced_response():
    scores = {dimension: 4 for dimension in SCORE_DIMENSIONS}
    content = "Scores:\n```json\n" + __import__("json").dumps(scores) + "\n```"

    assert parse_judge_scores(content) == scores


@pytest.mark.parametrize(
    "score",
    [0, 6, 2.5, True, "4"],
)
def test_parse_judge_scores_rejects_invalid_values(score):
    scores = {dimension: 3 for dimension in SCORE_DIMENSIONS}
    scores["email_realism"] = score

    with pytest.raises(ValueError):
        parse_judge_scores(__import__("json").dumps(scores))


def test_parse_judge_scores_rejects_missing_dimensions():
    with pytest.raises(ValueError):
        parse_judge_scores('{"purpose_preservation": 4}')