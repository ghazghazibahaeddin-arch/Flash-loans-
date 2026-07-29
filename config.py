"""
config.py
Central configuration for the multi-strategy DeFi opportunity engine.

All tunables live here so nothing is hardcoded deep in the scanning/
scoring logic. Values can be overridden via environment variables or a
local `config.yaml` (see `load_config()` at the bottom).

This engine is READ-ONLY: it discovers, scores, and reports opportunities.
It never holds a key, builds a transaction, or executes a trade. Acting on
anything it reports is a separate, deliberate, manual step taken by the
operator.
"""

from __future__ import annotations

import os
import yaml
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional


# --------------------------------------------------------------------------
# Per-chain network config
# --------------------------------------------------------------------------

@dataclass
class ChainConfig:
    name: str
    chain_id: Optional[int]           # None for non-EVM chains like Solana
    rpc_url: str
    ws_url: Optional[str] = None
    subgraph_url: Optional[str] = None
    enabled: bool = True
    # Cheap chains are the whole point here — gas/fee assumptions differ
    # a lot by chain, so each one carries its own fallback.
    native_symbol: str = "ETH"
    native_token_usd_fallback: float = 3_000.0
    gas_price_fallback_gwei: float = 0.05


@dataclass
class NetworkConfig:
    chains: Dict[str, ChainConfig] = field(default_factory=lambda: {
        "base": ChainConfig(
            name="base",
            chain_id=8453,
            rpc_url=os.getenv("BASE_RPC_URL", "https://mainnet.base.org"),
            subgraph_url=os.getenv(
                "BASE_SUBGRAPH_URL",
                "https://api.thegraph.com/subgraphs/name/PLACEHOLDER/base-uniswap",
            ),
            native_symbol="ETH",
            native_token_usd_fallback=3_000.0,
            gas_price_fallback_gwei=0.05,
        ),
        "arbitrum": ChainConfig(
            name="arbitrum",
            chain_id=42161,
            rpc_url=os.getenv("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc"),
            subgraph_url=os.getenv("ARBITRUM_SUBGRAPH_URL", "https://api.thegraph.com/subgraphs/name/PLACEHOLDER/arbitrum-uniswap"),
            native_symbol="ETH",
            native_token_usd_fallback=3_000.0,
            gas_price_fallback_gwei=0.1,
        ),
        "solana": ChainConfig(
            name="solana",
            chain_id=None,
            rpc_url=os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
            subgraph_url=None,   # Solana indexers typically aren't subgraph-based
            native_symbol="SOL",
            native_token_usd_fallback=150.0,
            gas_price_fallback_gwei=0.0,  # priced differently (per-signature lamports); handled in scanners.py
        ),
    })
    request_timeout_s: int = 10
    max_retries: int = 5
    backoff_base_s: float = 0.5
    backoff_max_s: float = 20.0

    def enabled_chains(self) -> List[ChainConfig]:
        return [c for c in self.chains.values() if c.enabled]


# --------------------------------------------------------------------------
# Which scanners run, and how often
# --------------------------------------------------------------------------

@dataclass
class ScannerToggle:
    enabled: bool = True
    poll_interval_s: float = 30.0


@dataclass
class ScannersConfig:
    cross_dex_arbitrage: ScannerToggle = field(default_factory=lambda: ScannerToggle(True, 3.0))
    liquidation: ScannerToggle = field(default_factory=lambda: ScannerToggle(True, 10.0))
    restaking: ScannerToggle = field(default_factory=lambda: ScannerToggle(True, 300.0))
    bridge_watchtower: ScannerToggle = field(default_factory=lambda: ScannerToggle(True, 300.0))
    liquidity_events: ScannerToggle = field(default_factory=lambda: ScannerToggle(True, 30.0))
    new_pools: ScannerToggle = field(default_factory=lambda: ScannerToggle(True, 60.0))
    smart_money: ScannerToggle = field(default_factory=lambda: ScannerToggle(False, 60.0))  # needs a wallet watchlist to be useful
    incentive_programs: ScannerToggle = field(default_factory=lambda: ScannerToggle(True, 3600.0))
    oracle_deviation: ScannerToggle = field(default_factory=lambda: ScannerToggle(True, 15.0))
    governance_events: ScannerToggle = field(default_factory=lambda: ScannerToggle(True, 600.0))

    # Wallets to watch for the smart-money scanner. Empty by default —
    # this scanner is a no-op until the operator supplies addresses.
    smart_money_watchlist: List[str] = field(default_factory=list)

    # Minimum pool TVL (USD) before we bother tracking it at all.
    min_tvl_usd: float = 5_000.0
    max_pools_per_pair: int = 5
    discovery_interval_s: int = 300
    price_staleness_s: float = 15.0
    concurrent_rpc_calls: int = 20


# --------------------------------------------------------------------------
# Economics: fees, slippage, gas, profit thresholds
# --------------------------------------------------------------------------

@dataclass
class EconomicsConfig:
    default_fee_bps: Dict[str, float] = field(default_factory=lambda: {
        "uniswap_v2": 30.0,
        "uniswap_v3_500": 5.0,
        "uniswap_v3_3000": 30.0,
        "uniswap_v3_10000": 100.0,
    })

    simulate_trade_sizes_usd: List[float] = field(
        default_factory=lambda: [500, 1_000, 5_000, 10_000, 25_000]
    )

    max_price_impact_pct: float = 3.0
    assumed_gas_units_per_swap: int = 150_000
    assumed_swaps_per_arb: int = 2

    # Minimum net profit (after fees, slippage, and gas) to REPORT, in USD.
    # This is a report-only threshold — it does not gate execution because
    # this tool does not execute anything. It just keeps the report from
    # being flooded with noise-level "opportunities".
    min_net_profit_usd: float = 5.0


# --------------------------------------------------------------------------
# Risk filtering — deliberately cautious, not strict.
# Nothing here silently deletes an opportunity from the report; it downgrades
# confidence and attaches a reason. Only near-meaningless data (e.g. a price
# with no liquidity context at all) gets excluded outright, because
# reporting it would be misleading rather than merely risky.
# --------------------------------------------------------------------------

@dataclass
class RiskConfig:
    stale_data_penalty: float = 30.0          # confidence points deducted if data is borderline stale
    thin_liquidity_usd_threshold: float = 20_000.0
    thin_liquidity_penalty: float = 20.0
    unverified_field_penalty: float = 15.0     # per missing/unverified numeric field
    new_pool_age_s_threshold: float = 3600.0   # pools younger than this get a caution note
    new_pool_penalty: float = 10.0
    high_value_review_threshold_usd: float = 25_000.0  # above this, always flagged for manual review regardless of score


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

@dataclass
class OutputConfig:
    log_level: str = "INFO"
    log_file: str = "engine.log"
    csv_output_path: str = "opportunities.csv"
    json_output_path: str = "opportunities.json"
    top_n_display: int = 15


# --------------------------------------------------------------------------
# Root config object
# --------------------------------------------------------------------------

@dataclass
class Config:
    network: NetworkConfig = field(default_factory=NetworkConfig)
    scanners: ScannersConfig = field(default_factory=ScannersConfig)
    economics: EconomicsConfig = field(default_factory=EconomicsConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    output: OutputConfig = field(default_factory=OutputConfig)


def load_config(path: str = "config.yaml") -> Config:
    """
    Load configuration, layering a local YAML file (if present) on top of
    the dataclass defaults above. Env vars (read at dataclass construction
    time, e.g. BASE_RPC_URL) take precedence over YAML for things like
    RPC URLs. Unknown keys are logged and ignored rather than crashing —
    a typo in config.yaml shouldn't take down the whole engine.
    """
    cfg = Config()

    if os.path.exists(path):
        with open(path, "r") as f:
            raw = yaml.safe_load(f) or {}

        for section_name in ("network", "scanners", "economics", "risk", "output"):
            section_overrides = raw.get(section_name, {})
            if not isinstance(section_overrides, dict):
                continue
            section_obj = getattr(cfg, section_name)
            for key, value in section_overrides.items():
                if hasattr(section_obj, key):
                    setattr(section_obj, key, value)
                else:
                    logging.getLogger(__name__).warning(
                        "Unknown config key '%s' in section '%s' — ignoring",
                        key, section_name,
                    )

    return cfg


def setup_logging(cfg: Config) -> None:
    logging.basicConfig(
        level=getattr(logging, cfg.output.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(cfg.output.log_file),
        ],
    )
