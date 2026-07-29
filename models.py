"""
models.py
Shared data structures used across every module in the engine.

Deliberately has ZERO external dependencies (no web3, no aiohttp) so that
analyzer.py, scanners.py (logic-only parts), and simulator.py can be
imported and unit tested without pulling in network libraries or a live
RPC connection. Only the actual fetch calls inside scanners.py depend on
web3/aiohttp.

This is a READ-ONLY research and analysis system. Nothing in this file,
or anywhere in this project, holds a private key, builds a transaction,
or signs/broadcasts anything on-chain.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any


# --------------------------------------------------------------------------
# Shared primitives
# --------------------------------------------------------------------------

@dataclass
class TokenInfo:
    address: str
    symbol: str
    decimals: int


@dataclass
class PoolState:
    """Normalized snapshot of a pool, regardless of V2/V3 origin."""
    pool_address: str
    protocol: str            # "uniswap_v2" | "uniswap_v3" | ...
    fee_bps: float
    token0: TokenInfo
    token1: TokenInfo
    price_t0_in_t1: float     # price of 1 token0, denominated in token1
    liquidity_depth_usd: Optional[float]  # approx, from subgraph if available
    fetched_at: float         # unix timestamp, for staleness checks
    block_number: int
    chain: str = "base"

    def is_stale(self, max_age_s: float) -> bool:
        return (time.time() - self.fetched_at) > max_age_s


# --------------------------------------------------------------------------
# Opportunity model — the common currency every scanner produces
# --------------------------------------------------------------------------

class OpportunityType(str, Enum):
    CROSS_DEX_ARBITRAGE = "cross_dex_arbitrage"
    LIQUIDATION = "liquidation"
    RESTAKING_REWARD = "restaking_reward"
    BRIDGE_WATCHTOWER_REWARD = "bridge_watchtower_reward"
    LIQUIDITY_EVENT = "liquidity_event"
    NEW_POOL = "new_pool"
    SMART_MONEY_MOVE = "smart_money_move"
    INCENTIVE_PROGRAM = "incentive_program"
    ORACLE_DEVIATION = "oracle_deviation"
    GOVERNANCE_EVENT = "governance_event"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"   # data too thin to assess — treated cautiously, not hidden


@dataclass
class Opportunity:
    """
    The universal shape every scanner normalizes its findings into, so the
    analyzer/scorer/reporter can work across ten different opportunity
    types without ten different code paths.

    This describes what was OBSERVED on-chain or via an indexer — it is
    not, and never becomes, an instruction to act. Everything downstream
    (score, risk, notes) is informational/report-only.
    """
    opportunity_id: str            # stable-ish id for dedup across cycles, e.g. "liq:base:0xabc:123"
    opp_type: OpportunityType
    protocol: str                  # e.g. "aave_v3", "uniswap_v3", "eigenlayer"
    chain: str                     # e.g. "base", "solana", "arbitrum"
    estimated_value_usd: Optional[float]
    estimated_fees_usd: Optional[float]
    discovered_at: float           # unix timestamp
    source_note: str               # human-readable "why this was flagged"
    raw: Dict[str, Any] = field(default_factory=dict)  # type-specific payload

    # Filled in by analyzer.py, not by the scanner itself.
    risk_level: RiskLevel = RiskLevel.UNKNOWN
    risk_notes: List[str] = field(default_factory=list)
    confidence_score: float = 0.0   # 0-100, how much to trust the estimate
    priority_score: float = 0.0     # 0-100, final ranking score
    net_profit_estimate_usd: Optional[float] = None

    def age_s(self) -> float:
        return time.time() - self.discovered_at

    def summary_line(self) -> str:
        val = f"${self.estimated_value_usd:,.2f}" if self.estimated_value_usd is not None else "n/a"
        net = f"${self.net_profit_estimate_usd:,.2f}" if self.net_profit_estimate_usd is not None else "n/a"
        return (
            f"[{self.risk_level.value.upper():7s}] {self.opp_type.value:24s} "
            f"{self.protocol:14s} ({self.chain:8s}) "
            f"value={val:>12s} net={net:>12s} "
            f"score={self.priority_score:5.1f} conf={self.confidence_score:5.1f} "
            f"— {self.source_note}"
    )
