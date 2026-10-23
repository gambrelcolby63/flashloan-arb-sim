"""Integer accounting for Morpho Blue liquidations.

The formulas match morpho-blue `MathLib.wTaylorCompounded`, `SharesMathLib.toAssetsUp`,
and `Morpho.liquidate`'s incentive factor. Float USD is only applied after the integer
debt and collateral amounts are known. Nothing here sends a transaction.
"""

from __future__ import annotations

WAD = 10**18
ORACLE_PRICE_SCALE = 10**36
VIRTUAL_SHARES = 10**6
VIRTUAL_ASSETS = 1
LIQUIDATION_CURSOR = 3 * 10**17
MAX_LIQUIDATION_INCENTIVE_FACTOR = 115 * 10**16

# A repeat liquidator in this study. One liquidation is not a bot.
REPEAT_LIQUIDATOR_MIN = 10


def lif_wad(lltv: int) -> int:
    """min(1.15, WAD / (WAD - cursor * (WAD - lltv) / WAD)), rounded down."""
    if lltv < 0 or lltv > WAD:
        raise ValueError("lltv out of range")
    inner = WAD - (LIQUIDATION_CURSOR * (WAD - lltv) // WAD)
    if inner <= 0:
        return MAX_LIQUIDATION_INCENTIVE_FACTOR
    return min(MAX_LIQUIDATION_INCENTIVE_FACTOR, WAD * WAD // inner)


def taylor_interest(total_borrow_assets: int, rate_wad: int, elapsed: int) -> int:
    """Interest added to totalBorrowAssets. Matches wMulDown(assets, wTaylorCompounded(rate, elapsed))."""
    if total_borrow_assets <= 0 or rate_wad <= 0 or elapsed <= 0:
        return 0
    first = rate_wad * elapsed
    second = first * first // (2 * WAD)
    third = second * first // (3 * WAD)
    return total_borrow_assets * (first + second + third) // WAD


def to_assets_up(shares: int, total_assets: int, total_shares: int) -> int:
    if shares <= 0:
        return 0
    numerator = shares * (total_assets + VIRTUAL_ASSETS)
    denominator = total_shares + VIRTUAL_SHARES
    return (numerator + denominator - 1) // denominator


def max_borrow_assets(collateral: int, oracle_price: int, lltv: int) -> int:
    if collateral <= 0 or oracle_price <= 0 or lltv <= 0:
        return 0
    quoted = collateral * oracle_price // ORACLE_PRICE_SCALE
    return quoted * lltv // WAD


def is_unhealthy(borrowed: int, collateral: int, oracle_price: int, lltv: int) -> bool:
    if borrowed <= 0:
        return False
    return max_borrow_assets(collateral, oracle_price, lltv) < borrowed


def accrued_borrow_assets(total_borrow_assets: int, rate_wad: int, elapsed: int) -> int:
    return total_borrow_assets + taylor_interest(total_borrow_assets, rate_wad, elapsed)


def position_borrowed(borrow_shares: int, total_borrow_assets: int, total_borrow_shares: int, rate_wad: int, elapsed: int) -> int:
    assets = accrued_borrow_assets(total_borrow_assets, rate_wad, elapsed)
    return to_assets_up(borrow_shares, assets, total_borrow_shares)


def usd(amount: int, decimals: int, price: float | None) -> float | None:
    if price is None or decimals < 0:
        return None
    return (amount / 10**decimals) * price


def bonus_usd(
    seized: int,
    seized_decimals: int,
    seized_price: float | None,
    repaid: int,
    repaid_decimals: int,
    repaid_price: float | None,
) -> float | None:
    """Seized collateral marked to `seized_price` minus repaid debt marked to `repaid_price`.

    This is a mark, not cash. A missing price returns None rather than zero.
    """
    seized_value = usd(seized, seized_decimals, seized_price)
    repaid_value = usd(repaid, repaid_decimals, repaid_price)
    if seized_value is None or repaid_value is None:
        return None
    return seized_value - repaid_value


def take_size(collateral: int, borrowed: int, oracle_price: int, lltv: int) -> dict[str, int | bool]:
    """Collateral seized and debt repaid for a full liquidation of an unhealthy position.

    Morpho Blue has no close factor: once unhealthy, repaying every borrow share seizes
    `repay * LIF` of collateral (oracle-quoted). If that exceeds the balance, the
    liquidator seizes all collateral and repays only the incentive-quoted amount.
    The remainder is bad debt, which is not profit.
    """
    lif = lif_wad(lltv)
    if oracle_price <= 0 or borrowed <= 0 or collateral <= 0:
        return {"repay": 0, "seize": 0, "bad_debt": True, "lif": lif}
    seize_for_full = (borrowed * lif // WAD) * ORACLE_PRICE_SCALE // oracle_price
    if 0 < seize_for_full <= collateral:
        return {"repay": borrowed, "seize": seize_for_full, "bad_debt": False, "lif": lif}
    quoted = collateral * oracle_price // ORACLE_PRICE_SCALE
    repay = (quoted * WAD + lif - 1) // lif
    if repay > borrowed:
        repay = borrowed
    return {"repay": repay, "seize": collateral, "bad_debt": True, "lif": lif}


def median(values: list[float | int]) -> float | None:
    xs = sorted(values)
    n = len(xs)
    if n == 0:
        return None
    mid = n // 2
    if n % 2 == 1:
        return float(xs[mid])
    return (float(xs[mid - 1]) + float(xs[mid])) / 2.0


# Loan-size buckets in USD. The right edge is exclusive, except the last bucket.
LOAN_SIZE_BUCKETS: tuple[tuple[float, float | None, str], ...] = (
    (0, 100, "under_100"),
    (100, 1_000, "100_to_1k"),
    (1_000, 10_000, "1k_to_10k"),
    (10_000, 100_000, "10k_to_100k"),
    (100_000, 1_000_000, "100k_to_1m"),
    (1_000_000, None, "at_least_1m"),
)


def quantile(values: list[float | int], q: float) -> float | None:
    """Linear quantile on a copy. q=0.5 matches `median` for both even and odd lengths."""
    if not 0 <= q <= 1:
        raise ValueError("q out of range")
    xs = sorted(float(value) for value in values)
    n = len(xs)
    if n == 0:
        return None
    if n == 1:
        return xs[0]
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


def size_summary(values: list[float | int]) -> dict:
    """Average, median, and bucket counts. Empty input stays None, not zero."""
    xs = sorted(float(value) for value in values)
    distribution = {name: 0 for _, _, name in LOAN_SIZE_BUCKETS}
    below_zero = 0
    for value in xs:
        if value < 0:
            below_zero += 1
            continue
        for low, high, name in LOAN_SIZE_BUCKETS:
            if value >= low and (high is None or value < high):
                distribution[name] += 1
                break
    if not xs:
        return {
            "n": 0,
            "average": None,
            "median": None,
            "min": None,
            "max": None,
            "p25": None,
            "p75": None,
            "below_zero": 0,
            "distribution": distribution,
        }
    return {
        "n": len(xs),
        "average": sum(xs) / len(xs),
        "median": median(xs),
        "min": xs[0],
        "max": xs[-1],
        "p25": quantile(xs, 0.25),
        "p75": quantile(xs, 0.75),
        "below_zero": below_zero,
        "distribution": distribution,
    }


def concentration(liquidator_counts: dict[str, int], liquidator_bonus: dict[str, float], repeat_min: int = REPEAT_LIQUIDATOR_MIN) -> dict:
    """Share of liquidations and of positive bonus held by the busiest addresses."""
    total = sum(liquidator_counts.values())
    positive = {k: v for k, v in liquidator_bonus.items() if v > 0}
    bonus_total = sum(positive.values())
    ranked = sorted(liquidator_counts.items(), key=lambda item: (-item[1], item[0]))
    repeat = [addr for addr, count in ranked if count >= repeat_min]

    def _share(addrs: list[str], source: dict[str, float] | dict[str, int], denom: float) -> float | None:
        if denom <= 0:
            return None
        return sum(source.get(addr, 0) for addr in addrs) / denom

    top1 = [ranked[0][0]] if ranked else []
    top5 = [addr for addr, _ in ranked[:5]]
    return {
        "liquidations": total,
        "unique_liquidators": len(liquidator_counts),
        "repeat_min_count": repeat_min,
        "repeat_liquidators": len(repeat),
        "repeat_addresses": repeat,
        "top1_count_share": _share(top1, liquidator_counts, total),
        "top5_count_share": _share(top5, liquidator_counts, total),
        "top1_positive_bonus_share": _share(top1, positive, bonus_total),
        "top5_positive_bonus_share": _share(top5, positive, bonus_total),
        "positive_bonus_usd": bonus_total,
        "top": [
            {
                "liquidator": addr,
                "count": count,
                "positive_bonus_usd": positive.get(addr, 0.0),
                "repeat": count >= repeat_min,
            }
            for addr, count in ranked[:5]
        ],
    }


def slowly_captured(events: list[dict], n_blocks: int) -> dict:
    """Upper bound for a searcher who is `n_blocks` slower than the moment a position opens.

    Being N blocks slower than the winner of an already-finished liquidation pays nothing:
    the position is gone. The figure that can be nonzero is different. If the measured
    delay is greater than N, the position was still open N blocks after it first became
    liquidatable, so that slower searcher could still have been first.

    Events with an unknown delay are excluded, not treated as zero and not treated as captured.
    Slippage is not subtracted here. Callers must say so. Gas is subtracted only when the
    event carries `gas_usd`. Flash fee is subtracted only when `flash_fee_usd` is a number.
    """
    included = []
    unknown_delay = 0
    too_fast = 0
    missing_bonus = 0
    missing_gas = 0
    for event in events:
        delay = event.get("delay_blocks")
        censored = bool(event.get("delay_censored"))
        at_least = event.get("delay_blocks_at_least")
        slow = False
        if delay is None and not censored:
            unknown_delay += 1
            continue
        if delay is not None and delay > n_blocks:
            slow = True
        elif censored and at_least is not None and at_least > n_blocks:
            slow = True
        elif delay is not None:
            too_fast += 1
            continue
        else:
            unknown_delay += 1
            continue
        if not slow:
            too_fast += 1
            continue
        bonus = event.get("bonus_usd")
        if bonus is None:
            missing_bonus += 1
            continue
        gas = event.get("gas_usd")
        if gas is None:
            missing_gas += 1
            continue
        fee = event.get("flash_fee_usd")
        if fee is None:
            missing_gas += 1
            continue
        net = bonus - gas - fee
        included.append({**event, "net_before_slippage_usd": net})
    positive = [row["net_before_slippage_usd"] for row in included if row["net_before_slippage_usd"] > 0]
    return {
        "n_blocks": n_blocks,
        "events": len(included),
        "unknown_delay": unknown_delay,
        "not_slow_enough": too_fast,
        "missing_bonus": missing_bonus,
        "missing_gas_or_fee": missing_gas,
        "positive_net_events": len(positive),
        "positive_net_before_slippage_usd": sum(positive),
        "median_delay_blocks": median(
            [row["delay_blocks"] for row in included if row.get("delay_blocks") is not None]
        ),
    }
