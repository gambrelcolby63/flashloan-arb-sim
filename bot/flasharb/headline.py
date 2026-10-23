"""Backfill the $5,000 headline on overlooked markets. Paper only.

Reads the cached Morpho and Euler liquidations, pulls Morpho Blue on Arbitrum
for the same 90 days, and scans Moonwell, Seamless, Fluid, and one Silo
contract across that window. Delay and a pool quote are requested only for
rows with priced debt of at least $5,000 in an overlooked market. This does
not send a transaction and does not replace the full loan-size log.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eth_abi import decode

from flasharb.liqmath import median, take_size, usd
from flasharb.liquidations import (
    BLOCK_SECONDS,
    CHAIN_NAME,
    FLUID_LIQUIDATOR_BASE,
    HEADLINE_DEBT_USD,
    MOONWELL_COMPTROLLER,
    MORPHO,
    OUT_PATH,
    SEAMLESS_POOL,
    SILO_LIQUIDATION_ARB,
    TOPIC_AAVE_LIQUIDATION,
    TOPIC_LIQUIDATE_BORROW,
    TOPIC_SILO_LIQUIDATION,
    WINDOW_DAYS,
    _call,
    _decode_liq,
    _flash_fee,
    _int,
    apply_morpho_marks,
    checksum,
    fetch_euler_liquidations,
    fetch_market_supply,
    fetch_morpho_liquidations,
    fill_delay_seconds,
    gql,
    headline_debt,
    liquidator_counts,
    load_chain,
    load_prices,
    measure_delay,
    net_after_costs,
    overlooked_class,
    price_at,
)
from flasharb.rpc import Rpc, RpcError


def is_size_error(message: str) -> bool:
    """Split getLogs only when the node rejected the result size, not on 429."""
    text = message.lower()
    if any(token in text for token in ("429", "403", "rate limit", "too many requests", "timeout", "timed out", "temporarily")):
        return False
    keys = ("more than", "max results", "response size", "too large", "block range", "query returned", "exceed", "413", "log limit")
    return any(key in text for key in keys)

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "analysis" / ".cache" / "headline"
# Tenderly first. mainnet.base.org is not in this list.
RPCS = {
    "mainnet": [
        "https://mainnet.gateway.tenderly.co",
        "https://eth-mainnet.public.blastapi.io",
        "https://eth.drpc.org",
    ],
    "base": [
        "https://base.gateway.tenderly.co",
        "https://base.publicnode.com",
        "https://base.drpc.org",
    ],
    "arbitrum": [
        "https://arb1.arbitrum.io/rpc",
        "https://arbitrum.publicnode.com",
        "https://arbitrum.gateway.tenderly.co",
    ],
}


def _rpc(chain: str) -> Rpc:
    urls = list(RPCS[chain])
    # Base Tenderly's public endpoint only serves 1,000-block log ranges.
    # Publicnode serves about 45 days of Base logs in large ranges.
    if chain == "base":
        urls = [
            "https://base.publicnode.com",
            "https://base.drpc.org",
            "https://base.gateway.tenderly.co",
        ]
    return Rpc(urls[0], timeout=45.0, backups=urls[1:])


def _archive_stop(message: str) -> bool:
    text = message.lower()
    return any(token in text for token in ("403", "archive", "personal token", "free plan", "maximum 1000", "10,000", "10000"))


def _head(chain: str) -> tuple[int, int]:
    rpc = _rpc(chain)
    number = rpc.block_number()
    stamp = int(rpc.block(number)["timestamp"], 16)
    return number, stamp


def fetch_logs_backward(
    rpc: Rpc,
    address: str | None,
    topic0: str | None,
    target_start: int,
    end: int,
    chunk: int,
) -> dict:
    """Walk from the head backward. Stop when a free node refuses older blocks.

    A stopped window is not an empty window. Callers must report `oldest`.
    """
    logs: list[dict] = []
    gaps: list[dict] = []
    pos = end
    oldest = end + 1
    stopped = None
    topics: list[Any] = [topic0] if topic0 else []
    while pos >= target_start:
        left = max(target_start, pos - chunk + 1)
        try:
            part = rpc.get_logs([address] if address else None, topics, left, pos)
        except RpcError as exc:
            if is_size_error(str(exc)) and pos - left >= 200:
                mid = (left + pos) // 2
                # Retry the two halves once; a further archive error stops the walk.
                try:
                    part = rpc.get_logs([address] if address else None, topics, left, mid)
                    logs.extend(part)
                    part = rpc.get_logs([address] if address else None, topics, mid + 1, pos)
                    logs.extend(part)
                    oldest = min(oldest, left)
                    pos = left - 1
                    continue
                except RpcError as retry:
                    if _archive_stop(str(retry)):
                        stopped = {"block": left, "error": str(retry)[:180]}
                        break
                    gaps.append({"start": left, "end": pos, "error": str(retry)[:180]})
                    break
            if _archive_stop(str(exc)):
                stopped = {"block": left, "error": str(exc)[:180]}
                break
            gaps.append({"start": left, "end": pos, "error": str(exc)[:180]})
            break
        logs.extend(part)
        oldest = min(oldest, left)
        pos = left - 1
        time.sleep(0.03)
    return {"logs": logs, "oldest": oldest if oldest <= end else None, "target_start": target_start, "end": end, "stopped": stopped, "gaps": gaps}


def fetch_logs(rpc: Rpc, address: str | None, topic0: str | None, start: int, end: int, chunk: int) -> tuple[list[dict], list[dict]]:
    """Split on result-size errors only. A 429 stays a gap."""
    logs: list[dict] = []
    gaps: list[dict] = []
    pos = start
    while pos <= end:
        hi = min(end, pos + chunk - 1)
        pending = [(pos, hi)]
        while pending:
            left, right = pending.pop()
            topics: list[Any] = [topic0] if topic0 else []
            try:
                part = rpc.get_logs([address] if address else None, topics, left, right)
            except RpcError as exc:
                if is_size_error(str(exc)) and right - left >= 200:
                    mid = (left + right) // 2
                    pending.append((mid + 1, right))
                    pending.append((left, mid))
                    continue
                time.sleep(1.0)
                try:
                    part = rpc.get_logs([address] if address else None, topics, left, right)
                except RpcError as retry:
                    gaps.append({"start": left, "end": right, "error": str(retry)[:180]})
                    continue
            if isinstance(part, list):
                logs.extend(part)
        pos = hi + 1
        time.sleep(0.05)
    return logs, gaps


def _window_blocks(chain: str, start_ts: int) -> dict:
    head, head_ts = _head(chain)
    span = int((head_ts - start_ts) / BLOCK_SECONDS[chain])
    return {"head": head, "head_ts": head_ts, "start": max(1, head - span), "end": head}


def _cached_json(name: str) -> Any | None:
    path = CACHE / name
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _save_json(name: str, payload: Any) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / name).write_text(json.dumps(payload, default=str))


def fetch_morpho_chain(start_ts: int, chain_id: int) -> list[dict]:
    name = f"morpho-{chain_id}.json"
    cached = _cached_json(name)
    if isinstance(cached, dict) and cached.get("start_ts") == start_ts and cached.get("events") is not None:
        return cached["events"]
    query = """
    query($first: Int!, $skip: Int!, $ts: Int!, $chain: Int!) {
      marketTransactions(
        first: $first
        skip: $skip
        orderBy: Timestamp
        orderDirection: Desc
        where: { type_in: [Liquidation], chainId_in: [$chain], timestamp_gte: $ts }
      ) {
        pageInfo { countTotal }
        items {
          txHash blockNumber timestamp logIndex
          chain { id }
          user { address }
          market {
            marketId lltv
            collateralAsset { symbol address decimals }
            loanAsset { symbol address decimals }
          }
          data {
            ... on MarketTransactionLiquidationData {
              liquidator repaidAssets repaidShares seizedAssets badDebtAssets badDebtShares
            }
          }
        }
      }
    }
    """
    events: list[dict] = []
    skip = 0
    total = None
    while skip < 20000:
        data = gql(query, {"first": 100, "skip": skip, "ts": start_ts, "chain": chain_id})
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
                "chain_id": chain_id,
                "chain": CHAIN_NAME[chain_id],
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
        print(f"morpho chain {chain_id} {skip}/{total}", flush=True)
        if total is not None and skip >= total:
            break
    dedup = {(event["tx"], event["log_index"]): event for event in events}
    events = list(dedup.values())
    _save_json(name, {"start_ts": start_ts, "events": events, "reported_total": total})
    return events


def _ensure_prices(prices: dict, events: list[dict], start_ts: int, end_ts: int) -> None:
    wanted: set[tuple[int, str]] = set()
    for event in events:
        for key in ("collateral_token", "loan_token"):
            token = event.get(key)
            if token and (event["chain_id"], token.lower()) not in prices:
                wanted.add((event["chain_id"], checksum(token)))
    if not wanted:
        return
    query = """
    query($address: String!, $chainId: Int!, $start: Int!, $end: Int!) {
      assetByAddress(address: $address, chainId: $chainId) {
        symbol
        price { usd }
        historicalPriceUsd(options: {startTimestamp: $start, endTimestamp: $end, interval: DAY}) { x y }
      }
    }
    """
    for index, (chain_id, token) in enumerate(sorted(wanted)):
        try:
            data = gql(query, {"address": token, "chainId": chain_id, "start": start_ts, "end": end_ts})
        except Exception as exc:  # noqa: BLE001
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
        if index % 20 == 0:
            print(f"extra prices {index}/{len(wanted)}", flush=True)
        time.sleep(0.05)


def _scan_compound_like(protocol: str, chain: str, chain_id: int, addresses: list[str], topic: str, kind: str, start: int, end: int, chunk: int) -> dict:
    cache_name = f"logs-{protocol}-{start}-{end}.json"
    cached = _cached_json(cache_name)
    if isinstance(cached, dict) and cached.get("events") is not None:
        return cached
    rpc = _rpc(chain)
    events: list[dict] = []
    gaps: list[dict] = []
    oldest = None
    stopped = None
    for address in addresses:
        if chain == "base":
            walked = fetch_logs_backward(rpc, address, topic, start, end, chunk)
            logs = walked["logs"]
            gaps.extend(walked["gaps"])
            if walked["oldest"] is not None:
                oldest = walked["oldest"] if oldest is None else max(oldest, walked["oldest"])
            if walked["stopped"] and stopped is None:
                stopped = walked["stopped"]
        else:
            logs, part_gaps = fetch_logs(rpc, address, topic, start, end, chunk)
            gaps.extend(part_gaps)
            oldest = start
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
            })
            if protocol == "seamless" and decoded.get("collateral_token") and decoded.get("loan_token"):
                decoded["market_id"] = f"{decoded['collateral_token'].lower()}-{decoded['loan_token'].lower()}"
            elif not decoded.get("market_id"):
                decoded["market_id"] = address.lower()
            events.append(decoded)
        print(f"{protocol} {address[:10]} events {len(events)} gaps {len(gaps)} oldest {oldest}", flush=True)
    payload = {
        "events": events,
        "gaps": gaps[:20],
        "gap_count": len(gaps),
        "start": start,
        "end": end,
        "oldest_scanned": oldest,
        "stopped": stopped,
    }
    _save_json(cache_name, payload)
    return payload


def _moonwell(start: int, end: int, prices: dict) -> dict:
    cache_name = f"moonwell-{start}-{end}.json"
    cached = _cached_json(cache_name)
    if isinstance(cached, dict) and cached.get("events") is not None:
        return cached
    rpc = _rpc("base")
    try:
        raw = _call(rpc, MOONWELL_COMPTROLLER, "getAllMarkets()", [], [], "latest")
        markets = [checksum(item) for item in decode(["address[]"], raw)[0]]
    except RpcError as exc:
        return {"events": [], "error": str(exc)[:200], "gaps": []}
    underlyings = {}
    for market in markets:
        try:
            underlying = decode(["address"], _call(rpc, market, "underlying()", [], [], "latest"))[0]
            decimals = decode(["uint8"], _call(rpc, underlying, "decimals()", [], [], "latest"))[0]
            underlyings[market.lower()] = {"token": checksum(underlying), "decimals": int(decimals)}
        except RpcError:
            continue
    scanned = _scan_compound_like("moonwell", "base", 8453, markets, TOPIC_LIQUIDATE_BORROW, "compound", start, end, 40_000)
    # _scan_compound_like caches before we attach underlyings. Re-apply every run from its events.
    events = []
    for event in scanned["events"]:
        info = underlyings.get((event.get("loan_token") or "").lower())
        row = dict(event)
        if info:
            row["loan_token"] = info["token"]
            row["loan_decimals"] = info["decimals"]
            row["market_id"] = (event.get("market_id") or "").lower()
        events.append(row)
    payload = {
        "events": events,
        "gaps": scanned.get("gaps"),
        "gap_count": scanned.get("gap_count"),
        "markets": len(markets),
        "start": start,
        "end": end,
        "oldest_scanned": scanned.get("oldest_scanned"),
        "stopped": scanned.get("stopped"),
    }
    _save_json(cache_name, payload)
    return payload


def _fill_decimals(events: list[dict]) -> None:
    cache: dict[tuple[str, str], int] = {}
    rpcs: dict[str, Rpc] = {}
    for event in events:
        chain = event["chain"]
        rpcs.setdefault(chain, _rpc(chain))
        for token_key, dec_key in (("loan_token", "loan_decimals"), ("collateral_token", "collateral_decimals")):
            token = event.get(token_key)
            if not token or event.get(dec_key) is not None:
                continue
            key = (chain, token.lower())
            if key not in cache:
                try:
                    cache[key] = int(decode(["uint8"], _call(rpcs[chain], token, "decimals()", [], [], "latest"))[0])
                except (RpcError, Exception):  # noqa: BLE001
                    cache[key] = -1
            if cache[key] >= 0:
                event[dec_key] = cache[key]


def _price_log_events(events: list[dict], prices: dict) -> None:
    for event in events:
        if event.get("protocol") == "moonwell":
            event["seized_usd"] = None
            event["seize_note"] = "seizeTokens is an mToken amount; collateral seized was not marked"
        loan_px = price_at(prices, event["chain_id"], event.get("loan_token"), event.get("timestamp") or 0)
        coll_px = price_at(prices, event["chain_id"], event.get("collateral_token"), event.get("timestamp") or 0)
        if event.get("loan_decimals") is not None:
            event["repaid_usd"] = usd(int(event.get("repaid_assets") or 0), int(event["loan_decimals"]), loan_px)
        if event.get("protocol") != "moonwell" and event.get("collateral_decimals") is not None:
            event["seized_usd"] = usd(int(event.get("seized_assets") or 0), int(event["collateral_decimals"]), coll_px)
        event["price_source"] = "daily_morpho_price"
        event.setdefault("collateral_symbol", None)
        event.setdefault("loan_symbol", None)


def _receipt_gas(rpc: Rpc, event: dict, prices: dict) -> None:
    try:
        receipt = rpc.request("eth_getTransactionReceipt", [event["tx"]])
    except RpcError as exc:
        event["gas_usd"] = None
        event["gas_note"] = str(exc)[:160]
        return
    if not receipt:
        event["gas_usd"] = None
        event["gas_note"] = "receipt missing"
        return
    gas_used = int(receipt["gasUsed"], 16)
    effective = int(receipt.get("effectiveGasPrice") or "0x0", 16)
    l1_fee = int(receipt["l1Fee"], 16) if receipt.get("l1Fee") else 0
    from flasharb.liquidations import WETH

    eth_price = price_at(prices, event["chain_id"], WETH[event["chain_id"]], event.get("timestamp") or 0)
    event["gas_used"] = gas_used
    event["eth_price_usd"] = eth_price
    if eth_price is None:
        event["gas_usd"] = None
        event["gas_note"] = "no ETH price for that day"
    else:
        event["gas_usd"] = (gas_used * effective + l1_fee) / 1e18 * eth_price
        event["gas_note"] = "winner receipt, including that transaction's route, not a bare liquidate"


def _quote_event(rpc: Rpc, cfg: dict, event: dict, prices: dict) -> None:
    block = max(1, int(event["block"]) - 1)
    event["quote_block"] = block
    event["quote_note"] = "quoter and reserves at the end of the block before the liquidation; the winner's own swap is not in that state"
    if event.get("protocol") == "moonwell":
        event["dex_out_usd"] = None
        event["dex_note"] = "seizeTokens is an mToken amount; no swap quote"
        event["net_usd"] = None
        event["net_missing"] = ["slippage_quote"]
        return
    seized = int(event.get("seized_assets") or 0)
    if seized <= 0 or not event.get("collateral_token") or not event.get("loan_token"):
        event["dex_out_usd"] = None
        event["dex_note"] = "no seized collateral or token address"
        return
    from flasharb.liquidations import _best_swap

    dex = _best_swap(rpc, cfg, event["collateral_token"], event["loan_token"], seized, block)
    event["dex"] = {key: dex.get(key) for key in ("amount_out", "kind", "venue", "fee", "fee_bps", "note", "pool")}
    if dex.get("amount_out") is None:
        event["dex_out_usd"] = None
        event["dex_note"] = dex.get("note")
        return
    loan_px = price_at(prices, event["chain_id"], event["loan_token"], event.get("timestamp") or 0)
    event["dex_out_usd"] = usd(int(dex["amount_out"]), int(event["loan_decimals"]), loan_px)
    fee = _flash_fee(rpc, cfg, event["loan_token"], int(event["repaid_assets"]), block)
    event.update(fee)
    net, missing = net_after_costs(event.get("dex_out_usd"), event.get("repaid_usd"), event.get("gas_usd"), event.get("flash_fee_usd"))
    event["net_usd"] = net
    event["net_missing"] = missing


def _summarize(events: list[dict]) -> dict:
    groups: dict[tuple, dict] = {}
    competitors = liquidator_counts(events)
    for event in events:
        key = (event.get("protocol"), event.get("chain"), (event.get("market_id") or "unknown").lower())
        bucket = groups.setdefault(key, {
            "protocol": event.get("protocol"),
            "chain": event.get("chain"),
            "market_id": event.get("market_id"),
            "collateral": event.get("collateral_symbol") or event.get("collateral"),
            "loan": event.get("loan_symbol") or event.get("loan"),
            "tier": event.get("market_tier"),
            "overlooked": event.get("overlooked"),
            "n": 0,
            "repaid": [],
            "delays": [],
            "delay_seconds": [],
            "censored": 0,
            "delay_unread": 0,
            "nets": [],
            "net_unquoted": 0,
        })
        bucket["n"] += 1
        if event.get("repaid_usd") is not None:
            bucket["repaid"].append(event["repaid_usd"])
        if event.get("delay_blocks") is not None:
            bucket["delays"].append(event["delay_blocks"])
        elif event.get("delay_censored"):
            bucket["censored"] += 1
        else:
            bucket["delay_unread"] += 1
        if event.get("delay_seconds") is not None:
            bucket["delay_seconds"].append(event["delay_seconds"])
        if event.get("net_usd") is None:
            bucket["net_unquoted"] += 1
        else:
            bucket["nets"].append(event["net_usd"])
    rows = []
    for key, bucket in groups.items():
        comp = competitors.get(key) or {}
        rows.append({
            "protocol": bucket["protocol"],
            "chain": bucket["chain"],
            "market_id": bucket["market_id"],
            "collateral": bucket["collateral"],
            "loan": bucket["loan"],
            "tier": bucket["tier"],
            "overlooked": bucket["overlooked"],
            "liquidations": bucket["n"],
            "repaid_usd_average": (sum(bucket["repaid"]) / len(bucket["repaid"])) if bucket["repaid"] else None,
            "repaid_usd_median": median(bucket["repaid"]),
            "repaid_usd_sum": sum(bucket["repaid"]) if bucket["repaid"] else None,
            "distinct_liquidators": comp.get("distinct_liquidators"),
            "liquidators": comp.get("liquidators"),
            "delay_measured": len(bucket["delays"]),
            "delay_censored_at_7d": bucket["censored"],
            "delay_unread": bucket["delay_unread"],
            "median_delay_blocks": median(bucket["delays"]),
            "median_delay_seconds": median(bucket["delay_seconds"]),
            "delay_gt_1": sum(1 for value in bucket["delays"] if value > 1),
            "delay_gt_5": sum(1 for value in bucket["delays"] if value > 5),
            "quoted": len(bucket["nets"]),
            "net_unquoted": bucket["net_unquoted"],
            "net_usd_average": (sum(bucket["nets"]) / len(bucket["nets"])) if bucket["nets"] else None,
            "net_usd_median": median(bucket["nets"]),
            "net_positive": sum(1 for value in bucket["nets"] if value > 0),
            "net_positive_usd": sum(value for value in bucket["nets"] if value > 0),
        })
    rows.sort(key=lambda row: (-(row["repaid_usd_sum"] or 0), row["protocol"] or "", row["market_id"] or ""))
    return {"markets": rows}


def _known_delays() -> dict[str, dict]:
    if not OUT_PATH.exists():
        return {}
    data = json.loads(OUT_PATH.read_text())
    found = {}
    for row in data.get("delay_sample") or []:
        if row.get("tx"):
            found[row["tx"].lower()] = row
    return found


def _apply_known_delay(event: dict, known: dict) -> bool:
    row = known.get((event.get("tx") or "").lower())
    if not row:
        return False
    if row.get("delay_blocks") is None and not row.get("delay_censored"):
        return False
    for key in ("delay_blocks", "delay_seconds", "delay_censored", "delay_blocks_at_least", "delay_note"):
        if key in row:
            event[key] = row[key]
    event["delay_source"] = "existing delay sample; not remeasured"
    return True


def _open_positions(start_ts: int) -> list[dict]:
    """Indexer positions with health factor <= 1 on mainnet, Base, and Arbitrum."""
    cached = _cached_json("open-underwater.json")
    if isinstance(cached, dict) and time.time() - float(cached.get("saved_at") or 0) < 6 * 3600:
        return cached["positions"]
    query = """
    query($first: Int!, $skip: Int!, $where: MarketPositionFilters!) {
      marketPositions(first: $first, skip: $skip, orderBy: HealthFactor, orderDirection: Asc, where: $where) {
        pageInfo { countTotal }
        items {
          healthFactor
          user { address }
          market {
            marketId lltv
            collateralAsset { symbol address decimals }
            loanAsset { symbol address decimals }
          }
          state { collateral collateralUsd borrowAssets borrowAssetsUsd }
        }
      }
    }
    """
    rows = []
    totals = {}
    for chain_id in (1, 8453, 42161):
        skip = 0
        total = None
        while skip < 5000:
            data = gql(query, {"first": 100, "skip": skip, "where": {"chainId_in": [chain_id], "healthFactor_lte": 1}})
            page = data["marketPositions"]
            total = page["pageInfo"]["countTotal"]
            items = page["items"] or []
            for item in items:
                state = item.get("state") or {}
                market = item["market"]
                collateral = market.get("collateralAsset") or {}
                loan = market.get("loanAsset") or {}
                rows.append({
                    "protocol": "morpho-blue",
                    "chain_id": chain_id,
                    "chain": CHAIN_NAME[chain_id],
                    "borrower": item["user"]["address"],
                    "market_id": market["marketId"],
                    "lltv": _int(market["lltv"]),
                    "health_factor": item.get("healthFactor"),
                    "collateral_symbol": collateral.get("symbol"),
                    "collateral_token": collateral.get("address"),
                    "collateral_decimals": int(collateral["decimals"]) if collateral.get("decimals") is not None else None,
                    "loan_symbol": loan.get("symbol"),
                    "loan_token": loan.get("address"),
                    "loan_decimals": int(loan["decimals"]) if loan.get("decimals") is not None else None,
                    "collateral_raw": _int(state.get("collateral")),
                    "collateral_usd": state.get("collateralUsd"),
                    "borrow_usd": state.get("borrowAssetsUsd"),
                    "borrow_assets": _int(state.get("borrowAssets")),
                })
            if not items or (total is not None and skip + len(items) >= total):
                break
            skip += len(items)
        totals[chain_id] = total
        print(f"open chain {chain_id} indexed {total} kept {sum(1 for row in rows if row['chain_id']==chain_id)}", flush=True)
    _save_json("open-underwater.json", {"saved_at": time.time(), "start_ts": start_ts, "totals": totals, "positions": rows})
    return rows


def _stamp_events(events: list[dict]) -> None:
    """Attach a block timestamp when the log scanner did not have one."""
    needed: dict[str, list[dict]] = {}
    for event in events:
        if event.get("timestamp"):
            continue
        needed.setdefault(event["chain"], []).append(event)
    for chain, rows in needed.items():
        rpc = _rpc(chain)
        cache: dict[int, int] = {}
        for event in rows:
            block = int(event["block"])
            if block not in cache:
                try:
                    cache[block] = int(rpc.block(block)["timestamp"], 16)
                except RpcError:
                    head, head_ts = _head(chain)
                    cache[block] = int(head_ts - (head - block) * BLOCK_SECONDS[chain])
                    event["timestamp_note"] = "approximate from head and block time"
            event["timestamp"] = cache[block]


def run() -> dict:
    report_path = OUT_PATH
    existing = json.loads(report_path.read_text())
    start_ts = int(existing["window"]["start_ts"])
    end_ts = int(existing["window"]["end_ts"])
    print("load cached morpho and euler", flush=True)
    morpho = fetch_morpho_liquidations(start_ts)
    euler = fetch_euler_liquidations(start_ts, end_ts)
    prices = load_prices(morpho + euler, start_ts, end_ts)
    apply_morpho_marks(morpho, prices)
    apply_morpho_marks(euler, prices)
    fetch_market_supply(morpho)
    print("morpho arbitrum", flush=True)
    arb = fetch_morpho_chain(start_ts, 42161)
    _ensure_prices(prices, arb, start_ts, end_ts)
    apply_morpho_marks(arb, prices)
    fetch_market_supply(arb)

    print("log windows", flush=True)
    base_win = _window_blocks("base", start_ts)
    arb_win = _window_blocks("arbitrum", start_ts)
    moon = _moonwell(base_win["start"], base_win["end"], prices)
    seamless = _scan_compound_like(
        "seamless", "base", 8453, [SEAMLESS_POOL], TOPIC_AAVE_LIQUIDATION, "aave",
        base_win["start"], base_win["end"], 40_000,
    )
    fluid_cache = f"fluid-{base_win['start']}-{base_win['end']}.json"
    fluid_saved = _cached_json(fluid_cache)
    if isinstance(fluid_saved, dict) and "oldest_scanned" in fluid_saved:
        fluid_walk = fluid_saved
    else:
        fluid_walk = fetch_logs_backward(_rpc("base"), FLUID_LIQUIDATOR_BASE, None, base_win["start"], base_win["end"], 40_000)
        _save_json(fluid_cache, {
            "logs": [{"topics": log.get("topics"), "blockNumber": log.get("blockNumber")} for log in fluid_walk["logs"]],
            "gaps": fluid_walk["gaps"],
            "oldest_scanned": fluid_walk["oldest"],
            "stopped": fluid_walk["stopped"],
        })
        fluid_walk["logs"] = fluid_walk["logs"]
    fluid_logs = fluid_walk.get("logs") or []
    fluid_gaps = fluid_walk.get("gaps") or []
    silo = _scan_compound_like(
        "silo", "arbitrum", 42161, [SILO_LIQUIDATION_ARB], TOPIC_SILO_LIQUIDATION, "silo",
        arb_win["start"], arb_win["end"], 400_000,
    )
    log_events = list(moon.get("events") or []) + list(seamless.get("events") or []) + list(silo.get("events") or [])
    _fill_decimals(log_events)
    _stamp_events(log_events)
    _ensure_prices(prices, log_events, start_ts, end_ts)
    _price_log_events(log_events, prices)

    all_events = list(morpho) + list(euler) + list(arb) + log_events
    for event in all_events:
        event["overlooked"] = overlooked_class(event.get("protocol"), event.get("chain"), event.get("market_tier"))
    headline = [
        event for event in all_events
        if event.get("overlooked") in {"euler-v2", "morpho-base-small", "morpho-arbitrum-isolated", "moonwell", "seamless", "fluid", "silo"}
        and headline_debt(event.get("repaid_usd"))
    ]
    unknown = [
        event for event in all_events
        if event.get("overlooked") == "morpho-base-supply-unknown" and headline_debt(event.get("repaid_usd"))
    ]
    print(f"headline events {len(headline)} supply-unknown {len(unknown)}", flush=True)

    known = _known_delays()
    cfgs = {name: load_chain(name) for name in ("mainnet", "base", "arbitrum")}
    rpcs = {name: _rpc(name) for name in cfgs}
    deadline = time.time() + 20 * 60
    headline.sort(key=lambda event: event.get("repaid_usd") or 0, reverse=True)
    for index, event in enumerate(headline):
        print(f"measure {index + 1}/{len(headline)} {event['protocol']} {event['chain']} ${event.get('repaid_usd'):.0f}", flush=True)
        if event.get("protocol") not in {"morpho-blue", "euler-v2"}:
            event["delay_note"] = "delay search is implemented for Morpho and Euler only"
        elif not _apply_known_delay(event, known):
            if time.time() > deadline:
                event["delay_note"] = "not measured; time budget spent on larger rows"
            else:
                result = measure_delay(rpcs[event["chain"]], event, {}, deadline)
                event.update(result)
                if event.get("delay_blocks") not in (None, 0) or event.get("first_liquidatable_block"):
                    fill_delay_seconds(rpcs[event["chain"]], event)
                event["delay_source"] = "archive eth_call for this headline row"
        _receipt_gas(rpcs[event["chain"]], event, prices)
        if time.time() > deadline and event.get("dex_out_usd") is None:
            event["dex_out_usd"] = None
            event["dex_note"] = "not quoted; time budget spent on larger rows"
            event["net_usd"] = None
            event["net_missing"] = ["slippage_quote"]
            continue
        _quote_event(rpcs[event["chain"]], cfgs[event["chain"]], event, prices)

    summary = _summarize(headline)
    delays = [event["delay_blocks"] for event in headline if event.get("delay_blocks") is not None]
    nets = [event["net_usd"] for event in headline if event.get("net_usd") is not None]
    slow_positive = [
        event for event in headline
        if event.get("net_usd") is not None and event["net_usd"] > 0
        and (
            (event.get("delay_blocks") is not None and event["delay_blocks"] > 5)
            or (event.get("delay_censored") and (event.get("delay_blocks_at_least") or 0) > 5)
        )
    ]
    open_rows = _open_positions(start_ts)
    fetch_market_supply(open_rows)
    sitting = []
    for row in open_rows:
        row["overlooked"] = overlooked_class(row["protocol"], row["chain"], row.get("market_tier"))
        if row["overlooked"] not in {"morpho-base-small", "morpho-arbitrum-isolated"}:
            continue
        if not headline_debt(row.get("borrow_usd")):
            continue
        collateral = row.get("collateral_usd")
        borrow = row.get("borrow_usd") or 0
        if borrow > 2_000_000 or collateral is None or collateral < borrow * 0.3:
            if borrow > 2_000_000:
                row["open_note"] = "debt mark above $2 million; not treated as a sitting loan"
            elif collateral is None:
                row["open_note"] = "no collateral mark; not treated as a sitting loan"
            else:
                row["open_note"] = "collateral mark under 30% of debt; not treated as a sitting loan"
            sitting.append(row)
            continue
        snap = None
        try:
            from flasharb.liquidations import morpho_health_latest

            snap = morpho_health_latest(rpcs[row["chain"]], row["market_id"], row["borrower"])
        except Exception as exc:  # noqa: BLE001
            snap = {"error": str(exc)[:160]}
        row["on_chain_unhealthy"] = None if not snap or snap.get("error") else bool(snap.get("unhealthy"))
        if not snap or snap.get("error") or not snap.get("unhealthy"):
            row["open_note"] = "indexer underwater but not confirmed unhealthy on chain" if snap and not snap.get("error") else "health read failed"
            sitting.append(row)
            continue
        fake = {
            "protocol": "morpho-blue",
            "chain": row["chain"],
            "block": rpcs[row["chain"]].block_number(),
            "market_id": row["market_id"],
            "borrower": row["borrower"],
        }
        delay = measure_delay(rpcs[row["chain"]], fake, {}, time.time() + 180)
        row["sitting_delay_blocks"] = delay.get("delay_blocks")
        row["sitting_delay_censored"] = delay.get("delay_censored")
        row["sitting_delay_blocks_at_least"] = delay.get("delay_blocks_at_least")
        row["sitting_note"] = delay.get("delay_note")
        take = take_size(snap["collateral"], snap["borrowed"], snap["price"], snap["lltv"])
        row["repay_assets"] = take["repay"]
        row["seize_assets"] = take["seize"]
        loan_px = price_at(prices, row["chain_id"], row.get("loan_token"), int(time.time()))
        row["repay_usd_on_chain"] = usd(int(take["repay"]), int(row["loan_decimals"] or 18), loan_px)
        from flasharb.liquidations import _best_swap

        dex = _best_swap(rpcs[row["chain"]], cfgs[row["chain"]], row["collateral_token"], row["loan_token"], int(take["seize"]))
        row["dex_out_usd"] = usd(dex["amount_out"], int(row["loan_decimals"] or 18), loan_px) if dex.get("amount_out") is not None else None
        row["dex_note"] = dex.get("note")
        sitting.append(row)

    def _public(event: dict) -> dict:
        keep = (
            "protocol", "chain", "market_id", "collateral_symbol", "loan_symbol", "market_tier", "overlooked",
            "tx", "block", "liquidator", "repaid_usd", "seized_usd", "delay_blocks", "delay_seconds",
            "delay_censored", "delay_blocks_at_least", "delay_note", "delay_source", "gas_usd", "gas_note",
            "dex_out_usd", "dex_note", "quote_block", "quote_note", "flash_fee_usd", "flash_source",
            "flash_fee_note", "net_usd", "net_missing",
        )
        return {key: event.get(key) for key in keep if event.get(key) is not None}

    payload = {
        "debt_min_usd": HEADLINE_DEBT_USD,
        "definition": (
            "Overlooked means Euler v2 on every chain, Morpho Blue on Base whose current supply "
            "is under $500,000, Morpho Blue on Arbitrum (isolated markets), Moonwell, Seamless, "
            "the Fluid liquidator on Base, and one Silo liquidation contract on Arbitrum. "
            "Head Morpho markets and mainnet Morpho are logged and are not this headline. "
            "A missing price is not treated as under $5,000."
        ),
        "supply_tier_is": "current Morpho supply, not supply on the liquidation day",
        "quote_is": "direct Uniswap v2, Solidly, or Uniswap v3 quote of the seized collateral into the loan token at the previous block. Slipstream was not searched. USD uses the daily loan-token mark.",
        "window": existing["window"],
        "log_windows": {
            "base": base_win,
            "arbitrum": arb_win,
            "moonwell_markets": moon.get("markets"),
            "moonwell_events": len(moon.get("events") or []),
            "moonwell_priced_debt": sum(1 for event in moon.get("events") or [] if event.get("repaid_usd") is not None),
            "moonwell_gaps": moon.get("gap_count"),
            "moonwell_oldest_scanned": moon.get("oldest_scanned"),
            "moonwell_stopped": moon.get("stopped"),
            "base_log_limit": "Publicnode serves about 45 days of Base logs. Tenderly's public Base endpoint allows 1,000 blocks per getLogs. The older half of the 90-day window was not scanned. A stop is not zero events.",
            "seamless_events": len(seamless.get("events") or []),
            "seamless_priced_debt": sum(1 for event in seamless.get("events") or [] if event.get("repaid_usd") is not None),
            "seamless_gaps": seamless.get("gap_count"),
            "fluid_logs": len(fluid_logs),
            "fluid_gaps": len(fluid_gaps),
            "silo_events": len(silo.get("events") or []),
            "silo_priced_debt": sum(1 for event in silo.get("events") or [] if event.get("repaid_usd") is not None),
            "silo_gaps": silo.get("gap_count"),
            "note": "A scanned event with no USD price is not counted as under $5,000. Silo logs from this contract do not carry token addresses, so a non-zero silo count with zero priced debt is unmeasured, not a claim that every repay was small.",
        },
        "counts": {
            "headline_events": len(headline),
            "by_class": {
                name: sum(1 for event in headline if event.get("overlooked") == name)
                for name in ("euler-v2", "morpho-base-small", "morpho-arbitrum-isolated", "moonwell", "seamless", "silo", "fluid")
            },
            "supply_unknown_base_ge_5k": len(unknown),
            "delay_measured": len(delays),
            "delay_zero": sum(1 for value in delays if value == 0),
            "delay_gt_1": sum(1 for value in delays if value > 1),
            "delay_gt_5": sum(1 for value in delays if value > 5),
            "delay_censored": sum(1 for event in headline if event.get("delay_censored")),
            "quoted": len(nets),
            "net_positive": sum(1 for value in nets if value > 0),
            "net_positive_usd": sum(value for value in nets if value > 0),
            "slow_and_positive_after_quote": len(slow_positive),
            "slow_and_positive_usd": sum(event["net_usd"] for event in slow_positive),
        },
        "by_market": summary["markets"],
        "events": [_public(event) for event in headline],
        "supply_unknown_events": [_public(event) for event in unknown],
        "open_overlooked_ge_5k": [
            {
                "chain": row.get("chain"),
                "market_id": row.get("market_id"),
                "collateral": row.get("collateral_symbol"),
                "loan": row.get("loan_symbol"),
                "tier": row.get("market_tier"),
                "borrower": row.get("borrower"),
                "borrow_usd": row.get("borrow_usd"),
                "collateral_usd": row.get("collateral_usd"),
                "on_chain_unhealthy": row.get("on_chain_unhealthy"),
                "sitting_delay_blocks": row.get("sitting_delay_blocks"),
                "sitting_delay_censored": row.get("sitting_delay_censored"),
                "sitting_delay_blocks_at_least": row.get("sitting_delay_blocks_at_least"),
                "repay_usd_on_chain": row.get("repay_usd_on_chain"),
                "dex_out_usd": row.get("dex_out_usd"),
                "dex_note": row.get("dex_note") or row.get("open_note") or row.get("sitting_note"),
            }
            for row in sitting
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    existing["headline_5k"] = payload
    report_path.write_text(json.dumps(existing, indent=2, default=str) + "\n")
    _save_json("headline-result.json", payload)
    print(json.dumps(payload["counts"], indent=2), flush=True)
    return payload


if __name__ == "__main__":
    raise SystemExit(run())
