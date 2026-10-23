"""Public-mempool lead-time monitor. Logging only.

Subscribes to a public websocket, classifies a few pending-transaction shapes,
and records the delay from first sight to the including block's timestamp.

This module does not sign, does not send, and does not resubmit anything it
sees. There is no private key. Flashbots Protect is queried with read calls
only: it hides transactions that were submitted to it, and it does not reveal
them. Snapshot votes are off-chain and are not in this feed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import websockets

from flasharb.liqmath import median
from flasharb.rpc import Rpc, RpcError

ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = ROOT / "analysis" / "mempool.json"

# Named so a reviewer can see the boundary. call() rejects these before any socket write.
FORBIDDEN_RPC_METHODS = frozenset(
    {
        "eth_sendRawTransaction",
        "eth_sendTransaction",
        "eth_sign",
        "eth_signTransaction",
        "eth_sendPrivateTransaction",
        "eth_sendBundle",
        "mev_sendBundle",
        "personal_sign",
        "personal_sendTransaction",
    }
)

MAINNET_WS = "wss://ethereum-rpc.publicnode.com"
MAINNET_HTTP = "https://ethereum-rpc.publicnode.com"
BASE_WS = "wss://base.publicnode.com"
BASE_HTTP = "https://base.publicnode.com"
ARBITRUM_WS = "wss://arbitrum.publicnode.com"
ARBITRUM_HTTP = "https://arbitrum.publicnode.com"
FLASHBOTS_HTTP = "https://rpc.flashbots.net"
FLASHBOTS_WS = "wss://rpc.flashbots.net"

MAINNET_BLOCK_SECONDS = 12

# Selectors pinned in test_mempool.py.
CAST_VOTE = "0x56781388"
CAST_VOTE_WITH_REASON = "0x7b3c71d3"
CAST_VOTE_WITH_REASON_AND_PARAMS = "0x5f398a14"
CAST_VOTE_BY_SIG = "0x3bccf4fd"
CAST_VOTE_WITH_REASON_AND_PARAMS_BY_SIG = "0x03420181"
GOVERNOR_EXECUTE = "0x2656227d"  # execute(address[],uint256[],bytes[],bytes32)
GOVERNOR_QUEUE = "0x160cbed7"
BRAVO_EXECUTE = "0xfe0d94c1"  # execute(uint256), only on a known Governor Bravo
BRAVO_QUEUE = "0xddf0b009"
AAVE_LIQUIDATION_CALL = "0x00a718a9"
MORPHO_LIQUIDATE = "0xd8eabcb8"
EULER_LIQUIDATE = "0xc1342574"
CHAINLINK_TRANSMIT = "0xb1dc65a4"
PYTH_UPDATE_PRICE_FEEDS = "0xef9e5e28"
ORACLE_POKE = "0x1dec8b17"
ERC20_TRANSFER = "0xa9059cbb"
ERC20_TRANSFER_FROM = "0x23b872dd"

VOTE_SELECTORS = {
    CAST_VOTE,
    CAST_VOTE_WITH_REASON,
    CAST_VOTE_WITH_REASON_AND_PARAMS,
    CAST_VOTE_BY_SIG,
    CAST_VOTE_WITH_REASON_AND_PARAMS_BY_SIG,
}
ORACLE_SELECTORS = {
    CHAINLINK_TRANSMIT: "chainlink_transmit",
    PYTH_UPDATE_PRICE_FEEDS: "pyth_update_price_feeds",
    ORACLE_POKE: "oracle_poke",
}

AAVE_POOLS = {
    "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2",  # Aave V3 Pool
    "0x7d2768de32b0b80b7a3454c06bdac94a69ddc7a9",  # Aave V2 LendingPool
}
MORPHO_BLUE = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"

# Governor Bravo proxies. execute(uint256) is too common to trust by selector alone.
KNOWN_GOVERNORS = {
    "0x408ed6354d4973f66138c91495f2f2fcbd8724c3": "Uniswap Governor",
    "0xc0da02939e1441f497fd74f78ce7decb17b66529": "Compound Governor Bravo",
    "0xec568fffba86c094cf06b22134b23074dfe2252c": "Aave Governance V2",
}

# Vaults that had a mainnet liquidation in this study's 90-day Euler sample,
# plus one vault from the open-book sample. Not the full Euler factory.
EULER_VAULTS = {
    "0x0d1b386187be8e96680bbddbf7bc05fc737f81b8",
    "0x3573a84bee11d49a1cbce2b291538de7a7dd81c6",
    "0x412d0e31790d77b6e7a7872a9fd6967b6e640229",
    "0x45c3b59d53e2e148aaa6a857521059676d5c0489",
    "0x481d4909d7ca2eb27c4975f08dce07dbef0d3fa7",
    "0x53afe3343f322c4189ab69e0d048efd154259419",
    "0x6f2598c99722428563f852f49002c6826316c751",
    "0x797dd80692c3b2dadabce8e30c07fde5307d48a9",
    "0x8aff4fe319c30475d27ec623d7d44bd5ecfe9616",
    "0xa28c23a459ff8773eb4dbe0e7250d93f79f1fe2b",
    "0xa992d3777282c44ee980e9b0ca9bd0c0e4f737af",
    "0xbc4b4ac47582c3e38ce5940b80da65401f4628f1",
    "0xc6137bc1378c2396051e06417704d31615f77cb9",
    "0xcbc9b61177444a793b85442d3a953b90f6170b7d",
    "0xd8b27cf359b7d15710a5be299af6e7bf904984c2",
    "0xe0a80d35bb6618cba260120b279d357978c42bce",
    "0xe1ce9af672f8854845e5474400b6ddc7ae458a10",
}

# Checked with eth_getCode before they were written down. Labels are the commonly
# published names of those addresses, not a fresh DAO-vote lookup.
TREASURIES = {
    "0x464c71f6c2f760dda6093dcb91c24c39e5d6e18c": "Aave Collector",
    "0x1a9c8182c09f50c8318d769245bea52c32be35bc": "Uniswap Timelock",
    "0xfe89cc7abb2c4183683ab71653c4cdc9b02d44b7": "ENS DAO wallet",
    "0x3e40d73eb977dc6a537af587d48316fee66e9c8c": "Lido Agent",
    "0x6d903f6003cca6255d85cca4d3b5e5146dc33925": "Compound Timelock",
    "0xbe8e3e3618f7474f8cb1d074a26affef007e98fb": "Maker Pause Proxy",
    "0xde0b295669a9fd93d5f28d9ec85e40f4cb697bae": "Ethereum Foundation",
}

# "Large" is a raw-unit cutoff, not a price we fetched during the run.
# 10_000 whole tokens for the stables, 5 whole tokens for WETH and wstETH, 0.5 WBTC, 5 native ETH.
LARGE_NATIVE_WEI = 5 * 10**18
KNOWN_TOKENS = {
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6, 10_000 * 10**6),
    "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6, 10_000 * 10**6),
    "0x6b175474e89094c44da98b954eedeac495271d0f": ("DAI", 18, 10_000 * 10**18),
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": ("WETH", 18, 5 * 10**18),
    "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0": ("wstETH", 18, 5 * 10**18),
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": ("WBTC", 8, 5 * 10**7),
}

LIQUIDATION_CATEGORIES = {"aave_liquidation", "morpho_liquidation", "euler_liquidation"}
USER_AGENT = "flashloan-arb-sim/0.1 (mempool log only; not a trader)"

SNAPSHOT_NOTE = (
    "Snapshot votes are signed messages stored off-chain at hub.snapshot.org. "
    "They are not Ethereum transactions, so a mempool subscription cannot see them."
)
FLASHBOTS_NOTE = (
    "Flashbots Protect hides a transaction that was submitted to it. It does not "
    "publish that transaction, and watching the Protect endpoint is not an earlier "
    "view of someone else's private order. The websocket redirects to an https host. "
    "txpool_status is refused. A pending-transaction filter on the HTTP front door is "
    "not stable across requests, which is what happens when filters are pinned to one "
    "backend. This monitor never calls eth_sendPrivateTransaction."
)
L2_NOTE = (
    "Base and Arbitrum take transactions on a private sequencer. There is no public "
    "pending pool to subscribe to. If the socket delivers new blocks and zero pending "
    "transactions, the observable lead time is zero."
)


class MempoolRefused(RuntimeError):
    pass


class MempoolClient:
    """Read-only JSON-RPC. Refuses every method that would sign or send."""

    def __init__(self, url: str, backups: list[str] | None = None):
        self.rpc = Rpc(url, timeout=20.0, backups=backups)

    def call(self, method: str, params: list[Any]) -> Any:
        if method in FORBIDDEN_RPC_METHODS or method.startswith("eth_send") or method.startswith("eth_sign") or method.startswith("personal_"):
            raise MempoolRefused(f"refusing {method}: this monitor does not sign or send")
        return self.rpc.request(method, params)


def _addr(value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith("0x"):
        return None
    return value.lower()


def _hex_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    return 0


def selector_of(tx: dict) -> str | None:
    data = tx.get("input") or tx.get("data") or "0x"
    if not isinstance(data, str) or len(data) < 10:
        return None
    return data[:10].lower()


def _word(data: str, index: int) -> int | None:
    # data includes the 0x and the 4-byte selector.
    start = 2 + 8 + index * 64
    end = start + 64
    if len(data) < end:
        return None
    return int(data[start:end], 16)


def _word_address(data: str, index: int) -> str | None:
    word = _word(data, index)
    if word is None:
        return None
    return "0x" + f"{word:040x}"[-40:]


def _token_size(token: str | None, amount: int | None) -> dict[str, Any]:
    if token is None or amount is None:
        return {"large": None, "size_known": False, "amount": amount, "token_symbol": None}
    meta = KNOWN_TOKENS.get(token)
    if meta is None:
        return {"large": None, "size_known": False, "amount": str(amount), "token_symbol": None}
    symbol, _decimals, cutoff = meta
    return {
        "large": amount >= cutoff,
        "size_known": True,
        "amount": str(amount),
        "token_symbol": symbol,
        "whole_tokens": amount / 10**_decimals,
    }


def classify_tx(tx: dict) -> dict[str, Any] | None:
    """Return a flag for a pending transaction, or None. Does not keep the raw input or signature."""
    to = _addr(tx.get("to"))
    sender = _addr(tx.get("from"))
    sel = selector_of(tx)
    data = tx.get("input") or tx.get("data") or "0x"
    if not isinstance(data, str):
        data = "0x"
    value = _hex_int(tx.get("value"))

    if sel in VOTE_SELECTORS:
        return {
            "category": "governance_vote",
            "detail": "governor_castVote",
            "to": to,
            "from": sender,
            "selector": sel,
            "proposal_id": str(_word(data, 0)) if _word(data, 0) is not None else None,
            "support": _word(data, 1) if sel == CAST_VOTE else None,
            "large": None,
            "size_known": False,
            "note": "Selector match. Not checked against a full governor directory. Snapshot votes are not in this feed.",
        }

    if sel in {GOVERNOR_EXECUTE, GOVERNOR_QUEUE}:
        return {
            "category": "governance_execute",
            "detail": "oz_governor_execute" if sel == GOVERNOR_EXECUTE else "oz_governor_queue",
            "to": to,
            "from": sender,
            "selector": sel,
            "named_governor": KNOWN_GOVERNORS.get(to or ""),
            "large": None,
            "size_known": False,
            "note": "OpenZeppelin Governor queue/execute signature. The calldata is not decoded or replayed.",
        }

    if sel in {BRAVO_EXECUTE, BRAVO_QUEUE} and to in KNOWN_GOVERNORS:
        return {
            "category": "governance_execute",
            "detail": "bravo_execute" if sel == BRAVO_EXECUTE else "bravo_queue",
            "to": to,
            "from": sender,
            "selector": sel,
            "named_governor": KNOWN_GOVERNORS[to],
            "proposal_id": str(_word(data, 0)) if _word(data, 0) is not None else None,
            "large": None,
            "size_known": False,
            "note": "execute(uint256) is only flagged when the target is one of the three named Governor Bravo proxies.",
        }

    if sel == AAVE_LIQUIDATION_CALL and to in AAVE_POOLS:
        debt = _word_address(data, 1)
        cover = _word(data, 3)
        size = _token_size(debt, cover)
        return {
            "category": "aave_liquidation",
            "detail": "aave_v3" if to == "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2" else "aave_v2",
            "to": to,
            "from": sender,
            "selector": sel,
            "debt_asset": debt,
            **size,
            "note": "debtToCover is large only when the debt asset is in the known-token list and clears that token's raw cutoff.",
        }

    if sel == MORPHO_LIQUIDATE and to == MORPHO_BLUE:
        return {
            "category": "morpho_liquidation",
            "detail": "morpho_blue",
            "to": to,
            "from": sender,
            "selector": sel,
            "large": None,
            "size_known": False,
            "note": "Morpho liquidate encodes shares and seized assets, not a USD size. The calldata is not replayed.",
        }

    if sel == EULER_LIQUIDATE and to in EULER_VAULTS:
        return {
            "category": "euler_liquidation",
            "detail": "euler_vault_seen_in_the_90_day_sample",
            "to": to,
            "from": sender,
            "selector": sel,
            "repay_assets": str(_word(data, 2)) if _word(data, 2) is not None else None,
            "large": None,
            "size_known": False,
            "note": "Target is a vault that appeared in this study. Repay assets are not converted to USD.",
        }

    if sel in ORACLE_SELECTORS:
        return {
            "category": "oracle_update",
            "detail": ORACLE_SELECTORS[sel],
            "to": to,
            "from": sender,
            "selector": sel,
            "large": None,
            "size_known": False,
            "note": "Oracle update selector. The report payload is not stored and not priced.",
        }

    treasury_hit = None
    amount = None
    token = None
    if sel == ERC20_TRANSFER and to in KNOWN_TOKENS:
        recipient = _word_address(data, 0)
        amount = _word(data, 1)
        token = to
        if sender in TREASURIES:
            treasury_hit = sender
        elif recipient in TREASURIES:
            treasury_hit = recipient
    elif sel == ERC20_TRANSFER_FROM and to in KNOWN_TOKENS:
        holder = _word_address(data, 0)
        recipient = _word_address(data, 1)
        amount = _word(data, 2)
        token = to
        for candidate in (sender, holder, recipient):
            if candidate in TREASURIES:
                treasury_hit = candidate
                break
    if treasury_hit is not None and token is not None:
        size = _token_size(token, amount)
        if size.get("large") is True:
            return {
                "category": "treasury_transfer",
                "detail": "erc20",
                "to": to,
                "from": sender,
                "selector": sel,
                "treasury": treasury_hit,
                "treasury_label": TREASURIES.get(treasury_hit),
                **size,
                "note": "ERC-20 transfer of a known token, to or from a listed treasury, at or above that token's raw cutoff.",
            }
    if value >= LARGE_NATIVE_WEI and (sender in TREASURIES or to in TREASURIES):
        treasury_hit = sender if sender in TREASURIES else to
        return {
            "category": "treasury_transfer",
            "detail": "native",
            "to": to,
            "from": sender,
            "selector": sel,
            "treasury": treasury_hit,
            "treasury_label": TREASURIES.get(treasury_hit or ""),
            "large": True,
            "size_known": True,
            "amount": str(value),
            "token_symbol": "ETH",
            "whole_tokens": value / 10**18,
            "note": "Native value of at least 5 ETH to or from a listed treasury.",
        }
    return None


def lead_seconds(first_seen: float, block_timestamp: int) -> float:
    """Inclusion block timestamp minus the wall-clock time the pending tx was first seen.

    Positive means the body was seen before the block timestamp. Negative means this
    process was late relative to that timestamp.
    """
    return float(block_timestamp) - float(first_seen)


class MonitorState:
    def __init__(self, sample_every: int = 80, sample_cap: int = 250):
        self.pending_objects = 0
        self.already_mined_on_arrival = 0
        self.heads = 0
        self.flagged: list[dict[str, Any]] = []
        self.feed_sample: list[dict[str, Any]] = []
        self.outstanding: dict[str, dict[str, Any]] = {}
        self.seen_hashes: set[str] = set()
        self.feed_seen = 0
        self.sample_every = sample_every
        self.sample_cap = sample_cap
        self.block_ts: dict[int, int] = {}
        self.poll_cursor = 0
        self.reconnects = 0
        self.errors: list[str] = []
        self.subs_ok = 0
        self.first_block: int | None = None
        self.last_block: int | None = None

    def note_error(self, message: str) -> None:
        if len(self.errors) < 20:
            self.errors.append(message[:240])

    def observe_head(self, head: dict) -> None:
        number = head.get("number")
        if not isinstance(number, str):
            return
        block = int(number, 16)
        self.heads += 1
        if self.first_block is None:
            self.first_block = block
        self.last_block = block
        timestamp = head.get("timestamp")
        if isinstance(timestamp, str):
            self.block_ts[block] = int(timestamp, 16)

    def observe_pending(self, tx: dict, now: float) -> None:
        if not isinstance(tx, dict):
            return
        tx_hash = tx.get("hash")
        if not isinstance(tx_hash, str):
            return
        if tx.get("blockNumber") not in (None, "0x", "0x0"):
            self.already_mined_on_arrival += 1
            return
        if tx_hash in self.seen_hashes:
            return
        self.seen_hashes.add(tx_hash)
        self.pending_objects += 1
        flag = classify_tx(tx)
        if flag is not None:
            record = _record(tx_hash, now, flag, "flagged")
            self.flagged.append(record)
            self.outstanding[tx_hash] = record
            return
        self.feed_seen += 1
        if self.feed_seen % self.sample_every == 0 and len(self.feed_sample) < self.sample_cap:
            record = _record(tx_hash, now, {"category": "feed_sample"}, "feed_sample")
            self.feed_sample.append(record)
            self.outstanding[tx_hash] = record

    def mark_included(self, tx_hash: str, block: int, block_timestamp: int) -> None:
        record = self.outstanding.get(tx_hash)
        if record is None or record.get("inclusion_block") is not None:
            return
        record["inclusion_block"] = block
        record["inclusion_timestamp"] = block_timestamp
        record["lead_seconds"] = lead_seconds(record["first_seen"], block_timestamp)
        record["seen_while_pending"] = True


def _record(tx_hash: str, now: float, flag: dict[str, Any], kind: str) -> dict[str, Any]:
    stored = {key: value for key, value in flag.items() if key not in {"input", "data", "r", "s", "v", "raw"}}
    stored.update(
        {
            "hash": tx_hash,
            "kind": kind,
            "first_seen": now,
            "inclusion_block": None,
            "inclusion_timestamp": None,
            "lead_seconds": None,
            "seen_while_pending": True,
        }
    )
    return stored


def poll_inclusion(state: MonitorState, client: MempoolClient, limit: int = 12) -> None:
    waiting = [tx_hash for tx_hash, record in state.outstanding.items() if record.get("inclusion_block") is None]
    if not waiting:
        return
    if state.poll_cursor >= len(waiting):
        state.poll_cursor = 0
    batch = waiting[state.poll_cursor : state.poll_cursor + limit]
    state.poll_cursor += limit
    for tx_hash in batch:
        try:
            tx = client.call("eth_getTransactionByHash", [tx_hash])
        except (RpcError, MempoolRefused) as exc:
            state.note_error(f"eth_getTransactionByHash: {exc}")
            return
        if not isinstance(tx, dict) or tx.get("blockNumber") in (None, "0x", "0x0"):
            continue
        block = int(tx["blockNumber"], 16)
        timestamp = state.block_ts.get(block)
        if timestamp is None:
            try:
                blk = client.call("eth_getBlockByNumber", [hex(block), False])
            except (RpcError, MempoolRefused) as exc:
                state.note_error(f"eth_getBlockByNumber: {exc}")
                return
            if not isinstance(blk, dict) or not isinstance(blk.get("timestamp"), str):
                continue
            timestamp = int(blk["timestamp"], 16)
            state.block_ts[block] = timestamp
        state.mark_included(tx_hash, block, timestamp)


def _lead_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    leads = [float(row["lead_seconds"]) for row in rows if isinstance(row.get("lead_seconds"), (int, float))]
    positive = [value for value in leads if value > 0]
    at_least_one_block = [value for value in leads if value >= MAINNET_BLOCK_SECONDS]
    return {
        "rows": len(rows),
        "included": len(leads),
        "not_included": len(rows) - len(leads),
        "median_lead_seconds": median(leads),
        "median_positive_lead_seconds": median(positive),
        "min_lead_seconds": min(leads) if leads else None,
        "max_lead_seconds": max(leads) if leads else None,
        "positive_lead": len(positive),
        "at_least_one_block": len(at_least_one_block),
    }


def assess(flagged: list[dict[str, Any]], feed_sample: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether a small player, looking only at this log, had a usable head start.

    A head start means the body was decoded while the transaction was still pending
    and the including block's timestamp was at least one mainnet block later.
    This function does not simulate a backrun and does not treat an oracle update
    as something this study traded on.
    """
    flagged_stats = _lead_stats(flagged)
    feed_stats = _lead_stats(feed_sample)
    by_category: dict[str, dict[str, Any]] = {}
    for row in flagged:
        bucket = by_category.setdefault(row["category"], [])
        bucket.append(row)
    categories = {name: _lead_stats(rows) for name, rows in sorted(by_category.items())}
    long_large_liquidations = [
        row
        for row in flagged
        if row.get("category") in LIQUIDATION_CATEGORIES
        and row.get("large") is True
        and isinstance(row.get("lead_seconds"), (int, float))
        and row["lead_seconds"] >= MAINNET_BLOCK_SECONDS
    ]
    any_block_of_lead = flagged_stats["at_least_one_block"] > 0
    if flagged_stats["included"] == 0:
        verdict = (
            "No flagged transaction was included in this window, so the flagged median "
            "lead time was not measured. A missing median is not a profit."
        )
    elif long_large_liquidations:
        verdict = (
            f"{len(long_large_liquidations)} large liquidation(s) were seen pending at least "
            f"{MAINNET_BLOCK_SECONDS}s before the block timestamp. That is a clock, not an edge: "
            "the same public mempool is visible to faster searchers, and this run did not copy "
            "or simulate those transactions."
        )
    elif any_block_of_lead:
        verdict = (
            "At least one flagged transaction was seen a full block before its timestamp, "
            "but none of those was a large priced liquidation. The lead time is not a trade "
            "this study can justify running."
        )
    else:
        median_text = "undefined" if flagged_stats["median_lead_seconds"] is None else f"{flagged_stats['median_lead_seconds']:.3f}s"
        verdict = (
            f"Flagged median lead time is {median_text}, under one mainnet block "
            f"({MAINNET_BLOCK_SECONDS}s). A small player on this public socket does not get a "
            "block of warning, and Flashbots Protect does not reveal the transactions it hid."
        )
    return {
        "flagged": flagged_stats,
        "by_category": categories,
        "feed_sample": feed_stats,
        "large_liquidations_with_a_block_of_lead": len(long_large_liquidations),
        "actionable_for_small_player": bool(long_large_liquidations),
        "worth_running": False,
        "verdict": verdict,
    }


def summarize_l2(chain: str, probe: dict[str, Any]) -> dict[str, Any]:
    pending = int(probe.get("pending_hash_notifications") or 0) + int(probe.get("pending_full_objects") or 0)
    heads = int(probe.get("new_heads") or 0)
    if pending == 0 and heads > 0:
        conclusion = (
            f"{chain} delivered {heads} new blocks and 0 pending transactions in "
            f"{probe.get('seconds')}s. That matches a private sequencer mempool. "
            "Observable lead time is 0."
        )
        lead = 0
    elif pending == 0 and heads == 0:
        conclusion = (
            f"{chain} delivered neither blocks nor pending transactions. The socket did not "
            "prove the mempool is empty; the probe failed to see the chain."
        )
        lead = None
    else:
        conclusion = (
            f"{chain} delivered {pending} pending notifications and {heads} blocks. "
            "That is not the empty public mempool this study expected. Lead time was not "
            "reconstructed from those notifications."
        )
        lead = None
    return {
        **probe,
        "observable_lead_seconds": lead,
        "conclusion": conclusion,
        "note": L2_NOTE,
    }


def _http_probe(url: str) -> dict[str, Any]:
    client = MempoolClient(url)
    out: dict[str, Any] = {"url": url}
    for method, params in (
        ("eth_chainId", []),
        ("txpool_status", []),
        ("eth_newPendingTransactionFilter", []),
    ):
        try:
            result = client.call(method, params)
            if method == "eth_newPendingTransactionFilter" and isinstance(result, str):
                try:
                    changes = client.call("eth_getFilterChanges", [result])
                    out[method] = {"filter": result, "changes": len(changes) if isinstance(changes, list) else changes}
                finally:
                    try:
                        client.call("eth_uninstallFilter", [result])
                    except (RpcError, MempoolRefused):
                        pass
            else:
                out[method] = result
        except (RpcError, MempoolRefused) as exc:
            out[method] = f"{type(exc).__name__}: {exc}"[:300]
    return out


async def _count_feed(url: str, seconds: float) -> dict[str, Any]:
    pending_hash = 0
    pending_full = 0
    heads = 0
    error = None
    accepted = 0
    try:
        async with websockets.connect(
            url,
            open_timeout=12,
            close_timeout=2,
            max_size=8_000_000,
            max_queue=2048,
            ping_interval=20,
            extra_headers={"User-Agent": USER_AGENT},
        ) as ws:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": ["newHeads"]}))
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "eth_subscribe", "params": ["newPendingTransactions"]}))
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "eth_subscribe", "params": ["newPendingTransactions", True]}))
            deadline = time.time() + seconds
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.2, deadline - time.time()))
                except asyncio.TimeoutError:
                    break
                msg = json.loads(raw)
                if "id" in msg:
                    if msg.get("result") and not msg.get("error"):
                        accepted += 1
                    continue
                result = (msg.get("params") or {}).get("result")
                if isinstance(result, str) and result.startswith("0x") and len(result) == 66:
                    pending_hash += 1
                elif isinstance(result, dict) and "parentHash" in result:
                    heads += 1
                elif isinstance(result, dict) and "hash" in result:
                    pending_full += 1
    except Exception as exc:  # noqa: BLE001 - the probe records the failure instead of inventing a zero
        error = f"{type(exc).__name__}: {exc}"[:300]
    return {
        "endpoint": url,
        "seconds": seconds,
        "subscription_acks": accepted,
        "pending_hash_notifications": pending_hash,
        "pending_full_objects": pending_full,
        "new_heads": heads,
        "error": error,
    }


async def check_flashbots() -> dict[str, Any]:
    """Read-only. Confirms the protect endpoint does not expose a pending feed."""
    ws_error = None
    ws_result = "no_pending_websocket"
    try:
        # A refused or redirected websocket is the observation. Nothing is sent.
        async with websockets.connect(
            FLASHBOTS_WS,
            open_timeout=8,
            close_timeout=2,
            extra_headers={"User-Agent": USER_AGENT},
        ) as ws:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}))
            await asyncio.wait_for(ws.recv(), timeout=8)
        ws_result = "connected"
    except Exception as exc:  # noqa: BLE001 - the failure is the measurement
        ws_error = f"{type(exc).__name__}: {exc}"[:300]
    http = await asyncio.to_thread(_http_probe, FLASHBOTS_HTTP)
    return {
        "http": FLASHBOTS_HTTP,
        "websocket": FLASHBOTS_WS,
        "websocket_result": ws_result,
        "websocket_error": ws_error,
        "http_probe": http,
        "sent_private_transaction": False,
        "note": FLASHBOTS_NOTE,
    }


def _public_report(row: dict[str, Any]) -> dict[str, Any]:
    hidden = {"input", "data", "r", "s", "v", "raw"}
    return {key: value for key, value in row.items() if key not in hidden}


def build_report(
    state: MonitorState,
    *,
    started: float,
    ended: float,
    seconds: float,
    l2: dict[str, Any],
    flashbots: dict[str, Any],
    endpoint: str,
) -> dict[str, Any]:
    assessment = assess(state.flagged, state.feed_sample)
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ended)),
        "mode": "log_only",
        "sends": False,
        "signs": False,
        "mainnet": {
            "endpoint": endpoint,
            "seconds": seconds,
            "started_unix": started,
            "ended_unix": ended,
            "pending_objects_with_null_block": state.pending_objects,
            "already_mined_on_arrival": state.already_mined_on_arrival,
            "new_heads": state.heads,
            "first_block": state.first_block,
            "last_block": state.last_block,
            "reconnects": state.reconnects,
            "errors": state.errors,
            "flagged_events": len(state.flagged),
            "assessment": assessment,
            "events": [_public_report(row) for row in state.flagged[:400]],
            "events_omitted": max(0, len(state.flagged) - 400),
            "feed_sample_rows": [
                {
                    "hash": row["hash"],
                    "first_seen": row["first_seen"],
                    "inclusion_block": row.get("inclusion_block"),
                    "inclusion_timestamp": row.get("inclusion_timestamp"),
                    "lead_seconds": row.get("lead_seconds"),
                }
                for row in state.feed_sample
            ],
        },
        "base": l2.get("base"),
        "arbitrum": l2.get("arbitrum"),
        "flashbots": flashbots,
        "snapshot": SNAPSHOT_NOTE,
        "methodology": {
            "lead_seconds": "including block timestamp minus wall-clock time the pending body was first seen. Positive is early. Negative is late.",
            "feed_sample": "Every 80th unclassified pending transaction, capped at 250, so the socket can be timed even when nothing is flagged.",
            "large": "USDC/USDT/DAI at least 10,000 whole tokens, WETH/wstETH at least 5, WBTC at least 0.5, native ETH at least 5. Not a live price.",
            "not_stored": "Raw input, r, s, and v are not written. Transactions are not resubmitted.",
            "l2": L2_NOTE,
            "flashbots": FLASHBOTS_NOTE,
            "snapshot": SNAPSHOT_NOTE,
        },
    }


async def _run_mainnet(state: MonitorState, url: str, http_url: str, seconds: float) -> None:
    client = MempoolClient(http_url)
    deadline = time.time() + seconds
    last_poll = 0.0
    while time.time() < deadline:
        try:
            async with websockets.connect(
                url,
                open_timeout=12,
                close_timeout=2,
                max_size=8_000_000,
                max_queue=4096,
                ping_interval=20,
                extra_headers={"User-Agent": USER_AGENT},
            ) as ws:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": ["newHeads"]}))
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "eth_subscribe", "params": ["newPendingTransactions", True]}))
                while time.time() < deadline:
                    timeout = min(5.0, deadline - time.time())
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.2, timeout))
                    except asyncio.TimeoutError:
                        raw = None
                    now = time.time()
                    if raw is not None:
                        msg = json.loads(raw)
                        if "id" in msg:
                            if msg.get("result") and not msg.get("error"):
                                state.subs_ok += 1
                            elif msg.get("error"):
                                state.note_error(f"subscribe: {msg['error']}")
                        else:
                            result = (msg.get("params") or {}).get("result")
                            if isinstance(result, dict) and "parentHash" in result:
                                state.observe_head(result)
                            elif isinstance(result, dict) and "hash" in result:
                                state.observe_pending(result, now)
                    if now - last_poll >= 5:
                        await asyncio.to_thread(poll_inclusion, state, client)
                        last_poll = now
                        print(
                            f"pending {state.pending_objects} flagged {len(state.flagged)} "
                            f"heads {state.heads} included "
                            f"{sum(1 for row in state.flagged if row.get('inclusion_block'))}",
                            flush=True,
                        )
        except Exception as exc:  # noqa: BLE001 - reconnect until the time budget ends
            state.reconnects += 1
            state.note_error(f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(1)


def _write(report: dict[str, Any]) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


async def run(seconds: float, l2_seconds: float, grace: float) -> dict[str, Any]:
    started = time.time()
    state = MonitorState()
    flashbots_task = check_flashbots()
    base_task = _count_feed(BASE_WS, l2_seconds)
    arb_task = _count_feed(ARBITRUM_WS, l2_seconds)
    mainnet_task = _run_mainnet(state, MAINNET_WS, MAINNET_HTTP, seconds)
    flashbots, base_feed, arb_feed, _main = await asyncio.gather(flashbots_task, base_task, arb_task, mainnet_task)
    if grace > 0 and any(row.get("inclusion_block") is None for row in state.outstanding.values()):
        client = MempoolClient(MAINNET_HTTP)
        grace_deadline = time.time() + grace
        while time.time() < grace_deadline and any(row.get("inclusion_block") is None for row in state.outstanding.values()):
            await asyncio.to_thread(poll_inclusion, state, client, 20)
            await asyncio.sleep(3)
    base_http = await asyncio.to_thread(_http_probe, BASE_HTTP)
    arb_http = await asyncio.to_thread(_http_probe, ARBITRUM_HTTP)
    ended = time.time()
    l2 = {
        "base": summarize_l2("Base", {**base_feed, "http_probe": base_http}),
        "arbitrum": summarize_l2("Arbitrum", {**arb_feed, "http_probe": arb_http}),
    }
    report = build_report(
        state,
        started=started,
        ended=ended,
        seconds=seconds,
        l2=l2,
        flashbots=flashbots,
        endpoint=MAINNET_WS,
    )
    _write(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Log public-mempool lead time. Does not send transactions.")
    parser.add_argument("--seconds", type=float, default=900.0, help="Mainnet subscribe duration.")
    parser.add_argument("--l2-seconds", type=float, default=45.0, help="Base and Arbitrum probe duration.")
    parser.add_argument("--grace", type=float, default=90.0, help="Seconds to keep resolving inclusion after the subscribe window.")
    args = parser.parse_args(argv)
    report = asyncio.run(run(args.seconds, args.l2_seconds, args.grace))
    flagged = report["mainnet"]["assessment"]["flagged"]
    print(
        f"flagged {report['mainnet']['flagged_events']} "
        f"median_lead {flagged['median_lead_seconds']} "
        f"actionable {report['mainnet']['assessment']['actionable_for_small_player']}",
        flush=True,
    )
    print(f"wrote {OUT_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
