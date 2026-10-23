import pytest

from flasharb.math_solidly import stable_out, volatile_out
from flasharb.math_v2 import get_amount_out, net_profit, optimal_input


def test_live_vectors_match_on_chain_quotes():
    # Camelot WETH/USDC getAmountOut, fee stored as 300 == 30 bps.
    assert get_amount_out(10**15, 137_871_552_368_452_388, 331_117_098, 30) == 2_377_239
    # BaseSwap router, 25 bps, exact.
    assert get_amount_out(10**17, 4_411_712_615_487_859_411, 10_611_377_089, 25) == 234_621_220


def test_aerodrome_volatile_matches_pair_quote():
    reserves = (892_466_641_544_550_013_560, 2_146_081_791_359)
    assert volatile_out(10**18, reserves[0], reserves[1], 30) == 2_394_774_052
    # Same integer result as the univ2 fee-on-input form for this input.
    assert get_amount_out(10**18, reserves[0], reserves[1], 30) == 2_394_774_052


def test_optimal_input_matches_brute_force():
    # Pool 1 is rich in the mid token; pool 2 is rich in the flash token.
    args = dict(
        reserve_in_1=100_000,
        reserve_out_1=400_000,
        fee1_bps=30,
        reserve_in_2=50_000,
        reserve_out_2=250_000,
        fee2_bps=30,
        premium_bps=5,
        gas_token=20,
    )
    best_x, best_p = 0, -10**18
    for x in range(1, 80_000):
        p = net_profit(x, **{k: args[k] for k in (
            "reserve_in_1", "reserve_out_1", "fee1_bps", "reserve_in_2", "reserve_out_2", "fee2_bps", "premium_bps", "gas_token"
        )})
        if p > best_p:
            best_x, best_p = x, p
    amount, profit = optimal_input(**args, max_in=80_000)
    assert best_p > 0
    # Integer profit is flat near the peak, so the chosen size can sit a little
    # off the brute-force argmax. The profit has to match.
    assert profit == best_p
    assert abs(amount - best_x) / best_x < 0.01


def test_no_trade_when_pools_agree():
    amount, profit = optimal_input(1_000_000, 1_000_000, 30, 1_000_000, 1_000_000, 30, 5, 0)
    assert amount == 0
    assert profit <= 0


def test_stable_near_peg_charges_the_fee():
    scale = 10**6
    reserve = 1_000_000 * scale
    amount = 1_000 * scale
    out = stable_out(amount, reserve, reserve, scale, scale, 5)
    # 5 bps off a near-1:1 stable swap. Allow the curve's integer slack.
    assert out > 990 * scale
    assert out < amount
    no_fee = stable_out(amount, reserve, reserve, scale, scale, 0)
    assert no_fee > out


def test_camelot_stable_fee_matches_get_amount_out():
    # WETH/wstETH Camelot stableSwap pool. Fee 5 / 1e5, then the stable invariant.
    amount = 6_590_001_263_650_890
    reserve_in = 6_590_001_263_650_890_081
    reserve_out = 18_702_610_726_656_660_668
    kept = amount - amount * 5 // 100_000
    assert stable_out(kept, reserve_in, reserve_out, 10**18, 10**18, 0) == 8_212_628_448_927_929


def test_volatile_fee_reduces_output():
    assert volatile_out(1_000_000, 10_000_000, 10_000_000, 30) < volatile_out(1_000_000, 10_000_000, 10_000_000, 5)
