# Experiment Results and Findings

**Dataset profile.** The local sample contains 1,000 messages. Parser-based profiling found sender and body fields in all messages, recipients in 981, subjects in 927, and all four required headers in 909. Lengths are long-tailed: mean 2,957.6 characters, median 1,449.5, p90 5,373, and p95 8,393. These describe this sample only. The phone regex matched every row, exposing an overbroad heuristic rather than reliable phone prevalence.

**Synthesis and evaluation.** The pipeline uses persistent pseudonyms, sends sanitized input to generation, and applies deterministic leakage, structure, and 5-gram checks. A paired five-message pilot (seed `417`) used `openai/gpt-4o-mini` as an independent 1–5 utility judge:

| Generation model | Parameters | Mean judge score | Acceptance | Mean generation cost/email | Candidate/source length ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| Llama 3.2 1B | 1B | 4.00 | 100% | $0.000124 | 0.93 |
| Llama 3.2 3B | 3B | 4.32 | 100% | $0.000140 | 0.68 |
| Llama 3.1 8B | 8B | 4.56 | 100% | $0.000044 | 0.48 |
| Gemma 4 31B | 31B | 4.45 | 80% | $0.000507 | 0.47 |

With at least 90% generation/judge coverage and a 0.25-point quality margin, Llama 3.1 8B is best value. Generation cost averaged $0.000044/email ($0.000196 including judge overhead). The 31B model failed one of five generations. 8B/31B outputs averaged about half the source length, so length-distribution preservation remains a weakness.

**Challenges and limitations.** Regex and lexical checks can miss identifiers or overmatch ordinary text; zero validator risk is not proof of privacy. Judge scores are subjective. This unstratified five-message pilot is neither human evaluation nor a controlled parameter-only study; model families and routing also differ. No clear 0.6B text model was listed, so 1B was the smallest tier. Reports exclude email text.