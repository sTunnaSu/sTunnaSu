# Phase 8 Step 3 — Execution lifecycle and traceability

## Repository and execution-engine findings

`BaseEngine` remains the authority for quantity, order type, limit price,
participation, execution eligibility, fill quantity, IOC/FOK/GTC behavior,
expiry, cancellation, portfolio cash, and positions. Step 3 adds one optional
fail-closed authorization boundary for new entry exposure and post-transition
observation callbacks. With no observer attached, the original engine path is
unchanged.

| Existing transition | Existing owner | Step 3 observation |
|---|---|---|
| A valid `OrderRecord` is registered in `pending_orders` | `BaseEngine._create_order` | `order_submitted` |
| Venue rejects an eligible order | `BaseEngine._process_order` | actual terminal callback |
| A bar executes remaining quantity | `OrderRecord.record_fill` plus `_execute_*_fill` | `fill_received` after `FillRecord` exists |
| IOC/FOK/max-unfilled cancels residual | existing engine | actual terminal callback; completed fills retained |
| `bar_idx > expires_bar_index` | existing engine | expiry terminal callback |
| A fill consumes all remaining quantity | existing engine | separate `fill_received`, then `order_terminal` |
| End-of-backtest/risk exit | existing engine | not budget-gated; original risk-reducing behavior is preserved |

`BaseEngine` has no broker acknowledgement callback. Its adapter therefore
records `UNSUPPORTED` in terminal evidence and does not emit
`broker_acknowledged`. A broker adapter must call
`ExecutionLifecycleService.record_acknowledgement` only for a real
acknowledgement and must declare its timestamp source.

## Implementation code map

| Responsibility | Code |
|---|---|
| Exact callback records and Step 3 enums | `domain/lifecycle.py` |
| ALLOW validation, immutable appends, release, audit, evaluation | `application/lifecycle.py` |
| Deterministic event-time reconstruction | `projections/order_lifecycle.py` |
| Execution observer contract | `ports/execution.py` |
| Existing BaseEngine integration | `adapters/base_engine.py`, `backtest/engines/base.py` |
| History correlation and additive v1→v2 migration | `domain/history.py`, `persistence/history_sqlite.py` |

## Handoff and idempotency policy

- The aggregate root must be exactly one immutable `decision_created` event
  whose decision is `ALLOW` and whose approved economics equal its frozen
  decision economics.
- `REJECT`, `DEFER`, flat, malformed, unmatched, ambiguous, and already
  submitted opportunities cannot authorize a new order.
- `decision_id` is the deterministic `client_order_id` contract for adapters.
  A production broker adapter must pass it to the venue's idempotent client
  order field. This closes the unavoidable process-crash window between an
  external side effect and its local callback.
- BaseEngine adapters allow one matching armed decision and then remove it.
  Restart replay sees `ALREADY_SUBMITTED` and will not arm a second order.
- Callback event idempotency is semantic: submission is unique per decision,
  acknowledgement per acknowledgement ID, fill per venue fill ID, and terminal
  per decision/order. Retries return the prior immutable event.

## Event-time, ingestion-time, and late messages

Ledger order is ingestion order and never changes. Fill projection order is:

1. adapter event timestamp;
2. recorded timestamp;
3. aggregate version;
4. fill ID.

An earlier event-time fill arriving late can replace the projected first fill.
Step 3 appends the new sample and append-only invalidates the formerly selected
sample. It never edits either fill event. An acknowledgement arriving after a
fill is handled the same way: ledger order stays intact while chronology uses
the broker timestamp.

Cumulative consistency is audited after event-time reconstruction. An
apparently incomplete cumulative sequence is not failed while earlier messages
may still be in flight. Overfill is immediately integrity evidence because
receiving more fills cannot repair it.

## Latency release and historical reproducibility

| Component | Definition | `component_available_at` |
|---|---|---|
| submission | `submitted_at - decision_at` | `submitted_at` |
| acknowledgement | `acknowledged_at - submitted_at` | `acknowledged_at` |
| fill | `first_fill_at - submitted_at` | `first_fill_at` |
| final_fill | `final_fill_at - first_fill_at` | `final_fill_at` |

The Step 2 `fill` forecast means first-fill latency. `final_fill` is separate
Step 3 quality evidence and is not silently added to the Step 2 model.

History queries retain `component_available_at < future decision_at`,
`recorded_at <= future decision_at`, current-decision exclusion, version/unit
compatibility, and point-in-time invalidation. Research before a later
invalidation sees what was legally known then; later research sees the
correction. Evidence is never deleted.

## Terminal audit and execution evaluation

The terminal audit checks frozen chronology, order correlation, event-time
cumulative quantities, quantity conservation, and terminal-state quantities.
Failures append `lifecycle_integrity_failure`, retain every event, and
invalidate only affected latency samples. The original ALLOW never changes.

`order_terminal` and `execution_evaluated` are separate. A late broker message
after terminal produces immutable integrity evidence and a revised
`execution_evaluated` event pointing to the prior evaluation. The terminal
event remains unique.

Actual execution cost uses:

`actual fees + implementation shortfall + actual cancellation fee`

Spread, slippage, and impact are reported separately and are not double-counted
inside implementation shortfall. Missing actual spread/impact stays `null`.
Implementation shortfall may be adapter-supplied or derived side-aware from
actual decision and fill prices. Costs cover executed quantity only. No-fill
cost is zero unless an actual cancellation fee is reported.

Step 3 always writes `realised_trade_pnl = null` and
`strategy_outcome_calculated = false`.

## Persistence migration

Latency-history schema v2 adds a non-null `order_id` with an empty legacy
default. The migration is additive. Empty legacy order IDs preserve the v1
semantic fingerprint; Step 3 samples include order correlation. The event
ledger needs no destructive migration because the event family was frozen in
Step 1.

## Concurrency analysis

- Ledger versions use optimistic concurrency with bounded retry.
- In-memory and SQLite stores serialize writes and enforce semantic idempotency.
- Concurrent duplicate fills and terminals append once.
- Different fills may race; deterministic replay reconstructs event-time state.
- A conflicting terminal race appends integrity evidence, not a second terminal.
- Restart reconstructs authorization/lifecycle from the ledger alone.
- Distributed exactly-once placement requires a venue that honors the
  deterministic `client_order_id`; Step 3 does not make a false guarantee when
  a venue lacks that facility.

## Known integration limits (not fabricated)

- BaseEngine timestamps are bar timestamps, not exchange nanosecond clocks.
- BaseEngine quantities/prices are floats internally; the adapter preserves
  their reported decimal text but cannot increase engine arithmetic precision.
- BaseEngine exposes commission and slippage, but not separate realised spread
  or market-impact attribution.
- BaseEngine acknowledgement is unsupported.
- Existing cancellation/rejection states are retained explicitly rather than
  silently relabelled as expiry.

## Step 4 integration contract

Step 4 consumes the latest deterministic Step 3 projection only after
`order_terminal` and at the frozen outcome horizon or actual strategy exit. It
must:

1. preserve the decision, Step 3 events, and latest execution-evaluation ID;
2. calculate strategy outcome separately from execution quality;
3. append one idempotent `outcome_evaluated` revision per frozen
   horizon/methodology identity;
4. record reference-price source, horizon timestamp, executed quantity,
   approved-but-unfilled counterfactual where configured, realised strategy
   P&L/return, and methodology version;
5. never place, cancel, chase, or amend an order; and
6. never rewrite `order_terminal` or `execution_evaluated`.

## Requirement-to-code-to-test traceability

| Requirement | Code | Tests |
|---|---|---|
| ALLOW-only/idempotent handoff | `ExecutionLifecycleService.authorize`, adapter `arm` | rejection and restart-arm tests |
| Actual submission/release | `record_submission` | valid/duplicate submission tests |
| Real/unsupported acknowledgement | `record_acknowledgement`, terminal availability | acknowledgement tests |
| Decimal, duplicate, reordered fills | `record_fill`, lifecycle projector | duplicate/out-of-order/precision/permutation tests |
| First/final fill release | component-history reconciliation | out-of-order history tests |
| Full/partial/no-fill terminal | `record_terminal`, adapter mapping | lifecycle and engine expiry tests |
| Cancellation/fill race | terminal reconciliation and evaluation revisions | crossing-in-flight test |
| Chronology audit/invalidation | audit and integrity-failure append | chronology/conflicting-terminal tests |
| Actual cost/no P&L | execution evaluation | full, sell-side, no-fill, adapter tests |
| Restart and migration | SQLite ledger/history | `test_step3_persistence.py` |
| Concurrency | ledger retry and deterministic projection | concurrent and permutation tests |
| Existing engine authority | optional hooks and BaseEngine adapter | adapter tests plus `test_base_engine.py` |

## Verification evidence (2026-07-16)

- Step 3 lifecycle, adapter, persistence, and state-machine suite: **34 passed**.
- Complete Phase 8 latency-budgeter suite: **107 passed**.
- Existing execution/accounting/reporting/validation regression gate:
  **206 passed**.
- BaseEngine plus engine-robustness rerun: **81 passed, 1 skipped**.
- Ruff: passed. MyPy (`--ignore-missing-imports`): passed for all 47
  Step 3 and BaseEngine source files. UTF-8 and whitespace checks: passed.
- Prescribed repository non-E2E suite: **5,586 passed, 12 skipped, 7 failed,
  9 setup errors**. The setup errors require Windows symlink privilege. Five
  failures assert POSIX `0600`/`0700` modes on Windows; one is the known
  sandbox-home cache-path expectation. The remaining Phase 7 failure is a
  raw-byte fixture hash mismatch caused by `core.autocrlf=true`; the same
  acceptance test passes against the exact committed Git blob.
