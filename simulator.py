"""
simulator.py
Pure-math profitability simulation for cross-DEX arbitrage discrepancies.
No RPC calls, no I/O — takes numbers already fetched by scanners.py and
computes what net profit WOULD be, for reporting purposes only.

Nothing in this file holds a key, builds a transaction, or executes
anything. It answers "if someone acted on this, what would the numbers
say?" — the acting part is a separate, manual, human decision.

Simplifications (documented, not hidden):
  - V3 price impact is approximated using current liquidity as if constant
    across the trade range — understates impact for trades that cross
    several ticks. A full tick-walk is out of scope for a monitoring tool.
  - Gas price and native-token/USD price are fetched live when possible;
    otherwise per-chain config fallbacks are used and the result says so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from config import Config, ChainConfig
from analyzer import Discrepancy
from models import PoolState

logger = logging.getLogger(__name__)


@dataclass
class SimulationResult:
    discrepancy: Discrepancy
    trade_size_usd: float
    gross_spread_pct: float
    buy_fee_bps: float
    sell_fee_bps: float
    buy_price_impact_pct: float
    sell_price_impact_pct: float
    gas_cost_usd: float
    gross_profit_usd: float
    net_profit_usd: float
    is_profitable: bool
    notes: List[str] = field(default_factory=list)

    def summary_line(self) -> str:
        return (
            f"[{'PROFIT' if self.is_profitable else 'skip  '}] "
            f"{self.discrepancy.pair_key} ({self.discrepancy.cheaper_pool.chain}) "
            f"size=${self.trade_size_usd:,.0f} "
            f"gross_spread={self.gross_spread_pct:.3f}% "
            f"net_profit=${self.net_profit_usd:,.2f} "
            f"(buy_impact={self.buy_price_impact_pct:.3f}% "
            f"sell_impact={self.sell_price_impact_pct:.3f}% "
            f"gas=${self.gas_cost_usd:.2f})"
        )


def _v2_price_impact_pct(reserve_in: float, reserve_out: float, amount_in: float) -> float:
    """Constant-product price impact for a V2-style pool."""
    if reserve_in <= 0 or amount_in <= 0:
        return 0.0
    spot_price = reserve_out / reserve_in
    amount_out = (reserve_out * amount_in) / (reserve_in + amount_in)
    if amount_in == 0 or spot_price == 0:
        return 0.0
    effective_price = amount_out / amount_in
    return abs(spot_price - effective_price) / spot_price * 100.0


def _v3_price_impact_pct_approx(liquidity: float, sqrt_price: float, amount_in: float) -> float:
    """Approximate V3 price impact treating liquidity as locally constant.
    First-order approximation, not an exact tick-walk — see module docstring."""
    if liquidity <= 0 or sqrt_price <= 0:
        return 100.0
    effective_depth = 2 * liquidity / sqrt_price
    if effective_depth <= 0:
        return 100.0
    return min((amount_in / effective_depth) * 100.0, 100.0)


class Simulator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        # Per-chain live values, set once per scan cycle by main.py via
        # set_live_gas_and_native_price(chain, ...). Falls back to each
        # chain's config defaults when not set.
        self._gas_price_gwei_by_chain: dict[str, float] = {}
        self._native_usd_by_chain: dict[str, float] = {}

    def set_live_gas_and_native_price(self, chain: str, gas_price_gwei: float, native_usd_price: float) -> None:
        self._gas_price_gwei_by_chain[chain] = gas_price_gwei
        self._native_usd_by_chain[chain] = native_usd_price

    def _gas_cost_usd(self, chain: str) -> tuple:
        """Returns (cost_usd, used_fallback: bool)."""
        chain_cfg: Optional[ChainConfig] = self.cfg.network.chains.get(chain)
        gas_fallback = chain_cfg.gas_price_fallback_gwei if chain_cfg else 0.05
        native_fallback = chain_cfg.native_token_usd_fallback if chain_cfg else 3000.0

        gas_price_gwei = self._gas_price_gwei_by_chain.get(chain, gas_fallback)
        native_usd = self._native_usd_by_chain.get(chain, native_fallback)
        used_fallback = chain not in self._gas_price_gwei_by_chain or chain not in self._native_usd_by_chain

        total_gas_units = (
            self.cfg.economics.assumed_gas_units_per_swap
            * self.cfg.economics.assumed_swaps_per_arb
        )
        gas_cost_native = (gas_price_gwei * 1e-9) * total_gas_units
        return gas_cost_native * native_usd, used_fallback

    def simulate_discrepancy(
        self,
        discrepancy: Discrepancy,
        trade_size_usd: float,
        cheaper_pool_reserves: Optional[tuple] = None,
        cheaper_pool_liquidity: Optional[float] = None,
        pricier_pool_reserves: Optional[tuple] = None,
        pricier_pool_liquidity: Optional[float] = None,
    ) -> SimulationResult:
        notes: List[str] = []
        cheaper = discrepancy.cheaper_pool
        pricier = discrepancy.pricier_pool

        price_for_sizing = (
            cheaper.price_t0_in_t1
            if cheaper.token0.symbol == discrepancy.pair_key.split("/")[0]
            else (1.0 / cheaper.price_t0_in_t1 if cheaper.price_t0_in_t1 else 0.0)
        )
        if price_for_sizing <= 0:
            notes.append("Could not size trade — non-positive reference price")
            amount_in_token0 = 0.0
        else:
            amount_in_token0 = trade_size_usd / price_for_sizing

        # --- Buy leg ---
        if cheaper.protocol == "uniswap_v2":
            if cheaper_pool_reserves is None:
                notes.append("Missing raw reserves for buy leg — impact set to 0 (UNVERIFIED)")
                buy_impact_pct = 0.0
            else:
                r0, r1 = cheaper_pool_reserves
                buy_impact_pct = _v2_price_impact_pct(r0, r1, amount_in_token0)
        else:
            if cheaper_pool_liquidity is None:
                notes.append("Missing liquidity for buy leg — impact set to 0 (UNVERIFIED)")
                buy_impact_pct = 0.0
            else:
                sqrt_price = cheaper.price_t0_in_t1 ** 0.5
                buy_impact_pct = _v3_price_impact_pct_approx(cheaper_pool_liquidity, sqrt_price, amount_in_token0)

        # --- Sell leg ---
        if pricier.protocol == "uniswap_v2":
            if pricier_pool_reserves is None:
                notes.append("Missing raw reserves for sell leg — impact set to 0 (UNVERIFIED)")
                sell_impact_pct = 0.0
            else:
                r0, r1 = pricier_pool_reserves
                sell_impact_pct = _v2_price_impact_pct(r0, r1, amount_in_token0)
        else:
            if pricier_pool_liquidity is None:
                notes.append("Missing liquidity for sell leg — impact set to 0 (UNVERIFIED)")
                sell_impact_pct = 0.0
            else:
                sqrt_price = pricier.price_t0_in_t1 ** 0.5
                sell_impact_pct = _v3_price_impact_pct_approx(pricier_pool_liquidity, sqrt_price, amount_in_token0)

        gross_spread_pct = discrepancy.spread_pct
        buy_fee_bps = cheaper.fee_bps
        sell_fee_bps = pricier.fee_bps

        total_cost_pct = (buy_fee_bps / 100.0) + (sell_fee_bps / 100.0) + buy_impact_pct + sell_impact_pct
        net_spread_pct = gross_spread_pct - total_cost_pct
        gross_profit_usd = trade_size_usd * (gross_spread_pct / 100.0)
        net_profit_before_gas_usd = trade_size_usd * (net_spread_pct / 100.0)

        gas_cost_usd, used_fallback = self._gas_cost_usd(cheaper.chain)
        if used_fallback:
            notes.append(f"Using FALLBACK gas/native-price config for chain '{cheaper.chain}', not live data")

        net_profit_usd = net_profit_before_gas_usd - gas_cost_usd

        max_impact = max(buy_impact_pct, sell_impact_pct)
        if max_impact > self.cfg.economics.max_price_impact_pct:
            notes.append(
                f"Price impact ({max_impact:.2f}%) exceeds max_price_impact_pct "
                f"({self.cfg.economics.max_price_impact_pct}%) — size likely unrealistic"
            )

        is_profitable = (
            net_profit_usd >= self.cfg.economics.min_net_profit_usd
            and max_impact <= self.cfg.economics.max_price_impact_pct
        )

        return SimulationResult(
            discrepancy=discrepancy, trade_size_usd=trade_size_usd,
            gross_spread_pct=gross_spread_pct, buy_fee_bps=buy_fee_bps, sell_fee_bps=sell_fee_bps,
            buy_price_impact_pct=buy_impact_pct, sell_price_impact_pct=sell_impact_pct,
            gas_cost_usd=gas_cost_usd, gross_profit_usd=gross_profit_usd,
            net_profit_usd=net_profit_usd, is_profitable=is_profitable, notes=notes,
        )

    def simulate_across_sizes(
        self, discrepancy: Discrepancy, trade_sizes_usd: List[float], **reserve_kwargs,
    ) -> List[SimulationResult]:
        return [self.simulate_discrepancy(discrepancy, size, **reserve_kwargs) for size in trade_sizes_usd]


def best_result_per_discrepancy(results: List[SimulationResult]) -> List[SimulationResult]:
    """Collapse multiple trade-size simulations per discrepancy down to
    the single most profitable size, for feeding into the Opportunity
    scoring pipeline (which expects one profit figure per opportunity)."""
    best: dict = {}
    for r in results:
        key = (r.discrepancy.cheaper_pool.pool_address, r.discrepancy.pricier_pool.pool_address)
        if key not in best or r.net_profit_usd > best[key].net_profit_usd:
            best[key] = r
    return list(best.values())
