"""Strict net-edge decision policy."""

from __future__ import annotations

from src.latency_budgeter.domain.decisions import DecisionOutcome, DecisionReason
from src.latency_budgeter.domain.values import BasisPoints


def decide_net_edge(
    net_edge_bps: BasisPoints,
    required_buffer_bps: BasisPoints,
) -> tuple[DecisionOutcome, DecisionReason]:
    """ALLOW only on strict greater-than; equality deterministically rejects."""
    if net_edge_bps.value > required_buffer_bps.value:
        return DecisionOutcome.ALLOW, DecisionReason.ALLOW_NET_EDGE_ABOVE_BUFFER
    return DecisionOutcome.REJECT, DecisionReason.REJECT_INSUFFICIENT_NET_EDGE
