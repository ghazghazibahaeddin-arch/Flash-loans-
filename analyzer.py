"""
analyzer.py
Detects candidate price discrepancies across pools for the same token pair.

This module does NOT decide profitability — that's simulator.py's job,
since profitability depends on trade size, slippage, and gas, not just
the headline price gap. Analyzer's job is narrower: given a set of
PoolStates for the same token pair, find pairs of pools whose prices
disagree by more than noise/fee thresholds, and hand those candidates
to the simulator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from itertools import combinations
from typing import List, Dict, Tuple
from collections import defaultdict

from models import PoolState

logger = logging.getLogger(__name__)


@dataclass
class Discrepancy:
    """A candidate price gap between two pools for the same pair."""
    pair_key: str                # e.g. "WETH/USDC"
    pool_a: PoolState
    pool_b: PoolState
    price_a: float                # price of token0-in-token1 at pool_a
    price_b: float
    spread_pct: float             # abs percentage difference, informational only
    cheaper_pool: PoolState       # buy token0 here (it's priced lower in t1 terms)
    pricier_pool: PoolState       # sell token0 here

    def __str__(self) -> str:
        return (
            f"{self.pair_key}: buy@{self.cheaper_pool.pool_address[:8]} "
            f"({self.cheaper_pool.protocol}) sell@{self.pricier_pool.pool_address[:8]} "
            f"({self.pricier_pool.protocol}) — {self.spread_pct:.3f}% spread"
        )


def _pair_key(pool: PoolState) -> str:
    """Canonical key so pools quoting the same pair (regardless of token0/1
    order) get grouped together."""
    symbols = sorted([pool.token0.symbol, pool.token1.symbol])
    return f"{symbols[0]}/{symbols[1]}"


def group_pools_by_pair(pools: List[PoolState]) -> Dict[str, List[PoolState]]:
    """Group pool states by the token pair they quote."""
    groups: Dict[str, List[PoolState]] = defaultdict(list)
    for p in pools:
        groups[_pair_key(p)].append(p)
    return dict(groups)


def _normalize_price(pool: PoolState, canonical_order: Tuple[str, str]) -> float:
    """
    Return price expressed consistently as
    price(canonical_order[0] in terms of canonical_order[1]),
    regardless of how token0/token1 are ordered in this specific pool.
    """
    if pool.token0.symbol == canonical_order[0]:
        return pool.price_t0_in_t1
    else:
        # token0/token1 are swapped relative to canonical order — invert.
        if pool.price_t0_in_t1 == 0:
            return float("inf")
        return 1.0 / pool.price_t0_in_t1


class Analyzer:
    def __init__(self, min_spread_pct: float = 0.05, max_staleness_s: float = 15.0):
        """
        min_spread_pct: minimum raw spread to even consider a pair of pools.
            This is intentionally set low (default 0.05%) since the
            simulator will apply the real fee/slippage/gas filter — the
            analyzer's threshold just avoids wasting simulation effort on
            pools that are trivially identical (e.g. two pools at the same
            price to several decimal places).
        max_staleness_s: pools with stale quotes are excluded from
            comparison, since a discrepancy against a stale price isn't real.
        """
        self.min_spread_pct = min_spread_pct
        self.max_staleness_s = max_staleness_s

    def find_discrepancies(self, pools: List[PoolState]) -> List[Discrepancy]:
        """
        Compare every pair of pools quoting the same token pair and return
        candidates whose price differs by more than min_spread_pct.
        """
        discrepancies: List[Discrepancy] = []

        fresh_pools = [p for p in pools if not p.is_stale(self.max_staleness_s)]
        stale_count = len(pools) - len(fresh_pools)
        if stale_count:
            logger.debug("Excluded %d stale pool quotes from comparison", stale_count)

        groups = group_pools_by_pair(fresh_pools)

        for pair_key, pair_pools in groups.items():
            if len(pair_pools) < 2:
                continue  # need at least 2 pools to compare

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

                if spread_pct < self.min_spread_pct:
                    continue

                cheaper, pricier = (pool_a, pool_b) if price_a < price_b else (pool_b, pool_a)

                discrepancies.append(Discrepancy(
                    pair_key=pair_key,
                    pool_a=pool_a,
                    pool_b=pool_b,
                    price_a=price_a,
                    price_b=price_b,
                    spread_pct=spread_pct,
                    cheaper_pool=cheaper,
                    pricier_pool=pricier,
                ))

        discrepancies.sort(key=lambda d: d.spread_pct, reverse=True)
        logger.info(
            "Found %d candidate discrepancies across %d pairs (%d pools, %d stale excluded)",
            len(discrepancies), len(groups), len(fresh_pools), stale_count,
        )
        return discrepancies
