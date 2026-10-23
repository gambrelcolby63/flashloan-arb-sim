"""Read-only liquidation study.

Pulls Morpho Blue and Euler liquidation history, reconstructs delay where an
archive node still has state, and quotes currently underwater positions.
It does not sign and does not broadcast.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eth_abi import decode
from eth_utils import keccak, to_checksum_address

from flasharb.abiutil import calldata, checksum
from flasharb.discover import CONFIG, load_chain, make_rpc
from flasharb.liqmath import (
    REPEAT_LIQUIDATOR_MIN,
    bonus_usd,
    concentration,
    is_unhealthy,
    median,
    position_borrowed,
    size_summary,
    slowly_captured,
    take_size,
    usd,
)
from flasharb.math_v2 import get_amount_out
from flasharb.rpc import Rpc, RpcError

ROOT = CONFIG.parent
CACHE = ROOT / "analysis" / ".cache"
OUT_PATH = ROOT / "analysis" / "liquidations.json"
MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
MORPHO_API = "https://api.morpho.org/graphql"
EULER_API = "https://v3.euler.finance/v3/liquidations"
WINDOW_DAYS = 90
# Searcher who hears about the position this many blocks after it first becomes liquidatable.
SLOW_BLOCKS = (1, 5)
# Delay search stops at a week. Longer than that is reported as censored, not as a precise delay.
LOOKBACK_DAYS = 7
DELAY_PER_CHAIN = 36
DELAY_LARGE_BONUS_USD = 100.0
DELAY_LARGE_CAP = 18
OPEN_BORROW_MIN_USD = 50.0
NEAR_HF = 1.02
QUOTE_CAP = 18
DUST_USD = 20.0
HEAD_SUPPLY_USD = 500_000.0
LONG_TAIL_SUPPLY_USD = 100_000.0
# Collaterals treated as head only when the market's supply USD is missing.
HEAD_SYMBOLS = {
    "WETH", "WETH.e", "USDC", "USDC.e", "USDT", "USD₮0", "DAI", "WBTC", "wstETH",
    "cbETH", "cbBTC", "weETH", "USDS", "USDe", "PYUSD", "RLUSD", "EURC", "GHO",
    "crvUSD", "sUSDS", "USDbC",
}

TOPIC_LIQUIDATE_BORROW = "0x" + keccak(text="LiquidateBorrow(address,address,uint256,address,uint256)").hex()
TOPIC_AAVE_LIQUIDATION = "0x" + keccak(
    text="LiquidationCall(address,address,address,uint256,uint256,address,bool)"
).hex()
TOPIC_SILO_LIQUIDATION = "0x" + keccak(
    text="LiquidationCall(address,address,address,uint256,uint256,bool)"
).hex()
TOPIC_EULER_LIQUIDATE = "0x" + keccak(text="Liquidate(address,address,address,uint256,uint256)").hex()

ARCHIVE = {
    "mainnet": [
        "https://eth.drpc.org",
        "https://mainnet.gateway.tenderly.co",
        "https://eth-mainnet.public.blastapi.io",
        "https://ethereum-rpc.publicnode.com",
    ],
    "base": [
        "https://base.drpc.org",
        "https://mainnet.base.org",
        "https://base.gateway.tenderly.co",
        "https://base.publicnode.com",
    ],
    "arbitrum": [
        "https://arbitrum.drpc.org",
        "https://arb1.arbitrum.io/rpc",
        "https://arbitrum.publicnode.com",
    ],
}

CHAIN_NAME = {1: "mainnet", 8453: "base", 42161: "arbitrum"}
WETH = {
    1: "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    8453: "0x4200000000000000000000000000000000000006",
    42161: "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
}
BLOCK_SECONDS = {"mainnet": 12, "base": 2, "arbitrum": 0.25}

MOONWELL_COMPTROLLER = "0xfBb21d0380beE3312B33c4353c8936a0F13EF26C"
SEAMLESS_POOL = "0x524AdCcEeDe99f8fb0D654704F48Ec2f0744B837"
FLUID_LIQUIDATOR_BASE = "0x8f5455245e74a2ea60a32751654a57b1aae4be99"
# Contract that emitted LiquidationCall in Arbitrum tx 0x25fe1e66... Not claimed to be the only Silo entry point.
SILO_LIQUIDATION_ARB = "0x20deb7395f88a18af2eb51a0c3053f15c1781933"


def _now() -> int:
    return int(time.time())


def _http_json(url: str, payload: dict | None = None, timeout: float = 60.0) -> Any:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "flashloan-liq-sim/0.1 (simulation; not a trader)",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def gql(query: str, variables: dict | None = None) -> dict:
    body = _http_json(MORPHO_API, {"query": query, "variables": variables or {}})
    if body.get("errors"):
        raise RuntimeError(json.dumps(body["errors"])[:500])
    return body["data"]


def _int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return int(value)


def _cache_load(name: str) -> Any | None:
    path = CACHE / name
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _cache_save(name: str, payload: Any) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / name).write_text(json.dumps(payload))


def _cache_fresh(cached: Any, start_ts: int) -> bool:
    if not isinstance(cached, dict) or not cached.get("events") and not cached.get("prices"):
        return False
    return abs(int(cached.get("start_ts") or 0) - start_ts) < 6 * 3600


def fetch_morpho_liquidations(start_ts: int) -> list[dict]:
    cached = _cache_load("morpho-liquidations.json")
    if _cache_fresh(cached, start_ts):
        return cached["events"]
    query = """
    query($first: Int!, $skip: Int!, $ts: Int!) {
      marketTransactions(
        first: $first
        skip: $skip
        orderBy: Timestamp
        orderDirection: Desc
        where: { type_in: [Liquidation], chainId_in: [1, 8453], timestamp_gte: $ts }
      ) {
        pageInfo { countTotal }
        items {
          txHash
          blockNumber
          timestamp
          logIndex
          chain { id }
          user { address }
          market {
            marketId
            lltv
            collateralAsset { symbol address decimals }
            loanAsset { symbol address decimals }
          }
          data {
            ... on MarketTransactionLiquidationData {
              liquidator
              repaidAssets
              repaidShares
              seizedAssets
              badDebtAssets
              badDebtShares
            }
          }
        }
      }
    }
    """
    events: list[dict] = []
    skip = 0
    total = None
    while True:
        data = gql(query, {"first": 100, "skip": skip, "ts": start_ts})
        page = data["marketTransactions"]
        total = page["pageInfo"]["countTotal"]
        items = page["items"] or []
        if not items:
            break
        for item in items:
            liq = item["data"]
            market = item["market"]
            events.append({
                "protocol": "morpho-blue",
                "chain_id": item["chain"]["id"],
                "chain": CHAIN_NAME[item["chain"]["id"]],
                "tx": item["txHash"],
                "block": _int(item["blockNumber"]),
                "timestamp": _int(item["timestamp"]),
                "log_index": item["logIndex"],
                "borrower": item["user"]["address"],
                "liquidator": liq["liquidator"],
                "market_id": market["marketId"],
                "lltv": _int(market["lltv"]),
                "collateral_symbol": market["collateralAsset"]["symbol"] if market["collateralAsset"] else None,
                "collateral_token": market["collateralAsset"]["address"] if market["collateralAsset"] else None,
                "collateral_decimals": int(market["collateralAsset"]["decimals"]) if market["collateralAsset"] else None,
                "loan_symbol": market["loanAsset"]["symbol"],
                "loan_token": market["loanAsset"]["address"],
                "loan_decimals": int(market["loanAsset"]["decimals"]),
                "repaid_assets": _int(liq["repaidAssets"]),
                "repaid_shares": _int(liq["repaidShares"]),
                "seized_assets": _int(liq["seizedAssets"]),
                "bad_debt_assets": _int(liq["badDebtAssets"]),
                "bad_debt_shares": _int(liq["badDebtShares"]),
                "bonus_source": "daily_morpho_price",
            })
        skip += len(items)
        print(f"morpho liquidations {skip}/{total} unique_so_far pending", flush=True)
        if total is not None and skip >= total:
            break
        if len(items) == 0:
            break
    dedup = {}
    for event in events:
        dedup[(event["chain_id"], event["tx"], event["log_index"])] = event
    events = list(dedup.values())
    _cache_save("morpho-liquidations.json", {"start_ts": start_ts, "events": events, "reported_total": total})
    return events


def fetch_euler_liquidations(start_ts: int, end_ts: int) -> list[dict]:
    cached = _cache_load("euler-liquidations.json")
    if _cache_fresh(cached, start_ts):
        return cached["events"]
    events: list[dict] = []
    for chain_id in (1, 8453, 42161):
        offset = 0
        total = None
        while True:
            url = (
                f"{EULER_API}?chainId={chain_id}&from={start_ts}&to={end_ts}"
                f"&limit=100&offset={offset}"
            )
            try:
                payload = _http_json(url)
            except urllib.error.HTTPError as exc:
                print(f"euler chain {chain_id} HTTP {exc.code}", flush=True)
                break
            rows = payload.get("data") or []
            total = (payload.get("meta") or {}).get("total")
            if not rows:
                break
            for row in rows:
                valuation = row.get("valuation") or {}
                events.append({
                    "protocol": "euler-v2",
                    "chain_id": chain_id,
                    "chain": CHAIN_NAME[chain_id],
                    "tx": row["txHash"],
                    "block": _int(row["blockNumber"]),
                    "timestamp": int(datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()),
                    "log_index": None,
                    "borrower": row["violator"],
                    "liquidator": row["liquidator"],
                    "market_id": row["vault"],
                    "vault": row["vault"],
                    "collateral_vault": row["collateral"],
                    "lltv": None,
                    "collateral_symbol": None,
                    "collateral_token": row["collateralAsset"],
                    "collateral_decimals": row["collateralAssetDecimals"],
                    "loan_symbol": None,
                    "loan_token": row["debtAsset"],
                    "loan_decimals": row["debtAssetDecimals"],
                    "repaid_assets": _int(row["repayAssets"]),
                    "seized_assets": _int(row["collateralAssets"]),
                    "bad_debt_assets": 0,
                    "bonus_usd": row.get("bonusUsd"),
                    "repaid_usd": row.get("repayAssetsUsd"),
                    "seized_usd": row.get("collateralAssetsUsd"),
                    "bonus_source": (valuation.get("source") or "euler-api"),
                    "valuation_status": valuation.get("status"),
                })
            offset += len(rows)
            print(f"euler chain {chain_id} {offset}/{total}", flush=True)
            if total is not None and offset >= total:
                break
    _cache_save("euler-liquidations.json", {"start_ts": start_ts, "events": events})
    return events


def fetch_daily_prices(events: list[dict], start_ts: int, end_ts: int) -> dict[tuple[int, str], dict]:
    wanted: set[tuple[int, str]] = set()
    for event in events:
        for key in ("collateral_token", "loan_token"):
            token = event.get(key)
            if token:
                wanted.add((event["chain_id"], to_checksum_address(token)))
    for chain_id, token in WETH.items():
        wanted.add((chain_id, to_checksum_address(token)))
    query = """
    query($address: String!, $chainId: Int!, $start: Int!, $end: Int!) {
      assetByAddress(address: $address, chainId: $chainId) {
        symbol
        price { usd }
        historicalPriceUsd(options: {startTimestamp: $start, endTimestamp: $end, interval: DAY}) { x y }
      }
    }
    """
    prices: dict[tuple[int, str], dict] = {}
    for index, (chain_id, token) in enumerate(sorted(wanted)):
        try:
            data = gql(query, {"address": token, "chainId": chain_id, "start": start_ts, "end": end_ts})
        except Exception as exc:  # noqa: BLE001 - one asset should not abort the study
            prices[(chain_id, token.lower())] = {"error": str(exc)[:180]}
            continue
        asset = data.get("assetByAddress")
        if not asset:
            prices[(chain_id, token.lower())] = {"symbol": None, "current": None, "series": []}
        else:
            prices[(chain_id, token.lower())] = {
                "symbol": asset.get("symbol"),
                "current": (asset.get("price") or {}).get("usd"),
                "series": asset.get("historicalPriceUsd") or [],
            }
        if index % 25 == 0:
            print(f"prices {index}/{len(wanted)}", flush=True)
        time.sleep(0.05)
    _cache_save(
        "prices.json",
        {"start_ts": start_ts, "prices": {f"{c}|{t}": v for (c, t), v in prices.items()}},
    )
    return prices


def _restore_price_keys(raw: dict) -> dict[tuple[int, str], dict]:
    out = {}
    for key, value in raw["prices"].items():
        chain_s, token = key.split("|", 1)
        out[(int(chain_s), token.lower())] = value
    return out


def load_prices(events: list[dict], start_ts: int, end_ts: int) -> dict[tuple[int, str], dict]:
    cached = _cache_load("prices.json")
    if isinstance(cached, dict) and cached.get("prices") and abs(int(cached.get("start_ts") or 0) - start_ts) < 6 * 3600:
        return _restore_price_keys(cached)
    return fetch_daily_prices(events, start_ts, end_ts)


def price_at(prices: dict, chain_id: int, token: str | None, ts: int) -> float | None:
    if not token:
        return None
    row = prices.get((chain_id, token.lower()))
    if not row or row.get("error"):
        return None
    series = row.get("series") or []
    best = None
    for point in series:
        if int(point["x"]) <= ts:
            best = point
    if best is not None and best.get("y") is not None:
        return float(best["y"])
    if series and series[0].get("y") is not None:
        return float(series[0]["y"])
    current = row.get("current")
    return float(current) if current is not None else None


def apply_morpho_marks(events: list[dict], prices: dict) -> None:
    for event in events:
        if event["protocol"] != "morpho-blue":
            symbol = price_symbol(prices, event["chain_id"], event.get("collateral_token"))
            if symbol and not event.get("collateral_symbol"):
                event["collateral_symbol"] = symbol
            loan_symbol = price_symbol(prices, event["chain_id"], event.get("loan_token"))
            if loan_symbol and not event.get("loan_symbol"):
                event["loan_symbol"] = loan_symbol
            continue
        cpx = price_at(prices, event["chain_id"], event["collateral_token"], event["timestamp"])
        lpx = price_at(prices, event["chain_id"], event["loan_token"], event["timestamp"])
        event["collateral_price_usd"] = cpx
        event["loan_price_usd"] = lpx
        event["bonus_usd"] = bonus_usd(
            event["seized_assets"], event["collateral_decimals"], cpx,
            event["repaid_assets"], event["loan_decimals"], lpx,
        )
        event["repaid_usd"] = usd(event["repaid_assets"], event["loan_decimals"], lpx)
        event["seized_usd"] = usd(event["seized_assets"], event["collateral_decimals"], cpx)
        event["price_is_daily_mark"] = True


def price_symbol(prices: dict, chain_id: int, token: str | None) -> str | None:
    if not token:
        return None
    row = prices.get((chain_id, token.lower())) or {}
    return row.get("symbol")


def fetch_market_supply(events: list[dict]) -> None:
    ids_by_chain: dict[int, set[str]] = {}
    for event in events:
        if event["protocol"] == "morpho-blue":
            ids_by_chain.setdefault(event["chain_id"], set()).add(event["market_id"])
    supply: dict[tuple[int, str], dict] = {}
    query = """
    query($ids: [String!]!, $chainId: Int!) {
      markets(first: 100, where: { uniqueKey_in: $ids, chainId_in: [$chainId] }) {
        items { marketId state { supplyAssetsUsd borrowAssetsUsd } }
      }
    }
    """
    for chain_id, ids in ids_by_chain.items():
        id_list = list(ids)
        for offset in range(0, len(id_list), 80):
            chunk = id_list[offset : offset + 80]
            try:
                data = gql(query, {"ids": chunk, "chainId": chain_id})
            except Exception as exc:  # noqa: BLE001
                print(f"market supply query failed: {exc}", flush=True)
                continue
            for item in data["markets"]["items"] or []:
                state = item.get("state") or {}
                supply[(chain_id, item["marketId"])] = {
                    "supply_assets_usd": state.get("supplyAssetsUsd"),
                    "borrow_assets_usd": state.get("borrowAssetsUsd"),
                }
    for event in events:
        if event["protocol"] != "morpho-blue":
            event["market_tier"] = "euler"
            continue
        row = supply.get((event["chain_id"], event["market_id"]), {})
        event["market_supply_usd"] = row.get("supply_assets_usd")
        event["market_borrow_usd"] = row.get("borrow_assets_usd")
        event["market_tier"] = tier_of(event.get("market_supply_usd"), event.get("collateral_symbol"))


def tier_of(supply_usd: float | None, symbol: str | None) -> str:
    if supply_usd is None:
        if symbol in HEAD_SYMBOLS:
            return "supply_unknown_head_symbol"
        return "supply_unknown"
    if supply_usd < LONG_TAIL_SUPPLY_USD:
        return "long_tail"
    if supply_usd >= HEAD_SUPPLY_USD:
        return "head"
    return "mid"


def archive_rpc(chain: str) -> Rpc:
    urls = ARCHIVE[chain]
    return Rpc(urls[0], timeout=25.0, backups=urls[1:])


def _call(rpc: Rpc, to: str, signature: str, types: list[str], args: list, block: int | str) -> bytes:
    data = "0x" + calldata(signature, types, args).hex()
    return rpc.eth_call(checksum(to), data, block)


def morpho_health(rpc: Rpc, market_id: str, borrower: str, block: int, cache: dict) -> dict | None:
    key = ("morpho", market_id, borrower.lower(), block)
    if key in cache:
        return cache[key]
    try:
        mid = bytes.fromhex(market_id[2:])
        params_key = ("params", market_id)
        if params_key not in cache:
            raw = _call(rpc, MORPHO, "idToMarketParams(bytes32)", ["bytes32"], [mid], block)
            loan, coll, oracle, irm, lltv = decode(
                ["address", "address", "address", "address", "uint256"], raw
            )
            cache[params_key] = (loan, coll, oracle, irm, lltv)
        loan, coll, oracle, irm, lltv = cache[params_key]
        raw = _call(rpc, MORPHO, "position(bytes32,address)", ["bytes32", "address"], [mid, checksum(borrower)], block)
        _supply, borrow_shares, collateral = decode(["uint256", "uint128", "uint128"], raw)
        raw = _call(rpc, MORPHO, "market(bytes32)", ["bytes32"], [mid], block)
        total_supply_assets, total_supply_shares, total_borrow_assets, total_borrow_shares, last_update, fee = decode(
            ["uint128"] * 6, raw
        )
        raw = _call(rpc, oracle, "price()", [], [], block)
        price = decode(["uint256"], raw)[0]
        ts_key = ("ts", rpc.url, block)
        if ts_key not in cache:
            blk = rpc.block(block)
            cache[ts_key] = int(blk["timestamp"], 16)
        elapsed = cache[ts_key] - last_update
        rate = 0
        rate_ok = True
        if elapsed > 0 and total_borrow_assets > 0 and int(irm, 16) != 0:
            try:
                raw = _call(
                    rpc,
                    irm,
                    "borrowRateView((address,address,address,address,uint256),(uint128,uint128,uint128,uint128,uint128,uint128))",
                    ["(address,address,address,address,uint256)", "(uint128,uint128,uint128,uint128,uint128,uint128)"],
                    [
                        (loan, coll, oracle, irm, lltv),
                        (total_supply_assets, total_supply_shares, total_borrow_assets, total_borrow_shares, last_update, fee),
                    ],
                    block,
                )
                rate = decode(["uint256"], raw)[0]
            except RpcError:
                rate_ok = False
        if not rate_ok:
            result = None
        else:
            borrowed = position_borrowed(borrow_shares, total_borrow_assets, total_borrow_shares, rate, max(elapsed, 0))
            result = {
                "unhealthy": is_unhealthy(borrowed, collateral, price, lltv),
                "borrowed": borrowed,
                "collateral": collateral,
                "price": price,
                "lltv": lltv,
                "empty": borrow_shares == 0 and collateral == 0,
            }
    except Exception as exc:  # noqa: BLE001
        result = {"error": str(exc)[:180]}
    cache[key] = result
    return result


def euler_unhealthy(rpc: Rpc, event: dict, block: int, cache: dict) -> dict | None:
    key = ("euler", event["tx"], block)
    if key in cache:
        return cache[key]
    try:
        raw = _call(
            rpc,
            event["vault"],
            "checkLiquidation(address,address,address)",
            ["address", "address", "address"],
            [checksum(event["liquidator"]), checksum(event["borrower"]), checksum(event["collateral_vault"])],
            block,
        )
        max_repay, max_yield = decode(["uint256", "uint256"], raw)
        result = {"unhealthy": max_repay > 0, "max_repay": max_repay, "max_yield": max_yield}
    except Exception as exc:  # noqa: BLE001
        result = {"error": str(exc)[:180]}
    cache[key] = result
    return result


def _unhealthy_at(rpc: Rpc, event: dict, block: int, cache: dict) -> bool | None:
    if block <= 0:
        return None
    if event["protocol"] == "morpho-blue":
        snap = morpho_health(rpc, event["market_id"], event["borrower"], block, cache)
    else:
        snap = euler_unhealthy(rpc, event, block, cache)
    if not snap or snap.get("error") or snap.get("empty"):
        return None
    return bool(snap["unhealthy"])


def measure_delay(rpc: Rpc, event: dict, cache: dict, deadline: float) -> dict:
    """Block delay from the first end-of-block state that was liquidatable, to the liquidation block.

    End-of-block state cannot see an oracle update that landed earlier in the liquidation
    block. A healthy read at block-1 with a successful liquidation in the next block is
    delay 0: it became liquidatable in that block.
    """
    if time.time() > deadline:
        return {"delay_blocks": None, "delay_note": "delay search stopped at the time budget"}
    block = event["block"]
    lookback = int(LOOKBACK_DAYS * 86400 / BLOCK_SECONDS[event["chain"]])
    prev = block - 1
    flag = _unhealthy_at(rpc, event, prev, cache)
    if flag is None:
        return {"delay_blocks": None, "delay_note": "health at the previous block could not be read"}
    if flag is False:
        return {
            "delay_blocks": 0,
            "delay_seconds": 0,
            "delay_censored": False,
            "delay_note": "healthy at the end of the previous block",
        }
    far = max(1, block - lookback)
    far_flag = _unhealthy_at(rpc, event, far, cache)
    if far_flag is None:
        return {"delay_blocks": None, "delay_note": "archive read failed during the lookback"}
    if far_flag is True:
        return {
            "delay_blocks": None,
            "delay_censored": True,
            "delay_blocks_at_least": block - far,
            "delay_note": f"still liquidatable {LOOKBACK_DAYS} days earlier; delay is at least that",
        }
    lo, hi = far, prev
    while lo + 1 < hi:
        if time.time() > deadline:
            return {"delay_blocks": None, "delay_note": "binary search stopped at the time budget"}
        mid = (lo + hi) // 2
        mid_flag = _unhealthy_at(rpc, event, mid, cache)
        if mid_flag is None:
            return {"delay_blocks": None, "delay_note": "archive read failed during the binary search"}
        if mid_flag:
            hi = mid
        else:
            lo = mid
    delay_blocks = block - hi
    return {
        "delay_blocks": delay_blocks,
        "delay_censored": False,
        "first_liquidatable_block": hi,
        "delay_note": "first end-of-block state that was already liquidatable",
    }


def fill_delay_seconds(rpc: Rpc, event: dict) -> None:
    first = event.get("first_liquidatable_block")
    if event.get("delay_blocks") == 0:
        event["delay_seconds"] = 0
        return
    if not first:
        return
    try:
        a = int(rpc.block(first)["timestamp"], 16)
        b = int(rpc.block(event["block"])["timestamp"], 16)
        event["delay_seconds"] = b - a
        event["first_liquidatable_timestamp"] = a
    except RpcError as exc:
        event["delay_seconds_note"] = str(exc)[:160]


def select_delay_sample(events: list[dict]) -> list[dict]:
    rng = random.Random(20261023)
    chosen: list[dict] = []
    seen: set[str] = set()
    by_chain: dict[str, list[dict]] = {}
    for event in events:
        if event["protocol"] != "morpho-blue":
            continue
        by_chain.setdefault(event["chain"], []).append(event)
    for chain, rows in by_chain.items():
        priced = [row for row in rows if row.get("bonus_usd") is not None]
        priced.sort(key=lambda row: row["bonus_usd"], reverse=True)
        large = [row for row in priced if row["bonus_usd"] >= DELAY_LARGE_BONUS_USD][:DELAY_LARGE_CAP]
        rest = [row for row in priced if row not in large]
        rng.shuffle(rest)
        pick = large + rest[: max(0, DELAY_PER_CHAIN - len(large))]
        for row in pick:
            key = row["tx"] + str(row["log_index"])
            if key not in seen:
                seen.add(key)
                chosen.append(row)
        print(f"delay sample {chain}: {len(pick)} of {len(rows)}", flush=True)
    for event in events:
        if event["protocol"] == "euler-v2":
            chosen.append(event)
    return chosen


def attach_gas_and_fee(events: list[dict], prices: dict, end_ts: int) -> None:
    """Gas from the receipt. ETH priced at the liquidation day when Morpho has that day, else the scan price.

    Flash fee is zero only when Morpho or Balancer holds at least the repaid amount of the
    loan token at the latest block. That is not the historical balance.
    """
    rpcs = {name: archive_rpc(name) for name in ("mainnet", "base", "arbitrum")}
    balancers = {}
    for name in rpcs:
        cfg = load_chain(name)
        balancers[name] = cfg["balancer_vault"]
    balance_cache: dict[tuple[str, str, str], int] = {}

    def token_balance(chain: str, token: str, holder: str) -> int | None:
        key = (chain, token.lower(), holder.lower())
        if key in balance_cache:
            return balance_cache[key]
        try:
            raw = _call(rpcs[chain], token, "balanceOf(address)", ["address"], [checksum(holder)], "latest")
            value = decode(["uint256"], raw)[0]
        except Exception:  # noqa: BLE001
            value = None
        balance_cache[key] = value  # type: ignore[assignment]
        return value

    for index, event in enumerate(events):
        rpc = rpcs[event["chain"]]
        try:
            receipt = rpc.request("eth_getTransactionReceipt", [event["tx"]])
        except RpcError as exc:
            event["gas_note"] = str(exc)[:160]
            continue
        if not receipt:
            event["gas_note"] = "receipt missing"
            continue
        gas_used = int(receipt["gasUsed"], 16)
        effective = int(receipt.get("effectiveGasPrice") or "0x0", 16)
        l1_fee = int(receipt["l1Fee"], 16) if receipt.get("l1Fee") else 0
        eth_price = price_at(prices, event["chain_id"], WETH[event["chain_id"]], event["timestamp"])
        event["gas_used"] = gas_used
        event["effective_gas_price"] = effective
        event["l1_fee_wei"] = l1_fee
        event["eth_price_usd"] = eth_price
        if eth_price is None:
            event["gas_usd"] = None
            event["gas_note"] = "no ETH price for that day"
        else:
            event["gas_usd"] = (gas_used * effective + l1_fee) / 1e18 * eth_price
        morpho_bal = token_balance(event["chain"], event["loan_token"], MORPHO)
        bal_bal = token_balance(event["chain"], event["loan_token"], balancers[event["chain"]])
        repaid = event["repaid_assets"]
        morpho_ok = morpho_bal is not None and morpho_bal >= repaid
        balancer_ok = bal_bal is not None and bal_bal >= repaid
        if morpho_ok or balancer_ok:
            event["flash_fee_usd"] = 0.0
            event["flash_source"] = "morpho" if morpho_ok else "balancer"
            event["flash_fee_note"] = "latest balance covers the repaid amount; historical balance was not checked"
        else:
            event["flash_fee_usd"] = None
            event["flash_source"] = None
            event["flash_fee_note"] = "latest Morpho and Balancer balances do not cover this repay, or the read failed"
        if index % 20 == 0:
            print(f"receipts {index}/{len(events)}", flush=True)


def _plausible_open(row: dict) -> bool:
    """Indexer rows a quote might actually exit.

    Billion-dollar health-factor rows in this API are bad debt or a broken USD
    mark: collateral covers only a few percent of the printed debt. Quoting those
    first hid every ordinary position. They are counted, and they are not treated
    as size.
    """
    borrow = row.get("borrow_usd") or 0
    collateral = row.get("collateral_usd")
    if borrow < OPEN_BORROW_MIN_USD or borrow > 2_000_000:
        return False
    if not row.get("collateral_token") or not row.get("loan_token"):
        return False
    if collateral is None or collateral < borrow * 0.3:
        return False
    return True


def fetch_open_morpho() -> dict[str, list[dict]]:
    query = """
    query($first: Int!, $skip: Int!, $where: MarketPositionFilters!) {
      marketPositions(first: $first, skip: $skip, orderBy: HealthFactor, orderDirection: Asc, where: $where) {
        pageInfo { countTotal }
        items {
          healthFactor
          priceVariationToLiquidationPrice
          user { address }
          market {
            marketId
            lltv
            collateralAsset { symbol address decimals }
            loanAsset { symbol address decimals }
          }
          state { collateral collateralUsd borrowAssets borrowAssetsUsd }
        }
      }
    }
    """

    def pages(where: dict) -> tuple[list[dict], int | None]:
        rows: list[dict] = []
        skip = 0
        total = None
        while skip < 5000:
            data = gql(query, {"first": 100, "skip": skip, "where": where})
            page = data["marketPositions"]
            total = page["pageInfo"]["countTotal"]
            items = page["items"] or []
            rows.extend(items)
            if not items or len(rows) >= (total or 0):
                break
            skip += len(items)
        return rows, total

    underwater_rows: list[dict] = []
    near_rows: list[dict] = []
    totals = {}
    for chain_id in (1, 8453):
        raw, total = pages({"chainId_in": [chain_id], "healthFactor_lte": 1})
        totals[f"hf_lte_1_{chain_id}"] = total
        underwater_rows.extend(_clean_positions(raw, chain_id, "underwater"))
        raw, total = pages({"chainId_in": [chain_id], "healthFactor_gte": 1, "healthFactor_lte": NEAR_HF})
        totals[f"hf_1_to_{NEAR_HF}_{chain_id}"] = total
        near_rows.extend(_clean_positions(raw, chain_id, "near"))
        print(f"open morpho chain {chain_id} underwater {totals[f'hf_lte_1_{chain_id}']} near {total}", flush=True)
    return {"underwater": underwater_rows, "near": near_rows, "totals": totals}


def _clean_positions(items: list[dict], chain_id: int, kind: str) -> list[dict]:
    rows = []
    for item in items:
        state = item.get("state") or {}
        market = item["market"]
        collateral = market.get("collateralAsset") or {}
        loan = market.get("loanAsset") or {}
        rows.append({
            "kind": kind,
            "chain_id": chain_id,
            "chain": CHAIN_NAME[chain_id],
            "borrower": item["user"]["address"],
            "health_factor": item.get("healthFactor"),
            "price_variation": item.get("priceVariationToLiquidationPrice"),
            "market_id": market["marketId"],
            "lltv": _int(market["lltv"]),
            "collateral_symbol": collateral.get("symbol"),
            "collateral_token": collateral.get("address"),
            "collateral_decimals": int(collateral["decimals"]) if collateral.get("decimals") is not None else None,
            "loan_symbol": loan.get("symbol"),
            "loan_token": loan.get("address"),
            "loan_decimals": int(loan["decimals"]) if loan.get("decimals") is not None else None,
            "collateral": _int(state.get("collateral")),
            "collateral_usd": state.get("collateralUsd"),
            "borrow_assets": _int(state.get("borrowAssets")),
            "borrow_usd": state.get("borrowAssetsUsd"),
        })
    return rows


def quote_open_positions(positions: list[dict], prices: dict) -> list[dict]:
    """Chain-check the largest indexer-underwater positions and quote an exit."""
    candidates = [
        row for row in positions
        if (row.get("borrow_usd") or 0) >= OPEN_BORROW_MIN_USD and row.get("collateral_token") and row.get("loan_token")
    ]
    candidates.sort(key=lambda row: row.get("borrow_usd") or 0, reverse=True)
    candidates = candidates[:QUOTE_CAP]
    rpcs = {name: archive_rpc(name) for name in ("mainnet", "base")}
    cfgs = {name: load_chain(name) for name in ("mainnet", "base")}
    cache: dict = {}
    quoted = []
    for row in candidates:
        chain = row["chain"]
        snap = morpho_health(rpcs[chain], row["market_id"], row["borrower"], "latest", cache)  # type: ignore[arg-type]
        # morpho_health expects an int block. "latest" breaks the timestamp cache. Call via a wrapper.
        quoted.append(_quote_one(rpcs[chain], cfgs[chain], row, prices, snap if isinstance(snap, dict) else {"error": "bad block"}))
    return quoted


def morpho_health_latest(rpc: Rpc, market_id: str, borrower: str) -> dict | None:
    head = rpc.block_number()
    return morpho_health(rpc, market_id, borrower, head, {})


def _quote_one(rpc: Rpc, cfg: dict, row: dict, prices: dict, snap: dict | None) -> dict:
    out = dict(row)
    if not snap or snap.get("error"):
        # The generic health helper was called with a bad block above. Re-read at head.
        snap = morpho_health_latest(rpc, row["market_id"], row["borrower"])
    if not snap or snap.get("error"):
        out["reason"] = "oracle_or_position_read_failed"
        out["detail"] = None if not snap else snap.get("error")
        return out
    out["on_chain_unhealthy"] = bool(snap.get("unhealthy"))
    out["on_chain_borrowed"] = snap.get("borrowed")
    out["on_chain_collateral"] = snap.get("collateral")
    if not snap.get("unhealthy"):
        out["reason"] = "indexer_underwater_but_healthy_on_chain"
        return out
    take = take_size(snap["collateral"], snap["borrowed"], snap["price"], snap["lltv"])
    out["repay_assets"] = take["repay"]
    out["seize_assets"] = take["seize"]
    out["bad_debt"] = take["bad_debt"]
    loan_px = price_at(prices, row["chain_id"], row["loan_token"], _now())
    out["repay_usd"] = usd(int(take["repay"]), row["loan_decimals"], loan_px)
    if int(take["seize"]) <= 0 or int(take["repay"]) <= 0:
        out["reason"] = "zero_seize_or_repay"
        return out
    dex_out = _best_swap(rpc, cfg, row["collateral_token"], row["loan_token"], int(take["seize"]))
    out["dex"] = dex_out
    if dex_out.get("amount_out") is None:
        out["reason"] = "no_direct_dex_pool"
        return out
    out["dex_out_usd"] = usd(dex_out["amount_out"], row["loan_decimals"], loan_px)
    if out["dex_out_usd"] is None or out["repay_usd"] is None:
        out["reason"] = "missing_loan_price"
        return out
    fee = _flash_fee(rpc, cfg, row["loan_token"], int(take["repay"]))
    out.update(fee)
    gas = _prospective_gas_usd(rpc, cfg, prices, row["chain_id"])
    out.update(gas)
    if fee.get("flash_fee_usd") is None or gas.get("gas_usd") is None:
        out["reason"] = "fee_or_gas_unknown"
        out["net_usd"] = None
        return out
    out["net_usd"] = out["dex_out_usd"] - out["repay_usd"] - fee["flash_fee_usd"] - gas["gas_usd"]
    if out["net_usd"] > 0:
        out["reason"] = "positive_after_slippage_gas_and_flash_fee"
    else:
        out["reason"] = "dex_proceeds_do_not_cover_repay_gas_and_fee"
    return out


def _best_swap(rpc: Rpc, cfg: dict, token_in: str, token_out: str, amount_in: int) -> dict:
    token_in = checksum(token_in)
    token_out = checksum(token_out)
    best: dict[str, Any] = {"amount_out": None}
    block = "latest"
    for fac in cfg["factories"]:
        kind = fac["kind"]
        try:
            if kind == "univ2":
                raw = _call(rpc, fac["address"], "getPair(address,address)", ["address", "address"], [token_in, token_out], block)
                pair = decode(["address"], raw)[0]
                if int(pair, 16) == 0:
                    continue
                reserves = decode(["uint112", "uint112", "uint32"], _call(rpc, pair, "getReserves()", [], [], block))
                token0 = decode(["address"], _call(rpc, pair, "token0()", [], [], block))[0]
                if token0.lower() == token_in.lower():
                    reserve_in, reserve_out = reserves[0], reserves[1]
                else:
                    reserve_in, reserve_out = reserves[1], reserves[0]
                out = get_amount_out(amount_in, reserve_in, reserve_out, int(fac["fee_bps"]))
                if best["amount_out"] is None or out > best["amount_out"]:
                    best = {"amount_out": out, "kind": "univ2", "pool": pair, "fee_bps": fac["fee_bps"], "venue": fac["name"]}
            elif kind == "solidly":
                for stable in (False, True):
                    raw = _call(
                        rpc, fac["address"], "getPool(address,address,bool)",
                        ["address", "address", "bool"], [token_in, token_out, stable], block,
                    )
                    pool = decode(["address"], raw)[0]
                    if int(pool, 16) == 0:
                        continue
                    out = decode(
                        ["uint256"],
                        _call(rpc, pool, "getAmountOut(uint256,address)", ["uint256", "address"], [amount_in, token_in], block),
                    )[0]
                    if best["amount_out"] is None or out > best["amount_out"]:
                        best = {"amount_out": out, "kind": "solidly", "stable": stable, "pool": pool, "venue": fac["name"]}
            elif kind == "univ3":
                for fee in fac.get("fees") or []:
                    raw = _call(
                        rpc, fac["address"], "getPool(address,address,uint24)",
                        ["address", "address", "uint24"], [token_in, token_out, fee], block,
                    )
                    pool = decode(["address"], raw)[0]
                    if int(pool, 16) == 0:
                        continue
                    out = _quote_v3(rpc, cfg["quoter_v2"], token_in, token_out, amount_in, fee)
                    if out is None:
                        continue
                    if best["amount_out"] is None or out > best["amount_out"]:
                        best = {"amount_out": out, "kind": "univ3", "fee": fee, "pool": pool, "venue": fac["name"]}
        except RpcError:
            continue
    if best["amount_out"] is None:
        best["note"] = "no univ2, solidly, or univ3 pool quoted; slipstream and other venues were not searched"
    return best


def _quote_v3(rpc: Rpc, quoter: str, token_in: str, token_out: str, amount: int, fee: int) -> int | None:
    try:
        raw = _call(
            rpc,
            quoter,
            "quoteExactInputSingle((address,address,uint256,uint24,uint160))",
            ["(address,address,uint256,uint24,uint160)"],
            [(token_in, token_out, amount, fee, 0)],
            "latest",
        )
    except RpcError:
        return None
    if len(raw) < 32:
        return None
    return int.from_bytes(raw[:32], "big")


def _flash_fee(rpc: Rpc, cfg: dict, loan_token: str, repay_assets: int) -> dict:
    try:
        morpho_bal = decode(["uint256"], _call(rpc, loan_token, "balanceOf(address)", ["address"], [checksum(MORPHO)], "latest"))[0]
        bal_bal = decode(
            ["uint256"],
            _call(rpc, loan_token, "balanceOf(address)", ["address"], [checksum(cfg["balancer_vault"])], "latest"),
        )[0]
    except RpcError as exc:
        return {"flash_fee_usd": None, "flash_fee_note": str(exc)[:160]}
    if morpho_bal >= repay_assets or bal_bal >= repay_assets:
        return {
            "flash_fee_usd": 0.0,
            "flash_source": "morpho" if morpho_bal >= repay_assets else "balancer",
            "flash_fee_note": "latest balance covers the repay; Morpho and Balancer flash fees are zero",
        }
    return {
        "flash_fee_usd": None,
        "flash_source": None,
        "flash_fee_note": "neither Morpho nor Balancer currently holds enough of the loan token",
    }


def _prospective_gas_usd(rpc: Rpc, cfg: dict, prices: dict, chain_id: int) -> dict:
    try:
        blk = rpc.block(rpc.block_number())
        base_fee = int(blk.get("baseFeePerGas") or "0x0", 16)
    except RpcError as exc:
        return {"gas_usd": None, "gas_note": str(exc)[:160]}
    eth = price_at(prices, chain_id, WETH[chain_id], _now())
    # 600_000 is not a measurement. Callers should overwrite with the median of receipts when that exists.
    return {
        "base_fee_wei": base_fee,
        "eth_price_usd": eth,
        "gas_units_assumed": None,
        "extra_l1_fee_usd": cfg.get("extra_l1_fee_usd"),
        "gas_usd": None,
        "gas_note": "filled from the measured median gas of historical liquidation receipts",
    }


def logs_rpc(chain: str) -> Rpc:
    # publicnode answered Base getLogs after drpc returned 403 for multi-address filters.
    if chain == "base":
        urls = [
            "https://base.publicnode.com",
            "https://mainnet.base.org",
            "https://base.drpc.org",
            "https://base.gateway.tenderly.co",
        ]
    elif chain == "arbitrum":
        urls = ARCHIVE["arbitrum"]
    else:
        urls = ARCHIVE["mainnet"]
    return Rpc(urls[0], timeout=25.0, backups=urls[1:])


def scan_logs(prices: dict) -> dict:
    """Short windows. Public getLogs cannot cover 90 days on Base or Arbitrum."""
    out: dict[str, Any] = {}
    base_rpc = logs_rpc("base")
    arb_rpc = logs_rpc("arbitrum")
    try:
        base_head = base_rpc.block_number()
    except RpcError as exc:
        return {"error": f"base head: {exc}"}
    try:
        arb_head = arb_rpc.block_number()
    except RpcError as exc:
        arb_head = None
        out["arbitrum_head_error"] = str(exc)[:200]
    base_span = 2 * 43_200  # about 2 days at a 2 second block. Not 90.
    arb_span = 2 * 345_600  # about 2 days at a 0.25 second block. Not 90.
    out["moonwell"] = _scan_moonwell(base_rpc, base_head, base_span, prices)
    out["seamless"] = _summarize_logs(_scan_topic(
        base_rpc, "seamless", "base", 8453, [checksum(SEAMLESS_POOL)], TOPIC_AAVE_LIQUIDATION,
        base_head - base_span, base_head, 20_000, prices, "aave",
    ))
    out["fluid_base"] = _scan_fluid(base_rpc, base_head, 14 * 43_200)
    if arb_head:
        out["silo_arbitrum"] = _summarize_logs(_scan_topic(
            arb_rpc, "silo", "arbitrum", 42161, [checksum(SILO_LIQUIDATION_ARB)], TOPIC_SILO_LIQUIDATION,
            arb_head - arb_span, arb_head, 40_000, prices, "silo",
        ))
    return out


def _summarize_logs(part: dict) -> dict:
    events = part.pop("all_events", [])
    counts: dict[str, int] = {}
    for event in events:
        liquidator = event.get("liquidator")
        if not liquidator:
            continue
        key = liquidator.lower()
        counts[key] = counts.get(key, 0) + 1
    part["unique_liquidators"] = len(counts)
    part["top_liquidators"] = sorted(counts.items(), key=lambda item: -item[1])[:5]
    part["sample_events"] = [
        {key: value for key, value in event.items() if key != "bonus_note"}
        for event in events[:12]
    ]
    return part


def _scan_moonwell(rpc: Rpc, head: int, span: int, prices: dict) -> dict:
    try:
        raw = _call(rpc, MOONWELL_COMPTROLLER, "getAllMarkets()", [], [], "latest")
        markets = [checksum(item) for item in decode(["address[]"], raw)[0]]
    except RpcError as exc:
        return {"included": False, "error": str(exc)[:300]}
    underlyings = {}
    for market in markets:
        try:
            underlying = decode(["address"], _call(rpc, market, "underlying()", [], [], "latest"))[0]
            decimals = decode(["uint8"], _call(rpc, underlying, "decimals()", [], [], "latest"))[0]
            underlyings[market.lower()] = {"token": checksum(underlying), "decimals": decimals}
        except RpcError:
            continue
    merged: list[dict] = []
    errors = []
    windows = []
    for market in markets:
        part = _scan_topic(
            rpc, "moonwell", "base", 8453, [market], TOPIC_LIQUIDATE_BORROW,
            head - span, head, 25_000, prices, "compound",
        )
        windows.append(part.get("window_blocks"))
        errors.extend(part.get("errors") or [])
        for event in part.get("all_events") or []:
            info = underlyings.get(event.get("loan_token", "").lower())
            if not info:
                merged.append(event)
                continue
            event["loan_token"] = info["token"]
            event["loan_decimals"] = info["decimals"]
            px = price_at(prices, 8453, info["token"], _now())
            event["repaid_usd"] = usd(event["repaid_assets"], info["decimals"], px)
            event["bonus_usd"] = None
            event["bonus_note"] = "seizeTokens is an mToken amount; the bonus was not converted"
            merged.append(event)
    counts: dict[str, int] = {}
    for event in merged:
        key = event["liquidator"].lower()
        counts[key] = counts.get(key, 0) + 1
    repaid = [event["repaid_usd"] for event in merged if event.get("repaid_usd") is not None]
    return {
        "included": True,
        "markets": len(markets),
        "window_blocks": [head - span, head],
        "complete": not errors,
        "errors": errors[:5],
        "events": len(merged),
        "repaid_usd_sum": sum(repaid) if repaid else None,
        "repaid_priced_events": len(repaid),
        "unique_liquidators": len(counts),
        "top_liquidators": sorted(counts.items(), key=lambda item: -item[1])[:5],
        "note": "About 2 days of Moonwell mToken logs, one market per request. Not 90 days. Bonus USD was not computed.",
        "sample_events": merged[:15],
    }


def _scan_topic(
    rpc: Rpc,
    protocol: str,
    chain: str,
    chain_id: int,
    addresses: list[str] | None,
    topic0: str,
    start: int,
    end: int,
    step: int,
    prices: dict,
    kind: str,
) -> dict:
    logs: list[dict] = []
    errors = []
    block = start
    chunks = 0
    while block <= end and chunks < 40:
        hi = min(end, block + step - 1)
        try:
            part = rpc.get_logs(addresses, [topic0], block, hi)
        except RpcError as exc:
            errors.append({"from": block, "to": hi, "error": str(exc)[:180]})
            if len(errors) >= 3:
                break
            step = max(1_000, step // 2)
            continue
        logs.extend(part)
        block = hi + 1
        chunks += 1
    events = []
    for log in logs:
        decoded = _decode_liq(log, kind)
        if not decoded:
            continue
        decoded.update({
            "protocol": protocol,
            "chain": chain,
            "chain_id": chain_id,
            "block": int(log["blockNumber"], 16),
            "tx": log["transactionHash"],
            "bonus_usd": None,
            "bonus_note": "log amounts were not marked to USD",
        })
        events.append(decoded)
    return {
        "included": True,
        "window_blocks": [start, min(end, block - 1) if block > start else start],
        "complete": block > end and not errors,
        "chunks": chunks,
        "errors": errors[:5],
        "logs": len(logs),
        "events": len(events),
        "note": "This is not a 90-day window.",
        "all_events": events,
        "sample_events": events[:20],
    }


def _decode_liq(log: dict, kind: str) -> dict | None:
    topics = log.get("topics") or []
    data = bytes.fromhex(log["data"][2:]) if log.get("data") and log["data"] != "0x" else b""
    try:
        if kind == "compound":
            liquidator, borrower, repay, collateral, seize = decode(
                ["address", "address", "uint256", "address", "uint256"], data
            )
            return {
                "liquidator": liquidator,
                "borrower": borrower,
                "loan_token": log["address"],
                "collateral_token": collateral,
                "repaid_assets": repay,
                "seized_assets": seize,
            }
        if kind == "aave":
            if len(topics) < 4:
                return None
            collateral = _topic_address(topics[1])
            debt = _topic_address(topics[2])
            user = _topic_address(topics[3])
            debt_to_cover, seized, liquidator, _receive = decode(["uint256", "uint256", "address", "bool"], data)
            return {
                "liquidator": liquidator,
                "borrower": user,
                "loan_token": debt,
                "collateral_token": collateral,
                "repaid_assets": debt_to_cover,
                "seized_assets": seized,
            }
        if kind == "silo":
            if len(topics) < 4:
                return None
            repay, seized, _receive = decode(["uint256", "uint256", "bool"], data)
            return {
                "liquidator": _topic_address(topics[1]),
                "market_id": _topic_address(topics[2]),
                "borrower": _topic_address(topics[3]),
                "repaid_assets": repay,
                "seized_assets": seized,
            }
    except Exception:  # noqa: BLE001
        return None
    return None


def _topic_address(topic: str) -> str:
    return checksum("0x" + topic[-40:])


def _scan_fluid(rpc: Rpc, head: int, span: int) -> dict:
    start = head - span
    topic_counts: dict[str, int] = {}
    errors = []
    block = start
    chunks = 0
    step = 20_000
    while block <= head and chunks < 40:
        hi = min(head, block + step - 1)
        try:
            part = rpc.get_logs([checksum(FLUID_LIQUIDATOR_BASE)], [], block, hi)
        except RpcError as exc:
            errors.append(str(exc)[:180])
            break
        for log in part:
            topic0 = (log.get("topics") or ["0x"])[0]
            topic_counts[topic0] = topic_counts.get(topic0, 0) + 1
        block = hi + 1
        chunks += 1
    return {
        "included": True,
        "address": FLUID_LIQUIDATOR_BASE,
        "window_blocks": [start, min(head, block - 1)],
        "complete": block > head and not errors,
        "chunks": chunks,
        "errors": errors[:3],
        "log_count": sum(topic_counts.values()),
        "topics": topic_counts,
        "note": (
            "Logs were counted. They were not priced. The ABI was not assumed, so bonus, "
            "collateral and liquidator were not decoded. This is not a 90-day window."
        ),
    }


def euler_open_sample() -> dict:
    """A thin sample of debtors on the largest Euler vaults. Not the full book."""
    checked = 0
    liquidatable = []
    near = []
    errors = []
    vaults_seen = 0
    for chain_id in (1, 8453, 42161):
        try:
            payload = _http_json(f"https://v3.euler.finance/v3/evk/vaults?chainId={chain_id}&limit=100")
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc)[:160])
            continue
        vaults = payload.get("data") or []
        # The list endpoint pages. Pull two more pages so a large vault is not stuck on page one.
        for offset in (100, 200):
            try:
                more = _http_json(
                    f"https://v3.euler.finance/v3/evk/vaults?chainId={chain_id}&limit=100&offset={offset}"
                )
            except Exception:  # noqa: BLE001
                break
            vaults.extend(more.get("data") or [])
        borrow_vaults = [
            vault for vault in vaults
            if not vault.get("isEscrow") and (vault.get("totalBorrowsUsd") or 0) >= 10_000
        ]
        borrow_vaults.sort(key=lambda vault: vault.get("totalBorrowsUsd") or 0, reverse=True)
        for vault in borrow_vaults[:4]:
            vaults_seen += 1
            try:
                holders = _http_json(
                    f"https://v3.euler.finance/v3/evk/vaults/{chain_id}/{vault['address']}/debt-holders?limit=12"
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc)[:160])
                continue
            for holder in (holders.get("data") or [])[:12]:
                if checked >= 48:
                    break
                checked += 1
                account = holder["account"]
                try:
                    positions = _http_json(f"https://v3.euler.finance/v3/accounts/{account}/positions")
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc)[:120])
                    continue
                for pos in positions.get("data") or []:
                    liquidity = pos.get("liquidity") or {}
                    liability = ((liquidity.get("liabilityValue") or {}).get("liquidation"))
                    collateral = ((liquidity.get("totalCollateralValue") or {}).get("liquidation"))
                    if not liability or not collateral:
                        continue
                    try:
                        liab = int(liability)
                        coll = int(collateral)
                    except ValueError:
                        continue
                    if liab <= 0:
                        continue
                    ratio = coll / liab
                    record = {
                        "chain_id": chain_id,
                        "account": account,
                        "vault": pos.get("vault"),
                        "liability": str(liab),
                        "collateral_liquidation_value": str(coll),
                        "ratio": ratio,
                        "snapshot": pos.get("snapshot"),
                    }
                    if ratio < 1:
                        liquidatable.append(record)
                    elif ratio < 1.02:
                        near.append(record)
    return {
        "accounts_checked": checked,
        "borrow_vaults_touched": vaults_seen,
        "liquidatable": liquidatable,
        "near": near[:10],
        "errors": errors[:5],
        "note": (
            "At most 48 debtors across the largest vaults that the vault list returned. "
            "Absence here is not proof the rest of the book is healthy. Ratios use Euler's "
            "liquidation collateral value over liquidation liability. Snapshot age is whatever "
            "the API returned."
        ),
    }


def _delay_slice(events: list[dict]) -> dict:
    delays = [event["delay_blocks"] for event in events if event.get("delay_blocks") is not None]
    return {
        "sample_events": len(events),
        "measured_events": len(delays),
        "censored_events": sum(1 for event in events if event.get("delay_censored")),
        "unreadable_events": sum(
            1 for event in events if event.get("delay_blocks") is None and not event.get("delay_censored")
        ),
        "median_delay_blocks": median(delays),
        "median_delay_seconds": median(
            [event["delay_seconds"] for event in events if event.get("delay_seconds") is not None]
        ),
        "delay_zero": sum(1 for value in delays if value == 0),
        "delay_gt_1": sum(1 for value in delays if value > 1),
        "delay_gt_5": sum(1 for value in delays if value > 5),
    }


def _market_key(row: dict) -> tuple[str, str, str]:
    return (
        row.get("protocol") or "unknown",
        row.get("chain") or "unknown",
        (row.get("market_id") or "unknown").lower(),
    )


def _fill_label(current: str | None, new: str | None) -> str | None:
    return current or new


def build_loan_sizes(events: list[dict], positions: list[dict]) -> dict:
    """Debt and collateral USD for every observed liquidation and every liquidatable position.

    Loan size is debt. For a liquidation that is the debt repaid in that event, which can
    be a partial repay. For an open position it is the outstanding borrow, not a completed
    repay. Collateral on an open position is the balance, not a seize. Missing prices are
    counted and left out of the average.
    """
    observed = []
    for event in events:
        if event.get("protocol") not in {"morpho-blue", "euler-v2"}:
            continue
        observed.append({
            "kind": "observed_liquidation",
            "protocol": event.get("protocol"),
            "chain": event.get("chain"),
            "market_id": event.get("market_id"),
            "collateral": event.get("collateral_symbol"),
            "loan": event.get("loan_symbol"),
            "tx": event.get("tx"),
            "block": event.get("block"),
            "debt_repaid_usd": event.get("repaid_usd"),
            "collateral_seized_usd": event.get("seized_usd"),
            "price_source": event.get("bonus_source"),
        })
    liquidatable = []
    for row in positions:
        if row.get("kind") not in {None, "underwater"}:
            continue
        health = row.get("health_factor")
        try:
            health_value = float(health) if health is not None else None
        except (TypeError, ValueError):
            health_value = None
        if health_value is not None and health_value > 1:
            continue
        liquidatable.append({
            "kind": "liquidatable_position",
            "protocol": "morpho-blue",
            "chain": row.get("chain"),
            "market_id": row.get("market_id"),
            "borrower": row.get("borrower"),
            "collateral": row.get("collateral_symbol"),
            "loan": row.get("loan_symbol"),
            "health_factor": health_value,
            "debt_outstanding_usd": row.get("borrow_usd"),
            "collateral_balance_usd": row.get("collateral_usd"),
            "price_source": "morpho_indexer",
        })
    observed_pack = _pack_sizes(observed, "debt_repaid_usd", "collateral_seized_usd")
    position_pack = _pack_sizes(liquidatable, "debt_outstanding_usd", "collateral_balance_usd")
    large_positions = [
        row["debt_outstanding_usd"]
        for row in liquidatable
        if row.get("debt_outstanding_usd") is not None and row["debt_outstanding_usd"] >= 50
    ]
    return {
        "loan_size_is": "debt in USD. Observed rows use debt repaid. Open rows use outstanding borrow.",
        "collateral_is": "Collateral seized for an observed liquidation. Collateral balance for an open position, which is not a seize.",
        "buckets_usd": {
            "under_100": "[0, 100)",
            "100_to_1k": "[100, 1000)",
            "1k_to_10k": "[1000, 10000)",
            "10k_to_100k": "[10000, 100000)",
            "100k_to_1m": "[100000, 1000000)",
            "at_least_1m": "[1000000, inf)",
        },
        "observed_liquidations": observed_pack,
        "liquidatable_positions": position_pack,
        "liquidatable_debt_at_least_50": size_summary(large_positions),
        "not_in_this_table": (
            "Moonwell's 142 LiquidateBorrow logs were not retained per event, so they have no "
            "per-market distribution. Seamless, Fluid, and the one Silo contract had zero events "
            "in their short windows. Near-threshold positions with health factor above 1 are not liquidatable."
        ),
    }


def _pack_sizes(rows: list[dict], debt_key: str, collateral_key: str) -> dict:
    grouped: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        key = _market_key(row)
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {
                "protocol": key[0],
                "chain": key[1],
                "market_id": row.get("market_id"),
                "collateral": row.get("collateral"),
                "loan": row.get("loan"),
                "rows": [],
            }
            grouped[key] = bucket
        bucket["collateral"] = _fill_label(bucket["collateral"], row.get("collateral"))
        bucket["loan"] = _fill_label(bucket["loan"], row.get("loan"))
        bucket["rows"].append(row)
    markets = []
    for bucket in grouped.values():
        debts = [row[debt_key] for row in bucket["rows"] if row.get(debt_key) is not None]
        collateral = [row[collateral_key] for row in bucket["rows"] if row.get(collateral_key) is not None]
        markets.append({
            "protocol": bucket["protocol"],
            "chain": bucket["chain"],
            "market_id": bucket["market_id"],
            "collateral": bucket["collateral"],
            "loan": bucket["loan"],
            "n": len(bucket["rows"]),
            "debt_priced": len(debts),
            "debt_unpriced": len(bucket["rows"]) - len(debts),
            "collateral_priced": len(collateral),
            "collateral_unpriced": len(bucket["rows"]) - len(collateral),
            "debt_usd": size_summary(debts),
            "collateral_usd": size_summary(collateral),
        })
    markets.sort(key=lambda item: (-item["n"], item["protocol"], item["chain"], item["market_id"] or ""))
    debts = [row[debt_key] for row in rows if row.get(debt_key) is not None]
    collateral = [row[collateral_key] for row in rows if row.get(collateral_key) is not None]
    return {
        "n": len(rows),
        "debt_priced": len(debts),
        "debt_unpriced": len(rows) - len(debts),
        "collateral_priced": len(collateral),
        "collateral_unpriced": len(rows) - len(collateral),
        "debt_usd": size_summary(debts),
        "collateral_usd": size_summary(collateral),
        "by_market": markets,
        "records": [
            {key: value for key, value in row.items() if value is not None}
            for row in rows
        ],
    }


def _group_bonus(events: list[dict]) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for event in events:
        key = f"{event['protocol']}:{event['chain']}:{event.get('market_tier') or 'na'}:{event.get('collateral_symbol') or 'unknown'}"
        bucket = groups.setdefault(key, {"events": 0, "bonus_usd": 0.0, "priced": 0, "unpriced": 0, "bad_debt_events": 0})
        bucket["events"] += 1
        if event.get("bad_debt_assets"):
            bucket["bad_debt_events"] += 1
        bonus = event.get("bonus_usd")
        if bonus is None:
            bucket["unpriced"] += 1
        else:
            bucket["priced"] += 1
            if bonus > 0:
                bucket["bonus_usd"] += bonus
    return groups


def _positive_sum(events: list[dict]) -> float:
    return sum(event["bonus_usd"] for event in events if (event.get("bonus_usd") or 0) > 0)


def build_report(
    morpho: list[dict],
    euler: list[dict],
    sample: list[dict],
    open_quotes: list[dict],
    open_meta: dict,
    logs: dict,
    euler_open: dict,
    start_ts: int,
    end_ts: int,
) -> dict:
    measured = [event for event in sample if event.get("delay_blocks") is not None or event.get("delay_censored")]
    delays = [event["delay_blocks"] for event in sample if event.get("delay_blocks") is not None]
    gas_values = [event["gas_used"] for event in sample if event.get("gas_used")]
    median_gas = median(gas_values)
    # Prospective gas uses the median measured gas units. It is not a guess of 600k.
    for row in open_quotes:
        if row.get("gas_usd") is None and median_gas is not None and row.get("base_fee_wei") and row.get("eth_price_usd"):
            execution = median_gas * row["base_fee_wei"] / 1e18 * row["eth_price_usd"]
            extra = row.get("extra_l1_fee_usd") or 0
            row["gas_units_assumed"] = median_gas
            row["gas_usd"] = execution + extra
            row["gas_note"] = (
                "median gasUsed of the historical liquidation receipts in the delay sample, "
                "times the current base fee and the daily-or-current ETH price, plus the chain "
                "L1/Timeboost cushion already used in the arb study"
            )
            if row.get("dex_out_usd") is not None and row.get("repay_usd") is not None and row.get("flash_fee_usd") is not None:
                row["net_usd"] = row["dex_out_usd"] - row["repay_usd"] - row["flash_fee_usd"] - row["gas_usd"]
                row["reason"] = (
                    "positive_after_slippage_gas_and_flash_fee"
                    if row["net_usd"] > 0
                    else "dex_proceeds_do_not_cover_repay_gas_and_fee"
                )
    opportunities = [row for row in open_quotes if (row.get("net_usd") or 0) > 0]
    opportunities.sort(key=lambda row: row["net_usd"], reverse=True)
    all_events = morpho + euler
    slow = {str(n): slowly_captured(sample, n) for n in SLOW_BLOCKS}
    counts = {}
    for event in all_events:
        counts[event["liquidator"].lower()] = counts.get(event["liquidator"].lower(), 0) + 1
    bonuses: dict[str, float] = {}
    for event in all_events:
        bonus = event.get("bonus_usd")
        if bonus is None:
            continue
        key = event["liquidator"].lower()
        bonuses[key] = bonuses.get(key, 0.0) + (bonus if bonus > 0 else 0.0)
    conc = concentration(counts, bonuses, REPEAT_LIQUIDATOR_MIN)
    near_big = [
        row for row in open_meta["near"]
        if (row.get("borrow_usd") or 0) >= 100
    ]
    near_big.sort(key=lambda row: row.get("health_factor") or 99)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "days": WINDOW_DAYS,
            "start_iso": datetime.fromtimestamp(start_ts, timezone.utc).isoformat(),
            "end_iso": datetime.fromtimestamp(end_ts, timezone.utc).isoformat(),
        },
        "counts": {
            "morpho_events": len(morpho),
            "euler_events": len(euler),
            "morpho_priced": sum(1 for event in morpho if event.get("bonus_usd") is not None),
            "euler_priced": sum(1 for event in euler if event.get("bonus_usd") is not None),
            "morpho_positive_bonus_usd": _positive_sum(morpho),
            "euler_positive_bonus_usd": _positive_sum(euler),
        },
        "by_market": _group_bonus(all_events),
        "loan_sizes": build_loan_sizes(all_events, open_meta.get("underwater") or []),
        "delay": {
            "sample_events": len(sample),
            "measured_events": len(delays),
            "censored_events": sum(1 for event in sample if event.get("delay_censored")),
            "unreadable_events": sum(1 for event in sample if event.get("delay_blocks") is None and not event.get("delay_censored")),
            "median_delay_blocks": median(delays),
            "median_delay_seconds": median(
                [event["delay_seconds"] for event in sample if event.get("delay_seconds") is not None]
            ),
            "delay_zero": sum(1 for value in delays if value == 0),
            "delay_gt_1": sum(1 for value in delays if value > 1),
            "delay_gt_5": sum(1 for value in delays if value > 5),
            "notes": sorted({event.get("delay_note") for event in sample if event.get("delay_note")}),
        },
        "gas": {
            "receipts": len(gas_values),
            "median_gas_used": median_gas,
            "median_gas_usd": median([event["gas_usd"] for event in sample if event.get("gas_usd") is not None]),
            "by_chain": {
                chain: {
                    "receipts": len([event for event in sample if event["chain"] == chain and event.get("gas_used")]),
                    "median_gas_used": median(
                        [event["gas_used"] for event in sample if event["chain"] == chain and event.get("gas_used")]
                    ),
                    "median_gas_usd": median(
                        [event["gas_usd"] for event in sample if event["chain"] == chain and event.get("gas_usd") is not None]
                    ),
                }
                for chain in ("mainnet", "base", "arbitrum")
            },
        },
        "delay_by_protocol": {
            protocol: _delay_slice([event for event in sample if event["protocol"] == protocol])
            for protocol in ("morpho-blue", "euler-v2")
        },
        "concentration": conc,
        "slowly_captured_before_slippage": slow,
        "arrival_after_winner": {
            "n_blocks": list(SLOW_BLOCKS),
            "net_usd": 0.0,
            "note": (
                "A searcher who arrives N blocks after the winner of a finished liquidation is "
                "late. These events are already closed. Profit after the winner is 0 unless a "
                "remainder was left, and remainders were not reconstructed."
            ),
        },
        "open_morpho": {
            "indexer_totals": open_meta["totals"],
            "underwater_indexed": len(open_meta["underwater"]),
            "underwater_borrow_usd_gte_50": sum(
                1 for row in open_meta["underwater"] if (row.get("borrow_usd") or 0) >= OPEN_BORROW_MIN_USD
            ),
            "dust_underwater_indexed": sum(
                1 for row in open_meta["underwater"] if (row.get("borrow_usd") or 0) < DUST_USD
            ),
            "plausible_underwater": sum(1 for row in open_meta["underwater"] if _plausible_open(row)),
            "bad_debt_on_indexer_marks": sum(
                1 for row in open_meta["underwater"]
                if (row.get("borrow_usd") or 0) >= OPEN_BORROW_MIN_USD
                and row.get("collateral_usd") is not None
                and row["collateral_usd"] < row["borrow_usd"] * 0.3
                and (row.get("borrow_usd") or 0) <= 2_000_000
            ),
            "indexer_outlier_above_2m": sum(
                1 for row in open_meta["underwater"] if (row.get("borrow_usd") or 0) > 2_000_000
            ),
            "near_indexed_hf_to_1_02": len(open_meta["near"]),
            "near_borrow_usd_gte_100": len(near_big),
            "closest_near": [
                {
                    "chain": row["chain"],
                    "collateral": row["collateral_symbol"],
                    "loan": row["loan_symbol"],
                    "health_factor": row["health_factor"],
                    "borrow_usd": row["borrow_usd"],
                    "collateral_usd": row["collateral_usd"],
                }
                for row in near_big[:8]
            ],
        },
        "quotes": open_quotes,
        "top_opportunities": opportunities[:5],
        "euler_open_sample": euler_open,
        "logs": logs,
        "delay_sample": [
            {
                "protocol": event["protocol"],
                "chain": event["chain"],
                "tx": event["tx"],
                "block": event["block"],
                "collateral": event.get("collateral_symbol"),
                "loan": event.get("loan_symbol"),
                "market_tier": event.get("market_tier"),
                "liquidator": event["liquidator"],
                "bonus_usd": event.get("bonus_usd"),
                "gas_usd": event.get("gas_usd"),
                "gas_used": event.get("gas_used"),
                "flash_fee_usd": event.get("flash_fee_usd"),
                "flash_source": event.get("flash_source"),
                "delay_blocks": event.get("delay_blocks"),
                "delay_seconds": event.get("delay_seconds"),
                "delay_censored": event.get("delay_censored"),
                "delay_blocks_at_least": event.get("delay_blocks_at_least"),
                "delay_note": event.get("delay_note"),
                "bad_debt_assets": event.get("bad_debt_assets"),
            }
            for event in sample
        ],
        "methodology": {
            "morpho_bonus": "seized * daily Morpho USD price minus repaid * daily Morpho USD price. Not the oracle price at the block, except where the delay probe read oracle.price.",
            "euler_bonus": "bonusUsd from Euler Data API v3, source field kept per event.",
            "delay": "Archive eth_call. Morpho health includes Taylor interest and virtual shares. Euler uses vault.checkLiquidation. Lookback is 7 days.",
            "sample": f"Morpho: up to {DELAY_LARGE_CAP} events per chain with daily-mark bonus >= ${DELAY_LARGE_BONUS_USD:.0f}, plus a seeded random fill to {DELAY_PER_CHAIN}. Euler: every event in the window.",
            "slippage_historical": "Not reconstructed, except the one Aerodrome quote recorded in config/liquidation-fork.json.",
            "flash_fee": "Zero only if the latest Morpho Blue or Balancer V2 balance covers the repaid amount.",
            "logs": "Moonwell and Seamless on Base were scanned for about 2 days (blocks 52908363-52994763). The Fluid Base liquidator was scanned for about 14 days (blocks 52389963-52994763). Silo on Arbitrum was one contract for about 2 days (blocks 516917727-517608927). Not 90 days.",
            "loan_size": "Per market, debt repaid and collateral seized for every priced Morpho and Euler liquidation, and outstanding debt plus collateral balance for every indexer position with health factor at or under 1. Unpriced rows are counted and omitted from the average. Moonwell events were not retained per market.",
            "no_live_sends": True,
        },
    }


def main() -> int:
    end_ts = _now()
    start_ts = end_ts - WINDOW_DAYS * 86400
    print(f"window {start_ts} .. {end_ts}", flush=True)
    morpho = fetch_morpho_liquidations(start_ts)
    euler = fetch_euler_liquidations(start_ts, end_ts)
    prices = load_prices(morpho + euler, start_ts, end_ts)
    apply_morpho_marks(morpho, prices)
    apply_morpho_marks(euler, prices)
    fetch_market_supply(morpho + euler)
    sample = select_delay_sample(morpho + euler)
    deadline = time.time() + 12 * 60
    cache: dict = {}
    rpcs = {name: archive_rpc(name) for name in ("mainnet", "base", "arbitrum")}
    for index, event in enumerate(sample):
        print(f"delay {index + 1}/{len(sample)} {event['protocol']} {event['chain']} {event['tx'][:10]}", flush=True)
        result = measure_delay(rpcs[event["chain"]], event, cache, deadline)
        event.update(result)
        if event.get("delay_blocks") not in (None, 0) or event.get("first_liquidatable_block"):
            fill_delay_seconds(rpcs[event["chain"]], event)
        if time.time() > deadline:
            print("delay budget exhausted", flush=True)
            break
    attach_gas_and_fee(sample, prices, end_ts)
    open_meta = fetch_open_morpho()
    # Quote with a fresh latest-block read inside _quote_one.
    open_quotes = []
    candidates = [row for row in open_meta["underwater"] if _plausible_open(row)]
    candidates.sort(key=lambda row: row.get("borrow_usd") or 0, reverse=True)
    cfgs = {name: load_chain(name) for name in ("mainnet", "base")}
    for row in candidates[:QUOTE_CAP]:
        print(f"quote {row['chain']} {row['collateral_symbol']}/{row['loan_symbol']} ${row['borrow_usd']:.0f}", flush=True)
        snap = morpho_health_latest(rpcs[row["chain"]], row["market_id"], row["borrower"])
        open_quotes.append(_quote_one(rpcs[row["chain"]], cfgs[row["chain"]], row, prices, snap))
    logs = scan_logs(prices)
    euler_open = euler_open_sample()
    report = build_report(morpho, euler, sample, open_quotes, open_meta, logs, euler_open, start_ts, end_ts)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2, default=str))
    print(f"wrote {OUT_PATH}", flush=True)
    delay = report["delay"]
    print(
        f"morpho {report['counts']['morpho_events']} euler {report['counts']['euler_events']} "
        f"median delay blocks {delay['median_delay_blocks']} measured {delay['measured_events']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
