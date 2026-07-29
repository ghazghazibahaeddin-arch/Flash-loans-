Base DEX Arbitrage Scanner
A read-only monitoring tool that watches Uniswap V2/V3-style pools on
Base, detects cross-pool price discrepancies, and simulates whether they'd
be profitable after fees, slippage, and gas — without ever executing a trade.
What this does
Discovers pools via a subgraph (periodic, slower pass).
Fetches live reserves/prices via direct RPC calls (eth_calls only).
Detects pairs of pools quoting the same token pair at different prices.
Simulates net profit for each discrepancy at several trade sizes,
accounting for pool fees, price impact/slippage, and gas cost.
Ranks and logs the results to console + CSV.
What this does not do
Does not hold, request, or use a private key.
Does not build, sign, or broadcast any transaction.
Does not connect to any wallet.
Does not front-run, sandwich, or otherwise interact with the mempool.
If you want to act on an opportunity this tool surfaces, that's a separate,
deliberate step you take yourself (or a separate execution system you build
with its own risk controls — position sizing, revert handling, MEV
protection, key management). This tool stops at "here's what the numbers say."
Setup
Bash
Required configuration
network.rpc_url — use a real RPC provider (Alchemy, Infura,
QuickNode, etc.), not the free public Base RPC. Public RPCs rate-limit
aggressively and will make the poll loop unreliable.
network.subgraph_url — currently a placeholder in config.py.
Point it at a real Uniswap-on-Base subgraph deployment before running
discovery, or manually populate PoolConfig.watched_pairs / feed
ScannerApp._tracked_pools directly if you'd rather skip subgraph discovery.
Known limitations (read before trusting the output)
V3 slippage is approximated, not exact. The simulator treats pool
liquidity as locally constant rather than walking the tick bitmap, which
understates price impact for trades large enough to cross several ticks.
This is a monitoring tool, not an execution-grade slippage model — verify
independently before sizing anything meaningful off of it.
Reserves/liquidity for slippage math aren't yet threaded end-to-end.
scanner.py's PoolState intentionally stores only the derived price, not
raw reserves — simulator.py expects raw reserves/liquidity to be passed
in explicitly. Wiring this from fetch_v2_pool_state /
fetch_v3_pool_state through to simulate_discrepancy in main.py is the
first thing to finish before trusting slippage numbers; until then, results
are annotated with "(UNVERIFIED)" notes when this data is missing.
Native token USD price is a config fallback, not a live feed. Wire in
a real price source (a stable pool quote, an oracle, etc.) for accurate
gas-cost-in-USD figures.
Watch pool-leg matching when wiring in real reserves. analyzer.py
groups pools by an alphabetized pair key (e.g. always USDC/WETH, never
WETH/USDC), so a given Discrepancy's cheaper_pool/pricier_pool may
have token0/token1 in either order relative to that canonical key —
they aren't guaranteed to match the pool's own on-chain token0/token1
order. When you thread real reserves/liquidity into
simulator.simulate_discrepancy, fetch them per-pool (matching that
specific pool's actual token0/token1), not by assuming a fixed order
across all pools being compared. Getting this backwards silently
attributes the wrong reserve depth to the wrong leg — it won't crash, it'll
just quietly misprice slippage.
Opportunities disappear fast. By the time you see a logged
opportunity, the on-chain price may have already moved. Treat this as
informational, not as a live quote you can fill.
Architecture
Code
config.py     — all tunables (RPC URLs, thresholds, fee assumptions)
scanner.py    — read-only data fetching (RPC + subgraph)
analyzer.py   — cross-pool price comparison, finds candidate discrepancies
simulator.py  — pure-math profitability simulation (fees/slippage/gas)
main.py       — orchestration loop, ranking, CSV/console output
Each module is independently testable: analyzer.py and simulator.py take
plain data structures and do no I/O, so they can be unit tested without a
network connection or a live RPC endpoint.
