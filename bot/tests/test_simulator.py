import shutil
import subprocess

import pytest

from flasharb.abiutil import selector
from flasharb.simulator import encode_execute, execute_selector


def test_pinned_selectors_match_eth_utils():
    # Importing the helper and calling these locks the scanner to the signatures
    # the fork tests already use. A drift raises inside selector().
    assert selector("getPair(address,address)").hex() == "e6a43905"
    assert selector("getReserves()").hex() == "0902f1ac"
    assert selector("aggregate3((address,bool,bytes)[])").hex() == "82ad56cb"
    assert selector("slot0()").hex() == "3850c7bd"


def test_execute_selector_matches_cast():
    if shutil.which("cast") is None:
        pytest.skip("cast not installed")
    sig = "execute((uint8,address,uint256,uint256,(uint8,address,address,address,uint256,uint256,uint160)[]))"
    expected = subprocess.check_output(["cast", "sig", sig], text=True).strip()
    assert "0x" + execute_selector().hex() == expected


def test_encode_execute_starts_with_the_selector():
    data = encode_execute(
        "aave",
        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        10**18,
        1,
        [
            (
                "univ2",
                "0xB4e16d0168e52d35cacd2c6185b44281Ec28C9Dc",
                "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                30,
                1,
                0,
            )
        ],
    )
    assert data.startswith(execute_selector())
    assert len(data) > 4
