# AgentLarry Phase 8 Runtime

This experimental package is the paper-only Phase 8 composition root. It connects coherent
Alpaca market snapshots, immutable accepted or shadow strategy versions,
deterministic modules, portfolio risk, the existing latency/causal gate, durable
order intents, Alpaca paper execution, reconciliation, position ownership,
protective exits, and reproducible reports.

Its presence and mocked tests do not make it execution-ready. The repository
audit remains `NO_GO`, no strategy is independently accepted, and no
prospective untouched out-of-sample advantage has been demonstrated.

The runtime has no live broker profile or live endpoint. Research and dry-run
modes cannot call the broker submission bridge. Paper execution requires all of:

1. an audit release decision permitting the requested paper stage;
2. an audit artifact naming a reviewed Git code revision whose protected
   execution paths are unchanged and clean in the running checkout;
3. `paper_execution_authorized: true` in validated configuration;
4. both `--paper-execute` and `--authorize-paper-execution` on the command line;
5. an immutable `ACCEPTED_PAPER` strategy with validated edge/calibration
   metadata and explicit `paper_order` permission;
6. a passing preflight and final reconciliation;
7. an `ALLOW` decision from the Phase 8 latency gate.

## Commands

The package can be imported and its CLI help inspected without credentials or
broker access. Running the research-only example performs Alpaca paper
read/preflight calls, so do that only as a separately authorized verification:

```powershell
python -m src.phase8_runtime --config .\agent\phase8_runtime.example.yaml --research-only
```

Dry-run and paper execution are intentionally unavailable from the example
configuration. A dry run requires a content-matched audit artifact whose hash
and decision explicitly permit `GO_FOR_DRY_RUN`. Paper execution must not be
enabled merely to force a trade; it requires a separate reviewed configuration,
an independently accepted strategy, and an audit decision permitting the
specific smoke-test stage.

## Durable safety properties

- Runtime events are append-only and hash chained.
- Strategy versions reject semantic mutation.
- Order and client IDs are unique and idempotent.
- Alpaca cumulative fills are projected by delta, preventing duplicate callback
  double counting.
- Ambiguous submissions reconcile by deterministic client order ID and never
  blind-resubmit.
- Owned quantities are separate from pre-existing external positions.
- Protective exits precede entries and use the same latency-gated paper bridge.
- Session loss, drawdown, exposure, reserve, concurrency, and order limits fail
  closed.
- Secrets are rejected from config and redacted from persisted event payloads.
- The legacy agent tool cannot submit; only this complete runtime may reach the
  Phase 8 paper bridge.

## Evidence-qualified reports

Each bounded run writes schema-versioned JSON and Markdown reports. Run-level
events are included with a verified hash-chain result; execution totals are
labelled as cumulative session metrics so a restart cannot make prior orders
disappear. The report reconciles intent, cumulative-fill, owned-allocation,
strategy-state, and attributable-equity projections. It also reports evidence
coverage. Broker fees are never invented: net P&L is emitted only when every
persisted fill has decision-time modelled-fee evidence, and broker-reported fees
remain explicitly unavailable when the paper connector did not supply them.

## Honest limitations

- AI, sentiment, and news modules are explicit unavailable adapters; a strategy
  requiring one is blocked.
- Generated strategies remain shadow-only. Promotion fails closed without
  prospective, untouched out-of-sample evidence.
- Shadow latency estimates are conservative research diagnostics; only the
  existing Phase 8 gate can authorize a paper order.
- Broker-side bracket/stop orders are not created by this runtime. Protective
  exits therefore depend on the bounded monitoring loop and fresh quotes.
- No live trading is supported or approved.
