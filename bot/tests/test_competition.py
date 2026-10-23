from flasharb.competition import ReplayTx, classify


def _world(values: dict[tuple, int]):
    state = {"p": dict(values)}

    def profit_fn(snapshot, a, b, direction):
        return int(snapshot["p"].get((a, b, direction), 0))

    def keys_for(touched):
        key = ("A", "B", "W")
        if "A" in touched or "B" in touched:
            return [key]
        return []

    def set_profit(value):
        def apply(snapshot, value=value):
            snapshot["p"][("A", "B", "W")] = value

        return apply

    return state, profit_fn, keys_for, set_profit


def test_same_block_capture():
    state, profit_fn, keys_for, set_profit = _world({})
    txs = [
        ReplayTx(10, 0, "0x1", ("A",), set_profit(100)),
        ReplayTx(10, 1, "0x2", ("A", "B"), set_profit(0)),
    ]
    report = classify(10, 12, state, txs, profit_fn, keys_for)
    assert report.counts()["captured_same_block"] == 1
    assert report.counts()["opportunities"] == 1
    assert report.episodes[0].remaining_profit == 0


def test_next_block_capture():
    state, profit_fn, keys_for, set_profit = _world({})
    txs = [
        ReplayTx(10, 0, "0x1", ("A",), set_profit(80)),
        ReplayTx(11, 0, "0x2", ("A", "B"), set_profit(0)),
    ]
    report = classify(10, 12, state, txs, profit_fn, keys_for)
    assert report.counts()["captured_next_block"] == 1
    assert report.episodes[0].remaining_profit == 0


def test_uncaptured_keeps_remaining_profit():
    state, profit_fn, keys_for, set_profit = _world({})
    txs = [ReplayTx(10, 0, "0x1", ("A",), set_profit(70))]
    report = classify(10, 12, state, txs, profit_fn, keys_for)
    assert report.counts()["uncaptured"] == 1
    assert report.episodes[0].remaining_profit == 70


def test_close_without_two_pool_tx_is_ambiguous():
    state, profit_fn, keys_for, set_profit = _world({})
    txs = [
        ReplayTx(10, 0, "0x1", ("A",), set_profit(40)),
        ReplayTx(11, 0, "0x2", ("A",), set_profit(0)),
    ]
    report = classify(10, 12, state, txs, profit_fn, keys_for)
    assert report.counts()["ambiguous"] == 1
    assert report.counts()["captured_same_block"] == 0
    assert report.counts()["captured_next_block"] == 0


def test_open_spread_is_not_counted_twice_and_can_reopen_after_it_dies():
    state, profit_fn, keys_for, set_profit = _world({})
    txs = [
        ReplayTx(10, 0, "0x1", ("A",), set_profit(50)),
        ReplayTx(12, 0, "0x2", ("B",), set_profit(0)),
        ReplayTx(13, 0, "0x3", ("A",), set_profit(25)),
    ]
    report = classify(10, 15, state, txs, profit_fn, keys_for)
    outcomes = [ep.outcome for ep in report.episodes]
    assert outcomes.count("uncaptured") == 2
    assert report.counts()["opportunities"] == 2
    assert [ep.remaining_profit for ep in report.episodes] == [50, 25]


def test_no_open_on_the_last_block():
    state, profit_fn, keys_for, set_profit = _world({})
    txs = [ReplayTx(12, 0, "0x1", ("A",), set_profit(100))]
    report = classify(10, 12, state, txs, profit_fn, keys_for)
    assert report.counts()["opportunities"] == 0
