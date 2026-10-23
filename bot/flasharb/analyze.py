"""Historical competition analysis for long-tail two-pool cycles.

The scan is read-only. It replays Sync / Swap logs, asks whether a dislocation
was closed by a later transaction that touched both pools in the same block or
the next one, and prices whatever was still open after gas, DEX fees, and the
Aave flash premium (or zero, when the Balancer vault actually holds the token).

Numbers written to analysis/latest.json come from this process. Nothing here
invents a capture rate.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from flasharb.abiutil import checksum, norm, words
from flasharb.competition import classify, empty_tier_row
from flasharb.discover import (
    CONCENTRATED,
    ROOT,
    STABLE_SYMBOLS,
    discover_pools,
    _multicall,
    encode_balance_of,
    encode_get_amount_out,
    encode_get_amounts_out,
    encode_quote_v3,
    fetch_replay,
    load_chain,
    load_states,
    make_rpc,
    probe_state,
    router_for,
    token_map,
)
from flasharb.math_v2 import get_amount_out
from flasharb.quotes import Pool, State, best_amount, quote_out
from flasharb.rpc import Rpc, RpcError

HEADLINE = ("long_tail", "cross_tier", "head", "mid")
Q96 = 1 << 96


def _copy_states(states: dict[str, State]) -> dict[str, State]:
    return {addr: st.copy() for addr, st in states.items()}


def _spot_token1_per_token0(pool: Pool, state: State) -> float | None:
    if pool.kind in CONCENTRATED:
        if state.sqrt_price_x96 <= 0:
            return None
        raw = (state.sqrt_price_x96 / Q96) ** 2
        return raw * 10 ** (pool.decimals0 - pool.decimals1)
    if state.reserve0 <= 0 or state.reserve1 <= 0:
        return None
    if pool.kind in {"solidly_stable", "camelot_stable"}:
        # Reserve ratio is not the marginal price on a stable curve.
        tiny = max(10 ** max(pool.decimals0 - 3, 0), 1)
        if state.reserve0 <= tiny * 20:
            tiny = max(state.reserve0 // 1000, 1)
        out, _exact = quote_out(pool, state, pool.token0, tiny)
        if out <= 0 or tiny <= 0:
            return None
        return (out / 10 ** pool.decimals1) / (tiny / 10 ** pool.decimals0)
    return (state.reserve1 / 10 ** pool.decimals1) / (state.reserve0 / 10 ** pool.decimals0)


def _reserves(pool: Pool, state: State) -> tuple[int, int]:
    if pool.kind in CONCENTRATED:
        if state.liquidity <= 0 or state.sqrt_price_x96 <= 0:
            return 0, 0
        r0 = state.liquidity * Q96 // state.sqrt_price_x96
        r1 = state.liquidity * state.sqrt_price_x96 // Q96
        return r0, r1
    return state.reserve0, state.reserve1


def _hop_fee_bps(pool: Pool, token_in: str) -> int:
    if pool.kind in CONCENTRATED:
        return max(pool.fee_pips // 100, 0)
    raw = pool.fee0_bps if token_in == pool.token0 else pool.fee1_bps
    if pool.kind == "camelot_stable":
        # Stored as a 1e5 numerator. 5 -> 0.5 bps; round up to 1 so the screen is not loose.
        return max(1, (raw * 10_000 + 99_999) // 100_000)
    return raw


def _mid_per_flash(pool: Pool, state: State, flash: str) -> float | None:
    """How many mid tokens one flash token buys at spot, before fees."""
    ratio = _spot_token1_per_token0(pool, state)
    if ratio is None or ratio <= 0:
        return None
    if flash == pool.token0:
        return ratio
    if flash == pool.token1:
        return 1.0 / ratio
    return None


def _spot_clears_fees(pa: Pool, sa: State, pb: Pool, sb: State, flash: str, premium_bps: int) -> bool:
    """False when even a tiny trade loses to fees. Larger size only gets worse."""
    left = _mid_per_flash(pa, sa, flash)
    right = _mid_per_flash(pb, sb, flash)
    if left is None or right is None or left <= 0 or right <= 0:
        return False
    hi, lo = (left, right) if left >= right else (right, left)
    mid = pa.token1 if flash == pa.token0 else pa.token0
    fee_a = _hop_fee_bps(pa, flash) + _hop_fee_bps(pb, mid)
    fee_b = _hop_fee_bps(pb, flash) + _hop_fee_bps(pa, mid)
    need = min(fee_a, fee_b) + premium_bps
    # One bip of slack so float rounding does not hide a real dust-sized edge.
    return (hi - lo) * 10_000 > lo * max(need - 1, 0)


def _side_usd(amount: int, decimals: int, price: float | None) -> float:
    if price is None or amount <= 0:
        return 0.0
    return amount / 10 ** decimals * price


def price_tokens(cfg: dict, pools: list[Pool], states: dict[str, State]) -> dict[str, float]:
    tokens = token_map(cfg)
    prices: dict[str, float] = {}
    for token in tokens.values():
        if token["symbol"] in STABLE_SYMBOLS:
            prices[token["address"]] = 1.0
    weth = cfg["weth"]

    # Repeated passes so a token priced off WETH can price something else.
    for _ in range(4):
        proposals: dict[str, tuple[float, float]] = {}
        for pool in pools:
            if not pool.trusted:
                continue
            state = states.get(pool.address)
            if state is None:
                continue
            ratio = _spot_token1_per_token0(pool, state)
            if ratio is None or ratio <= 0:
                continue
            r0, r1 = _reserves(pool, state)
            p0 = prices.get(pool.token0)
            p1 = prices.get(pool.token1)
            if p1 is not None and pool.token0 not in prices:
                depth = _side_usd(r1, pool.decimals1, p1)
                proposals.setdefault(pool.token0, (0.0, -1.0))
                if depth > proposals[pool.token0][1]:
                    proposals[pool.token0] = (ratio * p1, depth)
            if p0 is not None and pool.token1 not in prices:
                depth = _side_usd(r0, pool.decimals0, p0)
                proposals.setdefault(pool.token1, (0.0, -1.0))
                if depth > proposals[pool.token1][1]:
                    proposals[pool.token1] = (p0 / ratio, depth)
        # WETH is applied first when both sides are new only through stables,
        # which are already seeded. proposals only fills missing prices.
        changed = False
        for addr, (px, _depth) in proposals.items():
            if addr not in prices and px > 0:
                prices[addr] = px
                changed = True
        if not changed:
            break
    # Prefer the WETH/stable pool with the most stable-side depth for WETH itself,
    # overwriting a noisier path if one was picked above. Stables are $1.
    best_depth = -1.0
    best_px = prices.get(weth)
    for pool in pools:
        if not pool.trusted:
            continue
        sides = {pool.token0, pool.token1}
        if weth not in sides:
            continue
        other = pool.token1 if pool.token0 == weth else pool.token0
        if prices.get(other) != 1.0:
            continue
        state = states[pool.address]
        ratio = _spot_token1_per_token0(pool, state)
        if ratio is None or ratio <= 0:
            continue
        r0, r1 = _reserves(pool, state)
        if pool.token0 == weth:
            px = ratio * 1.0
            depth = _side_usd(r1, pool.decimals1, 1.0)
        else:
            px = 1.0 / ratio
            depth = _side_usd(r0, pool.decimals0, 1.0)
        if depth > best_depth and px > 0:
            best_depth = depth
            best_px = px
    if best_px is not None and best_depth > 0:
        prices[weth] = best_px
    return prices


def tvl_usd(pool: Pool, state: State, prices: dict[str, float]) -> float | None:
    p0 = prices.get(pool.token0)
    p1 = prices.get(pool.token1)
    if p0 is None or p1 is None:
        return None
    r0, r1 = _reserves(pool, state)
    return _side_usd(r0, pool.decimals0, p0) + _side_usd(r1, pool.decimals1, p1)


def tier_of_tvls(a: float | None, b: float | None, cfg: dict) -> str:
    if a is None or b is None:
        return "unpriced"
    lo = float(cfg["long_tail_min_usd"])
    hi = float(cfg["long_tail_max_usd"])
    head = float(cfg["head_min_usd"])

    def band(x: float) -> str:
        if x < lo:
            return "dust"
        if x <= hi:
            return "long"
        if x >= head:
            return "head"
        return "mid"

    ba, bb = band(a), band(b)
    if "dust" in (ba, bb):
        return "dust"
    if ba == "head" and bb == "head":
        return "head"
    if ba == "long" and bb == "long":
        return "long_tail"
    if {ba, bb} == {"long", "head"}:
        return "cross_tier"
    return "mid"


def _base_fee(block: dict) -> int:
    raw = block.get("baseFeePerGas")
    if raw:
        return int(raw, 16)
    return 0


def _gas_raw(token: dict, gas_units: int, base_fee: int, weth_usd: float, extra_usd: float, price: float) -> int | None:
    if price <= 0 or weth_usd <= 0:
        return None
    eth = gas_units * base_fee / 1e18
    usd = eth * weth_usd + extra_usd
    return int(usd / price * 10 ** int(token["decimals"]))


def _rel_close(local: int, chain: int, tol: float) -> bool:
    if chain <= 0 or local <= 0:
        return False
    return abs(local - chain) / chain <= tol


def _call_uints(rpc: Rpc, to: str, data: bytes, block: int) -> list[int] | None:
    try:
        raw = rpc.eth_call(checksum(to), "0x" + data.hex(), block)
    except RpcError:
        return None
    ws = words(raw)
    if not ws:
        return None
    return [int(w) for w in ws]


def _call_word(rpc: Rpc, to: str, data: bytes, block: int) -> int | None:
    try:
        raw = rpc.eth_call(checksum(to), "0x" + data.hex(), block)
    except RpcError:
        return None
    ws = words(raw)
    if not ws:
        return None
    return int(ws[0])


def validate_quotes(rpc: Rpc, cfg: dict, pools: list[Pool], states: dict[str, State], block: int, notes: list[str]) -> dict:
    """Spot-check formulas. A failed kind is marked untrusted and dropped from the headline."""
    report: dict[str, Any] = {"checked": [], "failed": []}
    by_dex: dict[str, list[Pool]] = defaultdict(list)
    for pool in pools:
        by_dex[pool.dex].append(pool)

    def fail(pool: Pool, reason: str) -> None:
        pool.trusted = False
        report["failed"].append({"pool": pool.address, "dex": pool.dex, "kind": pool.kind, "reason": reason})

    def sample(predicate, limit: int) -> list[Pool]:
        out = []
        for pool in pools:
            if not pool.trusted or not predicate(pool):
                continue
            st = states[pool.address]
            r0, r1 = _reserves(pool, st)
            if r0 <= 0 or r1 <= 0:
                continue
            out.append(pool)
            if len(out) >= limit:
                break
        return out

    # Constant-product factories, via their router when one is known.
    for dex, group in by_dex.items():
        router = router_for(cfg["name"], dex)
        xyk = [p for p in group if p.kind == "univ2" and p.trusted]
        if not xyk:
            continue
        if router is None:
            notes.append(f"{dex}: univ2 fee taken from config; no router spot-check on this chain.")
            continue
        pool = sample(lambda p, dex=dex: p.dex == dex and p.kind == "univ2", 1)
        if not pool:
            continue
        pool = pool[0]
        st = states[pool.address]
        amount = max(10 ** pool.decimals0 // 1000, 1)
        if st.reserve0 <= amount * 2:
            amount = max(st.reserve0 // 100, 1)
        local = get_amount_out(amount, st.reserve0, st.reserve1, pool.fee0_bps)
        # getAmountsOut returns uint[]: offset, length, amounts...
        returned = _call_uints(rpc, router, encode_get_amounts_out(amount, [pool.token0, pool.token1]), block)
        chain = returned[-1] if returned and len(returned) >= 3 else None
        if chain is None:
            notes.append(f"{dex}: router getAmountsOut unavailable; univ2 formula kept from the offline vector.")
            continue
        ok = _rel_close(local, chain, 0.005)
        report["checked"].append({"dex": dex, "kind": "univ2", "local": local, "chain": chain, "ok": ok})
        if not ok:
            for other in xyk:
                fail(other, f"router disagreed local={local} chain={chain}")
            notes.append(f"{dex}: router spot-check failed. Those pools are excluded.")

    # Every Camelot / Solidly pool has getAmountOut. A sample is not enough:
    # one stable pool priced as constant-product invents a huge edge.
    pair_quoted = [
        p for p in pools
        if p.trusted and p.kind in {"camelot", "camelot_stable", "solidly_volatile", "solidly_stable"}
        and _reserves(p, states[p.address])[0] > 0
    ]
    if pair_quoted:
        calls = []
        amounts = []
        for pool in pair_quoted:
            st = states[pool.address]
            amount = max(st.reserve0 // 1000, 1)
            amounts.append(amount)
            calls.append((pool.address, encode_get_amount_out(amount, pool.token0)))
        rows = _multicall(rpc, cfg["multicall3"], calls, block)
        failed_kinds: dict[str, int] = defaultdict(int)
        checked_kinds: dict[str, int] = defaultdict(int)
        for pool, amount, (ok, data) in zip(pair_quoted, amounts, rows):
            checked_kinds[pool.kind] += 1
            ws = words(data) if ok else []
            chain = int(ws[0]) if ws else 0
            local, exact = quote_out(pool, states[pool.address], pool.token0, amount)
            good = bool(ws) and exact and _rel_close(local, chain, 0.005)
            if len(report["checked"]) < 24:
                report["checked"].append({
                    "dex": pool.dex, "kind": pool.kind, "pool": pool.address,
                    "local": local, "chain": chain, "ok": good,
                })
            if not good:
                fail(pool, f"{pool.kind} getAmountOut local={local} chain={chain}")
                failed_kinds[pool.kind] += 1
        for kind, n in checked_kinds.items():
            bad = failed_kinds.get(kind, 0)
            if bad:
                notes.append(f"{kind}: {bad} of {n} pools failed getAmountOut and were excluded.")
            else:
                notes.append(f"{kind}: {n} pools matched getAmountOut within 0.5%.")

    # Uniswap V3 / Slipstream against QuoterV2. A within-tick quote must be within 1%.
    # A tick-capped quote is kept only when it is not above the quoter (it is a lower bound).
    if cfg.get("quoter_v2"):
        for kind in ("univ3", "slipstream"):
            samples = [
                p for p in pools
                if p.trusted and p.kind == kind and _reserves(p, states[p.address])[0] > 0
            ]
            if not samples:
                continue
            calls = []
            amounts = []
            locals_ = []
            exacts = []
            for pool in samples:
                st = states[pool.address]
                r0, _r1 = _reserves(pool, st)
                amount = max(min(r0 // 10000, 10 ** pool.decimals0), 1)
                amounts.append(amount)
                local, exact = quote_out(pool, st, pool.token0, amount)
                locals_.append(local)
                exacts.append(exact)
                calls.append((cfg["quoter_v2"], encode_quote_v3(pool.token0, pool.token1, amount, pool.fee_pips)))
            rows = _multicall(rpc, cfg["multicall3"], calls, block)
            kind_fail = 0
            for pool, local, exact, (ok, data) in zip(samples, locals_, exacts, rows):
                ws = words(data) if ok else []
                chain = int(ws[0]) if ws else 0
                if chain <= 0:
                    fail(pool, "quoter rejected the pool")
                    kind_fail += 1
                    continue
                over = local > chain and (local - chain) / chain > 0.01
                off_exact = exact and not _rel_close(local, chain, 0.01)
                if over or off_exact:
                    fail(pool, f"quoter disagreed local={local} chain={chain}")
                    kind_fail += 1
                elif len(report["checked"]) < 40:
                    report["checked"].append({
                        "dex": pool.dex, "kind": kind, "pool": pool.address,
                        "local": local, "chain": chain, "ok": True, "exact": exact,
                    })
            if kind_fail == len(samples):
                notes.append(
                    f"{kind}: QuoterV2 rejected every pool or disagreed by more than 1%. "
                    "That kind is excluded from the headline."
                )
            elif kind_fail:
                notes.append(f"{kind}: {kind_fail} of {len(samples)} pools failed the quoter check and were excluded.")
            else:
                notes.append(f"{kind}: {len(samples)} pools passed the QuoterV2 check (lower bounds allowed).")
    return report


def _pair_key(pool: Pool) -> tuple[str, str]:
    return tuple(sorted((pool.token0, pool.token1)))  # type: ignore[return-value]


def build_cycles(pools: list[Pool]) -> list[tuple[str, str, str]]:
    grouped: dict[tuple[str, str], list[Pool]] = defaultdict(list)
    for pool in pools:
        if not pool.trusted:
            continue
        grouped[_pair_key(pool)].append(pool)
    cycles = []
    for (t0, t1), group in grouped.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = sorted((group[i].address, group[j].address))
                cycles.append((a, b, t0))
                cycles.append((a, b, t1))
    return cycles


def _max_in(pool: Pool, state: State, token: str, price: float, cap_usd: float) -> int:
    decimals = pool.decimals0 if token == pool.token0 else pool.decimals1
    if price <= 0:
        return 0
    raw_cap = int(cap_usd / price * 10 ** decimals)
    r0, r1 = _reserves(pool, state)
    reserve_in = r0 if token == pool.token0 else r1
    if reserve_in <= 1:
        return 0
    return max(0, min(raw_cap, reserve_in // 2))


def run_chain(name: str, lookback: int | None, end_block: int | None, extend: bool) -> dict[str, Any]:
    cfg = load_chain(name)
    rpc, url = make_rpc(cfg)
    latest = rpc.block_number()
    end = end_block or (latest - 2)
    if end > latest:
        end = latest - 2
    window = lookback or int(cfg["lookback"])
    caps = {"mainnet": 400, "base": 3600, "arbitrum": 6000}
    cap = caps.get(name, 2000)
    notes: list[str] = []
    print(f"[{name}] rpc {url} head {latest} end {end}", flush=True)

    # Shrink until the node still has state, then optionally widen if the window is empty.
    start = max(1, end - window)
    while start < end and not probe_state(rpc, cfg, start - 1):
        window = max(20, window // 2)
        start = max(1, end - window)
        notes.append(f"Historical state unavailable; shortened the window to {window} blocks.")
        if window <= 20:
            break
    if not probe_state(rpc, cfg, start - 1):
        raise RpcError(f"{name}: node has no state at block {start - 1}")

    attempts = 0
    while True:
        attempts += 1
        result = _scan(cfg, rpc, url, start, end, notes)
        total = sum(row["opportunities"] for row in result["tiers"].values())
        result["lookback_attempts"] = attempts
        if not extend or total > 0 or attempts >= 2:
            if attempts > 1:
                result["notes"].append(f"Lookback widened to {end - start} blocks after an empty window.")
            return result
        grown = min(cap, max((end - start) * 2, window * 2))
        if grown <= end - start:
            result["notes"].append("Window was empty and could not be widened further (RPC cap).")
            return result
        old = end - start
        start = max(1, end - grown)
        if not probe_state(rpc, cfg, start - 1):
            result["notes"].append("Did not widen further: the node has no state that far back.")
            return result
        notes.append(f"No opportunities in the {old}-block window; retrying {end - start} blocks.")
        print(f"[{name}] empty window, widening to {end - start} blocks", flush=True)


def _scan(cfg: dict, rpc: Rpc, url: str, start: int, end: int, inherited_notes: list[str]) -> dict[str, Any]:
    name = cfg["name"]
    t0 = time.time()
    print(f"[{name}] discovering pools at block {end}", flush=True)
    pools = discover_pools(rpc, cfg, end)
    print(f"[{name}] {len(pools)} pools, loading state at {start - 1}", flush=True)
    states = load_states(rpc, cfg, pools, start - 1, update_camelot_fees=True)
    alive = []
    for pool in pools:
        st = states[pool.address]
        r0, r1 = _reserves(pool, st)
        if r0 > 0 and r1 > 0 and pool.token0 != "0x" + "0" * 40:
            alive.append(pool)
    pools = alive
    notes = list(inherited_notes)
    # Quote checks use the same block the reserves were read at, before TVL filtering,
    # so a mis-priced pool cannot delete its neighbours.
    validation = validate_quotes(rpc, cfg, pools, states, start - 1, notes)
    prices = price_tokens(cfg, pools, states)
    floor = float(cfg["long_tail_min_usd"]) * 0.25
    kept = []
    for pool in pools:
        if not pool.trusted:
            continue
        tv = tvl_usd(pool, states[pool.address], prices)
        # Dust on both sides of every cycle cannot enter the headline. Keep a pool once
        # it is at least a quarter of the long-tail minimum so a move across the line
        # during the window is still visible.
        if tv is not None and tv < floor:
            continue
        kept.append(pool)
    pools = kept
    weth_usd = prices.get(cfg["weth"])
    if not weth_usd:
        raise RpcError(f"{name}: could not price WETH")
    notes.append(
        f"WETH marked at ${weth_usd:.2f} from the deepest WETH/stable pool at block {start - 1}. "
        "Stables (USDC, USDT, DAI, USDbC, USDC.e) are pegged at $1. "
        "V3 / Slipstream TVL is active-liquidity depth at the current tick, not deposited TVL."
    )

    header_start = rpc.block(start)
    header_end = rpc.block(end)
    base_fee = max(_base_fee(header_start), _base_fee(header_end))
    if base_fee <= 0:
        try:
            base_fee = int(rpc.request("eth_gasPrice", []), 16)
            notes.append("Block header had no baseFeePerGas; used eth_gasPrice.")
        except RpcError:
            base_fee = 0
    tokens = token_map(cfg)
    gas_units = {"v2": int(cfg["gas_units_v2"]), "v3": int(cfg["gas_units_v3"])}
    extra = float(cfg["extra_l1_fee_usd"])

    # Balancer covers the flash with zero premium only when the vault holds the token.
    premiums: dict[str, int] = {}
    bal = cfg.get("balancer_vault")
    if bal:
        calls_note = []
        for token in tokens.values():
            try:
                raw = rpc.eth_call(checksum(token["address"]), "0x" + encode_balance_of(bal).hex(), end)
                bal_raw = int(words(raw)[0]) if words(raw) else 0
            except RpcError:
                bal_raw = 0
            price = prices.get(token["address"]) or 0
            usd = bal_raw / 10 ** int(token["decimals"]) * price if price else 0
            premium = 0 if usd >= float(cfg["max_flash_usd"]) else int(cfg["aave_premium_bps"])
            premiums[token["address"]] = premium
            if premium == 0:
                calls_note.append(token["symbol"])
        if calls_note:
            notes.append(
                "Balancer vault covered a full flash (0 premium) for: " + ", ".join(calls_note) + ". Other tokens use Aave's 5 bps."
            )
        else:
            notes.append("No configured token was deep enough in the Balancer vault. Premium is Aave's 5 bps.")
    else:
        for token in tokens:
            premiums[token] = int(cfg["aave_premium_bps"])

    by_addr = {p.address: p for p in pools}
    cycles = build_cycles(pools)
    index: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for cycle in cycles:
        index[cycle[0]].append(cycle)
        index[cycle[1]].append(cycle)
    print(f"[{name}] {len(pools)} priced pools, {len(cycles)} directed cycles, fetching logs {start}..{end}", flush=True)
    txs, log_count = fetch_replay(rpc, pools, start, end)
    print(f"[{name}] {log_count} logs, {len(txs)} txs", flush=True)

    initial = _copy_states(states)
    working = _copy_states(states)
    for tx in txs:
        tx.apply(working)
    end_states = load_states(rpc, cfg, pools, end)
    mismatched = []
    for pool in pools:
        got = working[pool.address]
        actual = end_states[pool.address]
        if pool.kind in CONCENTRATED:
            bad = got.sqrt_price_x96 != actual.sqrt_price_x96 or got.liquidity != actual.liquidity
        else:
            bad = got.reserve0 != actual.reserve0 or got.reserve1 != actual.reserve1
        if bad:
            mismatched.append(pool.address)
            pool.trusted = False
    if mismatched:
        notes.append(
            f"{len(mismatched)} pools failed log replay (missed mint/burn or an undecoded event) and were excluded."
        )
        cycles = build_cycles(pools)
        index = defaultdict(list)
        for cycle in cycles:
            index[cycle[0]].append(cycle)
            index[cycle[1]].append(cycle)

    state = _copy_states(initial)
    suspect: set[tuple[str, str, str]] = set()
    cap_usd = float(cfg["max_flash_usd"])
    suspect_bps = int(cfg["suspect_edge_bps"])
    evals = {"n": 0}
    spot_passes = {"n": 0}
    before_gas = {"n": 0}
    edge_track = {"max": 0.0}

    def gas_for(token_addr: str, *pair: Pool) -> int | None:
        token = tokens.get(token_addr)
        price = prices.get(token_addr)
        if token is None or price is None:
            return None
        units = gas_units["v3"] if any(p.kind in CONCENTRATED for p in pair) else gas_units["v2"]
        return _gas_raw(token, units, base_fee, weth_usd, extra, price)

    def profit_fn(snapshot: dict, a: str, b: str, flash: str) -> int:
        evals["n"] += 1
        if evals["n"] % 5000 == 0:
            print(f"[{name}] profit evals {evals['n']}", flush=True)
        pa, pb = by_addr.get(a), by_addr.get(b)
        if pa is None or pb is None or not pa.trusted or not pb.trusted:
            return 0
        if tier_of_tvls(tvl_usd(pa, snapshot[a], prices), tvl_usd(pb, snapshot[b], prices), cfg) in {"dust", "unpriced"}:
            return 0
        gas = gas_for(flash, pa, pb)
        if gas is None:
            return 0
        premium = premiums.get(flash, int(cfg["aave_premium_bps"]))
        left = _mid_per_flash(pa, snapshot[a], flash)
        right = _mid_per_flash(pb, snapshot[b], flash)
        if left and right and min(left, right) > 0:
            edge = abs(left - right) / min(left, right) * 10_000
            if edge > edge_track["max"]:
                edge_track["max"] = edge
        if not _spot_clears_fees(pa, snapshot[a], pb, snapshot[b], flash, premium):
            return 0
        spot_passes["n"] += 1
        best_net = 0
        best_amount_in = 0
        best_exact = True
        for first, second in ((pa, pb), (pb, pa)):
            if flash not in (first.token0, first.token1):
                continue
            limit = _max_in(first, snapshot[first.address], flash, prices.get(flash, 0), cap_usd)
            if limit <= 0:
                continue
            amount, net, exact = best_amount(
                first, snapshot[first.address], second, snapshot[second.address], flash, premium, gas, limit
            )
            if net > best_net:
                best_net = net
                best_amount_in = amount
                best_exact = exact
            if gas > 0 and net <= 0:
                amount0, net0, _exact0 = best_amount(
                    first, snapshot[first.address], second, snapshot[second.address], flash, premium, 0, limit
                )
                if net0 > 0:
                    before_gas["n"] += 1
        if best_net <= 0 or best_amount_in <= 0:
            return best_net
        if not best_exact:
            # Tick-capped V3 quote is a lower bound. Keep it, but do not treat a
            # wild number that also fails the suspect test as real.
            pass
        back_premium = best_amount_in * premium // 10_000
        # net = back - amount - premium - gas => back - amount = net + premium + gas
        gross = best_net + back_premium + gas
        gross_bps = gross * 10_000 // best_amount_in
        if gross_bps > suspect_bps:
            suspect.add((a, b, flash))
            return 0
        return best_net

    def keys_for(touched: set) -> list[tuple[str, str, str]]:
        out = []
        seen = set()
        for pool in touched:
            for cycle in index.get(pool, []):
                if cycle not in seen:
                    seen.add(cycle)
                    out.append(cycle)
        return out

    def tier_of(key: tuple[str, str, str]) -> str:
        pa, pb = by_addr[key[0]], by_addr[key[1]]
        return tier_of_tvls(tvl_usd(pa, state[key[0]], prices), tvl_usd(pb, state[key[1]], prices), cfg)

    report = classify(start, end, state, txs, profit_fn, keys_for, tier_of)
    # Spreads that were already open at the start never "transition", so the
    # classifier does not see them. Price whatever is still profitable at the
    # end and is not already an uncaptured episode.
    already = {ep.key for ep in report.episodes if ep.outcome == "uncaptured"}
    standing: list[tuple[tuple[str, str, str], int, str]] = []
    for cycle in cycles:
        if cycle in already:
            continue
        net = profit_fn(state, cycle[0], cycle[1], cycle[2])
        if net > 0:
            standing.append((cycle, net, tier_of(cycle)))
    tiers: dict[str, dict] = {}
    examples: list[dict] = []
    for ep in report.episodes:
        if ep.tier in {"dust", "unpriced", "open"}:
            continue
        row = tiers.setdefault(ep.tier, empty_tier_row())
        row["opportunities"] += 1
        if ep.outcome in row:
            row[ep.outcome] += 1
        token = tokens.get(ep.key[2])
        price = prices.get(ep.key[2], 0)
        if token and price:
            opened_usd = ep.opened_profit / 10 ** int(token["decimals"]) * price
            remain_usd = ep.remaining_profit / 10 ** int(token["decimals"]) * price
        else:
            opened_usd = 0.0
            remain_usd = 0.0
        if ep.outcome == "uncaptured" and remain_usd > 0:
            row["uncaptured_net_usd"] += remain_usd
        if ep.outcome.startswith("captured"):
            row["gross_opened_usd"] += max(opened_usd, 0)
        if ep.outcome == "uncaptured" and len(examples) < 8:
            examples.append(
                {
                    "block": ep.opened_block,
                    "tier": ep.tier,
                    "token": token["symbol"] if token else ep.key[2],
                    "net_usd": round(remain_usd, 4),
                    "pools": [ep.key[0], ep.key[1]],
                }
            )

    standing_rows = []
    for key, net, tier in standing:
        if tier in {"dust", "unpriced", "open"}:
            continue
        token = tokens.get(key[2])
        price = prices.get(key[2], 0)
        usd = net / 10 ** int(token["decimals"]) * price if token and price else 0.0
        if usd <= 0:
            continue
        row = tiers.setdefault(tier, empty_tier_row())
        row["opportunities"] += 1
        row["uncaptured"] += 1
        row["uncaptured_net_usd"] += usd
        row["standing_uncaptured"] = int(row.get("standing_uncaptured", 0)) + 1
        if len(standing_rows) < 8:
            standing_rows.append({
                "tier": tier,
                "token": token["symbol"] if token else key[2],
                "net_usd": round(usd, 4),
                "pools": [key[0], key[1]],
            })
    if standing_rows:
        notes.append(
            f"{len(standing)} directed cycles were still profitable at the end of the window and had not "
            "opened during it (standing residuals). They are included in uncaptured. "
            "Edges above the suspect threshold are not."
        )

    for row in tiers.values():
        row["uncaptured_net_usd"] = round(float(row["uncaptured_net_usd"]), 4)
        row["gross_opened_usd"] = round(float(row["gross_opened_usd"]), 4)

    trusted = sum(1 for p in pools if p.trusted)
    kind_counts: dict[str, int] = defaultdict(int)
    for pool in pools:
        if pool.trusted:
            kind_counts[pool.kind] += 1
    elapsed = round(time.time() - t0, 1)
    print(
        f"[{name}] done in {elapsed}s episodes {len(report.episodes)} suspect {len(suspect)} "
        f"spot_pass {spot_passes['n']} before_gas {before_gas['n']} max_edge_bps {edge_track['max']:.1f}",
        flush=True,
    )
    for note in notes:
        print(f"[{name}] note: {note}", flush=True)
    return {
        "chain": name,
        "rpc": url,
        "start_block": start,
        "end_block": end,
        "blocks": end - start,
        "base_fee_wei_max": base_fee,
        "base_fee_gwei_max": base_fee / 1e9,
        "weth_usd": round(weth_usd, 2),
        "token_prices_usd": {
            tokens[addr]["symbol"]: round(px, 6)
            for addr, px in prices.items()
            if addr in tokens and px > 0
        },
        "extra_l1_fee_usd": extra,
        "pools_discovered": len(pools),
        "pools_trusted": trusted,
        "pools_replay_mismatch": len(mismatched),
        "trusted_kinds": dict(kind_counts),
        "cycles": len(cycles),
        "logs": log_count,
        "txs_touching_pools": len(txs),
        "suspect_keys": len(suspect),
        "spot_screen_passes": spot_passes["n"],
        "positive_before_gas_only": before_gas["n"],
        "max_spot_edge_bps": round(edge_track["max"], 2),
        "quote_checks": validation,
        "tiers": tiers,
        "examples_uncaptured": examples,
        "standing_at_end": standing_rows,
        "elapsed_sec": elapsed,
        "notes": notes,
        "counts": report.counts(),
    }


def markdown_table(payload: dict) -> str:
    lines = [
        "| Chain | Blocks | Tier | Opportunities | Captured same block | Captured next block | Ambiguous | Uncaptured | Uncaptured net USD after gas | Notes |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for chain in payload["chains"]:
        if chain.get("error"):
            lines.append(f"| {chain.get('chain','')} |  |  |  |  |  |  |  |  | run failed: {chain['error'][:120]} |")
            continue
        span = f"{chain['start_block']}–{chain['end_block']}"
        tiers = chain.get("tiers") or {}
        # Always show the long-tail set and the head control, including zeros.
        shown = ["long_tail", "cross_tier", "head"]
        if int((tiers.get("mid") or {}).get("opportunities", 0)) > 0:
            shown.append("mid")
        fee_note = (
            f"max spot edge {chain.get('max_spot_edge_bps', 0)} bps; "
            f"fee-only evals {chain.get('positive_before_gas_only', 0)} "
            f"(cleared fees and premium, not the gas cushion)"
        )
        for i, tier in enumerate(shown):
            row = tiers.get(tier) or empty_tier_row()
            note = ""
            if i == 0:
                note = (
                    f"base fee {chain['base_fee_gwei_max']:.4f} gwei; "
                    f"WETH ${chain['weth_usd']}; "
                    f"suspect excluded {chain['suspect_keys']}; "
                    f"replay mismatches {chain['pools_replay_mismatch']}; "
                    + fee_note
                )
            lines.append(
                "| {chain} | {span} | {tier} | {opps} | {same} | {nxt} | {amb} | {unc} | {usd:.2f} | {note} |".format(
                    chain=chain["chain"],
                    span=span,
                    tier=tier,
                    opps=int(row["opportunities"]),
                    same=int(row["captured_same_block"]),
                    nxt=int(row["captured_next_block"]),
                    amb=int(row["ambiguous"]),
                    unc=int(row["uncaptured"]),
                    usd=float(row["uncaptured_net_usd"]),
                    note=note,
                )
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    parser = argparse.ArgumentParser(description="Read-only competition analysis")
    parser.add_argument("--chain", action="append", choices=["mainnet", "base", "arbitrum"])
    parser.add_argument("--lookback", type=int)
    parser.add_argument("--end-block", type=int)
    parser.add_argument("--no-extend", action="store_true")
    parser.add_argument("--out", default=str(ROOT / "analysis" / "latest.json"))
    args = parser.parse_args(argv)
    chains = args.chain or ["base", "arbitrum", "mainnet"]
    payload: dict[str, Any] = {
        "methodology": (
            "An opportunity opens when one transaction moves a two-pool cycle from "
            "not-profitable to profitable after the flash premium, DEX fee, and a gas "
            "cushion (max base fee of the window endpoints, plus the chain's L1/Timeboost "
            "dollar cushion). It is captured only if a later transaction in the same block "
            "or the next block touches both pools and the profit goes to zero or below. "
            "A spread that dies without such a two-pool transaction is ambiguous. A spread "
            "still profitable at the end of the next block is uncaptured and counted once. "
            "Edges above the suspect threshold are dropped. Dust TVL (either pool under the "
            "long-tail minimum) is not counted. This is not a live trading result."
        ),
        "chains": [],
    }
    for name in chains:
        try:
            payload["chains"].append(
                run_chain(name, args.lookback, args.end_block, extend=not args.no_extend)
            )
        except Exception as exc:  # noqa: BLE001 — record the failure, keep the other chains
            print(f"[{name}] FAILED {exc}", file=sys.stderr, flush=True)
            payload["chains"].append({"chain": name, "error": str(exc)})
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload["markdown"] = markdown_table(payload)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(payload["markdown"], flush=True)
    print(f"wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
