"""Within-tick Uniswap V3 quotes and a numeric search for arb size.

Tick bounds use the mathematical definition sqrt(1.0001^tick) * 2^96, written
here from that definition (not copied from the Uniswap V3 sources). The result
can differ from on-chain TickMath by a few units in the last place. Execution
never trusts this quote: the bot eth_calls the executor, and the competition
report drops V3 pools when a QuoterV2 spot check disagrees by more than 1%.
"""

from __future__ import annotations

import decimal
from typing import Callable

Q96 = 1 << 96
MIN_TICK = -887272
MAX_TICK = 887272
_SQRT_CACHE: dict[int, int] = {}


def sqrt_price_x96_at_tick(tick: int) -> int:
    cached = _SQRT_CACHE.get(tick)
    if cached is not None:
        return cached
    if tick < MIN_TICK or tick > MAX_TICK:
        raise ValueError(f"tick {tick} out of range")
    with decimal.localcontext() as ctx:
        ctx.prec = 80
        ctx.rounding = decimal.ROUND_DOWN
        sqrt_price = (decimal.Decimal("1.0001") ** tick).sqrt() * decimal.Decimal(Q96)
        out = int(sqrt_price.to_integral_value(rounding=decimal.ROUND_DOWN))
    _SQRT_CACHE[tick] = out
    return out


def tick_floor(tick: int, spacing: int) -> int:
    if spacing <= 0:
        raise ValueError("spacing")
    # Python floor division matches Uniswap's negative-tick adjustment.
    return (tick // spacing) * spacing


def _amount0_delta(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    if sqrt_a <= 0 or liquidity <= 0:
        return 0
    return (liquidity * Q96 * (sqrt_b - sqrt_a)) // (sqrt_a * sqrt_b)


def _amount1_delta(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    if liquidity <= 0:
        return 0
    return (liquidity * (sqrt_b - sqrt_a)) // Q96


def _next_sqrt_from_amount0(sqrt_p: int, liquidity: int, amount: int, add: bool) -> int:
    numerator = liquidity * Q96
    if add:
        return (numerator * sqrt_p) // (numerator + amount * sqrt_p)
    denominator = numerator - amount * sqrt_p
    if denominator <= 0:
        return 0
    return (numerator * sqrt_p) // denominator


def _next_sqrt_from_amount1(sqrt_p: int, liquidity: int, amount: int, add: bool) -> int:
    if liquidity <= 0:
        return sqrt_p
    quotient = (amount * Q96) // liquidity
    if add:
        return sqrt_p + quotient
    return sqrt_p - quotient if sqrt_p > quotient else 0


def max_in_within_tick(
    sqrt_price_x96: int,
    liquidity: int,
    zero_for_one: bool,
    fee_pips: int,
    sqrt_lower: int,
    sqrt_upper: int,
) -> int:
    """Largest input (before the fee) that stays inside the current tick."""
    if liquidity <= 0 or sqrt_lower <= 0 or sqrt_upper <= sqrt_lower:
        return 0
    if not (sqrt_lower <= sqrt_price_x96 <= sqrt_upper):
        return 0
    fee_factor = 1_000_000 - fee_pips
    if fee_factor <= 0:
        return 0
    if zero_for_one:
        amount_after_fee = _amount0_delta(sqrt_lower, sqrt_price_x96, liquidity)
    else:
        amount_after_fee = _amount1_delta(sqrt_price_x96, sqrt_upper, liquidity)
    if amount_after_fee <= 0:
        return 0
    return amount_after_fee * 1_000_000 // fee_factor


def quote_exact_in(
    sqrt_price_x96: int,
    liquidity: int,
    amount_in: int,
    zero_for_one: bool,
    fee_pips: int,
    sqrt_lower: int,
    sqrt_upper: int,
) -> tuple[int, bool]:
    """Return (amount_out, crossed_tick).

    If the trade would leave the current tick, the output is the amount received
    by trading only up to the boundary. That is a lower bound: later ticks can
    only add more output. Callers that need an exact V3 number must eth_call.
    """
    if amount_in <= 0 or liquidity <= 0 or fee_pips < 0 or fee_pips >= 1_000_000:
        return 0, False
    cap = max_in_within_tick(sqrt_price_x96, liquidity, zero_for_one, fee_pips, sqrt_lower, sqrt_upper)
    if cap <= 0:
        return 0, True
    crossed = amount_in > cap
    used_in = amount_in if not crossed else cap
    amount_after_fee = used_in * (1_000_000 - fee_pips) // 1_000_000
    if amount_after_fee <= 0:
        return 0, crossed
    if zero_for_one:
        next_sqrt = _next_sqrt_from_amount0(sqrt_price_x96, liquidity, amount_after_fee, add=True)
        if next_sqrt <= 0:
            return 0, True
        out = _amount1_delta(next_sqrt, sqrt_price_x96, liquidity)
    else:
        next_sqrt = _next_sqrt_from_amount1(sqrt_price_x96, liquidity, amount_after_fee, add=True)
        out = _amount0_delta(sqrt_price_x96, next_sqrt, liquidity)
    return out, crossed


def maximize(fn: Callable[[int], int], lo: int, hi: int, iterations: int = 96) -> tuple[int, int]:
    """Ternary search for a unimodal integer function. Returns (x, fn(x)).

    The final scan is only a handful of integers. A wide leftover range is not
    walked, so this stays cheap when `hi` is 10^24.
    """
    if hi < lo:
        return 0, -1
    lo = max(lo, 0)
    hi = max(hi, lo)
    best_x, best_p = lo, fn(lo)
    p_hi = fn(hi)
    if p_hi > best_p:
        best_x, best_p = hi, p_hi
    for _ in range(iterations):
        if hi - lo <= 2:
            break
        span = hi - lo
        m1 = lo + span // 3
        m2 = hi - span // 3
        if m2 <= m1:
            m2 = min(hi, m1 + 1)
        if m2 > hi or m1 <= lo:
            break
        p1, p2 = fn(m1), fn(m2)
        if p1 > best_p:
            best_x, best_p = m1, p1
        if p2 > best_p:
            best_x, best_p = m2, p2
        if p1 < p2:
            lo = m1
        else:
            hi = m2
    if hi - lo <= 8:
        for x in range(max(lo, 0), hi + 1):
            p = fn(x)
            if p > best_p:
                best_x, best_p = x, p
    return best_x, best_p
