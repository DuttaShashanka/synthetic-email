# Experiment Results and Findings

**Architecture and workflow.** The pipeline transforms a source email into a privacy-safe synthetic email through six stages, each gated by the next:

```text
source email
  ├── 1. Header parsing & PII harvesting
  │   Extract sender, recipients, dates, signatures, and salutations with
  │   regex heuristics (HEADER_PERSON_RE, SALUTATION_RE, SIGNATURE_NAME_RE,
  │   SIGN_OFF_RE). Augment with LLM-based named-entity extraction to catch
  │   person names and organization names appearing in the body text that
  │   regex heuristics may miss. Build a source-term deny-list (person names,
  │   organization names, project codes, source domains).
  ├── 2. Direct PII pseudonymization (stable, role-aware)
  │   Replace direct identifiers with fictional equivalents using a persistent
  │   entity graph (data/processed/entity_graph.sqlite):
  │     · Email addresses → {name}@{LLM-generated fictional domain}
  │       Domains are generated via the OpenRouter LLM (domain entity type),
  │       cached per source domain in the graph (synthetic_domain_v3) for
  │       cross-email consistency.
  │     · Person names → LLM-generated fictional names (cached in graph)
  │     · Organizations → LLM-generated fictional company names (cached)
  │     · Projects → LLM-generated fictional project names (cached)
  │     · Dates → deterministic replacement dates
  │     · Phone numbers → +1-202-555-xxxx
  │     · Money → randomized $25K–$874K amounts
  │     · Source domains/terms → redacted to "[REDACTED_SOURCE_TERM]" or
  │       industry-specific substitutes ("Northstar {Industry} Services")
  │   The entity graph records (entity_type, source_value, replacement) and
  │   relationship edges (email belongs_to person, email uses_domain domain)
  │   so the same source person always maps to the same synthetic identity
  │   within and across runs.
  ├── 3. Quasi-identifier abstraction
  │   Replace rare organization names, project codes (PROJECT_RE), acronyms,
  │   and distinctive factual tokens with industry-generic placeholders before
  │   transmission to the LLM. This is the last gate before external calls:
  │   only the sanitized text is sent.
  ├── 4. LLM semantic rewrite
  │   Send the sanitized email to an OpenRouter model with a system prompt
  │   that mandates fictional identities, rephrased subject/body, preserved
  │   communication purpose, and consistent sender identity. Temperature 0.25
  │   for deterministic outputs. The model rewrites every sentence; it never
  │   sees raw source identifiers.
  ├── 5. Post-generation validation
  │   The candidate is checked against:
  │     · Source string leakage (emails, phones, names, organizations)
  │     · Source-domain leakage (enron.com, alfers-carver.com, etc.)
  │     · Source-term leakage (deny-list terms)
  │     · Placeholder leakage (unresolved [[TOKEN_N]] placeholders)
  │     · 5-gram overlap (normalized word 5-grams vs. sanitized source)
  │     · Structural integrity (From, To, Date, Subject headers; readable
  │       body; valid synthetic domain from FICTITIOUS_DOMAINS or graph)
  │     · Sender/sign-off consistency (email local part ≟ sign-off name)
  ├── 6. Accept, retry, or fail closed
  │   If validation passes, the candidate is returned. If it fails, a
  │   category-specific retry prompt is generated and the candidate is
  │   regenerated, up to max_attempts. If all attempts fail, the pipeline
  │   raises RuntimeError and exits non-zero — never silently emits a
  │   leaky synthetic message.
```

**PII removal strategy.** PII removal is layered, not single-point:

- **Regex-based extraction** (headers, emails, phones, dates, salutations, signatures) provides structural PII detection and forms the initial deny-list.
- **LLM-based extraction** augments regex by identifying person names and organization names that appear in the body text but may not match regex patterns (e.g., names in possessive form like "Mark Haedicke's", or names embedded in prose). These are merged into the known-people set before `redact_source_terms` runs.
- **Salutation matching**: salutation first names (e.g., "Dear Andrew") are linked to the full person from the email headers (e.g., "Andrew Fastow <andrew.fastow@enron.com>") so the salutation uses the same pseudonym's first name rather than a separate generated identity.
- **Varied closings**: the pipeline selects from multiple sign-off phrases (e.g., "Thanks, [First Name]", "Kind regards, [Full Name]") randomly per output, rather than always using "Regards, [Full Name]". The LLM system prompt also encourages natural sign-off variety.
- **Direct identifiers** (names, emails, phones, dates, money) are replaced deterministically before any external call. The entity graph persists mappings so that "Craig Carver <ccarver@alfers-carver.com>" always maps to the same synthetic identity across emails and sessions.
- **Quasi-identifiers** (organizations, project codes, distinctive factual combinations) are abstracted to generic terms so the LLM cannot reconstruct them.
- **Post-generation checks** independently verify that no sanitized value, source domain, or source term appears in the candidate. The 5-gram overlap check catches verbatim or near-verbatim copying of sentence structure from the sanitized source.
- **Hash-based identity** in the graph uses SHA-256 of the lowercased source value. Hashes are not cryptographic anonymization — they are reproducible mappings that prevent rainbow-table reversal by using per-deployment graph files.

**Data distribution preservation.** The pipeline preserves the
statistical properties of the source corpus at the communication-pattern
level rather than the literal-content level:

- **Length distribution**: the LLM system prompt specifies "approximate
  length" as a constraint. The candidate/source length ratio is reported
  per model (see table above); all tested models produced shorter outputs,
  flagged as a distribution-preservation weakness.
- **Communication purpose**: each email's functional role (request,
  approval, escalation, update, scheduling, negotiation) is preserved
  and stated explicitly in the system prompt.
- **Author-recipient relationship**: sender and recipient pseudonyms are
  stable within and across sessions, preserving the graph of interpersonal
  relationships while breaking identity linkage to real people.
- **Email structure**: From/To/Date/Subject headers are restored from
  the sanitized source, preserving header-level metadata structure.
- **Industry mapping**: source-specific terms are replaced with
  industry-appropriate substitutes (e.g., "Enron" → "Northstar Agricultural
  Technology Services"), preserving domain context without preserving
  brand identity.
- **Email domains**: domains are LLM-generated as realistic but fictional
  corporate domains, cached per source domain in the entity graph for
  cross-email consistency. The base `FICTITIOUS_DOMAINS` set is always
  accepted by the validator, as are any graph-generated domains.

No claim is made that literal word-frequency or content-topic
distributions are preserved. The pipeline preserves purpose, relationship,
structure, and approximate length, which is the documented scope for this
assessment prototype.

**Dataset profile.** The local sample contains 1,000 messages. Parser-based profiling found sender and body fields in all messages, recipients in 981, subjects in 927, and all four required headers in 909. Lengths are long-tailed: mean 2,957.6 characters, median 1,449.5, p90 5,373, and p95 8,393. These describe this sample only. The phone regex matched every row, exposing an overbroad heuristic rather than reliable phone prevalence.

**Synthesis and evaluation.** The pipeline uses persistent pseudonyms, sends sanitized input to generation, and applies deterministic leakage, structure, and 5-gram checks. A paired 25-message pilot (seed `417`) used `openai/gpt-4o-mini` as an independent 1–5 utility judge:

| Generation model | Parameters | Mean judge score | Acceptance | Mean generation cost/email | Mean eval cost/email (incl. judge) | Candidate/source length ratio |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Llama 3.2 1B | 1B | 4.105 | 96% | $0.000158 | $0.000278 | 0.77 |
| Llama 3.2 3B | 3B | 4.514 | 92% | $0.000241 | $0.000379 | 0.74 |
| Llama 3.1 8B | 8B | 4.526 | 96% | $0.000055 | $0.000159 | 0.66 |
| Gemma 4 31B | 31B | 4.520 | 92% | $0.000382 | $0.000509 | 0.67 |

Gemma 4 31B had the strongest privacy profile by far: mean validator risk 0.0001 and mean 5-gram overlap 0.0001 (max 0.002), roughly 50–130× lower than the Llama tiers, but it needed both attempts for 10 of 25 messages and cost about 7× more than 8B. All failures were fail-closed (`validation_blocked`), never silent. Three messages failed for all four models (samples 11, 15, 19), marking systematically hard inputs; the rest were model-specific (e.g., only the 1B model failed sample 5). Judge overhead ($0.000104–$0.000138/email) exceeds the 8B generation cost by ~2×, so evaluation dominates total cost for cheap generators. The 1B model trails on realism (3.74) and purpose/relationship preservation (3.95), consistent with its dual role as PII extractor and rewriter. Outputs remained shorter than sources (ratio 0.66–0.77); length-distribution preservation is still a weakness.

**Challenges and limitations.** Regex and lexical checks can miss identifiers or overmatch ordinary text; zero validator risk is not proof of privacy. LLM-based extraction and generation add API costs and latency. Judge scores are subjective. The hybrid regex+LLM approach improves recall of body-level PII but does not eliminate false positives or false negatives. This unstratified 25-message pilot is neither human evaluation nor a controlled parameter-only study;
