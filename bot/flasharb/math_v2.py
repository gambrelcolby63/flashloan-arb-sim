"""Closed-form constant-product arbitrage sizing.

The fee is in basis points of the input (30 = 0.30%), matching `V2Math.sol`
and Uniswap V2 / Sushi / BaseSwap. Camelot stores the same 0.30% fee as 300
with a 1e5 denominator, which reduces to 30 bps.
"""

from __future__ import annotations

import math

FEE_DENOM = 10_000


def get_amount_out(
    amount_in: int,
    reserve_in: int,
    reserve_out: int,
    fee_bps: int,
    fee_denom: int = FEE_DENOM,
) -> int:
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    if fee_bps < 0 or fee_bps >= fee_denom:
        return 0
    amount_in_with_fee = amount_in * (fee_denom - fee_bps)
    return (amount_in_with_fee * reserve_out) // (reserve_in * fee_denom + amount_in_with_fee)


def round_trip_out(
    amount_in: int,
    reserve_in_1: int,
    reserve_out_1: int,
    fee1_bps: int,
    reserve_in_2: int,
    reserve_out_2: int,
    fee2_bps: int,
) -> int:
    """tokenIn -> tokenMid on pool 1, tokenMid -> tokenIn on pool 2."""
    mid = get_amount_out(amount_in, reserve_in_1, reserve_out_1, fee1_bps)
    if mid <= 0:
        return 0
    return get_amount_out(mid, reserve_in_2, reserve_out_2, fee2_bps)


def net_profit(
    amount_in: int,
    reserve_in_1: int,
    reserve_out_1: int,
    fee1_bps: int,
    reserve_in_2: int,
    reserve_out_2: int,
    fee2_bps: int,
    premium_bps: int,
    gas_token: int = 0,
) -> int:
    if amount_in <= 0:
        return -gas_token
    back = round_trip_out(
        amount_in, reserve_in_1, reserve_out_1, fee1_bps, reserve_in_2, reserve_out_2, fee2_bps
    )
    premium = amount_in * premium_bps // FEE_DENOM
    return back - amount_in - premium - gas_token


def optimal_input(
    reserve_in_1: int,
    reserve_out_1: int,
    fee1_bps: int,
    reserve_in_2: int,
    reserve_out_2: int,
    fee2_bps: int,
    premium_bps: int,
    gas_token: int = 0,
    max_in: int | None = None,
) -> tuple[int, int]:
    """Return (amount_in, net_profit) for the profit-maximising input.

    Pool 1 sells `tokenIn` for `tokenMid`. Pool 2 sells `tokenMid` back to `tokenIn`.
    `reserve_in_2` is pool 2's reserve of tokenMid. Profit is in tokenIn units,
    after the flash-loan premium and an optional gas cushion already denominated
    in tokenIn.
    """
    n1 = FEE_DENOM - fee1_bps
    n2 = FEE_DENOM - fee2_bps
    if min(n1, n2, reserve_in_1, reserve_out_1, reserve_in_2, reserve_out_2) <= 0:
        return 0, -gas_token
    if premium_bps < 0:
        return 0, -gas_token

    # E(x) = N x / (C + Dx x), repay factor kn/kd.
    # d/dx [E(x) - x kn/kd] = 0 => C + Dx x = sqrt(N * C * kd / kn).
    N = n1 * n2 * reserve_out_1 * reserve_out_2
    C = reserve_in_2 * reserve_in_1 * FEE_DENOM * FEE_DENOM
    Dx = n1 * (reserve_in_2 * FEE_DENOM + n2 * reserve_out_1)
    kn = FEE_DENOM + premium_bps
    kd = FEE_DENOM
    if Dx <= 0 or kn <= 0 or N <= 0 or C <= 0:
        return 0, -gas_token
    target = math.isqrt(N * C * kd * kn) // kn
    if target <= C:
        return 0, -gas_token
    guess = (target - C) // Dx
    if guess <= 0:
        return 0, -gas_token
    if max_in is not None:
        guess = min(guess, max_in)

    def profit_at(x: int) -> int:
        if x <= 0:
            return -10**36
        if max_in is not None and x > max_in:
            return -10**36
        return net_profit(
            x,
            reserve_in_1,
            reserve_out_1,
            fee1_bps,
            reserve_in_2,
            reserve_out_2,
            fee2_bps,
            premium_bps,
            gas_token,
        )

    best = guess
    best_p = profit_at(guess)
    step = max(1, guess // 50)
    for _ in range(16):
        improved = False
        for cand in (best - step, best + step):
            p = profit_at(cand)
            if p > best_p:
                best, best_p = cand, p
                improved = True
        if not improved:
            if step == 1:
                break
            step = max(1, step // 4)
    if best_p <= 0:
        return 0, best_p
    return best, best_p
