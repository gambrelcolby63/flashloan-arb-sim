"""Pool quotes used by the scanner. V2-style quotes are exact; V3 quotes are within-tick lower bounds."""

from __future__ import annotations

from dataclasses import dataclass

from flasharb.math_solidly import stable_out, volatile_out
from flasharb.math_v2 import optimal_input
from flasharb.math_v3 import max_in_within_tick, quote_exact_in, sqrt_price_x96_at_tick, tick_floor

XYK = {"univ2", "camelot", "solidly_volatile"}


@dataclass
class Pool:
    address: str
    dex: str
    kind: str
    token0: str
    token1: str
    fee0_bps: int = 30
    fee1_bps: int = 30
    fee_pips: int = 0
    tick_spacing: int = 1
    decimals0: int = 18
    decimals1: int = 18
    trusted: bool = True
    tvl_usd: float = 0.0


@dataclass
class State:
    reserve0: int = 0
    reserve1: int = 0
    sqrt_price_x96: int = 0
    liquidity: int = 0
    tick: int = 0

    def copy(self) -> "State":
        return State(self.reserve0, self.reserve1, self.sqrt_price_x96, self.liquidity, self.tick)


def _reserves_for(pool: Pool, state: State, token_in: str) -> tuple[int, int, int]:
    if token_in == pool.token0:
        return state.reserve0, state.reserve1, pool.fee0_bps
    if token_in == pool.token1:
        return state.reserve1, state.reserve0, pool.fee1_bps
    raise ValueError("token not in pool")


def quote_out(pool: Pool, state: State, token_in: str, amount_in: int) -> tuple[int, bool]:
    """Return (amount_out, exact). `exact` is false when a V3 quote was tick-capped."""
    if amount_in <= 0:
        return 0, True
    if pool.kind in XYK or pool.kind in {"solidly_stable", "camelot_stable"}:
        rin, rout, fee = _reserves_for(pool, state, token_in)
        if pool.kind == "camelot_stable":
            # Camelot's stable fee is a numerator over 1e5 (5 = 0.005%), taken off the
            # input, and the rest follows the Solidly stable invariant.
            if fee < 0 or fee >= 100_000:
                return 0, True
            kept = amount_in - amount_in * fee // 100_000
            scale_in = 10 ** (pool.decimals0 if token_in == pool.token0 else pool.decimals1)
            scale_out = 10 ** (pool.decimals1 if token_in == pool.token0 else pool.decimals0)
            return stable_out(kept, rin, rout, scale_in, scale_out, 0), True
        if pool.kind == "solidly_volatile" or pool.kind in {"univ2", "camelot"}:
            from flasharb.math_v2 import get_amount_out

            if pool.kind == "solidly_volatile":
                return volatile_out(amount_in, rin, rout, fee), True
            if pool.kind == "camelot":
                # fee stored as bps. 300 on-chain (1e5 denom) is 30 bps, same integer result.
                return get_amount_out(amount_in, rin, rout, fee), True
            return get_amount_out(amount_in, rin, rout, fee), True
        scale_in = 10 ** (pool.decimals0 if token_in == pool.token0 else pool.decimals1)
        scale_out = 10 ** (pool.decimals1 if token_in == pool.token0 else pool.decimals0)
        return stable_out(amount_in, rin, rout, scale_in, scale_out, fee), True
    if pool.kind in {"univ3", "slipstream"}:
        if state.liquidity <= 0 or state.sqrt_price_x96 <= 0 or pool.tick_spacing <= 0:
            return 0, False
        zero_for_one = token_in == pool.token0
        if token_in not in (pool.token0, pool.token1):
            return 0, False
        lower_tick = tick_floor(state.tick, pool.tick_spacing)
        upper_tick = lower_tick + pool.tick_spacing
        try:
            sqrt_lower = sqrt_price_x96_at_tick(lower_tick)
            sqrt_upper = sqrt_price_x96_at_tick(upper_tick)
        except ValueError:
            return 0, False
        out, crossed = quote_exact_in(
            state.sqrt_price_x96,
            state.liquidity,
            amount_in,
            zero_for_one,
            pool.fee_pips,
            sqrt_lower,
            sqrt_upper,
        )
        return out, not crossed
    return 0, False


def cycle_profit(
    first: Pool,
    first_state: State,
    second: Pool,
    second_state: State,
    token_in: str,
    amount_in: int,
    premium_bps: int,
    gas_token: int,
) -> tuple[int, bool]:
    token_mid = first.token1 if token_in == first.token0 else first.token0
    if token_mid not in (second.token0, second.token1):
        return -gas_token, False
    if token_in not in (second.token0, second.token1):
        return -gas_token, False
    mid, exact1 = quote_out(first, first_state, token_in, amount_in)
    if mid <= 0:
        return -gas_token, exact1
    back, exact2 = quote_out(second, second_state, token_mid, mid)
    premium = amount_in * premium_bps // 10_000
    return back - amount_in - premium - gas_token, exact1 and exact2


def best_amount(
    first: Pool,
    first_state: State,
    second: Pool,
    second_state: State,
    token_in: str,
    premium_bps: int,
    gas_token: int,
    max_in: int,
) -> tuple[int, int, bool]:
    """Return (amount_in, net_profit, exact)."""
    if max_in <= 0:
        return 0, -gas_token, True
    if first.kind in XYK and second.kind in XYK:
        rin, rout, fee1 = _reserves_for(first, first_state, token_in)
        token_mid = first.token1 if token_in == first.token0 else first.token0
        sin, sout, fee2 = _reserves_for(second, second_state, token_mid)
        # solidly volatile uses a different truncation. The closed form is the
        # Univ2 shape; hill-climbing inside optimal_input covers the wei gap.
        if first.kind == "solidly_volatile" or second.kind == "solidly_volatile":
            return _numeric(
                first, first_state, second, second_state, token_in, premium_bps, gas_token, max_in
            )
        amount, profit = optimal_input(rin, rout, fee1, sin, sout, fee2, premium_bps, gas_token, max_in)
        return amount, profit, True
    return _numeric(first, first_state, second, second_state, token_in, premium_bps, gas_token, max_in)


def _numeric(
    first: Pool,
    first_state: State,
    second: Pool,
    second_state: State,
    token_in: str,
    premium_bps: int,
    gas_token: int,
    max_in: int,
) -> tuple[int, int, bool]:
    from flasharb.math_v3 import maximize

    hi = max_in
    if first.kind in {"univ3", "slipstream"}:
        cap = _v3_cap(first, first_state, token_in)
        if cap > 0:
            hi = min(hi, cap)
    if hi <= 1:
        return 0, -gas_token, False

    def fn(x: int) -> int:
        profit, _ = cycle_profit(
            first, first_state, second, second_state, token_in, x, premium_bps, gas_token
        )
        return profit

    amount, profit = maximize(fn, 1, hi, iterations=96)
    _, exact = cycle_profit(
        first, first_state, second, second_state, token_in, amount, premium_bps, gas_token
    )
    if profit <= 0:
        return 0, profit, exact
    return amount, profit, exact


def _v3_cap(pool: Pool, state: State, token_in: str) -> int:
    if state.liquidity <= 0 or state.sqrt_price_x96 <= 0 or pool.tick_spacing <= 0:
        return 0
    lower_tick = tick_floor(state.tick, pool.tick_spacing)
    try:
        sqrt_lower = sqrt_price_x96_at_tick(lower_tick)
        sqrt_upper = sqrt_price_x96_at_tick(lower_tick + pool.tick_spacing)
    except ValueError:
        return 0
    return max_in_within_tick(
        state.sqrt_price_x96,
        state.liquidity,
        token_in == pool.token0,
        pool.fee_pips,
        sqrt_lower,
        sqrt_upper,
    )
