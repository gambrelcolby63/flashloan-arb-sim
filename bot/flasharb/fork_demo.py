"""Deploy the executor on a local Anvil fork, simulate, then submit to that fork only.

The well-known Anvil development key is used only after the submit URL is confirmed
to be loopback. It is not a secret, and this module will not sign for any other host.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from eth_account import Account
from eth_utils import to_checksum_address
from web3 import Web3

from flasharb.discover import ROOT, load_chain
from flasharb.math_v2 import optimal_input
from flasharb.simulator import encode_execute, eth_call_execute
from flasharb.submitter import PrivateSubmitter, SubmitRefused

# Anvil's public default account. Never used unless FORK_RPC_URL is loopback.
ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
UNI_FACTORY = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
SUSHI_FACTORY = "0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac"


def _require_loopback(w3: Web3) -> None:
    host = (w3.provider.endpoint_uri and __import__("urllib.parse", fromlist=["urlparse"]).urlparse(w3.provider.endpoint_uri).hostname) or ""
    PrivateSubmitter(mode="fork", submit_url=w3.provider.endpoint_uri)._assert_loopback(w3.provider.endpoint_uri)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SubmitRefused(f"refusing fork demo against {w3.provider.endpoint_uri}")


def _call(w3: Web3, fn_data: str, to: str) -> bytes:
    return w3.eth.call({"to": to_checksum_address(to), "data": fn_data})


def main() -> int:
    import os

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    url = os.environ.get("FORK_RPC_URL", "http://127.0.0.1:8545")
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 120}))
    _require_loopback(w3)
    if not w3.is_connected():
        print(f"anvil is not up at {url}", file=sys.stderr)
        return 1

    cfg = load_chain("mainnet")
    artifact = json.loads((ROOT / "out" / "FlashLoanArbExecutor.sol" / "FlashLoanArbExecutor.json").read_text())
    bytecode = artifact["bytecode"]["object"]
    account = Account.from_key(ANVIL_KEY)
    if account.address.lower() != ANVIL_ADDRESS.lower():
        raise SubmitRefused("unexpected anvil key")
    owner = to_checksum_address(account.address)

    # Prefer the unlocked anvil account. Fall back to the published dev key, still on loopback.
    def send(tx: dict) -> str:
        tx.setdefault("from", owner)
        tx.setdefault("chainId", w3.eth.chain_id)
        tx.setdefault("nonce", w3.eth.get_transaction_count(owner))
        tx.setdefault("gas", 4_000_000)
        if w3.eth.get_balance(owner) == 0:
            raise SubmitRefused("anvil account has no balance; is this a fresh dev chain?")
        try:
            sent = w3.eth.send_transaction(tx)
            w3.eth.wait_for_transaction_receipt(sent)
            return sent.hex()
        except Exception:
            signed = account.sign_transaction(tx)
            raw = signed.raw_transaction
            sent = w3.eth.send_raw_transaction(raw)
            w3.eth.wait_for_transaction_receipt(sent)
            return sent.hex()

    from eth_abi import decode, encode

    def selector(sig: str) -> bytes:
        from eth_utils import function_signature_to_4byte_selector

        return function_signature_to_4byte_selector(sig)

    def pair_of(factory: str) -> str:
        data = selector("getPair(address,address)") + encode(["address", "address"], [WETH, USDC])
        raw = _call(w3, "0x" + data.hex(), factory)
        return to_checksum_address(decode(["address"], raw)[0])

    uni = pair_of(UNI_FACTORY)
    sushi = pair_of(SUSHI_FACTORY)
    print(f"uni {uni} sushi {sushi}")

    init = bytes.fromhex(bytecode[2:]) + encode(
        ["address", "address", "address"],
        [owner, to_checksum_address(cfg["aave_pool"]), to_checksum_address(cfg["balancer_vault"])],
    )
    # Anvil unlocks this account. The dev key is only a fallback, and only on loopback.
    deploy = {"from": owner, "data": "0x" + init.hex(), "gas": 4_500_000}
    try:
        deploy_hash = w3.eth.send_transaction(deploy)
    except Exception:
        signed = account.sign_transaction(
            {
                **deploy,
                "nonce": w3.eth.get_transaction_count(owner),
                "chainId": w3.eth.chain_id,
                "gasPrice": w3.eth.gas_price,
            }
        )
        deploy_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(deploy_hash)
    if receipt["status"] != 1 or not receipt["contractAddress"]:
        print(f"deploy failed: {receipt}", file=sys.stderr)
        return 1
    executor = receipt["contractAddress"]
    print(f"executor {executor}")

    def reserves(pair: str) -> tuple[int, int, str]:
        token0 = to_checksum_address(decode(["address"], _call(w3, "0x" + selector("token0()").hex(), pair))[0])
        raw = _call(w3, "0x" + selector("getReserves()").hex(), pair)
        r0, r1 = decode(["uint112", "uint112", "uint32"], raw)[:2]
        return int(r0), int(r1), token0

    r0, r1, token0 = reserves(sushi)
    weth_reserve = r0 if token0.lower() == WETH.lower() else r1
    dump = weth_reserve // 4
    # Wrap ETH so the demo can sell WETH into Sushi and move the price.
    deposit = selector("deposit()")
    w3.eth.wait_for_transaction_receipt(
        w3.eth.send_transaction({"from": owner, "to": to_checksum_address(WETH), "data": "0x" + deposit.hex(), "value": dump, "gas": 100_000})
    )
    transfer = selector("transfer(address,uint256)") + encode(["address", "uint256"], [sushi, dump])
    w3.eth.wait_for_transaction_receipt(
        w3.eth.send_transaction({"from": owner, "to": to_checksum_address(WETH), "data": "0x" + transfer.hex(), "gas": 120_000})
    )
    # Reserves are the pre-transfer values until swap. Read them before transfer... we already transferred.
    # Recompute the quote from the reserves observed BEFORE the transfer: weth_reserve and the other side.
    other = r1 if token0.lower() == WETH.lower() else r0
    from flasharb.math_v2 import get_amount_out

    amount_out = get_amount_out(dump, weth_reserve, other, 30)
    zero_for_one = token0.lower() == WETH.lower()
    amount0_out = 0 if zero_for_one else amount_out
    amount1_out = amount_out if zero_for_one else 0
    swap = selector("swap(uint256,uint256,address,bytes)") + encode(
        ["uint256", "uint256", "address", "bytes"],
        [amount0_out, amount1_out, owner, b""],
    )
    w3.eth.wait_for_transaction_receipt(
        w3.eth.send_transaction({"from": owner, "to": sushi, "data": "0x" + swap.hex(), "gas": 250_000})
    )
    print(f"dumped {dump} wei WETH into sushi, bought {amount_out} of the other token")

    sr0, sr1, st0 = reserves(sushi)
    ur0, ur1, ut0 = reserves(uni)

    def ordered(r0_, r1_, token0_):
        if token0_.lower() == WETH.lower():
            return r0_, r1_
        return r1_, r0_

    s_weth, s_usdc = ordered(sr0, sr1, st0)
    u_weth, u_usdc = ordered(ur0, ur1, ut0)
    # Sell WETH on the pool where WETH is richer in USDC terms (higher USDC per WETH), buy it back on the cheap pool.
    uni_px = u_usdc / u_weth
    sushi_px = s_usdc / s_weth
    if uni_px >= sushi_px:
        sell_weth, sell_usdc, buy_weth, buy_usdc = u_weth, u_usdc, s_weth, s_usdc
        sell_pool, buy_pool = uni, sushi
    else:
        sell_weth, sell_usdc, buy_weth, buy_usdc = s_weth, s_usdc, u_weth, u_usdc
        sell_pool, buy_pool = sushi, uni
    amount, profit = optimal_input(sell_weth, sell_usdc, 30, buy_usdc, buy_weth, 30, 5, gas_token=0, max_in=3 * 10**18)
    print(f"sized amount {amount} expected profit {profit}")
    if amount <= 0 or profit <= 0:
        print("no profitable size after the dump; fork state was not skewed", file=sys.stderr)
        return 1

    def plan(min_profit: int, size: int) -> bytes:
        return encode_execute(
            "aave",
            WETH,
            size,
            min_profit,
            [
                ("univ2", sell_pool, WETH, USDC, 30, 1, 0),
                ("univ2", buy_pool, USDC, WETH, 30, 1, 0),
            ],
        )

    try:
        eth_call_execute(w3, executor, owner, plan(2**255, amount))
        print("huge minProfit did not revert", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"huge minProfit reverted as required ({type(exc).__name__})")

    data = plan(1, amount)
    eth_call_execute(w3, executor, owner, data)
    print("eth_call execute succeeded")

    def send_raw(raw: bytes) -> str:
        sent = w3.eth.send_raw_transaction(raw)
        w3.eth.wait_for_transaction_receipt(sent)
        return sent.hex()

    tx = {
        "from": owner,
        "to": to_checksum_address(executor),
        "data": data,
        "gas": 1_200_000,
        "nonce": w3.eth.get_transaction_count(owner),
        "chainId": w3.eth.chain_id,
        "gasPrice": w3.eth.gas_price,
    }
    signed = account.sign_transaction(tx)
    submitter = PrivateSubmitter(mode="fork", submit_url=url, send_raw=send_raw)
    result = submitter.submit(signed.raw_transaction, "mainnet", w3.eth.block_number + 1)
    print(f"fork submit posted={result.posted} endpoint={result.endpoint} method={result.method} tx={result.tx_hash}")
    if result.posted:
        print("fork mode must not post to Flashbots", file=sys.stderr)
        return 1
    if submitter.last_payload["rpc"]["method"] != "eth_sendPrivateTransaction":
        print("mainnet payload was not Flashbots Protect", file=sys.stderr)
        return 1
    print("anvil demo ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
