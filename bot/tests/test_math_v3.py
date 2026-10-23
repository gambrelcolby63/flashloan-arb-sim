from flasharb.math_v3 import maximize, quote_exact_in, sqrt_price_x96_at_tick, tick_floor


def _band(tick: int = 0, spacing: int = 10):
    lower = tick_floor(tick, spacing)
    sqrt_lower = sqrt_price_x96_at_tick(lower)
    sqrt_upper = sqrt_price_x96_at_tick(lower + spacing)
    sqrt_p = (sqrt_lower + sqrt_upper) // 2
    return sqrt_p, sqrt_lower, sqrt_upper


def test_tick_floor_matches_python_floor_division():
    assert tick_floor(5, 10) == 0
    assert tick_floor(-5, 10) == -10
    assert tick_floor(-10, 10) == -10


def test_fee_reduces_output_and_quote_is_positive():
    sqrt_p, lo, hi = _band()
    liquidity = 10**18
    amount = 10**12
    cheap, crossed_cheap = quote_exact_in(sqrt_p, liquidity, amount, True, 100, lo, hi)
    costly, _ = quote_exact_in(sqrt_p, liquidity, amount, True, 3000, lo, hi)
    assert cheap > costly > 0
    assert crossed_cheap is False


def test_output_grows_with_input_inside_the_tick():
    sqrt_p, lo, hi = _band()
    liquidity = 10**18
    outs = []
    for amount in (10**8, 10**10, 10**12, 10**14):
        out, crossed = quote_exact_in(sqrt_p, liquidity, amount, False, 500, lo, hi)
        assert crossed is False
        outs.append(out)
    assert outs[0] < outs[1] < outs[2] < outs[3]


def test_maximize_finds_a_unimodal_peak():
    def fn(x: int) -> int:
        return -((x - 50) ** 2)

    x, p = maximize(fn, 0, 1_000, iterations=40)
    assert x == 50
    assert p == 0


def test_maximize_does_not_walk_a_huge_range():
    target = 10**12

    def fn(x: int) -> int:
        return -abs(x - target)

    x, p = maximize(fn, 0, 10**18, iterations=160)
    assert abs(x - target) / target < 0.001
    assert p == -abs(x - target)
