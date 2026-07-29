"""
analyzer.py
Turns raw scanner output into scored, risk-annotated Opportunity objects.

Three responsibilities, kept separate on purpose so each is independently
testable and swappable:
  1. score.py-equivalent  -> ScoreEngine: confidence + priority scoring
  2. filters.py-equivalent -> RiskFilter: attaches risk level + notes
  3. priority.py-equivalent -> rank_opportunities(): final sort/trim

Philosophy: CAUTIOUS, NOT STRICT. Nothing here silently drops an
opportunity from the report just because it looks risky — it gets
downgraded (lower confidence, higher risk_level) and annotated with WHY,
so the human reviewing the report can decide. The only thing that gets
excluded outright is data that's too thin to mean anything (e.g. a
discrepancy with no liquidity context and no fallback estimate) — because
reporting that would be misleading, not because it's risky.

This module also contains the cross-DEX discrepancy detector (equivalent
to the original analyzer.py's Discrepancy logic), generalized to sit
alongside the other nine opportunity types under one Opportunity model.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from itertools import combinations
from typing import List, Dict, Tuple, Optional
from collections import defaultdict

from config import Config
from models import Opportunity, OpportunityType, RiskLevel, PoolState

logger = logging.getLogger(__name__)


# ==========================================================================
# Cross-DEX discrepancy detection (feeds CROSS_DEX_ARBITRAGE opportunities)
# ==========================================================================

@dataclass
class Discrepancy:
    """A candidate price gap between two pools for the same pair.
    Kept as its own dataclass (not folded into Opportunity.raw) because
    simulator.py needs strongly-typed access to both pools' full state."""
    pair_key: str
    pool_a: PoolState
    pool_b: PoolState
    price_a: float
    price_b: float
    spread_pct: float
    cheaper_pool: PoolState
    pricier_pool: PoolState


def _pair_key(pool: PoolState) -> str:
    symbols = sorted([pool.token0.symbol, pool.token1.symbol])
    return f"{symbols[0]}/{symbols[1]}"


def _normalize_price(pool: PoolState, canonical_order: Tuple[str, str]) -> float:
    if pool.token0.symbol == canonical_order[0]:
        return pool.price_t0_in_t1
    if pool.price_t0_in_t1 == 0:
        return float("inf")
    return 1.0 / pool.price_t0_in_t1


def find_discrepancies(
    pools: List[PoolState],
    min_spread_pct: float = 0.05,
    max_staleness_s: float = 15.0,
) -> List[Discrepancy]:
    """Compare every pair of pools quoting the same token pair (across all
    chains they were fetched from — pools list may span multiple chains,
    grouping is by symbol pair only within the SAME chain, see note below)."""
    discrepancies: List[Discrepancy] = []
    fresh_pools = [p for p in pools if not p.is_stale(max_staleness_s)]

    # Group by (chain, pair_key) — comparing a Base pool to a Solana pool
    # for "the same" symbol pair is not a valid arbitrage (different
    # assets, different settlement), so chain is part of the grouping key.
    groups: Dict[Tuple[str, str], List[PoolState]] = defaultdict(list)
    for p in fresh_pools:
        groups[(p.chain, _pair_key(p))].append(p)

    for (chain, pair_key), pair_pools in groups.items():
        if len(pair_pools) < 2:
            continue
        canonical_order = tuple(sorted([pair_pools[0].token0.symbol, pair_pools[0].token1.symbol]))

        for pool_a, pool_b in combinations(pair_pools, 2):
            try:
                price_a = _normalize_price(pool_a, canonical_order)
                price_b = _normalize_price(pool_b, canonical_order)
            except ZeroDivisionError:
                continue
            if price_a <= 0 or price_b <= 0 or price_a == float("inf") or price_b == float("inf"):
                continue

            spread_pct = abs(price_a - price_b) / min(price_a, price_b) * 100.0
            if spread_pct < min_spread_pct:
                continue

            cheaper, pricier = (pool_a, pool_b) if price_a < price_b else (pool_b, pool_a)
            discrepancies.append(Discrepancy(
                pair_key=pair_key, pool_a=pool_a, pool_b=pool_b,
                price_a=price_a, price_b=price_b, spread_pct=spread_pct,
                cheaper_pool=cheaper, pricier_pool=pricier,
            ))

    discrepancies.sort(key=lambda d: d.spread_pct, reverse=True)
    return discrepancies


def discrepancy_to_opportunity(disc: Discrepancy, net_profit_usd: Optional[float] = None) -> Opportunity:
    """Wraps a Discrepancy in the universal Opportunity shape so it flows
    through the same scoring/risk/report pipeline as the other nine types."""
    return Opportunity(
        opportunity_id=f"arb:{disc.cheaper_pool.chain}:{disc.cheaper_pool.pool_address}:{disc.pricier_pool.pool_address}:{int(time.time())}",
        opp_type=OpportunityType.CROSS_DEX_ARBITRAGE,
        protocol=f"{disc.cheaper_pool.protocol}/{disc.pricier_pool.protocol}",
        chain=disc.cheaper_pool.chain,
        estimated_value_usd=None,   # sized properly once simulator.py runs
        estimated_fees_usd=None,
        discovered_at=time.time(),
        source_note=(
            f"{disc.pair_key}: buy@{disc.cheaper_pool.pool_address[:8]} "
            f"sell@{disc.pricier_pool.pool_address[:8]} — {disc.spread_pct:.3f}% spread"
        ),
        raw={"discrepancy": disc},
        net_profit_estimate_usd=net_profit_usd,
    )


# ==========================================================================
# Risk filtering (equivalent of strategy/filters.ts)
# ==========================================================================

class RiskFilter:
    """
    Assigns risk_level + confidence_score + risk_notes to an Opportunity.
    Deliberately cautious rather than strict: everything stays in the
    report, annotated. High-value opportunities always get flagged for
    manual review regardless of how good the score looks, because that's
    exactly the case where a scoring bug would be most costly to trust
    blindly.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def assess(self, opp: Opportunity) -> Opportunity:
        notes: List[str] = []
        confidence = 100.0

        # --- Data completeness ---
        if opp.estimated_value_usd is None:
            notes.append("No USD value estimate available — sizing unknown")
            confidence -= self.cfg.risk.unverified_field_penalty
        if opp.estimated_fees_usd is None and opp.opp_type in (
            OpportunityType.CROSS_DEX_ARBITRAGE, OpportunityType.LIQUIDATION,
        ):
            notes.append("Fee/cost estimate not computed for this opportunity type")
            confidence -= self.cfg.risk.unverified_field_penalty

        # --- Staleness (age since discovery, not pool data staleness —
        # that's already filtered upstream in scanners.py / find_discrepancies) ---
        age_s = opp.age_s()
        if age_s > 30:
            notes.append(f"Opportunity is {age_s:.0f}s old — on-chain state may have moved")
            confidence -= self.cfg.risk.stale_data_penalty

        # --- Thin liquidity context ---
        if opp.opp_type == OpportunityType.CROSS_DEX_ARBITRAGE:
            disc = opp.raw.get("discrepancy")
            if disc is not None:
                for pool in (disc.cheaper_pool, disc.pricier_pool):
                    if pool.liquidity_depth_usd is not None and pool.liquidity_depth_usd < self.cfg.risk.thin_liquidity_usd_threshold:
                        notes.append(
                            f"Pool {pool.pool_address[:8]}... liquidity (${pool.liquidity_depth_usd:,.0f}) "
                            f"is below the thin-liquidity threshold — real fill may be worse than modeled"
                        )
                        confidence -= self.cfg.risk.thin_liquidity_penalty
                        break

        # --- New pool caution ---
        if opp.opp_type == OpportunityType.NEW_POOL:
            notes.append("Newly created pool — no track record, TVL/behavior unproven")
            confidence -= self.cfg.risk.new_pool_penalty

        # --- Type-specific caution notes (informational, not blocking) ---
        if opp.opp_type == OpportunityType.LIQUIDATION:
            notes.append("Liquidation bonus/discount not modeled — verify asset-specific bonus before acting")
        if opp.opp_type == OpportunityType.RESTAKING_REWARD:
            notes.append("Reward token USD value not priced — check current market price before valuing")
        if opp.opp_type == OpportunityType.SMART_MONEY_MOVE:
            notes.append("A large transfer is not inherently a signal — could be an internal transfer, exchange deposit, etc.")
        if opp.opp_type == OpportunityType.ORACLE_DEVIATION:
            notes.append("Oracle/DEX deviations can reflect the oracle catching up, not a real arbitrage window")

        confidence = max(0.0, min(100.0, confidence))

        # --- Determine risk level from confidence ---
        if confidence >= 70:
            risk_level = RiskLevel.LOW
        elif confidence >= 40:
            risk_level = RiskLevel.MEDIUM
        elif confidence > 0:
            risk_level = RiskLevel.HIGH
        else:
            risk_level = RiskLevel.UNKNOWN

        # --- Mandatory manual-review flag for high-value items, regardless
        # of how confident the score is. This is the "cautious not strict"
        # principle applied concretely: bigger stakes get a floor on scrutiny. ---
        if opp.estimated_value_usd is not None and opp.estimated_value_usd >= self.cfg.risk.high_value_review_threshold_usd:
            notes.append(
                f"Value (${opp.estimated_value_usd:,.0f}) exceeds manual-review threshold "
                f"(${self.cfg.risk.high_value_review_threshold_usd:,.0f}) — recommend independent verification before acting"
            )
            if risk_level == RiskLevel.LOW:
                risk_level = RiskLevel.MEDIUM  # never let high value hide behind a "low risk" label

        opp.confidence_score = confidence
        opp.risk_level = risk_level
        opp.risk_notes = notes
        return opp


# ==========================================================================
# Scoring (equivalent of strategy/score.ts)
# ==========================================================================

class ScoreEngine:
    """
    Computes priority_score (0-100) — a single ranking number that blends
    expected profit, confidence, and risk into one comparable value across
    ALL ten opportunity types. This is what lets the final report show a
    liquidation next to a cross-DEX arb next to a governance event, ranked
    on a common scale.
    """

    RISK_WEIGHT = {
        RiskLevel.LOW: 1.0,
        RiskLevel.MEDIUM: 0.7,
        RiskLevel.HIGH: 0.4,
        RiskLevel.UNKNOWN: 0.2,
    }

    def score(self, opp: Opportunity) -> Opportunity:
        # Base signal: net profit if we have it, else raw estimated value
        # (heavily discounted, since it's not net of costs), else a small
        # nonzero floor so the opportunity is still visible/comparable
        # rather than sorting to the bottom by default.
        if opp.net_profit_estimate_usd is not None:
            base_value = max(0.0, opp.net_profit_estimate_usd)
        elif opp.estimated_value_usd is not None:
            base_value = max(0.0, opp.estimated_value_usd) * 0.3
        else:
            base_value = 1.0

        # Diminishing returns on raw dollar value so a single $1M outlier
        # doesn't blow the 0-100 scale — log-ish compression via a simple
        # saturating curve.
        value_component = min(70.0, base_value / (base_value + 500.0) * 70.0 + (base_value > 0) * 5.0)

        confidence_component = (opp.confidence_score / 100.0) * 30.0

        risk_multiplier = self.RISK_WEIGHT.get(opp.risk_level, 0.2)

        raw_score = (value_component + confidence_component) * risk_multiplier
        opp.priority_score = round(max(0.0, min(100.0, raw_score)), 2)
        return opp


# ==========================================================================
# Priority / ranking (equivalent of strategy/priority.ts)
# ==========================================================================

def rank_opportunities(
    opportunities: List[Opportunity],
    top_n: int = 15,
    min_confidence_to_include: float = 0.0,
) -> List[Opportunity]:
    """
    Final sort step. min_confidence_to_include defaults to 0 (show
    everything) in keeping with "cautious, not strict" — set it above 0
    only if you deliberately want to hide the noisiest, least-verifiable
    opportunities from the top-N view (they're still in the full CSV/JSON
    export either way).
    """
    included = [o for o in opportunities if o.confidence_score >= min_confidence_to_include]
    included.sort(key=lambda o: o.priority_score, reverse=True)
    return included[:top_n]


class Analyzer:
    """Convenience wrapper bundling RiskFilter + ScoreEngine, since every
    opportunity needs both steps applied in sequence before ranking."""

    def __init__(self, cfg: Config):
        self.risk_filter = RiskFilter(cfg)
        self.score_engine = ScoreEngine()

    def process(self, opportunities: List[Opportunity]) -> List[Opportunity]:
        processed = []
        for opp in opportunities:
            opp = self.risk_filter.assess(opp)
            opp = self.score_engine.score(opp)
            processed.append(opp)
        return processed
