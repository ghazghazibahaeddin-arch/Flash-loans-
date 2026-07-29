"""
config.py
Central configuration for the DEX arbitrage scanner.

All tunables live here so nothing is hardcoded deep in the logic.
Values can be overridden via environment variables or a local `config.yaml`
(see `load_config()` at the bottom).
"""

from __future__ import annotations

import os
import yaml
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional


# --------------------------------------------------------------------------
# Network / RPC
# --------------------------------------------------------------------------

@dataclass
class NetworkConfig:
    chain_name: str = "base"
    chain_id: int = 8453
    # Public/free RPC as a default fallback. In production, use a paid
    # provider (Alchemy/Infura/QuickNode) — public RPCs rate-limit hard.
    rpc_url: str = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
    ws_url: Optional[str] = os.getenv("BASE_WS_URL", None)
    subgraph_url: str = os.getenv(
        "BASE_SUBGRAPH_URL",
        # Placeholder — user should point this at a real Uniswap-on-Base
        # subgraph deployment (e.g. via The Graph's hosted/decentralized
        # network, or a self-hosted equivalent).
        "https://api.thegraph.com/subgraphs/name/PLACEHOLDER/base-uniswap",
    )
    request_timeout_s: int = 10
    max_retries: int = 5
    backoff_base_s: float = 0.5   # exponential backoff base
    backoff_max_s: float = 20.0


# --------------------------------------------------------------------------
# Pool discovery / monitoring
# --------------------------------------------------------------------------

@dataclass
class PoolConfig:
    # Which protocols to scan. Extend this list as adapters are added.
    protocols: List[str] = field(default_factory=lambda: ["uniswap_v2", "uniswap_v3"])

    # Minimum pool TVL (in USD, approximate) to bother scanning.
    # Filters out dead/illiquid pools that would just waste RPC calls.
    min_tvl_usd: float = 5_000.0

    # How many top pools per token pair to track (by liquidity).
    max_pools_per_pair: int = 5

    # Token pairs of interest. Empty list = discover all via subgraph.
    # Format: list of (token0_symbol, token1_symbol) — resolved to
    # addresses at runtime via the subgraph or a token list.
    watched_pairs: List[tuple] = field(default_factory=list)

    # Refresh interval for the subgraph-based discovery pass (seconds).
    # This is separate from the RPC price polling loop, which runs much
    # faster — discovery is "what pools exist", polling is "what's the
    # price right now".
    discovery_interval_s: int = 300


# --------------------------------------------------------------------------
# Scanning loop
# --------------------------------------------------------------------------

@dataclass
class ScanConfig:
    poll_interval_s: float = 3.0        # how often to re-fetch prices via RPC
    concurrent_rpc_calls: int = 20       # cap on simultaneous RPC requests
    price_staleness_s: float = 15.0      # discard quotes older than this


# --------------------------------------------------------------------------
# Economics: fees, slippage, gas
# --------------------------------------------------------------------------

@dataclass
class EconomicsConfig:
    # Flat-fee tiers by protocol/pool (in basis points). Real values should
    # be read from the pool contract where possible (V3 pools expose fee());
    # these are fallbacks/defaults.
    default_fee_bps: Dict[str, float] = field(default_factory=lambda: {
        "uniswap_v2": 30.0,     # 0.30%
        "uniswap_v3_500": 5.0,   # 0.05%
        "uniswap_v3_3000": 30.0,  # 0.30%
        "uniswap_v3_10000": 100.0,  # 1.00%
    })

    # Assumed trade size(s) to simulate, in USD notional. The scanner
    # checks profitability at each size since slippage is size-dependent.
    simulate_trade_sizes_usd: List[float] = field(
        default_factory=lambda: [500, 1_000, 5_000, 10_000, 25_000]
    )

    # Max acceptable price impact per leg before we discard the size as
    # unrealistic (protects against reporting phantom profit on sizes that
    # would move the pool price too much to actually fill).
    max_price_impact_pct: float = 3.0

    # Gas estimate assumptions (Base is cheap, but not free).
    assumed_gas_units_per_swap: int = 150_000
    assumed_swaps_per_arb: int = 2      # buy + sell
    gas_price_gwei_fallback: float = 0.05  # Base is typically sub-cent; fetched live when possible
    native_token_usd_fallback: float = 3_000.0  # ETH/USD fallback if price feed fails

    # Minimum net profit (after fees, slippage, and gas) to report, in USD.
    # This is a report-only threshold; it does not gate execution because
    # this tool does not execute trades.
    min_net_profit_usd: float = 5.0


# --------------------------------------------------------------------------
# Logging / output
# --------------------------------------------------------------------------

@dataclass
class OutputConfig:
    log_level: str = "INFO"
    log_file: str = "scanner.log"
    csv_output_path: str = "opportunities.csv"
    top_n_display: int = 10  # how many top opportunities to print per cycle


# --------------------------------------------------------------------------
# Root config object
# --------------------------------------------------------------------------

@dataclass
class Config:
    network: NetworkConfig = field(default_factory=NetworkConfig)
    pools: PoolConfig = field(default_factory=PoolConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    economics: EconomicsConfig = field(default_factory=EconomicsConfig)
    output: OutputConfig = field(default_factory=OutputConfig)


def load_config(path: str = "config.yaml") -> Config:
    """
    Load configuration, layering a local YAML file (if present) on top of
    the dataclass defaults above. Environment variables (read at dataclass
    construction time, e.g. BASE_RPC_URL) take precedence over YAML for
    secrets like RPC URLs.
    """
    cfg = Config()

    if os.path.exists(path):
        with open(path, "r") as f:
            raw = yaml.safe_load(f) or {}

        # Shallow-merge each section; this keeps the loader simple and
        # explicit rather than doing deep recursive merging magic.
        for section_name in ("network", "pools", "scan", "economics", "output"):
            section_overrides = raw.get(section_name, {})
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
    """Configure root logging based on OutputConfig."""
    logging.basicConfig(
        level=getattr(logging, cfg.output.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(cfg.output.log_file),
        ],
  )
