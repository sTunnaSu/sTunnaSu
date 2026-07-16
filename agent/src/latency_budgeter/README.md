# Phase 8 Steps 1-4: Evidence, Gate, Execution, and Frozen Research

This package implements the observational foundation for AgentLarry's Latency
Budgeter. Step 1 records shared evidence without changing baseline behaviour.
Step 2 applies the optional immutable decision gate. Step 3 observes the
existing Phase 6/7 execution path. Step 4 evaluates frozen outcomes, constructs
matched causal reports, and applies pre-registered GO/REVISE/REJECT rules.

## Boundaries

- `domain/` owns immutable evidence, timestamps, identifiers, and event schemas.
- `configuration/` owns strict, frozen, versioned settings.
- `application/` owns raw-signal intake, non-blocking cohort classification,
  and decision orchestration.
- `estimation/` owns explicit percentile, latency, decay, and cost arithmetic.
- `policies/` owns timestamp, freshness, and strict net-edge rules.
- `ports/` defines ledger, history, strategy-edge, cost, and approved-output
  contracts.
- `persistence/` provides in-memory and durable SQLite adapters for immutable
  events and strict-prior latency history.
- `projections/` reconstructs decision summaries without mutating history.
- `research/` owns matched-arm validation, dependence-aware uncertainty,
  frozen release rules, and reproducible reports.

## Identifier contract

| Identifier | Owner | Generation | Persistence rule |
|---|---|---|---|
| `run_id` | run orchestrator | explicit UUID | globally unique |
| `signal_id` | strategy signal adapter | UUIDv5 over run, observation fingerprint, strategy version, side, and signal key | retry-stable; distinct signal keys represent distinct signals |
| `decision_id` | Phase 8 intake service | UUIDv5 over run, signal, and config version | one aggregate per opportunity |
| `event_id` | event producer | explicit UUID | globally unique; collision never overwrites |

The ledger additionally enforces a unique idempotency key. Repeating the key
with the same semantic content returns the existing event. Repeating it with
different content raises an error.

## Timestamp contract

All persisted timestamps are timezone-aware UTC and serialised with exactly six
microsecond digits followed by `Z`. Naive timestamps are rejected by default.
They may be converted only when the caller explicitly selects
`require_source_timezone` and supplies an unambiguous IANA timezone. Ambiguous
or non-existent daylight-saving wall times are rejected. The observation keeps
the original source timezone as metadata.

Frozen provenance durations are:

- `data_age_ms = decision_at - observed_at`
- `ingestion_delay_ms = source_capture_at - observed_at`
- `processing_delay_ms = decision_at - source_capture_at`

Historical OHLCV users must describe these as simulated bar-level timing, not
exchange-grade microstructure latency.

## Raw-signal counting and baseline compatibility

One successfully appended `decision_created` event increments
`raw_signal_count` once. An idempotent retry increments it zero times. The same
event stores the shared cohort flags and three reporting views:

1. all original baseline activity;
2. matched baseline activity when `common_phase8_eligible=true`;
3. unmatched integrity activity otherwise.

Classification never blocks or changes baseline execution.

## SQLite migration

Opening `SQLiteEventLedger` on a new dedicated database applies schema version
1. The schema enforces unique event IDs, idempotency keys, and per-decision
aggregate versions. `BEFORE UPDATE` and `BEFORE DELETE` triggers abort any
attempt to rewrite history. Appends use `BEGIN IMMEDIATE`, optimistic expected
versions, WAL mode, and full synchronous durability.

## Step 2 point-in-time contract

The enabled gate writes exactly one enriched `decision_created` root. Step 1's
`prepare_raw_signal()` lets the gate reuse the exact shared classification
without first writing a mutable placeholder.

The prior-history predicate is:

```text
component_available_at < decision_at
AND recorded_at <= decision_at
AND decision_id != current_decision_id
AND valid = true
AND no integrity invalidation was available by decision_at
AND unit/component-definition/estimator-schema match the frozen config
```

Rows are ordered by `component_available_at DESC, sample_id DESC`; the frozen
rolling limit is applied after all predicates. Percentiles use nearest rank:
`sorted_values[ceil(p*n/100)-1]`. This deliberately excludes future,
equal-availability, current-row, invalid, incompatible, and backfilled-late
samples.

The freshness boundary is inclusive (`data_age_ms <= freshness_limit_ms`). The
net-edge boundary is strict (`net_edge_bps > required_buffer_bps`), so equality
rejects. Decision arithmetic uses a 0.001 ms grid and a 1e-9 bps grid.

Costs reuse the Phase 6/7 convention by treating fee, spread, slippage, and
impact as separate adverse components. The declared liquidity role selects
exactly one of maker or taker fee; every selected component is summed once.

## SQLite history migration

`SQLiteLatencyHistoryStore` uses a namespaced
`phase8_schema_versions` record rather than SQLite's global `user_version`, so
it can safely share a database with `SQLiteEventLedger`. Schema v1 adds
append-only sample and invalidation tables, strict-prior indexes, and no-update
or-delete triggers. Values are stored as exact decimal text and timestamps as
canonical UTC text.

## Step 3 integration contract

`ApprovedOpportunityPort.publish()` is the only Step 2 output boundary. It is
called only after an ALLOW event is durably appended and receives:

- the frozen decision/run/signal identities;
- symbol, side, and exact `decision_at`; and
- immutable gross edge, component forecasts, decay, cost breakdown, net edge,
  buffer, config fingerprint, and evaluation version.

The port has no order method. A future Step 3 adapter must consume this value,
re-read and verify its immutable decision root, apply expiry/risk controls, and
then append real lifecycle events only as they occur. Rejected/deferred paths
publish nothing and contain only `decision_created`.

All later stages must:

- keep the raw signal, observation, classification, config snapshot, and
  original forecast unchanged;
- append with the current expected aggregate version;
- use the same `decision_id`, `run_id`, and `signal_id` throughout;
- apply any gate only in the enabled branch and only after shared cohort
  classification;
- leave the disabled baseline branch behaviour unchanged;
- record real lifecycle events only when they actually occur;
- never fabricate acknowledgements, fills, costs, latency, or outcomes; and
- store execution and outcome evaluation in their separate immutable events.

Realised latency, costs, fills, and outcomes may be appended later but may
never rewrite the original Step 2 economics.

## Step 3 implementation

The ALLOW-only execution lifecycle is now implemented. See
[STEP3_TRACEABILITY.md](STEP3_TRACEABILITY.md) for engine transition mapping, callback/idempotency
policy, event-time reconstruction, additive migration, known adapter limits,
tests, and the Step 4 integration contract.

## Step 4 implementation

Frozen outcome evaluation and causal reporting are implemented without adding
order authority. See [STEP4_TRACEABILITY.md](STEP4_TRACEABILITY.md) for the
outcome/counterfactual contract, exact denominators, matching audit,
statistical method, release-rule precedence, reproducibility manifest, known
limits, and requirement-to-test traceability.
