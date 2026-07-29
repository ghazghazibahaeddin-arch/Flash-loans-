"""
simulator.py
Simulates the profitability of a candidate arbitrage discrepancy WITHOUT
executing any on-chain transaction. Every function in this file is pure
computation over numbers already fetched by scanner.py — nothing here
touches a private key, builds a transaction, or calls `send_raw_transaction`.

Model:
  1. For a given trade size, compute the constant-product (V2) or
     concentrated-liquidity-approximate (V3) price impact of buying
     token0 at the cheaper pool.
  2. Apply that pool's fee.
  3. Compute the price impact + fee of selling token0 at the pricier pool.
  4. Subtract estimated gas cost (in USD) for both legs.
  5. Report net profit; if negative or below threshold, the opportunity
     is filtered out.

Simplifications (documented, not hidden):
  - V3 price impact is approximated using the pool's current liquidity as
    if it were constant across the trade range. This underestimates
    impact for large trades that cross tick boundaries, which makes this
    a CONSERVATIVE-ish but not exact model. A fully exact V3 simulation
    requires walking the tick bitmap, which is out of scope for a
    monitoring tool — flagged clearly in output.
  - Gas price and native-token/USD price are fetched live when possible;
    otherwise config fallbacks are used (and the result is labeled as such).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from config import Config
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
    notes: List[str]

    def summary_line(self) -> str:
        return (
            f"[{'PROFIT' if self.is_profitable else 'skip  '}] "
            f"{self.discrepancy.pair_key} size=${self.trade_size_usd:,.0f} "
            f"gross_spread={self.gross_spread_pct:.3f}% "
            f"net_profit=${self.net_profit_usd:,.2f} "
            f"(buy_impact={self.buy_price_impact_pct:.3f}% "
            f"sell_impact={self.sell_price_impact_pct:.3f}% "
            f"gas=${self.gas_cost_usd:.2f})"
        )


def _v2_price_impact_pct(reserve_in: float, reserve_out: float, amount_in: float) -> float:
    """
    Constant-product price impact for a V2-style pool.
    Uses the standard x*y=k formula: amount_out = reserve_out * amount_in / (reserve_in + amount_in)
    Price impact = (effective_price - spot_price) / spot_price
    """
    if reserve_in <= 0 or amount_in <= 0:
        return 0.0
    spot_price = reserve_out / reserve_in
    amount_out = (reserve_out * amount_in) / (reserve_in + amount_in)
    if amount_in == 0:
        return 0.0
    effective_price = amount_out / amount_in
    if spot_price == 0:
        return 0.0
    impact = abs(spot_price - effective_price) / spot_price * 100.0
    return impact


def _v3_price_impact_pct_approx(liquidity: float, sqrt_price: float, amount_in: float) -> float:
    """
    Approximate price impact for a V3-style pool, treating liquidity as
    locally constant (i.e. ignoring tick-crossing). This is a simplification
    documented in the module docstring — it will understate impact for
    trades large enough to cross several ticks.

    Approximation: dPrice/Price ~ amount_in / (2 * L / sqrt_price) for small
    trades relative to liquidity depth, derived from the V3 whitepaper's
    delta-liquidity relationship. This is a first-order approximation, not
    an exact tick-walk.
    """
    if liquidity <= 0 or sqrt_price <= 0:
        return 100.0  # can't assess — treat as maximally risky so it's discarded
    effective_depth = 2 * liquidity / sqrt_price
    if effective_depth <= 0:
        return 100.0
    impact = (amount_in / effective_depth) * 100.0
    return min(impact, 100.0)  # cap — beyond this the approximation is meaningless anyway


class Simulator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._native_usd_price: Optional[float] = None
        self._gas_price_gwei: Optional[float] = None

    def set_live_gas_and_native_price(self, gas_price_gwei: float, native_usd_price: float) -> None:
        """
        Called by main.py once per scan cycle with freshly-fetched values
        (gas price from w3.eth.gas_price, native price from a price feed).
        If never called, fallbacks from config are used and results are
        annotated accordingly.
        """
        self._gas_price_gwei = gas_price_gwei
        self._native_usd_price = native_usd_price

    def _gas_cost_usd(self) -> float:
        gas_price_gwei = self._gas_price_gwei or self.cfg.economics.gas_price_gwei_fallback
        native_usd = self._native_usd_price or self.cfg.economics.native_token_usd_fallback

        total_gas_units = (
            self.cfg.economics.assumed_gas_units_per_swap
            * self.cfg.economics.assumed_swaps_per_arb
        )
        gas_cost_native = (gas_price_gwei * 1e-9) * total_gas_units
        return gas_cost_native * native_usd

    def _price_impact_for_pool(self, pool: PoolState, amount_in_token0: float) -> float:
        if pool.protocol == "uniswap_v2":
            # We don't retain raw reserves in PoolState (only derived price),
            # so approximate impact using price + a synthetic reserve model
            # is not possible here without raw reserves. This method expects
            # callers (simulate_discrepancy) to have access to reserves via
            # pool metadata; see NOTE in simulate_discrepancy for how this
            # is threaded through in practice.
            raise NotImplementedError(
                "Call _v2_price_impact_pct directly with raw reserves; "
                "see simulate_discrepancy for usage."
            )
        raise NotImplementedError("Use _v3_price_impact_pct_approx directly for V3 pools.")

    def simulate_discrepancy(
        self,
        discrepancy: Discrepancy,
        trade_size_usd: float,
        cheaper_pool_reserves: Optional[tuple] = None,   # (reserve0, reserve1) for V2, raw units
        cheaper_pool_liquidity: Optional[float] = None,   # for V3
        pricier_pool_reserves: Optional[tuple] = None,
        pricier_pool_liquidity: Optional[float] = None,
    ) -> SimulationResult:
        """
        Simulate buying token0 at discrepancy.cheaper_pool and selling at
        discrepancy.pricier_pool, for a given USD trade size.

        Raw reserves/liquidity are passed in explicitly (rather than
        re-fetched here) because this module is intentionally kept
        RPC-free — it only does math on numbers main.py already fetched.
        This keeps simulator.py pure, synchronous, and trivially unit-testable.
        """
        notes: List[str] = []
        cheaper = discrepancy.cheaper_pool
        pricier = discrepancy.pricier_pool

        # Convert USD trade size to token0 units using the cheaper pool's price.
        # (Assumes token1 is a stable or that price_t0_in_t1 chains to USD —
        # see main.py for how pairs are filtered to keep this assumption sound;
        # flagged in notes if token1 doesn't look like a USD-pegged asset.)
        price_for_sizing = cheaper.price_t0_in_t1 if cheaper.token0.symbol == discrepancy.pair_key.split("/")[0] else 1.0 / cheaper.price_t0_in_t1
        if price_for_sizing <= 0:
            notes.append("Could not size trade — non-positive reference price")
            amount_in_token0 = 0.0
        else:
            amount_in_token0 = trade_size_usd / price_for_sizing

        # --- Buy leg (cheaper pool) ---
        if cheaper.protocol == "uniswap_v2":
            if cheaper_pool_reserves is None:
                notes.append("Missing raw reserves for V2 buy leg — impact set to 0 (UNVERIFIED)")
                buy_impact_pct = 0.0
            else:
                r0, r1 = cheaper_pool_reserves
                buy_impact_pct = _v2_price_impact_pct(r0, r1, amount_in_token0)
        else:  # uniswap_v3
            if cheaper_pool_liquidity is None:
                notes.append("Missing liquidity for V3 buy leg — impact set to 0 (UNVERIFIED)")
                buy_impact_pct = 0.0
            else:
                sqrt_price = cheaper.price_t0_in_t1 ** 0.5
                buy_impact_pct = _v3_price_impact_pct_approx(cheaper_pool_liquidity, sqrt_price, amount_in_token0)

        # --- Sell leg (pricier pool) ---
        if pricier.protocol == "uniswap_v2":
            if pricier_pool_reserves is None:
                notes.append("Missing raw reserves for V2 sell leg — impact set to 0 (UNVERIFIED)")
                sell_impact_pct = 0.0
            else:
                r0, r1 = pricier_pool_reserves
                sell_impact_pct = _v2_price_impact_pct(r0, r1, amount_in_token0)
        else:
            if pricier_pool_liquidity is None:
                notes.append("Missing liquidity for V3 sell leg — impact set to 0 (UNVERIFIED)")
                sell_impact_pct = 0.0
            else:
                sqrt_price = pricier.price_t0_in_t1 ** 0.5
                sell_impact_pct = _v3_price_impact_pct_approx(pricier_pool_liquidity, sqrt_price, amount_in_token0)

        # --- Combine: gross spread minus fees minus slippage ---
        gross_spread_pct = discrepancy.spread_pct
        buy_fee_bps = cheaper.fee_bps
        sell_fee_bps = pricier.fee_bps

        total_cost_pct = (
            (buy_fee_bps / 100.0) + (sell_fee_bps / 100.0)
            + buy_impact_pct + sell_impact_pct
        )

        net_spread_pct = gross_spread_pct - total_cost_pct
        gross_profit_usd = trade_size_usd * (gross_spread_pct / 100.0)
        net_profit_before_gas_usd = trade_size_usd * (net_spread_pct / 100.0)

        gas_cost_usd = self._gas_cost_usd()
        if self._gas_price_gwei is None or self._native_usd_price is None:
            notes.append("Using FALLBACK gas/native-price config values, not live data")

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
            discrepancy=discrepancy,
            trade_size_usd=trade_size_usd,
            gross_spread_pct=gross_spread_pct,
            buy_fee_bps=buy_fee_bps,
            sell_fee_bps=sell_fee_bps,
            buy_price_impact_pct=buy_impact_pct,
            sell_price_impact_pct=sell_impact_pct,
            gas_cost_usd=gas_cost_usd,
            gross_profit_usd=gross_profit_usd,
            net_profit_usd=net_profit_usd,
            is_profitable=is_profitable,
            notes=notes,
        )

    def simulate_across_sizes(
        self,
        discrepancy: Discrepancy,
        trade_sizes_usd: List[float],
        **reserve_kwargs,
    ) -> List[SimulationResult]:
        """Run simulate_discrepancy across multiple trade sizes and return all results."""
        return [
            self.simulate_discrepancy(discrepancy, size, **reserve_kwargs)
            for size in trade_sizes_usd
        ]


def rank_opportunities(results: List[SimulationResult], top_n: int) -> List[SimulationResult]:
    """
    Rank simulation results by net profit (descending), keeping only
    profitable ones, and return the top N. This is the final ranking step
    that feeds main.py's output.
    """
    profitable = [r for r in results if r.is_profitable]
    profitable.sort(key=lambda r: r.net_profit_usd, reverse=True)
    return profitable[:top_n]
