"""Encode `FlashLoanArbExecutor.execute` and eth_call it. This module does not broadcast."""

from __future__ import annotations

from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector

EXECUTE_SIG = "execute((uint8,address,uint256,uint256,(uint8,address,address,address,uint256,uint256,uint160)[]))"

KIND = {
    "univ2": 0,
    "univ3": 1,
    "solidly_volatile": 2,
    "solidly_stable": 2,
    "solidly": 2,
    "camelot": 3,
    "algebra": 4,
    "slipstream": 1,
}

SOURCE = {"aave": 0, "balancer": 1}


def execute_selector() -> bytes:
    return function_signature_to_4byte_selector(EXECUTE_SIG)


def encode_execute(
    source: str,
    flash_asset: str,
    flash_amount: int,
    min_profit: int,
    steps: list[tuple],
) -> bytes:
    """`steps` entries are (kind, pool, token_in, token_out, fee_bps, amount_out_min, sqrt_limit)."""
    encoded_steps = []
    for kind, pool, token_in, token_out, fee_bps, amount_out_min, sqrt_limit in steps:
        kind_id = kind if isinstance(kind, int) else KIND[kind]
        encoded_steps.append(
            (kind_id, pool, token_in, token_out, int(fee_bps), int(amount_out_min), int(sqrt_limit))
        )
    body = encode(
        ["(uint8,address,uint256,uint256,(uint8,address,address,address,uint256,uint256,uint160)[])"],
        [(SOURCE[source] if isinstance(source, str) else int(source), flash_asset, int(flash_amount), int(min_profit), encoded_steps)],
    )
    return execute_selector() + body


def eth_call_execute(w3, executor: str, caller: str, data: bytes, block: int | str = "latest") -> bytes:
    """Simulate `execute`. Raises the node's revert if the plan is not profitable."""
    return w3.eth.call({"from": caller, "to": executor, "data": data}, block_identifier=block)
