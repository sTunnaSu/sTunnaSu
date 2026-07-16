# Phase 8 Latency Budgeter — Independent Clean-Room Audit

Audit date: 2026-07-16  
Audited branch: `codex/alpaca-paper-crypto`  
Audited commit: `eca01be51fc8cc69c77f2ee750ca912d1901efca` plus the explicitly inventoried working tree  
Auditor posture: falsification-first; prior implementation summaries and traceability files treated as untrusted claims

## 1. Executive verdict

**Verdict: FAIL**

The implementation contains several well-designed mechanisms, and the original focused suite passed 139 tests. That is not sufficient for release. Six independent adversarial tests all failed and demonstrated defects at release-critical boundaries:

1. a measurement available exactly at the decision timestamp is accepted despite the frozen strict-before rule;
2. lifecycle-integrity failures are reported but do not invalidate the primary experiment;
3. an authorization can be consumed by an unrelated same-symbol/same-side signal;
4. a malformed authorization is discovered only after the engine has registered the order;
5. matched arms can use different frozen outcome prices/query evidence and still be declared valid;
6. matched arms can use different non-gate root configuration while relying on a caller-supplied equality fingerprint.

The evidentiary chain also fails independently of those defects. Every Phase 8 source file, test, assignment, and architecture plan is untracked. Git contains no Phase 8 history and therefore no immutable proof that the claimed preregistration preceded implementation or holdout inspection. The current experiment must be classified as **exploratory, not confirmatory**. No production composition root wires the gate, lifecycle, outcome evaluator, or release engine into AgentLarry outside tests.

Consequently:

- no `GO` decision is scientifically or operationally supportable;
- no realised out-of-sample quality advantage has been demonstrated;
- the current code must not govern paper or live orders;
- the architecture may continue as a research prototype only after the mandatory corrections and a genuinely prospective study.

## 2. Repository-integrity findings

### 2.1 Exact inventory

| Item | Independent result |
|---|---|
| Branch | `codex/alpaca-paper-crypto` |
| HEAD | `eca01be51fc8cc69c77f2ee750ca912d1901efca` |
| Upstream base / merge base | `e88db0f5a22f647a9d840509878349b6837f14e3` |
| Commits ahead of `upstream/main` | 45 |
| Tracked Phase 8 source files | 0 |
| Untracked Phase 8 source files | 60 |
| Tracked Phase 8 tests | 0 |
| Untracked Phase 8 tests before this audit | 19 |
| Audit-only adversarial test added | 1, still untracked |
| Phase 8 Git history | none |
| Tracked modified files | 9 |
| Tracked diff | 952 insertions, 48 deletions |
| Mixed Alpaca work | yes: connector, service, agent loop, and tests |
| Ignored generated runs | yes: `agent/.gitignore:7` ignores `runs/` |

The Phase 8 source first appears in mutable filesystem metadata on 2026-07-16. The assignment PDF and `devel plan.txt` have earlier local timestamps, but neither is tracked, signed, or registered in an append-only store. Filesystem timestamps are mutable and the PDF itself has a creation/modification timestamp inversion. They are not preregistration evidence.

### 2.2 Reproducibility consequence

A fresh clone at HEAD contains none of Phase 8. It cannot reproduce the source, tests, architecture, configuration, or results. Copies under ignored run directories do not repair this. The current implementation and study are local artifacts.

### 2.3 Mixed change-set consequence

`agent/backtest/engines/base.py` contains Phase 8 observer integration while the same working tree contains substantial Alpaca connector work. The change-set is not review-isolated. The unrelated annotation change inside the shared engine further weakens a claim that only Phase 8 integration changed that file.

## 3. Independent specification matrix

This matrix was reconstructed from the frozen assignment and architecture, not copied from existing traceability documents.

| Requirement | Classification | Independent evidence |
|---|---|---|
| Shared observation and cohort classification | Correctly implemented | `application/classification.py:38-104`; conjunction enforced by `domain/models.py:228-242` |
| Unchanged disabled baseline branch | Partially implemented | Observer is optional at `backtest/engines/base.py:384-404`, but the intent and participation/expiry lookups are constructed unconditionally at `1629-1643`; no byte-equivalence proof |
| Provenance and UTC semantics | Mostly implemented | UTC normalization and source metadata are widespread; decision `recorded_at` is nevertheless fabricated as `decision_at` at `application/gate.py:312-321` |
| Frozen configuration | Correctly modelled, not governed | Pydantic frozen/extra-forbid and SHA-256 fingerprint in `configuration/models.py`; no immutable preregistration registry |
| Prior-only latency history | Correctly implemented for history stores | Memory predicate `history_memory.py:82`; SQL predicate `history_sqlite.py:255` |
| Strict component availability | Partially implemented | History uses `<`; direct measurements use `>` instead of `>=` at `estimation/forecast.py:103-125` |
| Cold-start behavior | Correctly implemented | explicit fallback/defer paths in `estimation/forecast.py:32-91` |
| Latency forecast total | Correctly implemented mathematically | component sum in `estimation/forecast.py:143-196` |
| Decay mathematics | Correctly implemented | stable exponential decay and tested numeric boundaries |
| Cost convention | Contradictory | gate sums once at `estimation/costs.py:23-43`; outcome fallback doubles round-trip at `application/outcomes.py:623-630` |
| Strict net-edge gate | Correctly implemented | strict `>` at `policies/decision.py:9-17`; equality rejects |
| Immutable decision event | Mostly implemented | frozen payload/event ledger; ingestion timestamp is not independently recorded at `application/gate.py:312-321` |
| Execution lifecycle | Correctly retains late race evidence | rich order/fill/terminal model intentionally retains late-ingested fill/cancel races and appends integrity evidence |
| Component validation and release | Correctly implemented in isolation | point-in-time release/invalidation logic and strict history queries |
| Cross-event integrity audit | Partially implemented | chronology and conservation checks at `application/lifecycle.py:640-712`, but integrity failures do not invalidate the matched experiment |
| Execution evaluation | Partially implemented | actual costs/fills separated; `realised_decision_latency_ms` uses observed-to-decision age at `application/lifecycle.py:842-895` |
| Outcome evaluation | Partially implemented | actual-fill and no-fill separation is good; late fills can invalidate an already frozen outcome |
| Rejected/deferred counterfactual | Correctly implemented as diagnostic | simulated and excluded from realised P&L |
| Approved-unfilled counterfactual | Correctly implemented as diagnostic | distinct diagnostic class and no realised P&L contamination found |
| Four opportunity counts | Correctly implemented | explicit unique-opportunity grain at `research/experiment.py:597-615` |
| Matched experiment | Incorrect at release boundary | outcome evidence and non-gate configuration can differ without invalidation |
| Frozen holdout decision | Documented but not enforced | self-asserted timestamp at `research/policy.py:19-64`; supplementary evidence is caller-supplied booleans at `research/decision_rules.py:33-69` |
| Runtime composition | Test-only / missing | repository search found constructors and `set_order_lifecycle_observer` only in tests; no AgentLarry composition root |

## 4. Critical and high-severity defects

### F-01 — Equal-timestamp current-opportunity leakage

- **Severity:** High
- **Affected file and line:** `agent/src/latency_budgeter/estimation/forecast.py:103-125`, especially line 108
- **Violated requirement:** `component_available_at < decision_at`
- **Evidence:** direct measurements are rejected only when `available_at > decision_at`. The independent equality test was accepted as `MEASURED_PRE_DECISION` and failed.
- **Consequence:** information that is not strictly available before the decision can enter the forecast, violating point-in-time causality and making the gate optimistic or otherwise contaminated.
- **Minimal correction:** change the exclusion boundary to `available_at >= decision_at` and retain the strict-prior reason code.
- **Regression test required:** equality excluded, one microsecond before accepted, one microsecond after excluded; repeat with offset time zones and DST-adjacent UTC instants.

### F-02 — Lifecycle-integrity failures do not invalidate research release

- **Severity:** Critical
- **Affected file and line:** integrity is only counted at `agent/src/latency_budgeter/research/experiment.py:593` and `664-671`; experiment validity is decided without it at `297-356`
- **Violated requirement:** cross-event integrity failures must fail closed for causal/release claims
- **Evidence:** lifecycle code intentionally retains late-ingested fill/cancel races and appends integrity-failure evidence. The experiment builder reports the count but never adds a validity failure. The adversarial arm containing an integrity failure remained valid and eligible for a primary-improvement claim.
- **Consequence:** a corrupted or contradictory execution history can contribute realised outcomes, uncertainty, and a `GO` decision. Append-only evidence exists but has no control effect at the release boundary.
- **Minimal correction:** invalidate the primary comparison whenever a common-cohort summary contains lifecycle-integrity failures; preserve the events for diagnostics. Outcome evaluation should also label/block realised evidence from an invalid lifecycle.
- **Regression test required:** retained fill/cancel race, chronology failure, quantity mismatch, and corrupted ledger must make experiment validity false and prevent GO; clean reordered fills remain valid.

### F-03 — Authorization is bound only by symbol and side

- **Severity:** Critical
- **Affected file and line:** `agent/src/latency_budgeter/adapters/base_engine.py:79-96`; engine intent at `agent/backtest/engines/base.py:1629-1643`
- **Violated requirement:** immutable ALLOW handoff must authorize the same decision/signal, not merely a similar order
- **Evidence:** candidate selection checks only symbol and side. The engine intent carries no Phase 8 decision identity. An intent explicitly identifying an unrelated decision and signal consumed the armed authorization in the adversarial test.
- **Consequence:** a stale or unrelated ALLOW can authorize a different same-symbol/same-side signal. This is a causal and control-boundary failure.
- **Minimal correction:** carry immutable `decision_id` and `signal_id` from gate handoff into the engine intent and require exact equality before reservation. Never infer authorization by symbol/side.
- **Regression test required:** wrong decision, wrong signal, stale decision, same symbol/side simultaneous signals, and retry/restart cases.

### F-04 — Fail-closed authorization is not validated before mutation

- **Severity:** High
- **Affected file and line:** `agent/backtest/engines/base.py:395-416`; adapter type check at `agent/src/latency_budgeter/adapters/base_engine.py:98-101`
- **Violated requirement:** rejection must occur before actual submission/registration
- **Evidence:** any non-`None` object is accepted as an opaque token. Type validation occurs in `on_order_submitted`, after order registration. The adversarial observer returned `object()`; the order remained `open` and the callback failed only afterward.
- **Consequence:** authorization bugs or adapter drift can create engine orders while the Phase 8 ledger records no valid submission, breaking fail-closed enforcement and audit completeness.
- **Minimal correction:** add a pre-registration authorization-validation contract owned by the observer/adapter and reject before creating or registering an open order.
- **Regression test required:** malformed token, token for another decision, expired token, callback exception, and reservation rollback.

### F-05 — Matched arms may use different outcome evidence

- **Severity:** Critical
- **Affected file and line:** `agent/src/latency_budgeter/research/experiment.py:469-501`
- **Violated requirement:** matched arms must share identical data version, outcome horizon, reference evidence, and evaluation target
- **Evidence:** validation compares selected horizon fields, methodology, cost fields, and only `dataset_version` from provenance. It does not compare outcome reference price, query fingerprint, provider/source event, reference observation time, or reference convention. The adversarial test changed the outcome price from 101 to 999 and the query fingerprint while preserving the dataset label; the comparison remained valid.
- **Consequence:** the estimated arm effect can be caused by different labels/prices rather than the gate. Causal attribution is invalid.
- **Minimal correction:** compare a canonical frozen outcome-evidence fingerprint containing all release-critical target, reference-price, query, source, and provenance fields.
- **Regression test required:** mutate each evidence field independently and require primary-comparison invalidation.

### F-06 — Non-gate arm equality is self-attested

- **Severity:** Critical
- **Affected file and line:** `agent/src/latency_budgeter/research/experiment.py:145-155` and `503-533`
- **Violated requirement:** only `enabled`/the Phase 8 gate may differ between arms
- **Evidence:** `controlled_signature` trusts caller-supplied `non_gate_config_fingerprint`. Per-summary validation checks `enabled`, the arm gate fingerprint, and versions, but never derives a non-gate fingerprint from the frozen root configuration. The adversarial test used `tau_ms=10,000` in baseline and `90,000` in budgeter while claiming the same non-gate fingerprint; the experiment remained valid.
- **Consequence:** any non-gate difference can be hidden behind a matching string, invalidating the treatment-effect claim.
- **Minimal correction:** derive the canonical non-gate fingerprint from every root snapshot after removing only the predeclared treatment field; compare within and across arms.
- **Regression test required:** mutate each non-gate field, nested cost/horizon values, unknown fields, and per-opportunity configuration.

### F-07 — Preregistration and supplementary evidence are self-asserted

- **Severity:** Critical
- **Affected file and line:** `agent/src/latency_budgeter/research/policy.py:19-76`; `research/decision_rules.py:33-69`
- **Violated requirement:** decision rules, dates, thresholds, variants, and evidence must be frozen before holdout inspection
- **Evidence:** the policy only verifies a supplied `preregistered_at < holdout_start`. No append-only registry, commit identity, signature, or prior artifact is required. Supplementary quality evidence consists of caller-supplied booleans. `multiple_comparison_count` is also self-declared. Git contains no Phase 8 history.
- **Consequence:** dates, thresholds, nearby settings, and GO evidence can be created after results and still appear preregistered. The decision engine cannot distinguish prospective evidence from retrospective claims.
- **Minimal correction:** require a content-addressed preregistration record in an immutable registry committed before holdout start; bind policy, variants, data query, code version, and evidence artifacts to hashes.
- **Regression test required:** late registration, modified policy with backdated timestamp, unregistered setting, mismatched artifact hash, and undeclared variant count must block GO.

### F-08 — Reported maximum drawdown is not portfolio drawdown

- **Severity:** High
- **Affected file and line:** `agent/src/latency_budgeter/research/experiment.py:691-708`; release use at `research/decision_rules.py:235-242`
- **Violated requirement:** release risk constraint must measure actual portfolio drawdown
- **Evidence:** the calculation adds discrete realised outcome amounts in outcome order. It has no mark-to-market equity, overlapping-position exposure, intratrade path, or cash/position time series.
- **Consequence:** a strategy can pass the GO drawdown limit while suffering materially larger actual portfolio drawdown between outcomes.
- **Minimal correction:** source drawdown from the frozen timestamped portfolio equity ledger; until available, mark the rule `UNCERTAIN` and prohibit GO rather than label the proxy maximum drawdown.
- **Regression test required:** identical realised terminal P&L with different adverse intratrade paths must produce different portfolio drawdowns.

### F-09 — Round-trip cost convention is internally contradictory

- **Severity:** High
- **Affected file and line:** `agent/src/latency_budgeter/configuration/models.py:20-35`; `estimation/costs.py:23-43`; `application/outcomes.py:616-630`
- **Violated requirement:** frozen fees, spread, slippage, and impact must be applied consistently and exactly once
- **Evidence:** the gate always sums configured components once. The counterfactual fallback doubles the same fields when convention is `round_trip_components`, while immutable decision economics does not. No round-trip differential test exists.
- **Consequence:** identical frozen configuration can yield different simulated cost based on which evidence path is present; treatment decisions and diagnostics are not comparable.
- **Minimal correction:** define whether configured values are already round-trip totals or one-way components requiring two legs, encode leg scope explicitly, and use one shared estimator for gate and outcomes.
- **Regression test required:** maker/taker, one-way, round-trip, long/short, actual-fill, and fallback paths reconcile exactly.

### F-10 — Phase 8 is absent from version history and runtime composition

- **Severity:** Critical
- **Affected file and line:** all files under `agent/src/latency_budgeter/` and `agent/tests/latency_budgeter/` (untracked); optional hook at `agent/backtest/engines/base.py:384-438`
- **Violated requirement:** reproducible implementation and operational integration
- **Evidence:** `git ls-files` returns zero Phase 8 source/tests and `git log --all --` returns no history. Repository-wide constructor search found Phase 8 services only in tests; the sole production-facing surface is an optional observer hook.
- **Consequence:** a fresh clone has no Phase 8; no agent run can activate the full pipeline through a reviewed composition root; reported results cannot be tied to a commit.
- **Minimal correction:** isolate, review, and commit the complete implementation/spec/tests; add an explicit paper/backtest-only composition root behind disabled-by-default configuration.
- **Regression test required:** clean-clone installation and an end-to-end disabled/enabled composition smoke test.

## 5. Temporal leakage report

### Verified controls

- In-memory and SQLite history require `component_available_at < decision_at`.
- Current decision IDs are excluded from prior windows.
- incompatible definition versions, units, invalid samples, and later invalidations are filtered point-in-time.
- timestamp normalization is UTC-aware and rejects naive datetimes.
- rolling windows are deterministic and bounded.

### Failed controls

- **Equal timestamp:** direct predecision measurements accept equality (F-01).
- **Event-time versus ingestion-time:** `DECISION_CREATED.recorded_at` is set to `decision_at` at `application/gate.py:312-321`, so actual publication lag is not observable.
- **Integrity fail-open:** retained late/race evidence can create lifecycle failures that do not invalidate the research comparison (F-02).

### Additional semantic concern

`application/lifecycle.py:842-895` labels observed-to-decision elapsed time as `realised_decision_latency_ms`. The forecast already carries `data_age_ms` separately from forecast decision latency. This calibration compares unlike quantities unless the architecture explicitly defines decision latency as data age, which it does not. Risk-processing latency is not realised. This is a **Medium** semantic/calibration finding; it should block calibration claims until labels and clocks are aligned.

## 6. Mathematical audit

### Independently verified

- `forecast_total_latency_ms` sums data age, decision, risk, submission, acknowledgement, and fill components.
- `exp(-latency/tau)` is implemented with stable positive-domain checks.
- edge at fill equals gross edge times decay.
- net edge equals decayed edge minus selected fee plus spread, slippage, and impact.
- maker and taker are alternatives, not both charged.
- the strict gate rejects equality.
- basis-point and millisecond value objects use deterministic Decimal quantization.
- nearest-rank percentile is explicit; no hidden interpolation was found.
- unsupported acknowledgement is explicit zero with an unsupported mode, not silently missing.
- realised strategy outcome does not subtract implementation shortfall twice; execution quality reports it separately.
- partial fills use actual executed quantity and preserve unfilled quantity separately.

### Not verified / contradictory

- round-trip cost semantics are inconsistent (F-09).
- realised decision-latency calibration is semantically misaligned.
- no empirical evidence establishes that the exponential decay model or chosen `tau` predicts edge decay.

## 7. Event-sourcing audit

### Strengths independently verified

- events and payloads are immutable/frozen;
- in-memory and SQLite stores enforce optimistic aggregate versions, event uniqueness, and idempotency conflicts;
- SQLite has append-only update/delete triggers and durable settings;
- semantic event fingerprints exclude retry-local identifiers appropriately;
- projections rebuild deterministically from event streams;
- order terminal, execution evaluated, and outcome evaluated are separate event types;
- duplicate callbacks and conflicting idempotency keys have meaningful tests;
- out-of-order fill projection is sorted by event time.

### Defects and limitations

- lifecycle integrity failures are retained correctly but do not fail closed at the research boundary (F-02);
- observer callback failures are logged after engine mutation at `backtest/engines/base.py:406-438`, so the execution ledger can silently miss actual submissions/fills/terminal transitions;
- no transactional outbox or recovery scan proves eventual publication after a process crash between engine mutation and event append;
- the SQLite event ledger uses global `PRAGMA user_version`, which needs an explicit database-ownership rule if colocated with other schemas;
- replay from an empty event stream correctly fails because no aggregate exists; rebuilding an empty projection store from a non-empty ledger works, but an operational rebuild command is not wired.

## 8. Baseline compatibility audit

The disabled observer path does not authorize, reject, or append Phase 8 events. That is a useful design property. It is not literally byte-for-byte unchanged:

- observer fields and branches are added to the engine;
- order intent is constructed unconditionally;
- participation and expiry helper calls occur before the observer-enabled check;
- callback failure logging adds side effects and possible performance cost;
- one unrelated shared-engine annotation change is mixed into the diff.

No evidence showed changed pricing, fills, fees, cancellation, sizing, or accounting when the observer is absent. Classification: **partially verified compatibility**, not proven identity. A differential golden-master test over representative engines is required.

## 9. Counterfactual and outcome audit

### Verified

- executed outcomes use weighted actual fill evidence and frozen horizon/exit references;
- rejected/deferred counterfactuals are simulated and labelled;
- approved-unfilled diagnostics are a distinct class;
- diagnostic rows are excluded from realised P&L and the primary realised metric;
- no-fill realised fields are null/zero-contribution according to the estimand;
- retries with changed policy/config/dataset identity are generally rejected;
- original forecasts are preserved in the event stream.

### Failed or uncertain

- invalid lifecycle evidence can remain eligible for the matched/release result (F-02);
- cost fallback convention is inconsistent (F-09);
- matched arms do not prove identical reference evidence (F-05);
- fixed-horizon evidence can be internally well-formed but is not independently bound to a point-in-time market-data registry.

## 10. Matched-experiment audit

The opportunity key, unique common cohort, zero outcome for unexecuted common opportunities, partial-fill handling, diagnostic exclusion, and four counts are coherent. Missing common opportunities, duplicates, outcome asymmetry, version mismatches, and selected horizon/cost differences are tested.

The primary causal comparison nevertheless fails audit because:

1. outcome target/reference evidence may differ (F-05);
2. non-gate configuration equality is self-attested (F-06);
3. the baseline integration is not wired into a production matched-run orchestrator;
4. manifests and data/code versions are caller strings rather than independently resolved immutable artifacts.

Any one of the first two defects is sufficient to invalidate a claimed treatment effect.

## 11. Statistical audit

### Reproducible aspects

- fixed seed produces reproducible bootstrap draws;
- paired effects use equal common-opportunity weighting;
- circular moving blocks preserve local within-symbol serial order;
- symbols are resampled as clusters;
- mean, median, percentile intervals, regime means, sample size, cluster count, and warnings are reported;
- diagnostic counterfactuals are excluded.

### Methodological concerns

- Resampling symbol clusters uniformly while preserving each selected symbol's original length changes opportunity weights when cluster sizes differ. The estimand therefore alternates between equal-opportunity and cluster-resampled populations unless this weighting is formally justified.
- `probability_effect_positive` is the proportion of bootstrap replicate means above zero. It is neither a posterior probability nor, by itself, a valid probability that the true effect is positive. The label overstates interpretation.
- `familywise_confidence_level` is a Bonferroni-adjusted marginal interval confidence level, not observed familywise coverage.
- a default minimum of two clusters is too weak for reliable clustered inference; warnings do not automatically block GO unless policy thresholds do.
- heavy-tail handling is descriptive (mean plus median/percentiles), not a robustness method.
- `multiple_comparison_count` and nearby setting list are self-declared rather than derived from an immutable search registry.
- regime effects have no uncertainty intervals and can be unstable under imbalance.

Classification: **Medium-to-High model-risk concern**. The bootstrap is reproducible code, but its inferential target and release interpretation need a statistical methods note, stronger minimum clusters, artifact-backed variant counts, and simulation-based coverage validation.

## 12. Pre-registration classification

**Classification: exploratory only.**

The assignment and architecture appear, by mutable local timestamps, to predate local Python implementation by roughly one day. That is not proof of preregistration. There is no tracked commit, signed timestamp, registry event, immutable content hash, or remote record. The policy can be instantiated later with any earlier `preregistered_at` value. Existing thresholds, holdout dates, cost convention, reason codes, variants, and GO/REVISE/REJECT rules therefore cannot be shown to have been frozen before outcomes or test feedback.

The current experiment must not be retroactively described as confirmatory. A new prospective study must be registered after code freeze and before any holdout access.

## 13. New adversarial tests and results

Audit-only file: `agent/tests/latency_budgeter/test_independent_audit_adversarial.py`

| Test | Defect class | Current result |
|---|---|---|
| exact-decision-time measurement | temporal boundary/metamorphic | FAIL: equality accepted |
| lifecycle integrity in matched arm | corrupted-ledger / release fail-closed | FAIL: experiment remains valid |
| unrelated signal consumes authorization | causal identity / cross-signal | FAIL: authorization returned |
| malformed token before order registration | mutation-sensitive fail-closed | FAIL: order remains open |
| different outcome price/query across arms | denominator/evidence contamination | FAIL: experiment valid |
| different non-gate root configuration | cross-configuration spoof | FAIL: experiment valid |

Command:

```text
.venv\Scripts\python.exe -m pytest agent\tests\latency_budgeter\test_independent_audit_adversarial.py -q
```

Result before remediation: **6 failed in 2.51s**.

Pre-audit verification evidence:

| Check | Result |
|---|---|
| Focused Phase 8 suite | 139 passed in 3.76s |
| Integrated execution/accounting suite | 213 passed, 1 failed in 7.41s |
| Integrated failure | Phase 7 fixture byte hash changes under Windows CRLF checkout |
| Ruff, Phase 8 + shared engine | passed |
| mypy, normal | 2 missing-stub errors (`yaml`, `pandas`) |
| mypy with missing imports ignored | 56 source files passed |

The Phase 7 hash failure is reproducible and environment-sensitive: the expected hash is the LF-normalized Git blob while the Windows checkout under `core.autocrlf=true` hashes CRLF bytes. It is not a Phase 8 logic failure, but it makes the current cross-platform test claim false and should be corrected by hashing canonical bytes or disabling conversion for that fixture.

## 14. Reproducibility assessment

**Current reproducibility: failed.**

Positive elements exist inside the local code: canonical JSON, SHA-256 fingerprints, immutable events, fixed bootstrap seeds, explicit versions, and deterministic report generation. They cannot compensate for the whole implementation being untracked. The report manifest also trusts caller-supplied code/data/config identifiers rather than resolving them from immutable artifacts.

Minimum reproducibility package:

1. tracked source, tests, migrations, architecture, and assignment;
2. clean commit and isolated Phase 8 branch;
3. content-addressed preregistration record committed before holdout start;
4. immutable data/query manifests and market-data snapshots;
5. clean-clone setup and deterministic end-to-end command;
6. canonical newline handling for hashed fixtures;
7. generated report and ledger snapshot hashes independently re-derived from the clean clone.

## 15. Innovation assessment

| Claim | Evidence available | Evidence missing | Falsification condition | Confidence |
|---|---|---|---|---|
| Architecture novelty | component-level point-in-time release, append-only invalidation, opportunity-grain matched design, execution/outcome separation | prior-art review and comparison with existing execution TCA/gating systems | equivalent prior system or no meaningful design distinction | Low-to-medium |
| Implementation quality | modular boundaries, immutable events/config, 139 original tests, deterministic serialization | corrected adversarial boundaries, tracked code, runtime wiring, crash recovery | boundary failures or unreproducible clean clone | Medium for local craftsmanship; low for release readiness |
| Forecast accuracy | forecast/calibration fields exist | prospective calibration, benchmark model, proper realised component labels, confidence bands | forecast no better than unconditional prior or systematically biased | Not demonstrated |
| Realised OOS economic advantage | matched-effect/report machinery exists | valid preregistered untouched study, identical arms, robust uncertainty, ablation | CI includes non-economic effect, retention/risk failure, advantage disappears after controls | Not demonstrated |

The differentiated mechanisms are plausible engineering contributions, especially component-level point-in-time release and explicit separation of realised outcomes from diagnostics. Complexity alone is not innovation. Publication-quality novelty and economic advantage remain unsupported.

## 16. Mandatory corrections

1. Track and isolate the complete Phase 8 change-set; remove unrelated Alpaca work from its review unit.
2. Enforce strict `<` for every direct and historical component availability path.
3. Preserve late fill/cancel race evidence, but make lifecycle-integrity failures invalidate realised research/release claims.
4. Bind authorization to exact immutable decision and signal identity; validate before order registration.
5. Derive matched-arm non-gate equality from canonical root snapshots, not caller strings.
6. Require identical canonical outcome evidence across arms.
7. Replace self-asserted preregistration/evidence with content-addressed immutable records.
8. Replace the realised-opportunity drawdown proxy with actual portfolio equity drawdown or make GO impossible when unavailable.
9. Resolve and centralize one-way/round-trip cost semantics.
10. Correct ingestion timestamps and realised latency component labels.
11. Add transactional/outbox recovery for observer event publication or explicitly fail the run when the audit ledger diverges.
12. Wire the system through a disabled-by-default paper/backtest composition root and prove clean-clone operation.
13. Freeze a new prospective holdout only after corrections, tests, data manifest, and policy are committed.

## 17. Optional improvements

- Use property-based generation for timestamp offsets, timezone representations, and lifecycle callback orders.
- Add a formal state-machine model and differential replay oracle.
- Validate bootstrap interval coverage with synthetic dependent processes.
- Report uncertainty for regime effects and calibration curves.
- Require more independent symbol clusters and sensitivity to cluster weighting.
- Rename bootstrap replicate proportion to avoid probabilistic overclaiming.
- Namespace SQLite schema versions if stores may share a database.
- Add golden-master baseline differential tests and performance benchmarks.
- Add signed report manifests and a machine-readable audit evidence index.

## 18. Final evidence table

| Claim | Reported | Independently verified | Empirically demonstrated | Confidence | Remaining evidence required |
|---|---|---|---|---|---|
| Phase 8 source is complete and preserved | yes | no; all files untracked | no | High confidence in failure | tracked clean commit and clean-clone reproduction |
| Prior-only history prevents leakage | yes | yes for memory/SQLite history; no for equal-time direct measurements | adversarial failure | High | strict-boundary fix plus temporal property tests |
| Gate math is correct | yes | mostly | numeric tests and source derivation | High, except cost convention | unified round-trip semantics and differential tests |
| Event ledger is append-only and replayable | yes | mostly | existing persistence/replay tests | Medium-high | terminal closure, crash recovery, malformed-event tests |
| Lifecycle is integrity-safe | yes | partially; race evidence retained but release is fail-open | integrity-contaminated experiment failure | High confidence in defect | fail-closed experiment/outcome rules and race tests |
| Authorization is fail-closed | yes | no | wrong-signal and malformed-token failures | High confidence in defect | exact identity binding and pre-mutation validation |
| Baseline is unchanged | yes | partially | no full differential study | Medium | golden-master differential and performance tests |
| Realised and simulated outcomes are separated | yes | yes in normal path | existing tests/source audit | High | prove stability against late fills/outcomes |
| Matched arms differ only by gate | yes | no | two adversarial failures | High confidence in defect | derived config and canonical evidence fingerprints |
| Four counts/denominator are correct | yes | yes for tested opportunity model | existing tests/source audit | Medium-high | production orchestrator and real dataset replay |
| Holdout was preregistered | yes/implied | no immutable evidence | no | High confidence in failure | prospective content-addressed registration |
| Bootstrap uncertainty is defensible | yes | reproducible but methodologically under-justified | deterministic synthetic tests only | Medium-low | methods note, coverage simulation, stronger clusters |
| Maximum drawdown enforces portfolio risk | yes | no; realised-outcome proxy only | no | High confidence in defect | timestamped portfolio equity ledger |
| Forecast is calibrated | implied | no | no prospective calibration study | High confidence in absence | frozen prospective calibration benchmark |
| Budgeter improves OOS net expectancy | implied objective | no | no valid prospective matched holdout | High confidence in absence | corrected system and untouched registered study |
| System is operationally integrated | implied | no; test-only composition | no | High confidence in absence | paper/backtest-only composition root and smoke run |
| System is innovative | implied | differentiated ideas exist | no prior-art/ablation evidence | Low | prior-art comparison and empirical ablation |

**Final disposition:** retain as an exploratory research prototype. Do not release, paper-trade under its authority, or claim economic advantage until the mandatory corrections are complete and a new prospective study passes independent review.

## Post-audit remediation record

The formal audit above was completed before production corrections. The following minimal corrections were then made from confirmed adversarial evidence:

| Finding | Correction | Verification |
|---|---|---|
| F-01 | direct measurements now require `available_at < decision_at` | equality adversarial test passes |
| F-02 | any common-cohort lifecycle-integrity failure invalidates the matched experiment and primary claim | corrupted-lifecycle adversarial test passes |
| F-03 | BaseEngine intents carry decision/signal IDs and the adapter requires exact identity | wrong-signal adversarial test passes |
| F-04 | observers must validate authorization before order registration | malformed-token adversarial test passes |
| F-05 | full horizon/provenance and frozen reference evidence are compared across arms | changed-price/query adversarial test passes |
| F-06 | non-gate fingerprints are derived from frozen root snapshots with only `enabled` removed | spoofed-root-config adversarial test passes |
| Cross-platform fixture | Phase 7 fixture hash is computed from canonical LF bytes | integrated suite passes on Windows |

Post-remediation verification:

| Check | Result |
|---|---|
| Independent adversarial suite | 6 passed in 2.06s |
| Complete focused Phase 8 suite | 145 passed in 3.53s |
| Integrated execution/accounting/acceptance suite | 214 passed in 7.81s |
| Ruff | passed |
| mypy with missing third-party stubs ignored | 56 source files passed |
| Full repository suite | 5,625 passed, 12 skipped, 6 failed, 9 setup errors in 546.72s |

The full-suite failures are not Phase 8 regressions. Five assert POSIX `0600` mode bits on Windows. Nine setup errors require unprivileged symlink creation, which Windows denied with `WinError 1314`. The remaining sandbox-home failure is the downstream consequence of the same unavailable loader-path symlink. They remain genuine repository portability defects and are not counted as passing.

The verdict remains **FAIL** after these code corrections because the larger release blockers remain: Phase 8 is untracked, no immutable preregistration exists, runtime composition is absent, portfolio drawdown is a realised-outcome proxy, cost convention is contradictory, and no valid prospective OOS advantage has been demonstrated.
