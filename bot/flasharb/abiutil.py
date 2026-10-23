"""Small ABI helpers. Selectors that the scanner depends on are pinned."""

from __future__ import annotations

from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

# Pinned so a signature typo cannot silently scan the wrong function.
SELECTORS = {
    "getPair(address,address)": "e6a43905",
    "getPool(address,address,uint24)": "1698ee82",
    "getPool(address,bool)": None,  # not used
    "getPool(address,address,bool)": "79bc57d5",
    "getPool(address,address,int24)": "28af8d0b",
    "getAmountOut(uint256,address)": "f140a35a",
    "stable()": "22be3de1",
    "stableSwap()": "9e548b7f",
    "token0()": "0dfe1681",
    "token1()": "d21220a7",
    "fee()": "ddca3f43",
    "getFee(address,bool)": "cc56b2c5",
    "slot0()": "3850c7bd",
    "liquidity()": "1a686502",
    "getReserves()": "0902f1ac",
    "getAmountsOut(uint256,address[])": "d06ca61f",
    "balanceOf(address)": "70a08231",
    "totalSupply()": "18160ddd",
    "aggregate3((address,bool,bytes)[])": "82ad56cb",
    "tickSpacing()": None,
}

TOPICS = {
    "sync112": "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1",
    "sync256": "0xcf2aa50876cdfbb541206f89af0ee78d44a2abf8d328e37fa4917f982149848a",
    "swap_v3": "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67",
}

ZERO = "0x" + "0" * 40


def selector(signature: str) -> bytes:
    raw = function_signature_to_4byte_selector(signature)
    pinned = SELECTORS.get(signature)
    if pinned is not None and raw.hex() != pinned:
        raise RuntimeError(f"selector drift for {signature}: {raw.hex()} != {pinned}")
    return raw


def calldata(signature: str, types: list[str], args: list) -> bytes:
    return selector(signature) + encode(types, args)


def words(data: bytes) -> list[int]:
    if not data:
        return []
    return [int.from_bytes(data[i : i + 32], "big") for i in range(0, len(data) - 31, 32)]


def word_address(word: int) -> str:
    return "0x" + f"{word:040x}"[-40:]


def as_int256(word: int) -> int:
    if word >= 2**255:
        return word - 2**256
    return word


def checksum(address: str) -> str:
    return to_checksum_address(address)


def norm(address: str) -> str:
    return address.lower()


def decode_aggregate(data: bytes) -> list[tuple[bool, bytes]]:
    if not data:
        return []
    rows = decode(["(bool,bytes)[]"], data)[0]
    return [(bool(ok), bytes(ret)) for ok, ret in rows]
