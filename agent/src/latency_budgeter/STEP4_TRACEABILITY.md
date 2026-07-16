# Phase 8 Step 4 — Frozen outcomes, matched research, and release governance

## Repository findings and frozen protocol interpretation

Steps 1–3 already provide one immutable `decision_created` root, frozen
forecast economics, append-only execution events, deterministic lifecycle
replay, and a separate `execution_evaluated` event. Step 4 therefore adds no
order-routing capability and never edits earlier evidence. It appends one
`outcome_evaluated` event after either the exact frozen horizon or a valid
actual strategy exit, then derives research products by replay.

The conservative v1 interpretation is:

1. an outcome policy must be registered strictly before its decision;
2. fixed-horizon evidence must preserve horizon, reference convention,
   dataset identity, source identity, and point-in-time availability;
3. actual-exit evidence is permitted only for an executed `ALLOW` and must
   match the exact executed quantity;
4. post-fill strategy P&L uses actual fill prices and explicit fees, actual
   cancellation fees, and actual exit costs; implementation shortfall remains
   execution-quality evidence and is not subtracted twice;
5. rejected/deferred and approved-unfilled calculations are labelled simulated
   diagnostics and never become fills, trades, or realised P&L;
6. a common unexecuted opportunity contributes zero to realised net expectancy,
   while its simulated markout remains in a separate diagnostics table;
7. no missing arm, missing outcome, changed horizon, changed cost convention,
   changed data version, or duplicate opportunity may produce an improvement
   claim; and
8. the primary frozen holdout cannot be replaced by nearby-setting analysis.

## Data lineage and denominator design

| Product | Grain / denominator | Source | Diagnostics allowed? |
|---|---|---|---|
| Raw signals | unique cross-arm `opportunity_key` | frozen raw strategy signal | no |
| Common eligible | unique opportunity with frozen common-cohort flag | decision root | no |
| Approved | common opportunity whose immutable decision is `ALLOW` | decision root | no |
| Executed | common opportunity with at least one unique actual fill | lifecycle replay | no |
| Net expectancy / common | all evaluated common opportunities; unexecuted = zero | actual outcome only | no |
| Net expectancy / executed | executed common opportunities with actual net outcome | actual outcome only | no |
| Turnover | actual entry fill notional / frozen initial capital | fills | no |
| Drawdown | sequenced actual net outcome amounts | actual outcomes | no |
| Concentration | HHI of actual entry fill notional by symbol | fills | no |
| Calibration | equal-weight executed opportunity with aligned forecast/actual fields | frozen forecast + later actual | no |
| Counterfactual diagnostics | rejected/deferred or expired-unfilled opportunity | frozen shadow method | yes, separate only |

The cross-arm key hashes the original observation fingerprint, generated time,
strategy version, symbol, side, and an optional pre-strategy
`cross_arm_opportunity_id` discriminator. Run IDs, decision IDs, retries, fill
count, and partial-fill messages cannot inflate opportunity counts.

## Implementation map

| Responsibility | Code |
|---|---|
| Frozen outcome policy, triggers, reference evidence, labels | `domain/outcomes.py` |
| Outcome reference-price port | `ports/outcomes.py` |
| Idempotent horizon/actual-exit evaluator and forecast comparison | `application/outcomes.py` |
| Full decision summary rebuilt from immutable ledger | `projections/research_summary.py` |
| Frozen holdout thresholds and search declaration | `research/policy.py` |
| Dependence-aware paired uncertainty | `research/statistics.py` |
| Arm manifests, matching audit, four counts, and metrics | `research/experiment.py` |
| Frozen GO/REVISE/REJECT rules | `research/decision_rules.py` |
| Machine JSON, human Markdown, and reproducibility manifest | `research/reporting.py` |

## Statistical framework

The primary estimand is the paired budgeter-minus-baseline net outcome in basis
points per identical common eligible opportunity. Every opportunity receives
equal weight. Executed partial fills use only actual executed quantity;
unexecuted opportunities contribute zero realised P&L. The primary estimator
is reported with an untrimmed mean, median, effect size, and percentile
intervals.

Uncertainty uses a deterministic paired symbol-clustered circular moving-block
bootstrap. Symbols are sampled as clusters and local blocks preserve serial
dependence within each selected symbol. Regime means are reported separately.
The familywise interval uses a Bonferroni adjustment for the pre-registered
number of comparisons. Small opportunity or symbol-cluster counts produce an
explicit warning and cannot earn GO. This is a robustness method, not proof of
stationarity or causal transport to future regimes.

## Release-rule precedence

- **GO:** every required frozen rule is `PASS`.
- **REVISE:** the primary effect is directionally positive and no hard rule is
  false, but finite evidence is explicitly `UNCERTAIN` (for example sample
  size, retention reliability, or incomplete pre-registered evidence).
- **REJECT:** matching is invalid, the primary effect is not positive, the
  uncertainty interval rules out the minimum effect, benefit disappears under
  a nearby pre-registered setting, rejection is excessive, or a required risk,
  quality, provenance, cost, calibration, regime, or completeness rule fails.

REVISE is not a default state. One failed required condition blocks GO. A
negative result is retained without post-holdout tuning.

## Reproducibility manifest

Every report records:

- hashes of both arm ledger snapshots (derived from sorted opportunity keys,
  per-decision event-stream hashes, and aggregate versions);
- code, strategy, configuration, dataset, evaluation, cohort, and policy
  versions/fingerprints;
- frozen query parameters and holdout interval;
- bootstrap seed, block length, iterations, confidence level, and comparison
  count;
- experiment, supplementary-evidence, decision-rule, and report fingerprints;
  and
- the explicitly supplied decision/report timestamps.

Canonical sorted JSON plus an injected fixed report timestamp produces
byte-identical JSON and Markdown on regeneration from the same evidence.

## Known limits (kept explicit)

- Exposure remains `null` until a position-time series is supplied; Step 4
  does not fabricate it from entry/exit summaries.
- Entry turnover is available. Round-trip turnover requires immutable exit
  notional evidence.
- Opportunity-realisation drawdown is not a substitute for intratrade mark-to-
  market drawdown.
- Bootstrap intervals do not repair poor provenance, regime omissions,
  selection bias, or a small number of independent symbols.
- Counterfactuals use frozen cost assumptions and reference prices only. They
  do not invent a broker path, queue position, fill, or realised trade.

## Requirement-to-code-to-test traceability

| Requirement | Code | Tests |
|---|---|---|
| Exact-once frozen-horizon evaluation | `OutcomeEvaluationService.evaluate_due/evaluate` | horizon boundary, duplicate scheduler, dataset drift |
| Actual-exit path | trigger validation and realised values | actual exit, rejected exit forbidden, exit-cost retry conflict |
| Executed and partial-fill outcome | `_realised_values` | full and partial-fill tests |
| No-fill handling | null realised schema | no-fill approved-unfilled test |
| Rejected/deferred diagnostics | `_counterfactual_values` | parameterised REJECT/DEFER test |
| Diagnostic labels and P&L separation | outcome schema and projection | counterfactual and expectancy tests |
| Immutable forecasts and execution evaluation | append-only payload + projection | immutable-root and missing-ack test |
| Deterministic projection rebuild | `ResearchDecisionSummaryProjector` | replay-equality test |
| Four opportunity counts | `MatchedExperimentBuilder._metrics` | unique count/fill inflation test |
| Same matched opportunity set | validity audit | equal, missing, and duplicate tests |
| Unmatched baseline separation | unmatched baseline product | dedicated unmatched test |
| Same cost/data/horizon/evaluation | manifest and pair audit | cost-model, horizon, missing-outcome tests |
| Retention and expectancy denominators | arm metrics | denominator/zero-unexecuted test |
| Drawdown, concentration, calibration | arm metrics | metric and integrity test |
| Dependence-aware uncertainty | clustered moving-block bootstrap | seeded, clustered, regime, small-sample test |
| Frozen GO rules | `FrozenHoldoutDecisionEngine` | all-pass and one-failure tests |
| Deterministic REVISE/REJECT | rule precedence | incomplete-evidence and negative-primary tests |
| Frozen holdout / nearby isolation | frozen Pydantic policy + evidence validation | mutation, early decision, nearby-not-primary tests |
| Report reproducibility and products | `Step4ReportGenerator` | byte equality, write/read, required-section test |

## Architecture compliance audit

- Original decision and forecast events are never overwritten.
- One outcome is accepted per decision and frozen policy; conflicting policy,
  price, dataset, horizon, actual-exit quantity, or cost retries fail closed.
- Actual strategy outcomes, execution quality, and simulated diagnostics have
  disjoint fields and labels.
- Four counts use unique opportunity keys, not event or fill counts.
- Only a valid identical common cohort can create a primary effect estimate.
- Non-gate manifest fields are fingerprinted and compared before analysis.
- Invalid experiments return no uncertainty estimate and cannot claim benefit.
- Holdout thresholds and nearby setting IDs are frozen before holdout start.
- GO requires every declared condition; nearby evidence cannot replace the
  primary result.
- Reports regenerate from immutable snapshot hashes and frozen metadata.

## Verification evidence (2026-07-16)

- New Step 4 outcome/research/end-to-end tests: **32 passed**.
- Complete Phase 8 Steps 1–4 package suite: **139 passed**.
- Phase 8 plus execution, partial-fill, execution-realism, accounting,
  reporting, and validation regression gate: **332 passed**.
- Ruff over the Phase 8 package and tests: **passed**.
- MyPy over the ten new/changed Step 4 source targets with third-party missing
  stubs ignored: **passed**. A whole-package run reports only the repository's
  pre-existing missing `types-PyYAML` and `pandas-stubs` packages.
- Repository non-E2E suite: **5,618 passed, 12 skipped, 7 failed, 9 setup
  errors**. None involve the latency-budgeter package. Nine setup errors require
  Windows symlink privilege; five failures assert POSIX `0600`/`0700` modes on
  Windows; one is the known sandbox-home cache-path expectation; one is the
  known CRLF-sensitive Phase 7 fixture byte hash.
- `git diff --check`: **passed**. Step 4 secret-pattern scan: **no matches**.
