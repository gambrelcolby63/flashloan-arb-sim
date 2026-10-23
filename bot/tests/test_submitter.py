import pytest

from flasharb.submitter import LIVE_ACK, PrivateSubmitter, SubmitRefused


def test_simulate_builds_flashbots_payload_and_does_not_send():
    sent = []
    posted = []
    bot = PrivateSubmitter(
        mode="simulate",
        send_raw=lambda raw: sent.append(raw) or "0xdead",
        transport=lambda endpoint, body: posted.append((endpoint, body)),
    )
    result = bot.submit(b"\x01\x02", "mainnet", 123)
    assert result.posted is False
    assert result.endpoint == "not-sent"
    assert result.method == "eth_sendPrivateTransaction"
    assert sent == []
    assert posted == []
    assert bot.last_payload["endpoint"].startswith("https://rpc.flashbots.net")
    assert bot.last_payload["rpc"]["params"][0]["maxBlockNumber"] == hex(123)


def test_fork_refuses_a_public_host():
    bot = PrivateSubmitter(
        mode="fork",
        submit_url="https://ethereum-rpc.publicnode.com",
        send_raw=lambda raw: "0x1",
    )
    with pytest.raises(SubmitRefused):
        bot.submit(b"\x01", "mainnet", 1)


def test_fork_records_sequencer_payload_but_sends_only_to_loopback():
    sent = []
    bot = PrivateSubmitter(
        mode="fork",
        submit_url="http://127.0.0.1:8545",
        send_raw=lambda raw: sent.append(raw) or "0xabc",
    )
    result = bot.submit(b"\xaa", "base", 5)
    assert result.posted is False
    assert result.tx_hash == "0xabc"
    assert result.endpoint == "http://127.0.0.1:8545"
    assert sent == [b"\xaa"]
    assert bot.last_payload["rpc"]["method"] == "eth_sendRawTransaction"
    assert "mainnet-sequencer.base.org" in bot.last_payload["endpoint"]


def test_arbitrum_payload_is_sequencer_direct():
    payload = PrivateSubmitter().build_payload(b"\x01", "arbitrum", 9)
    assert payload["rpc"]["method"] == "eth_sendRawTransaction"
    assert "arb1.arbitrum.io" in payload["endpoint"]
    assert "Timeboost" in payload["note"]


def test_live_mode_without_the_ack_is_refused():
    called = []
    bot = PrivateSubmitter(mode="live", allow_live="", transport=lambda *args: called.append(args))
    with pytest.raises(SubmitRefused):
        bot.submit(b"\x01", "mainnet", 1)
    assert called == []


def test_live_mode_refuses_loopback_even_with_the_ack():
    bot = PrivateSubmitter(
        mode="live",
        allow_live=LIVE_ACK,
        transport=lambda endpoint, body: (_ for _ in ()).throw(AssertionError("should not post")),
    )
    with pytest.raises(SubmitRefused):
        bot._reject_if_read_rpc("http://127.0.0.1:8545")
    with pytest.raises(SubmitRefused):
        bot._reject_if_read_rpc("http://localhost:8545")
