"""
scanner.py
Read-only data acquisition layer.

Responsibilities:
  - Discover pools via subgraph (slower, periodic).
  - Fetch live reserves/prices via direct RPC calls (fast, frequent).
  - Normalize V2 and V3 pool data into a common `PoolState` shape.

This module performs NO writes/transactions of any kind — every call here
is an `eth_call` (read) or an HTTP GET/POST to a subgraph endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

import aiohttp
from web3 import Web3
from web3.exceptions import ContractLogicError, TransactionNotFound

from config import Config
from models import TokenInfo, PoolState

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Minimal ABIs — only the read methods we actually call.
# Keeping ABIs minimal avoids pulling in write/owner methods we'll never use.
# --------------------------------------------------------------------------

ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol",
     "outputs": [{"name": "", "type": "string"}], "type": "function"},
]

UNISWAP_V2_PAIR_ABI = [
    {"constant": True, "inputs": [], "name": "getReserves",
     "outputs": [
         {"name": "_reserve0", "type": "uint112"},
         {"name": "_reserve1", "type": "uint112"},
         {"name": "_blockTimestampLast", "type": "uint32"},
     ], "type": "function"},
    {"constant": True, "inputs": [], "name": "token0",
     "outputs": [{"name": "", "type": "address"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "token1",
     "outputs": [{"name": "", "type": "address"}], "type": "function"},
]

UNISWAP_V3_POOL_ABI = [
    {"constant": True, "inputs": [], "name": "slot0",
     "outputs": [
         {"name": "sqrtPriceX96", "type": "uint160"},
         {"name": "tick", "type": "int24"},
         {"name": "observationIndex", "type": "uint16"},
         {"name": "observationCardinality", "type": "uint16"},
         {"name": "observationCardinalityNext", "type": "uint16"},
         {"name": "feeProtocol", "type": "uint8"},
         {"name": "unlocked", "type": "bool"},
     ], "type": "function"},
    {"constant": True, "inputs": [], "name": "liquidity",
     "outputs": [{"name": "", "type": "uint128"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "token0",
     "outputs": [{"name": "", "type": "address"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "token1",
     "outputs": [{"name": "", "type": "address"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "fee",
     "outputs": [{"name": "", "type": "uint24"}], "type": "function"},
]


class RetryableRPCError(Exception):
    """Raised when an RPC call fails in a way that's worth retrying."""


class Scanner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.w3 = Web3(Web3.HTTPProvider(
            cfg.network.rpc_url,
            request_kwargs={"timeout": cfg.network.request_timeout_s},
        ))
        self._token_cache: Dict[str, TokenInfo] = {}
        self._semaphore = asyncio.Semaphore(cfg.scan.concurrent_rpc_calls)

        if not self.w3.is_connected():
            logger.warning(
                "Web3 provider did not respond to a connectivity check at "
                "startup (%s). Will still attempt calls — some providers "
                "fail is_connected() but work fine for eth_call.",
                cfg.network.rpc_url,
            )

    # ----------------------------------------------------------------
    # Retry wrapper
    # ----------------------------------------------------------------

    async def _with_retries(self, coro_factory, description: str):
        """
        Run an async callable with exponential backoff retries.
        `coro_factory` must be a zero-arg callable returning a fresh
        coroutine each time (since a coroutine object can't be re-awaited).
        """
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.cfg.network.max_retries + 1):
            try:
                async with self._semaphore:
                    return await coro_factory()
            except (RetryableRPCError, asyncio.TimeoutError, aiohttp.ClientError) as e:
                last_exc = e
                delay = min(
                    self.cfg.network.backoff_base_s * (2 ** (attempt - 1)),
                    self.cfg.network.backoff_max_s,
                )
                logger.warning(
                    "Attempt %d/%d failed for %s: %s — retrying in %.1fs",
                    attempt, self.cfg.network.max_retries, description, e, delay,
                )
                await asyncio.sleep(delay)
            except (ContractLogicError, TransactionNotFound) as e:
                # Not retryable — the call itself is invalid (e.g. wrong
                # ABI for this address, or the pool doesn't exist).
                logger.error("Non-retryable error for %s: %s", description, e)
                raise

        logger.error(
            "Exhausted retries (%d) for %s. Last error: %s",
            self.cfg.network.max_retries, description, last_exc,
        )
        raise RetryableRPCError(f"Exhausted retries for {description}") from last_exc

    # ----------------------------------------------------------------
    # Token metadata (cached — decimals/symbol never change)
    # ----------------------------------------------------------------

    async def get_token_info(self, address: str) -> TokenInfo:
        address = Web3.to_checksum_address(address)
        if address in self._token_cache:
            return self._token_cache[address]

        contract = self.w3.eth.contract(address=address, abi=ERC20_ABI)

        async def _fetch():
            loop = asyncio.get_event_loop()
            try:
                decimals = await loop.run_in_executor(None, contract.functions.decimals().call)
                symbol = await loop.run_in_executor(None, contract.functions.symbol().call)
                return decimals, symbol
            except Exception as e:
                raise RetryableRPCError(str(e)) from e

        decimals, symbol = await self._with_retries(_fetch, f"token_info({address})")
        info = TokenInfo(address=address, symbol=symbol, decimals=decimals)
        self._token_cache[address] = info
        return info

    # ----------------------------------------------------------------
    # Uniswap V2-style pools
    # ----------------------------------------------------------------

    async def fetch_v2_pool_state(self, pool_address: str, fee_bps: float = 30.0) -> PoolState:
        pool_address = Web3.to_checksum_address(pool_address)
        contract = self.w3.eth.contract(address=pool_address, abi=UNISWAP_V2_PAIR_ABI)

        async def _fetch():
            loop = asyncio.get_event_loop()
            try:
                reserves = await loop.run_in_executor(None, contract.functions.getReserves().call)
                token0_addr = await loop.run_in_executor(None, contract.functions.token0().call)
                token1_addr = await loop.run_in_executor(None, contract.functions.token1().call)
                block_number = await loop.run_in_executor(None, lambda: self.w3.eth.block_number)
                return reserves, token0_addr, token1_addr, block_number
            except Exception as e:
                raise RetryableRPCError(str(e)) from e

        (reserve0, reserve1, _ts), token0_addr, token1_addr, block_number = await self._with_retries(
            _fetch, f"v2_pool_state({pool_address})"
        )

        token0 = await self.get_token_info(token0_addr)
        token1 = await self.get_token_info(token1_addr)

        # Normalize reserves by decimals before computing price.
        r0 = reserve0 / (10 ** token0.decimals)
        r1 = reserve1 / (10 ** token1.decimals)

        if r0 == 0:
            raise RetryableRPCError(f"Zero reserve0 for pool {pool_address} — likely drained or bad pool")

        price_t0_in_t1 = r1 / r0

        return PoolState(
            pool_address=pool_address,
            protocol="uniswap_v2",
            fee_bps=fee_bps,
            token0=token0,
            token1=token1,
            price_t0_in_t1=price_t0_in_t1,
            liquidity_depth_usd=None,  # filled in by discovery/subgraph pass if available
            fetched_at=time.time(),
            block_number=block_number,
        )

    # ----------------------------------------------------------------
    # Uniswap V3-style pools
    # ----------------------------------------------------------------

    async def fetch_v3_pool_state(self, pool_address: str) -> PoolState:
        pool_address = Web3.to_checksum_address(pool_address)
        contract = self.w3.eth.contract(address=pool_address, abi=UNISWAP_V3_POOL_ABI)

        async def _fetch():
            loop = asyncio.get_event_loop()
            try:
                slot0 = await loop.run_in_executor(None, contract.functions.slot0().call)
                token0_addr = await loop.run_in_executor(None, contract.functions.token0().call)
                token1_addr = await loop.run_in_executor(None, contract.functions.token1().call)
                fee = await loop.run_in_executor(None, contract.functions.fee().call)
                block_number = await loop.run_in_executor(None, lambda: self.w3.eth.block_number)
                return slot0, token0_addr, token1_addr, fee, block_number
            except Exception as e:
                raise RetryableRPCError(str(e)) from e

        slot0, token0_addr, token1_addr, fee, block_number = await self._with_retries(
            _fetch, f"v3_pool_state({pool_address})"
        )

        token0 = await self.get_token_info(token0_addr)
        token1 = await self.get_token_info(token1_addr)

        sqrt_price_x96 = slot0[0]
        # Standard V3 price formula: price = (sqrtPriceX96 / 2^96)^2,
        # then adjust for token decimal difference.
        raw_price = (sqrt_price_x96 / (2 ** 96)) ** 2
        decimal_adjustment = 10 ** (token0.decimals - token1.decimals)
        price_t0_in_t1 = raw_price * decimal_adjustment

        if price_t0_in_t1 <= 0:
            raise RetryableRPCError(f"Non-positive price computed for pool {pool_address}")

        return PoolState(
            pool_address=pool_address,
            protocol="uniswap_v3",
            fee_bps=fee / 100.0,  # V3 fee() returns hundredths of a bip
            token0=token0,
            token1=token1,
            price_t0_in_t1=price_t0_in_t1,
            liquidity_depth_usd=None,
            fetched_at=time.time(),
            block_number=block_number,
        )

    # ----------------------------------------------------------------
    # Batch fetch across many pools concurrently (bounded by semaphore)
    # ----------------------------------------------------------------

    async def fetch_all(self, pool_refs: List[Tuple[str, str, float]]) -> List[PoolState]:
        """
        pool_refs: list of (pool_address, protocol, fee_bps_or_ignored)
        Returns successfully-fetched PoolStates; logs and skips failures
        rather than letting one bad pool kill the whole batch.
        """
        async def _fetch_one(addr: str, protocol: str, fee_bps: float) -> Optional[PoolState]:
            try:
                if protocol == "uniswap_v2":
                    return await self.fetch_v2_pool_state(addr, fee_bps)
                elif protocol == "uniswap_v3":
                    return await self.fetch_v3_pool_state(addr)
                else:
                    logger.warning("Unknown protocol '%s' for pool %s — skipping", protocol, addr)
                    return None
            except Exception as e:
                logger.error("Failed to fetch pool %s (%s): %s", addr, protocol, e)
                return None

        results = await asyncio.gather(*[
            _fetch_one(addr, proto, fee) for addr, proto, fee in pool_refs
        ])
        return [r for r in results if r is not None]

    # ----------------------------------------------------------------
    # Subgraph-based discovery (periodic, not on the hot polling path)
    # ----------------------------------------------------------------

    async def discover_pools_via_subgraph(self, min_tvl_usd: float, limit: int = 100) -> List[Dict]:
        """
        Query a Uniswap-style subgraph for high-TVL pools on Base.
        Returns raw pool dicts with address/protocol/tvl — caller decides
        which ones to start polling via RPC.

        NOTE: `subgraph_url` in config.py is a placeholder. Point it at a
        real deployment (The Graph hosted service, decentralized network,
        or a self-hosted graph-node) before running this in anger.
        """
        query = """
        {
          pools(first: %d, orderBy: totalValueLockedUSD, orderDirection: desc,
                where: { totalValueLockedUSD_gt: "%f" }) {
            id
            feeTier
            totalValueLockedUSD
            token0 { id symbol decimals }
            token1 { id symbol decimals }
          }
        }
        """ % (limit, min_tvl_usd)

        async def _fetch():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        self.cfg.network.subgraph_url,
                        json={"query": query},
                        timeout=aiohttp.ClientTimeout(total=self.cfg.network.request_timeout_s),
                    ) as resp:
                        if resp.status != 200:
                            raise RetryableRPCError(f"Subgraph HTTP {resp.status}")
                        data = await resp.json()
                        if "errors" in data:
                            raise RetryableRPCError(f"Subgraph errors: {data['errors']}")
                        return data.get("data", {}).get("pools", [])
            except aiohttp.ClientError as e:
                raise RetryableRPCError(str(e)) from e

        try:
            return await self._with_retries(_fetch, "subgraph_discovery")
        except RetryableRPCError:
            logger.error(
                "Subgraph discovery failed after retries. Falling back to "
                "whatever pools are already configured in watched_pairs. "
                "Check that network.subgraph_url points at a real endpoint."
            )
            return []
