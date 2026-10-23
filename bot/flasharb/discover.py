"""Discover long-tail and head pools and pull the logs needed to replay them.

Reads go through a public (or env-provided) JSON-RPC. Nothing here signs or sends.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eth_abi import encode

from flasharb.abiutil import (
    TOPICS,
    ZERO,
    as_int256,
    calldata,
    checksum,
    decode_aggregate,
    norm,
    selector,
    word_address,
    words,
)
from flasharb.quotes import Pool, State
from flasharb.rpc import Rpc, RpcError

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"

# One router per constant-product factory, used only as a quote spot-check.
ROUTERS = {
    "uniswap_v2": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    "sushiswap": {
        "mainnet": "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F",
        "arbitrum": "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506",
    },
    "baseswap": "0x327Df1E6de05895d2ab08513aaDD9313Fe505d86",
}

RPC_FALLBACKS = {
    "mainnet": [
        "https://ethereum-rpc.publicnode.com",
        "https://1rpc.io/eth",
        "https://eth.drpc.org",
    ],
    "base": [
        "https://mainnet.base.org",
        "https://base.publicnode.com",
        "https://base.drpc.org",
        "https://1rpc.io/base",
    ],
    "arbitrum": [
        "https://arb1.arbitrum.io/rpc",
        "https://arbitrum.publicnode.com",
        "https://arbitrum.drpc.org",
        "https://1rpc.io/arb",
    ],
}

STABLE_SYMBOLS = {"USDC", "USDT", "DAI", "USDbC", "USDC.e"}
CONCENTRATED = {"univ3", "slipstream"}


def load_chain(name: str) -> dict[str, Any]:
    path = CONFIG / "chains" / f"{name}.json"
    cfg = json.loads(path.read_text())
    cfg["tokens"] = [
        {**t, "address": norm(t["address"])}
        for t in cfg["tokens"]
    ]
    for key in ("aave_pool", "balancer_vault", "multicall3", "quoter_v2", "weth", "usdc"):
        if cfg.get(key):
            cfg[key] = norm(cfg[key])
    for fac in cfg["factories"]:
        fac["address"] = norm(fac["address"])
    return cfg


def token_map(cfg: dict[str, Any]) -> dict[str, dict]:
    return {t["address"]: t for t in cfg["tokens"]}


def make_rpc(cfg: dict[str, Any], timeout: float = 60.0) -> tuple[Rpc, str]:
    import os

    env = os.environ.get(cfg["rpc_env"], "").strip()
    candidates = []
    if env:
        candidates.append(env)
    default = cfg.get("default_rpc")
    if default and default not in candidates:
        candidates.append(default)
    for url in RPC_FALLBACKS.get(cfg["name"], []):
        if url not in candidates:
            candidates.append(url)
    if not candidates:
        raise RpcError("no RPC configured")
    rpc = Rpc(candidates[0], timeout=timeout, backups=candidates[1:])
    try:
        rpc.block_number()
    except RpcError as exc:
        raise RpcError(f"no RPC responded: {exc}") from exc
    return rpc, rpc.url


def _multicall(rpc: Rpc, multicall: str, calls: list[tuple[str, bytes]], block: int | str) -> list[tuple[bool, bytes]]:
    if not calls:
        return []
    out: list[tuple[bool, bytes]] = []
    step = 70
    for i in range(0, len(calls), step):
        chunk = calls[i : i + step]
        payload = [(checksum(target), True, data) for target, data in chunk]
        cd = "0x" + calldata("aggregate3((address,bool,bytes)[])", ["(address,bool,bytes)[]"], [payload]).hex()
        try:
            raw = rpc.eth_call(checksum(multicall), cd, block)
            decoded = decode_aggregate(raw)
        except RpcError:
            decoded = []
            for target, data in chunk:
                try:
                    ret = rpc.eth_call(checksum(target), "0x" + data.hex(), block)
                    decoded.append((True, ret))
                except RpcError:
                    decoded.append((False, b""))
        if len(decoded) != len(chunk):
            decoded = [(False, b"")] * len(chunk)
        out.extend(decoded)
    return out


def _addr_from_call(ok: bool, data: bytes) -> str:
    if not ok or len(data) < 32:
        return ZERO
    return norm(word_address(words(data)[0]))


def discover_pools(rpc: Rpc, cfg: dict[str, Any], block: int) -> list[Pool]:
    """Every pool the configured factories have for the token list, at `block`."""
    tokens = cfg["tokens"]
    pairs: list[tuple[dict, dict]] = []
    for i, a in enumerate(tokens):
        for b in tokens[i + 1 :]:
            pairs.append((a, b))

    calls: list[tuple[str, bytes]] = []
    meta: list[tuple[str, str, dict, dict]] = []
    for fac in cfg["factories"]:
        kind = fac["kind"]
        factory = fac["address"]
        for a, b in pairs:
            if kind in {"univ2", "camelot"}:
                data = calldata("getPair(address,address)", ["address", "address"], [checksum(a["address"]), checksum(b["address"])])
                calls.append((factory, data))
                meta.append((fac["name"], kind, a, b))
            elif kind == "solidly":
                for stable in (False, True):
                    data = calldata(
                        "getPool(address,address,bool)",
                        ["address", "address", "bool"],
                        [checksum(a["address"]), checksum(b["address"]), stable],
                    )
                    calls.append((factory, data))
                    meta.append((fac["name"], "solidly_stable" if stable else "solidly_volatile", a, b))
            elif kind == "univ3":
                for fee in fac["fees"]:
                    data = calldata(
                        "getPool(address,address,uint24)",
                        ["address", "address", "uint24"],
                        [checksum(a["address"]), checksum(b["address"]), int(fee)],
                    )
                    calls.append((factory, data))
                    meta.append((fac["name"], "univ3", a, b))
            elif kind == "slipstream":
                for spacing in fac["tick_spacings"]:
                    data = calldata(
                        "getPool(address,address,int24)",
                        ["address", "address", "int24"],
                        [checksum(a["address"]), checksum(b["address"]), int(spacing)],
                    )
                    calls.append((factory, data))
                    meta.append((fac["name"], "slipstream", a, b))

    found = _multicall(rpc, cfg["multicall3"], calls, block)
    pools: list[Pool] = []
    seen: set[str] = set()
    for (dex, kind, a, b), (ok, data) in zip(meta, found):
        addr = _addr_from_call(ok, data)
        if addr == ZERO or addr in seen:
            continue
        seen.add(addr)
        fee_bps = 30
        for fac in cfg["factories"]:
            if fac["name"] == dex and "fee_bps" in fac:
                fee_bps = int(fac["fee_bps"])
        pools.append(
            Pool(
                address=addr,
                dex=dex,
                kind=kind,
                token0=a["address"],
                token1=b["address"],
                fee0_bps=fee_bps,
                fee1_bps=fee_bps,
                decimals0=int(a["decimals"]),
                decimals1=int(b["decimals"]),
            )
        )
    _hydrate(rpc, cfg, pools, block)
    return [p for p in pools if p.token0 != ZERO and p.token1 != ZERO]


def _hydrate(rpc: Rpc, cfg: dict[str, Any], pools: list[Pool], block: int) -> None:
    """Fill token order, fees, and (for concentrated pools) tick spacing."""
    if not pools:
        return
    tokens = token_map(cfg)
    calls: list[tuple[str, bytes]] = []
    plan: list[tuple[str, str]] = []
    t0 = selector("token0()")
    t1 = selector("token1()")
    fee = selector("fee()")
    spacing = selector("tickSpacing()")
    stable = selector("stable()")
    for pool in pools:
        calls.append((pool.address, t0))
        plan.append((pool.address, "token0"))
        calls.append((pool.address, t1))
        plan.append((pool.address, "token1"))
        if pool.kind in CONCENTRATED:
            calls.append((pool.address, fee))
            plan.append((pool.address, "fee"))
            calls.append((pool.address, spacing))
            plan.append((pool.address, "spacing"))
        elif pool.kind in {"solidly_volatile", "solidly_stable"}:
            calls.append((pool.address, stable))
            plan.append((pool.address, "stable"))
    rows = _multicall(rpc, cfg["multicall3"], calls, block)
    by_addr = {p.address: p for p in pools}
    solidly_flags: dict[str, bool] = {}
    for (addr, what), (ok, data) in zip(plan, rows):
        pool = by_addr[addr]
        ws = words(data) if ok else []
        if what == "token0" and ws:
            pool.token0 = norm(word_address(ws[0]))
        elif what == "token1" and ws:
            pool.token1 = norm(word_address(ws[0]))
        elif what == "fee" and ws:
            pool.fee_pips = int(ws[0])
        elif what == "spacing" and ws:
            pool.tick_spacing = int(as_int256(ws[0])) or pool.tick_spacing
        elif what == "stable" and ws:
            solidly_flags[addr] = bool(ws[0])
    for pool in pools:
        a = tokens.get(pool.token0)
        b = tokens.get(pool.token1)
        if a is None or b is None:
            pool.token0 = ZERO
            continue
        pool.decimals0 = int(a["decimals"])
        pool.decimals1 = int(b["decimals"])
        if pool.address in solidly_flags:
            pool.kind = "solidly_stable" if solidly_flags[pool.address] else "solidly_volatile"
        if pool.kind in CONCENTRATED and (pool.fee_pips <= 0 or pool.tick_spacing <= 0):
            pool.trusted = False
    _load_solidly_fees(rpc, cfg, pools, block)
    _load_camelot_fees(rpc, cfg, pools, block)


def _load_solidly_fees(rpc: Rpc, cfg: dict[str, Any], pools: list[Pool], block: int) -> None:
    solidly = [p for p in pools if p.kind in {"solidly_volatile", "solidly_stable"}]
    if not solidly:
        return
    factories = {f["name"]: f["address"] for f in cfg["factories"]}
    calls = []
    for pool in solidly:
        factory = factories[pool.dex]
        stable = pool.kind == "solidly_stable"
        data = calldata(
            "getFee(address,bool)",
            ["address", "bool"],
            [checksum(pool.address), stable],
        )
        calls.append((factory, data))
    rows = _multicall(rpc, cfg["multicall3"], calls, block)
    for pool, (ok, data) in zip(solidly, rows):
        ws = words(data) if ok else []
        if not ws:
            pool.trusted = False
            continue
        fee = int(ws[0])
        if fee <= 0 or fee >= 10_000:
            pool.trusted = False
            continue
        pool.fee0_bps = fee
        pool.fee1_bps = fee


def _load_camelot_fees(rpc: Rpc, cfg: dict[str, Any], pools: list[Pool], block: int) -> None:
    camelot = [p for p in pools if p.kind in {"camelot", "camelot_stable"}]
    if not camelot:
        return
    reserves = selector("getReserves()")
    stable = selector("stableSwap()")
    calls: list[tuple[str, bytes]] = []
    for pool in camelot:
        calls.append((pool.address, reserves))
        calls.append((pool.address, stable))
    rows = _multicall(rpc, cfg["multicall3"], calls, block)
    for i, pool in enumerate(camelot):
        ok_r, data_r = rows[2 * i]
        ok_s, data_s = rows[2 * i + 1]
        ws = words(data_r) if ok_r else []
        if len(ws) < 4:
            pool.trusted = False
            continue
        is_stable = bool(words(data_s)[0]) if ok_s and words(data_s) else False
        if is_stable:
            # Keep the 1e5 numerator. quote_out divides by 100_000 for this kind.
            pool.kind = "camelot_stable"
            pool.fee0_bps = int(ws[2])
            pool.fee1_bps = int(ws[3])
        else:
            pool.kind = "camelot"
            pool.fee0_bps = _camelot_bps(int(ws[2]))
            pool.fee1_bps = _camelot_bps(int(ws[3]))


def _camelot_bps(raw: int) -> int:
    """Camelot stores 300 for a 0.30% fee (1e5 denominator). 25 stays 25 bps."""
    if raw >= 100:
        return raw // 10
    return raw


def load_states(
    rpc: Rpc,
    cfg: dict[str, Any],
    pools: list[Pool],
    block: int,
    update_camelot_fees: bool = False,
) -> dict[str, State]:
    calls: list[tuple[str, bytes]] = []
    plan: list[tuple[str, str]] = []
    reserves = selector("getReserves()")
    slot0 = selector("slot0()")
    liquidity = selector("liquidity()")
    for pool in pools:
        if pool.kind in CONCENTRATED:
            calls.append((pool.address, slot0))
            plan.append((pool.address, "slot0"))
            calls.append((pool.address, liquidity))
            plan.append((pool.address, "liq"))
        else:
            calls.append((pool.address, reserves))
            plan.append((pool.address, "reserves"))
    rows = _multicall(rpc, cfg["multicall3"], calls, block)
    states = {p.address: State() for p in pools}
    for (addr, what), (ok, data) in zip(plan, rows):
        st = states[addr]
        ws = words(data) if ok else []
        if what == "reserves" and len(ws) >= 2:
            st.reserve0 = int(ws[0])
            st.reserve1 = int(ws[1])
            if update_camelot_fees and len(ws) >= 4:
                pool = next(p for p in pools if p.address == addr)
                if pool.kind == "camelot_stable":
                    pool.fee0_bps = int(ws[2])
                    pool.fee1_bps = int(ws[3])
                elif pool.kind == "camelot":
                    pool.fee0_bps = _camelot_bps(int(ws[2]))
                    pool.fee1_bps = _camelot_bps(int(ws[3]))
        elif what == "slot0" and len(ws) >= 2:
            st.sqrt_price_x96 = int(ws[0])
            st.tick = int(as_int256(ws[1]))
        elif what == "liq" and ws:
            st.liquidity = int(ws[0])
    return states


def probe_state(rpc: Rpc, cfg: dict[str, Any], block: int) -> bool:
    try:
        rpc.eth_call(checksum(cfg["weth"]), "0x" + selector("totalSupply()").hex(), block)
        return True
    except RpcError:
        return False


def fetch_replay(rpc: Rpc, pools: list[Pool], start: int, end: int):
    """Return ReplayTx list covering [start, end], applying absolute reserves / slot0."""
    from flasharb.competition import ReplayTx

    addresses = [checksum(p.address) for p in pools]
    kinds = {p.address: p.kind for p in pools}
    logs = _get_logs(rpc, addresses, start, end)
    grouped: dict[tuple[int, int], list[dict]] = {}
    for log in logs:
        block = int(log["blockNumber"], 16)
        tx_index = int(log["transactionIndex"], 16)
        grouped.setdefault((block, tx_index), []).append(log)
    txs = []
    for (block, tx_index) in sorted(grouped):
        entries = sorted(grouped[(block, tx_index)], key=lambda row: int(row["logIndex"], 16))
        updates: list[tuple[str, str, tuple]] = []
        touched: list[str] = []
        for entry in entries:
            addr = norm(entry["address"])
            topic0 = entry["topics"][0].lower()
            data = bytes.fromhex(entry["data"][2:]) if entry.get("data") and entry["data"] != "0x" else b""
            ws = words(data)
            if topic0 in {TOPICS["sync112"], TOPICS["sync256"]} and len(ws) >= 2 and kinds.get(addr) not in CONCENTRATED:
                updates.append((addr, "reserves", (int(ws[0]), int(ws[1]))))
                touched.append(addr)
            elif topic0 == TOPICS["swap_v3"] and len(ws) >= 5 and kinds.get(addr) in CONCENTRATED:
                sqrt_price = int(ws[2])
                liquidity = int(ws[3])
                tick = int(as_int256(ws[4]))
                updates.append((addr, "v3", (sqrt_price, liquidity, tick)))
                touched.append(addr)
        if not updates:
            continue
        tx_hash = entries[0].get("transactionHash", "")

        def apply(state: dict, updates=updates) -> None:
            for addr, kind, payload in updates:
                st = state.get(addr)
                if st is None:
                    continue
                if kind == "reserves":
                    st.reserve0, st.reserve1 = payload
                else:
                    st.sqrt_price_x96, st.liquidity, st.tick = payload

        txs.append(
            ReplayTx(
                block=block,
                tx_index=tx_index,
                tx_hash=tx_hash,
                pools=tuple(dict.fromkeys(touched)),
                apply=apply,
            )
        )
    return txs, len(logs)


def _get_logs(rpc: Rpc, addresses: list[str], start: int, end: int) -> list[dict]:
    topics = [[TOPICS["sync112"], TOPICS["sync256"], TOPICS["swap_v3"]]]
    out: list[dict] = []
    # Small chunks: public RPCs cap both the block span and the result size.
    addr_step = 25
    block_step = 400
    for i in range(0, len(addresses), addr_step):
        chunk = addresses[i : i + addr_step]
        cursor = start
        while cursor <= end:
            stop = min(end, cursor + block_step - 1)
            print(f"  logs {cursor}-{stop} ({len(chunk)} pools)", flush=True)
            out.extend(_get_logs_split(rpc, chunk, topics, cursor, stop))
            cursor = stop + 1
    return out


def _get_logs_split(rpc: Rpc, addresses: list[str], topics: list, start: int, end: int) -> list[dict]:
    try:
        return rpc.get_logs(addresses, topics, start, end)
    except RpcError as exc:
        message = str(exc).lower()
        # Rate limits are not fixed by splitting the query into more queries.
        if "429" in message or "rate" in message or "http 5" in message:
            raise
        if start >= end:
            if len(addresses) <= 1:
                raise
            mid = len(addresses) // 2
            return _get_logs_split(rpc, addresses[:mid], topics, start, end) + _get_logs_split(
                rpc, addresses[mid:], topics, start, end
            )
        mid = (start + end) // 2
        return _get_logs_split(rpc, addresses, topics, start, mid) + _get_logs_split(
            rpc, addresses, topics, mid + 1, end
        )


def router_for(chain: str, dex: str) -> str | None:
    entry = ROUTERS.get(dex)
    if entry is None:
        return None
    if isinstance(entry, dict):
        return entry.get(chain)
    return entry


def encode_balance_of(holder: str) -> bytes:
    return calldata("balanceOf(address)", ["address"], [checksum(holder)])


def encode_get_amount_out(amount: int, token_in: str) -> bytes:
    return calldata("getAmountOut(uint256,address)", ["uint256", "address"], [amount, checksum(token_in)])


def encode_get_amounts_out(amount: int, path: list[str]) -> bytes:
    return calldata(
        "getAmountsOut(uint256,address[])",
        ["uint256", "address[]"],
        [amount, [checksum(p) for p in path]],
    )


def encode_quote_v3(token_in: str, token_out: str, amount: int, fee: int) -> bytes:
    # quoteExactInputSingle((address,address,uint256,uint24,uint160))
    sig = "quoteExactInputSingle((address,address,uint256,uint24,uint160))"
    body = encode(
        ["(address,address,uint256,uint24,uint160)"],
        [(checksum(token_in), checksum(token_out), amount, fee, 0)],
    )
    return selector(sig) + body
