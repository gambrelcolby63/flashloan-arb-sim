from flasharb.liquidations import TOPIC_AAVE_LIQUIDATION, TOPIC_EULER_LIQUIDATE, TOPIC_LIQUIDATE_BORROW, TOPIC_SILO_LIQUIDATION
from flasharb.liqmath import (
    bonus_usd,
    concentration,
    is_unhealthy,
    lif_wad,
    max_borrow_assets,
    position_borrowed,
    slowly_captured,
    take_size,
    taylor_interest,
    median,
)


def test_liquidation_topics_are_pinned():
    # Checked against Moonwell/Compound, Aave V3, the Silo log in
    # 0x25fe1e666c3ee5d313d60a686e24af23d54bf5279f0ae51c40be58d905c7c799,
    # and an Euler Liquidate log.
    assert TOPIC_LIQUIDATE_BORROW == "0x298637f684da70674f26509b10f07ec2fbc77a335ab1e7d6215a4b2484d8bb52"
    assert TOPIC_AAVE_LIQUIDATION == "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"
    assert TOPIC_SILO_LIQUIDATION == "0x3a84f64446e8eada995aa9da2ddbfcd9b5d5d650503b19f024096d04c05ef2a9"
    assert TOPIC_EULER_LIQUIDATE == "0x8246cc71ab01533b5bebc672a636df812f10637ad720797319d5741d5ebb3962"


def test_lif_matches_morpho_cursor():
    # 77% LLTV. inner = 1e18 - 0.3e18 * 0.23e18 / 1e18 = 0.931e18.
    assert lif_wad(770000000000000000) == 10**36 // 931000000000000000
    # 62.5% LLTV. inner = 1 - 0.3 * 0.375 = 0.8875.
    assert lif_wad(625000000000000000) == 10**36 // 887500000000000000
    # Low LLTV would exceed the 15% cap. 30% LLTV: 1 / (1 - 0.3 * 0.7) ≈ 1.266.
    assert lif_wad(300000000000000000) == 115 * 10**16


def test_paxg_interest_and_health_at_previous_block():
    # Mainnet PAXG/PYUSD position at block 26255931, the block before liquidation
    # 0x719b010be1dd7e963f8317193ae705420137a950ad5d972a1c1ac72ec4bb00cd.
    # Debt matches the later Liquidate.repaidAssets within 1 unit. The position
    # was still healthy, so the liquidation was a same-block race.
    total_borrow = 147576381180
    rate = 2009903267
    elapsed = 45024
    assert taylor_interest(total_borrow, rate, elapsed) == 13355364
    borrowed = position_borrowed(
        borrow_shares=16242768290127,
        total_borrow_assets=total_borrow,
        total_borrow_shares=131513886936450336,
        rate_wad=rate,
        elapsed=elapsed,
    )
    assert borrowed == 18228234
    price = 3991147000000000000000000000
    collateral = 5945921407081705
    lltv = 770000000000000000
    assert max_borrow_assets(collateral, price, lltv) == 18272905
    assert is_unhealthy(borrowed, collateral, price, lltv) is False


def test_bonus_is_none_without_a_price():
    assert bonus_usd(100, 18, None, 50, 6, 1.0) is None
    assert bonus_usd(2 * 10**18, 18, 10.0, 15 * 10**6, 6, 1.0) == 5.0


def test_take_size_bad_debt_does_not_repay_the_whole_debt():
    # Collateral quotes to 50, debt is 100, LIF is a bit over 1. Seize everything,
    # repay only the quoted collateral divided by LIF.
    collateral = 50 * 10**18
    price = 10**36  # 1 collateral raw unit quotes as 1 loan raw unit, same decimals in this toy
    borrowed = 100 * 10**18
    lltv = 625000000000000000
    take = take_size(collateral, borrowed, price, lltv)
    assert take["bad_debt"] is True
    assert take["seize"] == collateral
    assert 0 < take["repay"] < borrowed


def test_median_and_concentration():
    assert median([]) is None
    assert median([4, 0, 0]) == 0
    assert median([1, 2, 3, 4]) == 2.5
    report = concentration(
        {"0xa": 12, "0xb": 3, "0xc": 1},
        {"0xa": 100.0, "0xb": -5.0, "0xc": 1.0},
    )
    assert report["repeat_liquidators"] == 1
    assert report["top"][0]["repeat"] is True
    assert report["top1_count_share"] == 12 / 16
    assert report["positive_bonus_usd"] == 101.0


def test_slowly_captured_excludes_unknown_and_fast_races():
    events = [
        {"delay_blocks": 0, "bonus_usd": 50.0, "gas_usd": 1.0, "flash_fee_usd": 0.0},
        {"delay_blocks": 8, "bonus_usd": 20.0, "gas_usd": 2.0, "flash_fee_usd": 0.0},
        {"delay_blocks": None, "bonus_usd": 100.0, "gas_usd": 1.0, "flash_fee_usd": 0.0},
        {"delay_blocks": 3, "delay_censored": True, "delay_blocks_at_least": 1000, "bonus_usd": 5.0, "gas_usd": 1.0, "flash_fee_usd": 0.0},
        {"delay_blocks": 9, "bonus_usd": 4.0, "gas_usd": None, "flash_fee_usd": 0.0},
    ]
    # The censored row also has delay_blocks set; delay > N includes it once.
    report = slowly_captured(events, 5)
    assert report["unknown_delay"] == 1
    assert report["not_slow_enough"] == 1
    assert report["missing_gas_or_fee"] == 1
    assert report["positive_net_events"] == 2
    assert report["positive_net_before_slippage_usd"] == (20.0 - 2.0) + (5.0 - 1.0)
