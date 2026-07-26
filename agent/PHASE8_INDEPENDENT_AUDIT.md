# Phase 8 Latency Budgeter - Integration and Independent Audit Status

This document is the durable repository status for AgentLarry Phase 8. It
replaces earlier working-tree audit notes whose pre-remediation inventory had
become stale after the latency-budgeter core was committed and the experimental
paper runtime was added locally.

## Executive verdict

Phase 6/6A/7 execution realism has its own accepted offline contract in
`agent/backtest/PHASE_6_7_ACCEPTANCE.md`.

The Phase 8 research core is present and tested. The experimental Phase 8
runtime, Alpaca paper adapter, provider fixes, and their deterministic tests are
part of the intended repository state. They remain experimental and do not
establish execution readiness.

Current decision: **NO_GO** for dry-run release authority and all paper-order
stages.

No live-trading use is supported or approved.

## Canonical architecture

| Layer | Canonical location | Responsibility |
|---|---|---|
| Historical execution | `agent/backtest/` | Bar-level order lifecycle, fills, costs, portfolio accounting, reports, validation |
| Phase 8 research core | `agent/src/latency_budgeter/` | Immutable evidence, prior-only latency history, decay/cost gate, lifecycle observations, causal research reports |
| Experimental runtime | `agent/src/phase8_runtime/` | Paper-only composition, configuration, risk limits, persistence, strategy state, bounded cycles, reports |
| Paper bridge | `agent/src/trading/phase8_paper.py` | Phase 8 decision-to-Alpaca-paper lifecycle and reconciliation |
| Broker adapter | `agent/src/trading/connectors/alpaca/` | Explicit Alpaca paper/live profile separation and lazy SDK access |
| Agent diagnostics | `agent/src/tools/phase8_paper_tool.py` | Non-submitting Phase 8 diagnostic and reconciliation tools |
| OAuth credentials | `agent/src/providers/codex_credentials.py` | Canonical, atomic Codex credential storage and legacy migration |

The runtime is launched explicitly with `python -m src.phase8_runtime`. Import,
CLI help, tool discovery, and tests must not contact a broker. The sanitized
example configuration is research-only and has paper execution disabled.

## Preserved safety boundaries

- Phase 8 paper execution is disabled by default.
- Only the `alpaca-paper-trade` profile and paper endpoint are accepted by the
  runtime configuration.
- There is no live endpoint or live profile in the Phase 8 composition root.
- Paper submission requires reviewed release evidence, a content hash, an exact
  protected code revision, a clean protected tree, an accepted strategy, a
  passing preflight, explicit configuration authorization, two CLI flags, and
  an `ALLOW` latency decision.
- The auto-discovered Phase 8 agent order tool cannot submit an order.
- Broker imports and SDK access are lazy.
- Credentials and broker state live outside the repository.
- Persisted Phase 8 events redact credential-shaped values.
- Mocked connector tests are not evidence of real Alpaca behavior.

## Research-core remediation retained

Earlier independent review identified defects in timestamp causality,
authorization binding, lifecycle validation, matching, cost semantics, and
release evidence. The committed research core now includes strict-prior
history, stable identifiers, append-only events, lifecycle integrity,
matched-arm validation, frozen release rules, and disabled-baseline
compatibility.

The local integration also fixes repeated lifecycle reconciliation so a sample
derived from one immutable source event remains idempotent even when a later
callback advances the reconciliation clock.

These corrections improve software integrity. They do not create a profitable
strategy or prospective evidence.

## Validation semantics

- Headline trade classification, win rate, profit factor, expectancy, and
  trade-order Monte Carlo use `net_pnl` when present.
- Legacy records genuinely lacking `net_pnl` retain the historical `pnl`
  fallback.
- A zero-gross-P&L exit remains a loss when commission or slippage makes
  `net_pnl` negative.
- The existing sequential equity-curve diagnostic is called rolling-window
  analysis. It does not retrain a model and is not prospective walk-forward
  validation.
- The trade-order Monte Carlo diagnostic permutes the same trade outcomes. It
  is not a random-entry strategy null.

## Evidence status

The repository has strong deterministic, offline test coverage for the
historical execution engine, latency-budget research core, experimental
runtime, paper bridge with fake adapters, safety gates, and OAuth/provider
paths.

The following evidence is absent:

- a real research-only Alpaca read/preflight run tied to a reviewed revision;
- a reviewed dry run;
- a real Alpaca paper acknowledgement/fill lifecycle;
- an independently accepted strategy;
- a precommitted strategy-search registry;
- prospective untouched out-of-sample promotion evidence;
- a demonstrated latency-budget improvement over a matched baseline;
- a complete current repository-wide green test result on Windows.

No result may be described as confirmatory while those items are absent.

## Known limitations

- Phase 6/7 latency is bar-level rather than exchange-timestamp microstructure.
- Historical OHLC limit fills do not model queue position or an order book.
- The Phase 8 runtime remains intentionally conservative and oversized modules
  exceed the contributor guide's preferred line limits; they should be split
  only at reviewed, behavior-preserving boundaries.
- Runtime persistence and the pre-existing latency ledger are separate SQLite
  stores; cross-store atomic publication is not yet provided.
- Runtime-managed protective exits are not broker-native and cannot protect a
  position while the bounded runtime is stopped.
- Alpaca cumulative-fill and ambiguity behavior is tested with deterministic
  fakes, not a real paper account.
- AI, sentiment, and news adapters are explicitly unavailable.
- Generated strategies remain shadow-only without prospective evidence.

## Release gates still required

1. Preserve and review the complete intended source tree.
2. Pass focused compile, CLI, MCP/API composition, safety, provider, Phase 6/7,
   Phase 8, portability, lint, formatting, typing, and sensitive-file checks.
3. Reproduce the intended state from a clean isolated checkout.
4. Commit the reviewed implementation with DCO sign-off.
5. Freeze a new prospective experiment and untouched holdout.
6. Produce an independently accepted strategy with evidence.
7. Issue a new content-addressed release audit for each increasingly mutating
   stage.

## Runtime release marker

Runtime release decision: **NO_GO**

Runtime code revision: **eb49f7bc837e3befb4439be4770e79040bafbfa6**

The marker names the base Git revision observed while the integrated
implementation was still a dirty working tree. It cannot authorize dry-run or
paper execution. A future reviewed audit must name the exact committed code
revision and be hashed into the stage-specific configuration.
