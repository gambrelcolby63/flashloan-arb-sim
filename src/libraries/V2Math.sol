// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @notice Constant-product quote used by Uniswap V2, SushiSwap, and BaseSwap.
///         `feeBps` is the fee in basis points (30 = 0.30%) deducted from the input.
library V2Math {
    error FeeTooHigh();

    function getAmountOut(uint256 amountIn, uint256 reserveIn, uint256 reserveOut, uint256 feeBps)
        internal
        pure
        returns (uint256)
    {
        if (amountIn == 0 || reserveIn == 0 || reserveOut == 0) return 0;
        if (feeBps >= 10_000) revert FeeTooHigh();
        uint256 amountInWithFee = amountIn * (10_000 - feeBps);
        return (amountInWithFee * reserveOut) / (reserveIn * 10_000 + amountInWithFee);
    }
}
