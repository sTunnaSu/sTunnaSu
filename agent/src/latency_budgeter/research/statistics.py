"""Dependence-aware paired uncertainty for the matched Step 4 experiment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Sequence

import numpy as np

from src.latency_budgeter.domain.timestamps import normalize_timestamp


@dataclass(frozen=True, slots=True)
class MatchedEffectObservation:
    """One equal-weight common opportunity in both experiment arms."""

    opportunity_key: str
    timestamp: datetime
    symbol: str
    baseline_net_bps: Decimal
    budgeter_net_bps: Decimal
    regime: str = "unclassified"

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", normalize_timestamp(self.timestamp))
        object.__setattr__(self, "baseline_net_bps", Decimal(str(self.baseline_net_bps)))
        object.__setattr__(self, "budgeter_net_bps", Decimal(str(self.budgeter_net_bps)))
        if not self.opportunity_key or not self.symbol:
            raise ValueError("opportunity_key and symbol are required")

    @property
    def effect_bps(self) -> Decimal:
        return self.budgeter_net_bps - self.baseline_net_bps


@dataclass(frozen=True, slots=True)
class ClusteredBlockBootstrapResult:
    """Machine-readable paired effect and uncertainty evidence."""

    observed_mean_effect_bps: float
    observed_median_effect_bps: float
    standard_error_bps: float | None
    confidence_interval_low_bps: float | None
    confidence_interval_high_bps: float | None
    median_confidence_interval_low_bps: float | None
    median_confidence_interval_high_bps: float | None
    probability_effect_positive: float | None
    sample_size: int
    symbol_cluster_count: int
    block_length: int
    iterations: int
    seed: int
    requested_confidence_level: float
    familywise_confidence_level: float
    multiple_comparison_count: int
    method: str
    small_sample_warning: str | None
    regime_effects_bps: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_mean_effect_bps": self.observed_mean_effect_bps,
            "observed_median_effect_bps": self.observed_median_effect_bps,
            "standard_error_bps": self.standard_error_bps,
            "confidence_interval_low_bps": self.confidence_interval_low_bps,
            "confidence_interval_high_bps": self.confidence_interval_high_bps,
            "median_confidence_interval_low_bps": self.median_confidence_interval_low_bps,
            "median_confidence_interval_high_bps": self.median_confidence_interval_high_bps,
            "probability_effect_positive": self.probability_effect_positive,
            "sample_size": self.sample_size,
            "symbol_cluster_count": self.symbol_cluster_count,
            "block_length": self.block_length,
            "iterations": self.iterations,
            "seed": self.seed,
            "requested_confidence_level": self.requested_confidence_level,
            "familywise_confidence_level": self.familywise_confidence_level,
            "multiple_comparison_count": self.multiple_comparison_count,
            "method": self.method,
            "small_sample_warning": self.small_sample_warning,
            "regime_effects_bps": dict(sorted(self.regime_effects_bps.items())),
            "primary_weighting": "equal common opportunity",
            "partial_fill_handling": "actual executed-quantity outcome; unexecuted opportunity contributes zero",
            "heavy_tail_handling": "untrimmed mean effect plus median and percentile intervals",
        }


def clustered_moving_block_bootstrap(
    observations: Sequence[MatchedEffectObservation],
    *,
    iterations: int,
    block_length: int,
    confidence_level: float,
    seed: int,
    multiple_comparison_count: int = 1,
    minimum_sample_size: int = 30,
    minimum_clusters: int = 2,
) -> ClusteredBlockBootstrapResult:
    """Bootstrap symbol clusters and serial blocks under a fixed random seed.

    Symbols are sampled with replacement.  Within each selected symbol, a
    circular moving-block bootstrap preserves local serial dependence.  The
    primary statistic remains the equal-opportunity paired mean; no diagnostic
    counterfactual is accepted as an observation.
    """
    if iterations < 1 or block_length < 1:
        raise ValueError("iterations and block_length must be positive")
    if not 0 < confidence_level < 1:
        raise ValueError("confidence_level must be between zero and one")
    if multiple_comparison_count < 1:
        raise ValueError("multiple_comparison_count must be positive")
    ordered = tuple(sorted(observations, key=lambda item: (item.timestamp, item.opportunity_key)))
    effects = np.asarray([float(item.effect_bps) for item in ordered], dtype=float)
    clusters: dict[str, list[MatchedEffectObservation]] = {}
    for item in ordered:
        clusters.setdefault(item.symbol, []).append(item)
    cluster_names = sorted(clusters)
    observed_mean = float(np.mean(effects)) if effects.size else 0.0
    observed_median = float(np.median(effects)) if effects.size else 0.0
    warning_parts: list[str] = []
    if len(ordered) < minimum_sample_size:
        warning_parts.append(
            f"sample_size={len(ordered)} below preregistered minimum={minimum_sample_size}"
        )
    if len(cluster_names) < minimum_clusters:
        warning_parts.append(
            f"symbol_clusters={len(cluster_names)} below preregistered minimum={minimum_clusters}"
        )
    if not ordered or not cluster_names:
        return ClusteredBlockBootstrapResult(
            observed_mean_effect_bps=observed_mean,
            observed_median_effect_bps=observed_median,
            standard_error_bps=None,
            confidence_interval_low_bps=None,
            confidence_interval_high_bps=None,
            median_confidence_interval_low_bps=None,
            median_confidence_interval_high_bps=None,
            probability_effect_positive=None,
            sample_size=0,
            symbol_cluster_count=0,
            block_length=block_length,
            iterations=iterations,
            seed=seed,
            requested_confidence_level=confidence_level,
            familywise_confidence_level=confidence_level,
            multiple_comparison_count=multiple_comparison_count,
            method="paired_symbol_clustered_circular_moving_block_bootstrap_v1",
            small_sample_warning="no matched observations",
            regime_effects_bps={},
        )

    rng = np.random.default_rng(seed)
    means = np.empty(iterations, dtype=float)
    medians = np.empty(iterations, dtype=float)
    for iteration in range(iterations):
        selected_names = rng.choice(cluster_names, size=len(cluster_names), replace=True)
        sampled_effects: list[float] = []
        for selected_name in selected_names:
            sequence = clusters[str(selected_name)]
            target = len(sequence)
            cluster_sample: list[float] = []
            while len(cluster_sample) < target:
                start = int(rng.integers(0, target))
                for offset in range(block_length):
                    item = sequence[(start + offset) % target]
                    cluster_sample.append(float(item.effect_bps))
                    if len(cluster_sample) == target:
                        break
            sampled_effects.extend(cluster_sample)
        means[iteration] = float(np.mean(sampled_effects))
        medians[iteration] = float(np.median(sampled_effects))

    family_alpha = (1.0 - confidence_level) / multiple_comparison_count
    lower_q = 100.0 * family_alpha / 2.0
    upper_q = 100.0 * (1.0 - family_alpha / 2.0)
    regime_effects: dict[str, float] = {}
    for regime in sorted({item.regime for item in ordered}):
        values = [float(item.effect_bps) for item in ordered if item.regime == regime]
        regime_effects[regime] = float(np.mean(values))
    return ClusteredBlockBootstrapResult(
        observed_mean_effect_bps=observed_mean,
        observed_median_effect_bps=observed_median,
        standard_error_bps=float(np.std(means, ddof=1)) if iterations > 1 else 0.0,
        confidence_interval_low_bps=float(np.percentile(means, lower_q)),
        confidence_interval_high_bps=float(np.percentile(means, upper_q)),
        median_confidence_interval_low_bps=float(np.percentile(medians, lower_q)),
        median_confidence_interval_high_bps=float(np.percentile(medians, upper_q)),
        probability_effect_positive=float(np.mean(means > 0.0)),
        sample_size=len(ordered),
        symbol_cluster_count=len(cluster_names),
        block_length=block_length,
        iterations=iterations,
        seed=seed,
        requested_confidence_level=confidence_level,
        familywise_confidence_level=1.0 - family_alpha,
        multiple_comparison_count=multiple_comparison_count,
        method="paired_symbol_clustered_circular_moving_block_bootstrap_v1",
        small_sample_warning="; ".join(warning_parts) if warning_parts else None,
        regime_effects_bps=regime_effects,
    )
