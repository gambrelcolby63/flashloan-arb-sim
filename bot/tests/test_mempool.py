"""Classifier and lead-time tests. No RPC, no signing, no sending."""

from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector

from flasharb.mempool import (
    AAVE_LIQUIDATION_CALL,
    BRAVO_EXECUTE,
    CAST_VOTE,
    CHAINLINK_TRANSMIT,
    EULER_LIQUIDATE,
    FORBIDDEN_RPC_METHODS,
    GOVERNOR_EXECUTE,
    MORPHO_LIQUIDATE,
    SNAPSHOT_NOTE,
    MempoolClient,
    MempoolRefused,
    MonitorState,
    assess,
    classify_tx,
    lead_seconds,
    summarize_l2,
)


USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
AAVE_V3 = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
EULER = "0x797DD80692c3b2dAdabCe8e30C07fDE5307D48a9"
ENS = "0xFe89cc7aBB2C4183683ab71653C4cdc9B02D44b7"
BRAVO = "0xc0Da02939E1441F497fd74F78cE7Decb17B66529"
SENDER = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"


def _tx(to: str, signature: str, types: list[str], args: list, value: int = 0, sender: str = SENDER, nonce: int = 1) -> dict:
    selector = function_signature_to_4byte_selector(signature)
    body = encode(types, args) if types else b""
    return {
        "hash": "0x" + f"{nonce:064x}",
        "from": sender,
        "to": to,
        "input": "0x" + (selector + body).hex(),
        "value": hex(value),
        "blockNumber": None,
        "r": "0x" + "11" * 32,
        "s": "0x" + "22" * 32,
        "v": "0x1b",
    }


def test_selectors_are_pinned():
    assert "0x" + function_signature_to_4byte_selector("castVote(uint256,uint8)").hex() == CAST_VOTE
    assert "0x" + function_signature_to_4byte_selector("execute(address[],uint256[],bytes[],bytes32)").hex() == GOVERNOR_EXECUTE
    assert "0x" + function_signature_to_4byte_selector("execute(uint256)").hex() == BRAVO_EXECUTE
    assert "0x" + function_signature_to_4byte_selector("liquidationCall(address,address,address,uint256,bool)").hex() == AAVE_LIQUIDATION_CALL
    assert "0x" + function_signature_to_4byte_selector(
        "liquidate((address,address,address,address,uint256),address,uint256,uint256,bytes)"
    ).hex() == MORPHO_LIQUIDATE
    assert "0x" + function_signature_to_4byte_selector("liquidate(address,address,uint256,uint256)").hex() == EULER_LIQUIDATE
    assert "0x" + function_signature_to_4byte_selector(
        "transmit(bytes32[3],bytes,bytes32[],bytes32[],bytes32)"
    ).hex() == CHAINLINK_TRANSMIT


def test_snapshot_is_not_a_mempool_event():
    assert "off-chain" in SNAPSHOT_NOTE
    assert "eth_sendPrivateTransaction" in FORBIDDEN_RPC_METHODS
    assert "eth_sendRawTransaction" in FORBIDDEN_RPC_METHODS


def test_refuses_to_sign_or_send_before_any_rpc():
    client = MempoolClient("http://127.0.0.1:1")
    for method in ("eth_sendRawTransaction", "eth_sign", "eth_sendPrivateTransaction", "personal_sign"):
        try:
            client.call(method, ["0x"])
        except MempoolRefused:
            continue
        raise AssertionError(method)


def test_cast_vote_is_flagged_and_signature_is_dropped():
    tx = _tx(BRAVO, "castVote(uint256,uint8)", ["uint256", "uint8"], [7, 1])
    flag = classify_tx(tx)
    assert flag is not None
    assert flag["category"] == "governance_vote"
    assert flag["proposal_id"] == "7"
    assert flag["support"] == 1
    state = MonitorState()
    state.observe_pending(tx, now=1000.0)
    stored = state.flagged[0]
    assert "r" not in stored and "s" not in stored and "input" not in stored
    assert stored["first_seen"] == 1000.0


def test_bravo_execute_only_on_a_named_governor():
    named = _tx(BRAVO, "execute(uint256)", ["uint256"], [4])
    stranger = _tx(OTHER, "execute(uint256)", ["uint256"], [4])
    assert classify_tx(named)["category"] == "governance_execute"
    assert classify_tx(stranger) is None


def test_oz_execute_is_flagged_without_replaying_the_payload():
    tx = _tx(
        OTHER,
        "execute(address[],uint256[],bytes[],bytes32)",
        ["address[]", "uint256[]", "bytes[]", "bytes32"],
        [[OTHER], [0], [b""], b"\x11" * 32],
    )
    flag = classify_tx(tx)
    assert flag["category"] == "governance_execute"
    assert "input" not in flag


def test_aave_liquidation_size_uses_the_raw_cutoff():
    large = _tx(
        AAVE_V3,
        "liquidationCall(address,address,address,uint256,bool)",
        ["address", "address", "address", "uint256", "bool"],
        [WETH, USDC, OTHER, 10_000 * 10**6, False],
    )
    dust = _tx(
        AAVE_V3,
        "liquidationCall(address,address,address,uint256,bool)",
        ["address", "address", "address", "uint256", "bool"],
        [WETH, USDC, OTHER, 1 * 10**6, False],
        nonce=2,
    )
    elsewhere = _tx(
        OTHER,
        "liquidationCall(address,address,address,uint256,bool)",
        ["address", "address", "address", "uint256", "bool"],
        [WETH, USDC, OTHER, 10_000 * 10**6, False],
        nonce=3,
    )
    assert classify_tx(large)["large"] is True
    assert classify_tx(large)["whole_tokens"] == 10_000
    assert classify_tx(dust)["large"] is False
    assert classify_tx(dust)["category"] == "aave_liquidation"
    assert classify_tx(elsewhere) is None


def test_morpho_and_euler_only_on_known_contracts():
    morpho = _tx(
        MORPHO,
        "liquidate((address,address,address,address,uint256),address,uint256,uint256,bytes)",
        ["(address,address,address,address,uint256)", "address", "uint256", "uint256", "bytes"],
        [(USDC, WETH, OTHER, OTHER, 10**18), SENDER, 0, 1, b""],
    )
    euler = _tx(
        EULER,
        "liquidate(address,address,uint256,uint256)",
        ["address", "address", "uint256", "uint256"],
        [SENDER, WETH, 1, 0],
        nonce=2,
    )
    assert classify_tx(morpho)["category"] == "morpho_liquidation"
    assert classify_tx(morpho)["size_known"] is False
    assert classify_tx(euler)["category"] == "euler_liquidation"
    assert classify_tx({**euler, "to": OTHER}) is None


def test_oracle_transmit_is_flagged_and_not_priced():
    # The report bytes are not required for the selector match. A short stub still flags.
    tx = {
        "hash": "0x" + "03" * 32,
        "from": SENDER,
        "to": OTHER,
        "input": CHAINLINK_TRANSMIT + "00" * 32,
        "value": "0x0",
        "blockNumber": None,
    }
    flag = classify_tx(tx)
    assert flag["category"] == "oracle_update"
    assert flag["detail"] == "chainlink_transmit"
    assert flag["size_known"] is False


def test_treasury_transfer_cutoff():
    large = _tx(
        USDC,
        "transfer(address,uint256)",
        ["address", "uint256"],
        [OTHER, 10_000 * 10**6],
        sender=ENS,
    )
    small = _tx(
        USDC,
        "transfer(address,uint256)",
        ["address", "uint256"],
        [OTHER, 100 * 10**6],
        sender=ENS,
        nonce=2,
    )
    stranger = _tx(
        USDC,
        "transfer(address,uint256)",
        ["address", "uint256"],
        [OTHER, 50_000 * 10**6],
        nonce=3,
    )
    native = _tx(OTHER, "transfer(address,uint256)", ["address", "uint256"], [SENDER, 1], value=5 * 10**18, sender=ENS, nonce=4)
    # The native value is what qualifies; the ERC-20 amount is dust, so the native path still flags.
    assert classify_tx(large)["category"] == "treasury_transfer"
    assert classify_tx(large)["treasury_label"] == "ENS DAO wallet"
    assert classify_tx(small) is None
    assert classify_tx(stranger) is None
    assert classify_tx(native)["token_symbol"] == "ETH"
    assert classify_tx(native)["whole_tokens"] == 5


def test_lead_time_and_duplicate_pending_bodies():
    assert lead_seconds(1000.0, 1006) == 6
    assert lead_seconds(1000.0, 999) == -1
    state = MonitorState(sample_every=1, sample_cap=2)
    tx = _tx(OTHER, "transfer(address,uint256)", ["address", "uint256"], [SENDER, 1])
    state.observe_pending(tx, now=50.0)
    state.observe_pending(tx, now=80.0)
    assert state.pending_objects == 1
    assert state.feed_sample[0]["first_seen"] == 50.0
    mined = {**tx, "blockNumber": "0x10"}
    state.observe_pending(mined, now=90.0)
    assert state.already_mined_on_arrival == 1
    state.mark_included(tx["hash"], 16, 62)
    assert state.feed_sample[0]["lead_seconds"] == 12


def test_assessment_is_not_actionable_under_one_block():
    short = [{"category": "aave_liquidation", "large": True, "lead_seconds": 2.0}]
    result = assess(short, [])
    assert result["actionable_for_small_player"] is False
    assert result["worth_running"] is False
    assert result["flagged"]["median_lead_seconds"] == 2.0


def test_a_block_of_lead_on_a_large_liquidation_is_called_out_and_still_not_a_bot():
    long = [{"category": "aave_liquidation", "large": True, "lead_seconds": 20.0}]
    result = assess(long, [])
    assert result["actionable_for_small_player"] is True
    assert result["worth_running"] is False
    assert "did not copy" in result["verdict"]


def test_l2_zero_pending_with_blocks_is_zero_lead():
    summary = summarize_l2("Base", {"seconds": 45, "pending_hash_notifications": 0, "pending_full_objects": 0, "new_heads": 20})
    assert summary["observable_lead_seconds"] == 0
    assert "private sequencer" in summary["conclusion"]


def test_l2_silence_is_not_invented_as_zero():
    summary = summarize_l2("Arbitrum", {"seconds": 45, "pending_hash_notifications": 0, "pending_full_objects": 0, "new_heads": 0})
    assert summary["observable_lead_seconds"] is None
