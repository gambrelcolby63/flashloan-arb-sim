"""Aerodrome / Velodrome quotes.

Volatile pools are constant product after a fee taken off the input
(`amountIn -= amountIn * fee / 10_000`). Stable pools use Solidly's
x^3 y + y^3 x invariant. The stable solver below follows that published
curve; `analyze` drops stable pools when a same-block `getAmountOut` check
disagrees.
"""

from __future__ import annotations

ONE = 10**18


def volatile_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int) -> int:
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0 or fee_bps < 0 or fee_bps >= 10_000:
        return 0
    kept = amount_in - (amount_in * fee_bps) // 10_000
    if kept <= 0:
        return 0
    return (kept * reserve_out) // (reserve_in + kept)


def _k(x: int, y: int) -> int:
    a = (x * y) // ONE
    b = (x * x) // ONE + (y * y) // ONE
    return (a * b) // ONE


def _f(x0: int, y: int) -> int:
    yy = (y * y) // ONE
    yyy = (yy * y) // ONE
    xx = (x0 * x0) // ONE
    xxx = (xx * x0) // ONE
    return (x0 * yyy) // ONE + (xxx * y) // ONE


def _d(x0: int, y: int) -> int:
    yy = (y * y) // ONE
    xx = (x0 * x0) // ONE
    xxx = (xx * x0) // ONE
    return (3 * x0 * yy) // ONE + xxx


def _get_y(x0: int, xy: int, y: int) -> int:
    for _ in range(255):
        y_prev = y
        k = _f(x0, y)
        d = _d(x0, y)
        if d == 0:
            return y
        if k < xy:
            dy = ((xy - k) * ONE) // d
            y = y + dy
        else:
            dy = ((k - xy) * ONE) // d
            if dy >= y:
                y = 0
            else:
                y = y - dy
        if y > y_prev:
            if y - y_prev <= 1:
                return y
        elif y_prev - y <= 1:
            return y
    return y


def stable_out(
    amount_in: int,
    reserve_in: int,
    reserve_out: int,
    scale_in: int,
    scale_out: int,
    fee_bps: int,
) -> int:
    """`scale_*` is 10 ** token_decimals, matching the pair's decimals0/decimals1."""
    if min(amount_in, reserve_in, reserve_out, scale_in, scale_out) <= 0:
        return 0
    if fee_bps < 0 or fee_bps >= 10_000:
        return 0
    amount_in = amount_in - (amount_in * fee_bps) // 10_000
    if amount_in <= 0:
        return 0
    x_res = (reserve_in * ONE) // scale_in
    y_res = (reserve_out * ONE) // scale_out
    xy = _k(x_res, y_res)
    amount_in_norm = (amount_in * ONE) // scale_in
    y_out_norm = y_res - _get_y(amount_in_norm + x_res, xy, y_res)
    if y_out_norm <= 0:
        return 0
    return (y_out_norm * scale_out) // ONE
