# Phase 8 Step 2 Traceability

## Repository findings

- Step 1 already supplied immutable observation/signal/cohort evidence,
  deterministic identities, frozen configuration, and append-only event
  ledgers.
- The shared backtester has mature Phase 6/7 realised commission/slippage and
  fill-ledger accounting. It does not expose a decision-time gross-edge
  estimator or a forward cost-estimator interface.
- Step 2 therefore introduces narrow edge and cost ports. Its frozen cost
  adapter follows the same adverse-cost convention and never imports or calls
  the broker/order path.
- Step 1 intake was split into non-persisting preparation plus its original
  backward-compatible persistence method. This lets Step 2 write one complete,
  immutable `decision_created` event instead of mutating a placeholder.

## Architecture decisions

1. The injected clock is sampled once per new decision. Retries read the
   existing root before invoking edge/history/cost estimators.
2. Shared classification is executed once through `SharedCohortClassifier`.
   Both classifier and enabled gate use exact microsecond-derived milliseconds.
3. Strict-prior history requires `component_available_at < decision_at` and
   `recorded_at <= decision_at`; it excludes the current decision and any
   invalid/incompatible/integrity-affected sample before applying the window.
4. Percentiles use the documented nearest-rank definition with deterministic
   tie ordering in persistence.
5. Maker and taker fees are alternatives. Exactly one fee plus spread,
   slippage, and impact is deducted once.
6. ALLOW publishes an immutable value through `ApprovedOpportunityPort` only
   after the root event is durable. Idempotent replay repairs an interrupted
   publication without duplicating it.
7. Step 2 has no dependency on a connector, broker, order service, or live
   trading API.

## Requirement matrix

| Requirement | Code | Primary tests |
|---|---|---|
| Injectable deterministic clock | `application/gate.py` | `test_step2_gate.py` |
| UTC provenance and exact durations | `domain/timestamps.py`, `policies/timestamps.py` | `test_timestamps.py`, `test_step2_numeric.py`, `test_step2_gate.py` |
| Invalid/stale/common-cohort gates | `application/gate.py`, `application/classification.py` | `test_step2_gate.py` |
| Gross edge and availability | `ports/edge.py`, `application/gate.py` | `test_step2_gate.py` |
| Frozen config and drift prevention | `configuration/models.py`, `application/gate.py` | `test_configuration.py`, `test_step2_gate.py` |
| Strict prior-only history | `ports/history.py`, `persistence/history_memory.py`, `persistence/history_sqlite.py` | `test_step2_history.py` |
| Explicit percentile/window | `estimation/percentile.py`, `estimation/forecast.py` | `test_step2_history.py`, `test_step2_gate.py` |
| Cold-start fallback/defer | `estimation/forecast.py`, `application/gate.py` | `test_step2_gate.py` |
| Component forecast and unsupported ack | `estimation/forecast.py`, `domain/decisions.py` | `test_step2_gate.py` |
| Decay arithmetic | `estimation/decay.py`, `domain/values.py` | `test_step2_numeric.py`, `test_step2_gate.py` |
| Cost single-count and side convention | `estimation/costs.py`, `ports/costs.py` | `test_step2_numeric.py`, `test_step2_gate.py` |
| Strict net-edge gate | `policies/decision.py`, `application/gate.py` | `test_step2_numeric.py`, `test_step2_gate.py` |
| Frozen economics/event immutability | `domain/decisions.py`, `domain/events.py`, `application/gate.py` | `test_step2_gate.py`, `test_intake_projection.py` |
| ALLOW-only Step 3 handoff | `ports/approved.py`, `persistence/approved_memory.py` | `test_step2_gate.py` |
| No execution events on reject/defer | `application/gate.py` | `test_step2_gate.py` |
| Baseline unchanged | `application/intake.py`, `application/classification.py` | `test_classification.py`, `test_step2_gate.py` |
| SQLite migration/coexistence | `persistence/history_sqlite.py` | `test_step2_history.py` |
| Performance regression guard | history/estimation/gate/ledger | `test_step2_benchmarks.py` |

## Leakage audit

- Future, equal-availability, current-decision, invalid, late-recorded,
  incompatible-version/unit, and point-in-time-invalidated history rows are
  tested as excluded.
- Current decision/risk measurements are accepted only when valid, definition
  compatible, and available no later than the decision.
- A gross-edge estimate timestamp after the decision produces an auditable
  invalid-timestamp rejection.
- Existing decisions are loaded before estimators run, preventing retry-time
  history changes from mutating economics.
- Configuration fingerprint mismatch on retry is a hard error.

## Numeric audit

- Basis points use Decimal with a 1e-9 bps grid; milliseconds use a 0.001 ms
  grid derived from integer microseconds.
- Negative/non-finite durations are rejected and oversized durations raise
  explicitly.
- Decay validates positive tau, non-negative forecast, finite range `[0, 1]`,
  and stable underflow to zero for extreme ratios.
- The buffer comparison is strict; equality rejects. Near-threshold rounding,
  decay monotonicity, and cost reconciliation are tested.

## Step 3 contract

Step 3 may consume only `ApprovedOpportunity`. Before submission it must verify
the matching immutable root/config fingerprint, enforce the frozen expiry and
risk policies, and append actual lifecycle events under the same identities.
It must be idempotent by `decision_id`. Rejected and deferred decisions have no
approved output and cannot reach this boundary.

