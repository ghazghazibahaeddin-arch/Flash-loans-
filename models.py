"""
models.py
Shared data structures used across scanner.py, analyzer.py, and simulator.py.

Deliberately has ZERO external dependencies (no web3, no aiohttp) so that
analyzer.py and simulator.py — which do pure computation — can be imported
and unit tested without pulling in network libraries or an RPC connection.
Only scanner.py (which does the actual fetching) depends on web3/aiohttp.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class TokenInfo:
    address: str
    symbol: str
    decimals: int


@dataclass
class PoolState:
    """Normalized snapshot of a pool, regardless of V2/V3 origin."""
    pool_address: str
    protocol: str            # "uniswap_v2" | "uniswap_v3"
    fee_bps: float
    token0: TokenInfo
    token1: TokenInfo
    price_t0_in_t1: float     # price of 1 token0, denominated in token1
    liquidity_depth_usd: Optional[float]  # approx, from subgraph if available
    fetched_at: float         # unix timestamp, for staleness checks
    block_number: int

    def is_stale(self, max_age_s: float) -> bool:
        return (time.time() - self.fetched_at) > max_age_s
