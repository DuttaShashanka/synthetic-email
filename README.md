# Privacy-first synthetic enterprise email prototype

This repository implements a defense-in-depth pipeline for converting a source enterprise email into a synthetic, realistic email suitable for research and model-training experiments.

It is deliberately **not** a regex-only anonymizer and does not position an LLM as the privacy boundary. The pipeline:

1. Parses common email headers.
2. Detects direct identifiers with deterministic rules.
3. Pseudonymizes known identifiers with stable, role-aware fictional replacements.
4. Rewrites quasi-identifying events and rare business facts with an LLM, using only the pre-sanitized text.
5. Applies post-generation disclosure-risk checks, including source-string leakage, contact leakage, company-domain leakage, placeholder leakage, n-gram overlap, and structural validation.
6. Regenerates boundedly when a candidate fails validation; otherwise it fails closed.

## Important limitations

This is an assessment prototype, not a certification of anonymization. “Complete de-identification” cannot be proven using regexes, an LLM, or lexical checks alone. Production deployment should add a governed entity-resolution service, stronger PII/NER models, semantic nearest-neighbor checks against the source corpus, human review for high-risk records, policy controls, audit logging, and ideally local inference for confidential inputs.

## Assessment Deliverables

- [Experiment plan](reports/experiment_plan.md)
- [Experiment results and findings](reports/experiment_results.md)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
test -f .env || cp .env.example .env
# Set OPENROUTER_API_KEY in .env
```

## Demo UI

```bash
streamlit run app.py
```

The workbench visualizes parsing, direct-identifier sanitization, rewriting, and validation. Offline mode requires no API key. OpenRouter mode reads `OPENROUTER_API_KEY` and `OPENROUTER_MODEL` from `.env`; only the directly sanitized email is sent to the provider. Do not use confidential email unless external processing is approved.

The **Replacements** tab shows highlighted source-to-synthetic mappings detected during sanitization. Edit a synthetic value and select **Save replacements** to update the current preview and the local entity graph. Edits are validated before persistence; linked person and email mappings are kept in sync.

## Run with LLM rewriting

```bash
python -m src.main examples/input_email.txt \
  --industry "agricultural technology" \
  --output synthetic_email.txt \
  --report validation_report.json
```

The pipeline stores stable pseudonym mappings and recognized person/email/domain relationships in `data/processed/entity_graph.sqlite`. This directory is ignored by Git. Set `ENTITY_GRAPH_PATH` or pass `--entity-graph` to share a registry across runs or select a separate registry for a study. Keep the registry access-controlled: it stores pseudonyms and relationship edges, while source identifiers are stored as hashes. Hashes are not anonymization and may be guessable; the graph uses heuristic entity recognition and does not guarantee complete coreference or preserve statistical distributions by itself.

## Run offline / deterministic fallback

```bash
python -m src.main examples/input_email.txt --offline
```

Offline mode produces a safe but less natural pseudonymized email. The LLM mode is the intended quality path. Only the direct-identifier-sanitized content is transmitted to OpenRouter.

## What the validator checks

- Original email addresses and phone numbers.
- Original source tokens, person names, organization names, and domains supplied in the source deny-list.
- `enron` references and source domains.
- Unresolved placeholder tokens such as `[[PERSON_1]]`.
- Excessive copied phrase overlap, using normalized 5-grams.
- Basic email structure: headers, subject, readable body, and an address using one of the prototype's synthetic `.com` domains.

The generated `.com` addresses are illustrative identities for synthetic data only. They are not verified as unregistered and must not be used to send email or represent real organizations.

The validator returns a risk report. It does not silently pass unsafe output: after the configured retry limit, the CLI exits with a non-zero error.

## Architecture

```text
source email
  -> parse headers / collect sensitive terms
  -> deterministic PII substitution (stable mapping)
  -> LLM semantic rewrite of sanitized text
  -> post-generation privacy + structure + overlap validation
  -> accept, regenerate, or fail closed
```

## Testing

```bash
pytest -q
```

## Sample Evaluation

Run a reproducible, small-sample evaluation with deterministic privacy checks and LLM-as-judge utility scores:

```bash
python -m src.evaluate --sample-size 5 --seed 417
```

The report is written to `reports/sample_evaluation.json` and contains aggregate message-length, validator-risk, n-gram-overlap, and 1-to-5 judge scores for purpose preservation, relationship preservation, realism, structure, and training utility. The judge sees only the sanitized source and generated candidate. Raw messages and email text are not saved in the report. Scores are subjective utility signals, not privacy guarantees or statistically representative estimates of the full corpus. The generation and judge requests both use OpenRouter and incur provider usage.

Compare the bundled 1B, 3B, 8B, and 31B model candidates on the same seeded sample:

```bash
python -m src.compare_models --sample-size 5 --seed 417
```

The comparison report is written to `reports/model_comparison.json`. It records model acceptance, validator risk, LLM-judge scores, token usage, and cost using provider-reported usage when available or the live OpenRouter catalogue rates otherwise. The recommendation selects the lowest generation-cost candidate within 0.25 points of the highest mean judge score, requiring at least 90% generation and judge coverage; judge cost is reported separately as evaluation overhead. The current catalogue has no clearly identified 0.6B text-generation candidate, so 1B is used as the smallest tier. This small paired sample is a pilot rather than a parameter-controlled or corpus-representative study.

### Recorded Comparison Results

Run on 2026-10-06 with seed `417`, five messages, and `openai/gpt-4o-mini` as the fixed judge:

| Generation model | Parameters | Mean judge score (1-5) | Generation acceptance | Mean generation cost / email |
| --- | ---: | ---: | ---: | ---: |
| `meta-llama/llama-3.2-1b-instruct` | 1B | 4.00 | 100% | $0.000124 |
| `meta-llama/llama-3.2-3b-instruct` | 3B | 4.32 | 100% | $0.000140 |
| `meta-llama/llama-3.1-8b-instruct` | 8B | 4.56 | 100% | $0.000044 |
| `google/gemma-4-31b-it` | 31B | 4.45 | 80% | $0.000507 |

Under the stated rule, Llama 3.1 8B is the best value in this sample: it scored highest overall, had 100% acceptance, and had the lowest generation cost. Its mean evaluation cost, including judge overhead, was approximately `$0.000196` per email. The 31B model accepted four of five outputs. Model families differ as well as parameter count, so this is an initial screening result, not a controlled scaling study.
