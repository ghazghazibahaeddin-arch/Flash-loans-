"""
main.py
Orchestrates the full scan cycle:

  1. (Periodically) discover pools via subgraph.
  2. Fetch live pool state for all tracked pools via RPC (read-only).
  3. Analyze fetched states for cross-pool price discrepancies.
  4. Simulate profitability of each discrepancy across configured trade sizes.
  5. Rank and output the best opportunities (console + CSV log).

This process never signs or broadcasts a transaction. It is a monitoring
and research tool. To act on any opportunity it reports, a human (or a
separate, explicitly-built execution system with its own risk controls)
would need to do so independently.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import signal
import time
from datetime import datetime, timezone
from typing import List, Tuple

from config import Config, load_config, setup_logging
from scanner import Scanner
from models import PoolState
from analyzer import Analyzer, Discrepancy
from simulator import Simulator, SimulationResult, rank_opportunities

logger = logging.getLogger(__name__)


class ScannerApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.scanner = Scanner(cfg)
        self.analyzer = Analyzer(
            min_spread_pct=0.05,
            max_staleness_s=cfg.scan.price_staleness_s,
        )
        self.simulator = Simulator(cfg)
        self._tracked_pools: List[Tuple[str, str, float]] = []  # (address, protocol, fee_bps)
        self._shutdown = asyncio.Event()
        self._csv_initialized = False

    # ------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------

    async def refresh_pool_list(self) -> None:
        """Periodic subgraph-based discovery of which pools to track."""
        logger.info("Running pool discovery via subgraph...")
        raw_pools = await self.scanner.discover_pools_via_subgraph(
            min_tvl_usd=self.cfg.pools.min_tvl_usd,
            limit=200,
        )

        if not raw_pools:
            logger.warning(
                "Discovery returned no pools. If this persists, check "
                "network.subgraph_url in config — it may still be the "
                "placeholder value. Falling back to any statically "
                "configured pools."
            )
            return

        tracked: List[Tuple[str, str, float]] = []
        for p in raw_pools:
            try:
                fee_tier = p.get("feeTier")
                protocol = "uniswap_v3" if fee_tier is not None else "uniswap_v2"
                fee_bps = (float(fee_tier) / 100.0) if fee_tier is not None else 30.0
                tracked.append((p["id"], protocol, fee_bps))
            except (KeyError, TypeError, ValueError) as e:
                logger.debug("Skipping malformed pool entry from subgraph: %s (%s)", p, e)

        self._tracked_pools = tracked
        logger.info("Now tracking %d pools after discovery.", len(self._tracked_pools))

    # ------------------------------------------------------------
    # One full scan cycle
    # ------------------------------------------------------------

    async def run_scan_cycle(self) -> List[SimulationResult]:
        if not self._tracked_pools:
            logger.warning("No pools tracked yet — skipping scan cycle.")
            return []

        # Fetch live prices for all tracked pools.
        pool_states: List[PoolState] = await self.scanner.fetch_all(self._tracked_pools)
        logger.info("Fetched live state for %d/%d tracked pools", len(pool_states), len(self._tracked_pools))

        if len(pool_states) < 2:
            logger.info("Fewer than 2 pools returned valid state — nothing to compare this cycle.")
            return []

        # Try to get a live gas price; fall back silently to config default
        # (simulator.py annotates results when it's using the fallback).
        try:
            gas_price_wei = self.scanner.w3.eth.gas_price
            gas_price_gwei = gas_price_wei / 1e9
            # NOTE: native token USD price should come from a real price feed
            # (e.g. a stable pool's quote, or an oracle). Wiring a specific
            # feed is left to the operator; using config fallback here keeps
            # this module honest about what it does vs. doesn't fetch live.
            native_usd_price = self.cfg.economics.native_token_usd_fallback
            self.simulator.set_live_gas_and_native_price(gas_price_gwei, native_usd_price)
        except Exception as e:
            logger.warning("Could not fetch live gas price, using fallback: %s", e)

        # Find discrepancies.
        discrepancies: List[Discrepancy] = self.analyzer.find_discrepancies(pool_states)

        if not discrepancies:
            logger.info("No discrepancies found this cycle.")
            return []

        # Simulate profitability for each discrepancy at each configured size.
        # NOTE: raw reserves/liquidity for slippage calc are not carried on
        # PoolState by design (see scanner.py) — a production wiring would
        # fetch them alongside price in fetch_v2_pool_state/fetch_v3_pool_state
        # and thread them through here. Flagged clearly so this gap is visible
        # rather than silently assumed away.
        all_results: List[SimulationResult] = []
        for disc in discrepancies:
            results = self.simulator.simulate_across_sizes(
                disc, self.cfg.economics.simulate_trade_sizes_usd,
            )
            all_results.extend(results)

        ranked = rank_opportunities(all_results, top_n=self.cfg.output.top_n_display)
        return ranked

    # ------------------------------------------------------------
    # Output
    # ------------------------------------------------------------

    def _ensure_csv_header(self) -> None:
        if self._csv_initialized:
            return
        path = self.cfg.output.csv_output_path
        write_header = not os.path.exists(path)
        if write_header:
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp_utc", "pair", "cheaper_pool", "cheaper_protocol",
                    "pricier_pool", "pricier_protocol", "trade_size_usd",
                    "gross_spread_pct", "net_profit_usd", "gas_cost_usd",
                    "buy_impact_pct", "sell_impact_pct", "notes",
                ])
        self._csv_initialized = True

    def log_results(self, results: List[SimulationResult]) -> None:
        if not results:
            print("No profitable opportunities this cycle.")
            return

        print(f"\n{'='*100}")
        print(f"Top {len(results)} opportunities @ {datetime.now(timezone.utc).isoformat()}")
        print(f"{'='*100}")
        for r in results:
            print(r.summary_line())
            if r.notes:
                for n in r.notes:
                    print(f"    note: {n}")
        print(f"{'='*100}\n")

        # Append to CSV for historical record-keeping.
        self._ensure_csv_header()
        with open(self.cfg.output.csv_output_path, "a", newline="") as f:
            writer = csv.writer(f)
            for r in results:
                writer.writerow([
                    datetime.now(timezone.utc).isoformat(),
                    r.discrepancy.pair_key,
                    r.discrepancy.cheaper_pool.pool_address,
                    r.discrepancy.cheaper_pool.protocol,
                    r.discrepancy.pricier_pool.pool_address,
                    r.discrepancy.pricier_pool.protocol,
                    r.trade_size_usd,
                    round(r.gross_spread_pct, 4),
                    round(r.net_profit_usd, 4),
                    round(r.gas_cost_usd, 4),
                    round(r.buy_price_impact_pct, 4),
                    round(r.sell_price_impact_pct, 4),
                    "; ".join(r.notes),
                ])

    # ------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------

    async def run(self) -> None:
        logger.info(
            "Starting scanner on chain=%s rpc=%s poll_interval=%.1fs",
            self.cfg.network.chain_name, self.cfg.network.rpc_url,
            self.cfg.scan.poll_interval_s,
        )

        await self.refresh_pool_list()
        last_discovery = time.time()

        while not self._shutdown.is_set():
            cycle_start = time.time()
            try:
                if time.time() - last_discovery > self.cfg.pools.discovery_interval_s:
                    await self.refresh_pool_list()
                    last_discovery = time.time()

                results = await self.run_scan_cycle()
                self.log_results(results)

            except Exception as e:
                # Catch-all at the top level so one bad cycle doesn't kill
                # the whole process — log it and keep going.
                logger.exception("Unhandled error in scan cycle: %s", e)

            elapsed = time.time() - cycle_start
            sleep_for = max(0.0, self.cfg.scan.poll_interval_s - elapsed)
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass  # normal case — just means it's time for the next cycle

        logger.info("Scanner shut down cleanly.")

    def request_shutdown(self) -> None:
        logger.info("Shutdown requested — finishing current cycle then stopping.")
        self._shutdown.set()


async def _main() -> None:
    cfg = load_config("config.yaml")
    setup_logging(cfg)

    app = ScannerApp(cfg)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.request_shutdown)
        except NotImplementedError:
            # add_signal_handler isn't available on all platforms (e.g. Windows)
            pass

    await app.run()


if __name__ == "__main__":
    asyncio.run(_main())
