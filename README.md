# flashloan-arb-sim

Simulation only. This repository does not broadcast to Ethereum, Base, or Arbitrum unless you deliberately arm live mode, which is not recommended. The default `ARB_MODE` is `simulate`: the bot builds a private-orderflow payload and stops. Fork mode delivers that transaction to a loopback Anvil and nowhere else.

The on-chain piece is an owner-only, non-upgradeable executor. It borrows with an Aave V3 flash loan or a Balancer V2 flash loan (fee zero), swaps across two pools, and reverts unless the balance covers the premium and `minProfit`. Gas is not visible on-chain. The bot folds a gas cushion into the off-chain profit check.

The question this repo actually answers is whether a naive two-pool search still finds money in places searchers supposedly ignore: Base and Arbitrum, and long-tail pools with low TVL. The historical scan below says no, not above gas, in the windows that were measured.

## Architecture

```mermaid
flowchart LR
  subgraph offchain [Off-chain scanner]
    Discover[Discover pools]
    Quote[Quote and size]
    Compete[Replay blocks]
    Submit[PrivateSubmitter]
  end
  subgraph chain [Fork or local Anvil]
    Exec[FlashLoanArbExecutor]
    Aave[Aave V3]
    Bal[Balancer V2 vault]
    Pools[V2 / V3 / Solidly / Camelot]
  end
  Discover --> Quote
  Quote --> Compete
  Quote -->|eth_call| Exec
  Submit -->|fork mode, loopback only| Exec
  Exec --> Aave
  Exec --> Bal
  Exec --> Pools
  Aave --> Exec
  Bal --> Exec
```

`FlashLoanArbExecutor` is not a proxy, has no `delegatecall`, and has no owner-transfer function. `owner` is immutable. `nonReentrant` runs across the flash callback. A concentrated-liquidity callback clears the active pool before it pays the token, so a malicious token cannot be paid twice. The contract trusts the owner to pick pools. It does not trust pools or tokens.

Routes are explicit. `DexKind` covers Uniswap V2-style pairs (Uniswap V2, Sushi, BaseSwap), Uniswap V3, Solidly volatile and stable (Aerodrome / Velodrome), Camelot V2 volatile, and Camelot stable. Slipstream is quoted off-chain only when QuoterV2 agrees; the executor treats a passing Slipstream pool as a Uniswap V3 swap because that pool uses the same callback.

## Niches this bot looks at

Head WETH/USDC books on mainnet are a control, not the target. The configured set is:

- Base: Aerodrome volatile and stable, Aerodrome Slipstream, Uniswap V3, Sushi, BaseSwap. Flash from Aave V3 (`0xA238…d1c5`) or Balancer.
- Arbitrum: Camelot V2 (volatile and stable), Sushi, Uniswap V3. Native USDC books are mostly dust; USDC.e is the quote that actually has inventory. Flash from Aave V3 (`0x794a…14aD`) or Balancer.
- A pool is long-tail when both legs sit between $2k and $100k. Cross-tier means one long-tail leg and one head leg (at least $500k). Under $2k is dust and is not counted. V3 “TVL” is active liquidity at the current tick, not deposited TVL.

## Private order flow

`PrivateSubmitter` builds the payload a searcher would post, then refuses to post it unless live mode is armed.

| Chain | Payload | What it does not do |
| --- | --- | --- |
| Mainnet | `eth_sendPrivateTransaction` to `https://rpc.flashbots.net` (Flashbots Protect) | It does not reserve state. Another bundle can still land first. |
| Base | `eth_sendRawTransaction` to `https://mainnet-sequencer.base.org` | Base has no public mempool and no Flashbots bundles. This is a latency race. |
| Arbitrum | `eth_sendRawTransaction` to `https://arb1.arbitrum.io/rpc` | Ordering is the sequencer. Priority is Timeboost, not a private bundle. |

Modes:

- `simulate` (default). Payload is built and returned. Nothing is sent.
- `fork`. The payload is recorded, then `send_raw` is called only if `FORK_RPC_URL` is `127.0.0.1`, `localhost`, or `::1`. The Flashbots and sequencer URLs are not contacted.
- `live`. Requires `ARB_MODE=live` and `ARB_ALLOW_LIVE=i-understand-live-trading-is-not-recommended`. Not recommended. There is no key in this repo. Do not put one in a commit.

## Run it from a phone

GitHub Codespaces uses `.devcontainer`. The image installs Foundry and, on create, a Python venv plus `forge build`. Forwarded port 8545 is for Anvil.

```bash
make test          # forge unit tests + pytest. No RPC, no broadcast.
make test-fork     # pinned forks. Skips a chain when the node no longer has that block.
make analyze       # read-only competition scan. Writes analysis/latest.json.
make liquidations  # read-only liquidation study. Writes analysis/liquidations.json.
make mempool       # read-only mempool log, about 20 minutes. Writes analysis/mempool.json.
make anvil-demo    # local fork only. Submits to 127.0.0.1.
```

Copy `.env.example` to `.env` if you want different RPCs. Public endpoints are the default. Never commit `.env` or a funded key. The Anvil demo uses Anvil’s published default account, and only after the URL is confirmed to be loopback.

Fork tests are pinned in `config/fork-blocks.json` (mainnet `26252880`, Base `52969900`, Arbitrum `517431000`). Override with `FORK_BLOCK_MAINNET`, `FORK_BLOCK_BASE`, `FORK_BLOCK_ARBITRUM`. CI uses the same public RPCs and exits 0 when a node returns a prune, archive, or rate-limit error. An assertion failure still fails the job. Unit tests always run.

## Historical competition

The scanner replays `Sync` and Uniswap V3 `Swap` logs. An opportunity opens when one transaction moves a two-pool cycle from not profitable to profitable after DEX fees, the flash premium, and a gas cushion. The cushion is `gas_units * max(base fee at the two ends of the window) * WETH price`, plus $0.03 on Base and $0.08 on Arbitrum for L1 calldata / Timeboost. It is captured only when a later transaction in the same block or the next block touches both pools and the profit goes to zero or below. If the spread dies without such a transaction, the result is ambiguous, not “a bot got it.” If it is still profitable at the end of the next block, it is uncaptured and counted once. Edges above 25% are dropped as suspect (tax tokens, broken decimals). Spreads already open at the first block are not counted until they reset; anything still profitable at the last block is reported as a standing residual. There were none.

Quotes are checked against the chain at the start block before a pool can enter the headline. Camelot stable is the Solidly invariant after a fee over 1e5, not constant product. Solidly and Camelot pools that miss `getAmountOut` by more than 0.5% are dropped. Uniswap V3 quotes more than 1% above QuoterV2 are dropped. A within-tick V3 quote that is below QuoterV2 is kept as a lower bound. Slipstream pools QuoterV2 cannot price are dropped. Replay that does not match the chain at the end block drops that pool (one pool on Base, three on Arbitrum, none on mainnet).

WETH was about $2,400 from the deepest WETH/stable pool. USDC, USDT, DAI, USDbC, and USDC.e are pegged at $1 in this table. Balancer’s flash fee is zero when the vault holds at least the $25k flash cap (WETH and USDC on Base; WETH, USDC.e, USDT, WBTC, DAI, and RDNT on Arbitrum; WETH, USDC, DAI, USDT, WBTC, AAVE, and LDO on mainnet). Every other token pays Aave’s 5 bps.

These windows are short. They are not an annualised PnL.

| Chain | Blocks | Tier | Opportunities | Captured same block | Captured next block | Ambiguous | Uncaptured | Uncaptured net USD after gas | Notes |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| base | 52970045–52971845 | long_tail | 0 | 0 | 0 | 0 | 0 | 0.00 | base fee 0.0050 gwei; WETH $2407.22; suspect excluded 0; replay mismatches 1; max spot edge 380.46 bps; fee-only evals 11750 (cleared fees and premium, not the gas cushion) |
| base | 52970045–52971845 | cross_tier | 0 | 0 | 0 | 0 | 0 | 0.00 |  |
| base | 52970045–52971845 | head | 2 | 0 | 0 | 2 | 0 | 0.00 |  |
| base | 52970045–52971845 | mid | 1 | 0 | 0 | 1 | 0 | 0.00 |  |
| arbitrum | 517443647–517447647 | long_tail | 0 | 0 | 0 | 0 | 0 | 0.00 | base fee 0.0200 gwei; WETH $2398.11; suspect excluded 0; replay mismatches 3; max spot edge 113.45 bps; fee-only evals 290 (cleared fees and premium, not the gas cushion) |
| arbitrum | 517443647–517447647 | cross_tier | 0 | 0 | 0 | 0 | 0 | 0.00 |  |
| arbitrum | 517443647–517447647 | head | 0 | 0 | 0 | 0 | 0 | 0.00 |  |
| mainnet | 26253231–26253311 | long_tail | 0 | 0 | 0 | 0 | 0 | 0.00 | base fee 0.0440 gwei; WETH $2399.47; suspect excluded 0; replay mismatches 0; max spot edge 527.18 bps; fee-only evals 70 (cleared fees and premium, not the gas cushion) |
| mainnet | 26253231–26253311 | cross_tier | 0 | 0 | 0 | 0 | 0 | 0.00 |  |
| mainnet | 26253231–26253311 | head | 14 | 4 | 2 | 8 | 0 | 0.00 |  |
| mainnet | 26253231–26253311 | mid | 2 | 0 | 0 | 2 | 0 | 0.00 |  |

Source: `analysis/latest.json`, produced by `python -m flasharb.analyze` against public RPCs. The first Base window (900 blocks) and the first Arbitrum window (2000 blocks) were empty after gas, so each was widened once.

### What the numbers say

Uncaptured net profit after gas is **$0.00** on every chain and every tier, including long-tail and cross-tier. Nothing that cleared fees, the flash premium, and the gas cushion was still profitable at the end of the next block. There was also no standing residual at the end of any window.

On mainnet, in 80 blocks, the head set produced 14 dislocations that did clear gas. Six were closed by a later transaction that touched both pools: four in the same block, two in the next. Those six had about **$14.49** of model profit at the moment they opened (that is the size of the dislocation, not cash a searcher banked). The other eight were ambiguous: the spread was gone one block later, but no single later transaction touched both pools, so this replay will not call them captures. Two mid-tier dislocations were also ambiguous. Zero stayed open.

Base, across 1800 blocks, produced three dislocations that cleared gas (two head, one mid). All three were ambiguous. None were an atomic two-pool backrun that this replay could see, and none were left behind. Arbitrum, across 4000 blocks, produced zero dislocations that cleared gas at any tier.

The scanner was not blind to smaller gaps. A fee-only evaluation is a moment where the spot gap cleared both DEX fees and the flash premium and the sized trade was profitable before the gas cushion. Base recorded 11750 of those, Arbitrum 290, mainnet 70. The widest spot gap was 380 bps on Base, 113 bps on Arbitrum, and 527 bps on mainnet, all under the 25% suspect cutoff. On a thin pool a 3% gap is often only a few cents after impact. The gas cushions in these windows were about $0.04 on Base ($0.03 L1 plus a 0.005 gwei base fee), about $0.11 on Arbitrum ($0.08 Timeboost/L1 plus a 0.02 gwei base fee), and about $0.05 on mainnet (0.044 gwei, no extra L1 line). The long-tail gaps did not clear that hurdle.

That is why a naive bot loses. The dislocations large enough to pay mainnet gas in the head books were taken in the same block or the next one. The long-tail gaps that were still sitting there were smaller than gas, which is a different way of saying they were not a trade. Private order flow does not change the arithmetic: Flashbots Protect hides a mainnet transaction from the public mempool and does not reserve the state, Base submission is a race to the sequencer, and Arbitrum priority is Timeboost. A bot that sends the public way, or the private way, still arrives after the searcher who already closed the spread, and still pays gas on a residual that does not cover gas.

Two limits belong next to the table. Log replay at transaction boundaries misses reverted attempts and misses bundles that never hit the public log stream as two separate swaps. A V3 quote that stops at the current tick is a lower bound, so some real V3 profit is missing from the count rather than invented. Eighty mainnet blocks and a few thousand L2 blocks are a sample, not a career.

## Crafted fork tests

Unit tests do not need a node. Fork tests fork the pinned block, dump WETH into the thinner pool, and require the executor to keep a profit and then to revert on a round trip, a non-owner, and an impossible `amountOutMin`. Measured profits from those tests, when the pinned state was still available:

| Fork | Route | Profit |
| --- | --- | --- |
| Mainnet 26252880 | Aave, Uni V2 / Sushi after dumping a quarter of Sushi’s WETH | 0.0275 WETH |
| Mainnet 26252880 | Aave, Uni V3 0.05% / Sushi, same dump | 0.0276 WETH |
| Mainnet 26252880 | Balancer, Uni V2 / Sushi, smaller dump | 0.0215 WETH |
| Base 52969900 | Aave, Aerodrome volatile / BaseSwap, dump a quarter of BaseSwap WETH | 0.0266 WETH |
| Base 52969900 | Balancer, same pair, smaller dump | 0.0207 WETH |
| Arbitrum 517431000 | Aave, Sushi / Camelot, dump a quarter of Camelot’s WETH against USDC.e | 0.0274 WETH |

The Balancer rows are smaller because the test stops at the first size that works, not because the zero premium made the trade worse. These are manufactured prices on a fork. They show the executor repays and keeps a surplus. They are not the historical result above.

## Flash-loan liquidations

Same rules as the arb scan. Nothing in this path broadcasts. Morpho and Euler numbers below are from their public APIs for 25 July 2026 17:59 UTC through 23 October 2026 17:59 UTC. Delay, gas, and the open-book quotes are archive `eth_call`s and receipts. Where a field could not be read, it is left out of the sum.

### What was liquidated

| Source | Events | Priced | Positive paper bonus USD | Window |
| --- | ---: | ---: | ---: | --- |
| Morpho Blue, mainnet + Base | 4406 | 4080 | 1,351,043.21 | 90 days |
| Euler v2, mainnet + Base + Arbitrum | 99 | 99 | 506.12 | 90 days |
| Moonwell, Base | 142 | 132 repaid, bonus not priced | repaid 315.80, bonus unknown | 2 days, blocks 52908363–52994763 |
| Seamless, Base | 0 |  |  | same 2 days, scan completed |
| Fluid Vault T1 liquidator on Base `0x8f5455245e74a2ea60a32751654a57b1aae4be99` | 0 logs |  |  | 14 days, blocks 52389963–52994763 |
| Silo, one Arbitrum contract `0x20deb7395f88a18af2eb51a0c3053f15c1781933` | 0 |  |  | 2 days, blocks 516917727–517608927 |

Morpho’s paper bonus is seized collateral times that day’s Morpho USD price, minus repaid debt at the loan token’s daily price. It is not cash. The median positive Morpho bonus is $5.06. Nineteen events over $10,000, all on mainnet, are $1,174,891 of the $1,351,043. The largest is PT-reUSD-10DEC2026 / USDC in block 25829882: the daily mark says $346,905.66 of bonus on a $13.01 million repay. The oracle price in that block says the bonus was 340,440.82 USDC. Those two marks agree within about two percent, so the size is not a decimal bug. Selling thirteen million dollars of that principal token was not quoted. 326 Morpho events have no daily price. 267 of those are Base AFAKE.

Euler’s $506.12 is `bonusUsd` from `https://v3.euler.finance/v3/liquidations`, valued from their historical price snapshots.

Moonwell is every `LiquidateBorrow` on the 21 markets `getAllMarkets` returned, one market per request. The repaid USD uses the underlying token and the Morpho price when one exists. `seizeTokens` is an mToken amount, so the bonus is unknown. One address, `0x16e41af5e034113802d06064fbeb020cc3d47b19`, sent 130 of the 142. Seamless, Fluid, and that Silo contract were scanned cleanly and were empty in those short windows. That is not a 90-day claim, and it is not a claim about every Silo or every Fluid vault.

### Delay

Delay is the number of blocks from the first end-of-block state that was already liquidatable to the liquidation block. A position that was healthy at the end of the previous block has delay 0: it became liquidatable in the liquidation block, which an end-of-block call cannot see inside. Morpho health uses `oracle.price`, Taylor-compounded interest, and virtual shares. Euler uses `vault.checkLiquidation`. The lookback stops at 7 days. Past that, the row is censored, not given a fake precise delay.

Morpho delay was measured on 72 events, not on 4,406. The sample is up to 18 events per chain with a daily-mark bonus of at least $100, plus a seeded random fill to 36. Euler is every event in the 90 days.

| Set | Sample | Readable delay | Delay 0 | Median blocks | Median seconds | Censored at 7 days | Unreadable |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Morpho Blue | 72 | 58 | 47 | 0 | 0 | 8 | 6 |
| Euler v2 | 99 | 63 | 3 | 339 | 1,908 | 26 | 10 |

Of the positive paper bonus inside the Morpho sample, $1,056,411 was delay 0. $2,108 had a readable delay above 5 blocks. The money in that sample was taken in the block it opened.

Euler’s 339-block median is only the 63 readable rows. Twenty-six more were still liquidatable a week earlier, so the true Euler median is longer. The whole Euler paper bonus for the quarter is $506.12.

Gas is the winner’s receipt: `gasUsed * effectiveGasPrice`, plus Base `l1Fee` when the receipt has it, times the ETH price on that day. Median gas used is high because those transactions include the winner’s route, not a bare liquidate.

| Chain | Receipts in the sample | Median gas used | Median gas USD |
| --- | ---: | ---: | ---: |
| mainnet | 92 | 727,366.5 | 0.194 |
| base | 73 | 962,956 | 0.018 |
| arbitrum | 6 | 950,864 | 0.036 |

### Who got them

4,505 Morpho and Euler liquidations, 384 liquidators. An address with at least 10 liquidations in the window is marked as a repeat liquidator. There are 81 of those. The busiest five did 31.1% of the count and 0.107% of the positive paper USD. The count leaderboard is the small, repeated Base flow. The USD is 19 mainnet events, done by seven addresses. `0x51a453d677396F62fbb1dff9925a205fa96fAB5e` did 8 of those 19.

A searcher who arrives N blocks after the winner of a finished liquidation is late. Profit after the winner is $0 in this study. Remainders were not reconstructed.

The other figure is a searcher who is N blocks slower than the moment the position opens, and the winner was slower than that. On the delay sample, with a zero flash fee only when Morpho or Balancer’s latest balance covers the repay, and with no slippage subtracted:

| Slower than the open by | Rows with positive net before slippage | That net, USD | Of which is bad-debt marks |
| --- | ---: | ---: | ---: |
| 1 block | 59 | 53,366.01 | 31,744.48 |
| 5 blocks | 56 | 34,020.29 | 31,744.48 |

The bad-debt slice is mostly sNUSD / USDC positions that were still liquidatable seven days later. That paper bonus was not a race that a faster bot won. It was collateral the market did not clear. The one large non-bad-debt row a 1-block delay still reaches is PT-reUSD / USDC with delay 2 and $19,335.18 before slippage, on about $734k of collateral. A 5-block delay misses it. After that, the sitting non-bad-debt marks are small: stkWELL on Base sat 7,458 blocks (about 4.1 hours) for a $1,710.70 daily-mark bonus, and ynRWAx sat 21,232 blocks for $396.85. Neither exit was quoted.

### What is sitting there now

Morpho’s indexer currently reports 1,758 mainnet positions and 191 Base positions with health factor at or under 1, and 611 positions with health factor from 1 to 1.02. Health factor here is collateral value times LLTV, divided by debt. 1,435 of the underwater rows are under $20 of debt. 38 print more than $2 million of debt. 290 between $50 and $2 million have collateral marks under 30% of the debt. Those are bad debt or a broken USD mark. They were not treated as size.

Forty-seven underwater positions have debt between $50 and $2 million and collateral marks of at least 30% of the debt. The 18 largest were read on chain. All 18 were unhealthy on chain. Fourteen are ynETHx / msETH and have no Uniswap v2, Uniswap v3, or Solidly pool between the two tokens in the configured factories. USDC / LCAP on Base had no loan-token price, so no net. Three wbCOIN / USDC positions on Base are bad debt: Aerodrome’s `getAmountOut` on the seized collateral returned 4,478 raw USDC units, about $0.0045, against repays of $366 to $422. Flash fee was zero. Gas, using the sample’s median gas and the current base fee, was about $0.038. Net was about -$366 to -$419.

There is no fifth realistic opportunity, and there is no first. Near-threshold positions are not liquidatable. The closest large one is USDe / DAI on mainnet, health factor 1.00008, $116,126 of debt.

Euler’s open-book check covered 48 debtors on 12 vaults whose total borrows were at least $10,000, not the whole book. Nine vault rows on four sub-accounts came back liquidatable. Three of those addresses differ only in the last nibble and sit on one vault, with liquidation collateral at 0.808 and 0.834 of liquidation liability, from a snapshot 5,708 seconds old. The fourth account has two zero-collateral rows and one row at 0.933, from a snapshot 832 seconds old. Liabilities are Euler’s unit of account, not dollars. No swap was quoted, so none of that is counted as profit.

### Fork

`test/fork/LiquidationFork.t.sol` forks Base transaction `0x26fc826ded7f7ba937b65b770bfb015cfa11ec5b79c26d461fbca1c59686d4d9` (AERO / USDC, block 52987569) before that transaction, flash-borrows 200 USDC from Morpho Blue at zero fee, repays the borrower’s full borrow shares, and sells the seized AERO on Aerodrome volatile pool `0x6cDcB1C4A4d1C3C6d054b27AC5B77e89eAFb971d`. On `https://base.gateway.tenderly.co` the contract kept 11,894,036 USDC units, which is $11.894036, before gas. At the previous block the same position was still healthy: borrowed 94,194,651 against a max of 94,385,616. A bot one block slower than this liquidation gets none of the $11.89. A drpc replay of the same fork was rate-limited and produced no second number.

### Verdict

This is not worth running as a speed game. On the Morpho sample that contains the paper bonus, the median delay is 0 blocks and about $1.06 million of that sample’s positive marks were already gone by the next block. What sits longer is bad debt, a token with no pool, or a few hundred to a couple of thousand dollars of daily-mark bonus that was not shown to survive a swap. Euler’s entire 90-day paper bonus is $506.12. The smaller lenders were either empty in the short log window or, on Moonwell, already worked by one address for a few hundred dollars of repaid debt over two days. The fork shows the mechanic works and that the winner’s slot on that AERO position paid $11.89 before gas.

## Public mempool lead time

Logging only. `python -m flasharb.mempool` subscribes to pending transactions and writes `analysis/mempool.json`. It does not sign, send, or resubmit. The raw calldata and the signature (`r`, `s`, `v`) are not stored. Snapshot votes are signed messages on hub.snapshot.org. They never enter this feed.

### Mainnet

`wss://ethereum-rpc.publicnode.com`, 1,200 seconds, ending 2026-10-23T18:51:58Z. The socket returned **14,984** pending bodies. All 14,984 had a null block number, so they were seen before inclusion. None were already mined on arrival. The same socket delivered **99** new heads, blocks 26257128–26257226, with no reconnect and no RPC error.

**Flagged events: 0.** The median lead time for a flagged event was not measured, because there was nothing to measure.

The classifier was looking for Governor `castVote` and `execute`, Aave `liquidationCall` on the V2 and V3 pools, Morpho Blue `liquidate`, Euler `liquidate` on the 17 vaults from the liquidation study, Chainlink `transmit`, Pyth `updatePriceFeeds`, `poke(bytes32[][])`, and large transfers of USDC, USDT, DAI, WETH, wstETH, WBTC, or native ETH involving seven listed treasuries. Large means at least 10,000 whole stablecoins, 5 WETH or wstETH, 0.5 WBTC, or 5 ETH. That cutoff is a raw unit count, not a price fetched during the run. None of those shapes appeared in the 14,984 bodies.

The socket is not silent. Every 80th unclassified transaction was timed, 187 rows, 183 of them included before the grace period ended. Lead time is the including block’s timestamp minus the wall clock when the body was first seen. Positive means this process was early.

| Feed sample | Value |
| --- | ---: |
| Included | 183 of 187 |
| Median lead, seconds | 6.44 |
| Median of the positive leads, seconds | 6.85 |
| Minimum, seconds | -5.02 |
| Maximum, seconds | 178.03 |
| At least one 12-second block | 24 |
| At least 60 seconds | 7 |
| Negative (seen after the block timestamp) | 15 |

The unrounded median is 6.438694000244141 seconds, in `analysis/mempool.json`. The seven waits over a minute were not decoded as liquidations, votes, oracle updates, or treasury transfers. They are slow unclassified transactions, not a book of skipped liquidations.

### Base and Arbitrum

Sixty seconds each, concurrent with the mainnet watch, on the publicnode websockets. Blocks arrived. Pending notifications did not.

| Chain | New blocks | Pending notifications | HTTP pending filter | txpool_status | Observable lead |
| --- | ---: | ---: | ---: | --- | ---: |
| Base | 30 | 0 | 0 changes | pending 0, queued 0 | 0 seconds |
| Arbitrum | 217 | 0 | 0 changes | method handler crashed | 0 seconds |

Base produced a block about every 2 seconds and Arbitrum about every 0.28 seconds, and neither subscription returned a pending hash or a full pending body. That is a private sequencer. A newcomer does not get a public head start on either chain. The Arbitrum `txpool_status` crash is a node error, not a mempool we were allowed to read.

### Flashbots Protect

Protect hides a transaction that was submitted to it. It does not publish that transaction for someone else to read early.

`wss://rpc.flashbots.net` is not a websocket. The client was redirected to `https://protectrpc.flashbots.net`. `eth_chainId` on the HTTP endpoint returned 1. `txpool_status` returned HTTP 403. During the run, creating a pending filter and reading it back returned `filter not found`. Five more attempts after the run created a filter every time: four follow-ups were `filter not found`, and one returned 0 changes. No hash from those calls was stored. Nothing was sent, signed, or submitted.

### Is the lead time actionable?

No. Across 14,984 public pending bodies and 99 mainnet blocks, the events this watcher was built to notice did not appear, so there is no flagged lead time to act on. The head start that does exist, on ordinary unclassified transactions, has a median of 6.44 seconds. That is under one mainnet block, and it is the same public mempool every other reader of this socket can see. Base and Arbitrum show no pending transactions at all. Protect removes transactions from this picture instead of revealing them. A small player simulating on this feed does not get a trade.

Two limits sit next to the zero. Twenty minutes can miss a governance vote or a large public liquidation that happens to fall outside the window. A liquidation sent to a searcher’s own contract does not match `liquidationCall` or Morpho `liquidate`, and an Euler vault outside the 17 listed ones is not flagged. Both of those absences make the flagged count a lower bound, not a claim that no liquidation was mined.

## Layout

- `src/FlashLoanArbExecutor.sol` — borrow, swap, repay, `minProfit`
- `src/FlashLoanLiquidator.sol` — Morpho or Balancer flash loan, Morpho Blue liquidate, Solidly swap
- `src/libraries/V2Math.sol`, `SafeTransfer.sol` — constant-product math and transfers that bubble the inner revert
- `test/` — unit tests and mocks. `test/fork/Fork.t.sol` — pinned arb forks. `test/fork/LiquidationFork.t.sol` — one historical liquidation
- `bot/flasharb/` — quotes, sizing, competition accounting, liquidation study, private submitter
- `config/chains/` — tokens, factories, TVL bands, gas units
- `config/liquidation-fork.json` — the AERO liquidation the fork test replays
- `analysis/latest.json` — the arb table above
- `analysis/liquidations.json` — the liquidation study
- `analysis/mempool.json` — the public-mempool lead-time log
- `bot/flasharb/mempool.py` — pending-transaction classifier. It does not sign or send.

## What I learned

