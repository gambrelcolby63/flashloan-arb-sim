// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20, IBalancerVault, ISolidlyPair} from "./interfaces/Interfaces.sol";
import {SafeTransfer} from "./libraries/SafeTransfer.sol";

interface IMorphoBlue {
    struct MarketParams {
        address loanToken;
        address collateralToken;
        address oracle;
        address irm;
        uint256 lltv;
    }

    function flashLoan(address token, uint256 assets, bytes calldata data) external;

    function liquidate(
        MarketParams calldata marketParams,
        address borrower,
        uint256 seizedAssets,
        uint256 repaidShares,
        bytes calldata data
    ) external returns (uint256, uint256);
}

/// @title FlashLoanLiquidator
/// @notice Owner-only helper that flash-liquidates one Morpho Blue position and
///         sells the seized collateral on a Solidly-style pool (Aerodrome / Velodrome).
/// @dev This exists to prove the mechanic on a fork. It does not search for positions,
///      it does not hold a key, and nothing in this repo broadcasts it.
///      Morpho's flash loan premium is zero. Balancer V2's flash fee is zero.
///      The contract keeps whatever loan-token surplus remains after repayment.
///      Gas is paid by the caller and is not part of `minProfit`.
contract FlashLoanLiquidator {
    uint256 private constant _NOT_ENTERED = 1;
    uint256 private constant _ENTERED = 2;

    address public immutable owner;
    address public immutable morpho;
    address public immutable balancerVault;

    uint256 private _status;

    struct Job {
        IMorphoBlue.MarketParams market;
        address borrower;
        uint256 seizedAssets;
        uint256 repaidShares;
        address swapPool;
        uint256 minSwapOut;
        uint256 minProfit;
    }

    error NotOwner();
    error Reentrancy();
    error BadCallback();
    error ZeroAddress();
    error InconsistentInput();
    error Slippage();
    error Shortfall();

    event LiquidationExecuted(address indexed loanToken, uint256 flashAssets, uint256 profit);

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier nonReentrant() {
        if (_status == _ENTERED) revert Reentrancy();
        _status = _ENTERED;
        _;
        _status = _NOT_ENTERED;
    }

    constructor(address _owner, address _morpho, address _balancerVault) {
        if (_owner == address(0) || _morpho == address(0)) revert ZeroAddress();
        owner = _owner;
        morpho = _morpho;
        balancerVault = _balancerVault;
        _status = _NOT_ENTERED;
    }

    /// @notice Morpho Blue flash-loan of the loan token, liquidate, swap, repay. Premium is zero.
    function liquidateWithMorphoFlash(Job calldata job, uint256 flashAssets) external onlyOwner nonReentrant {
        if (flashAssets == 0) revert InconsistentInput();
        if (job.market.loanToken == address(0) || job.swapPool == address(0)) revert ZeroAddress();
        IMorphoBlue(morpho).flashLoan(job.market.loanToken, flashAssets, abi.encode(job));
    }

    /// @notice Balancer V2 flash-loan. The vault's fee is zero. The loan token is transferred back.
    function liquidateWithBalancerFlash(Job calldata job, uint256 flashAssets) external onlyOwner nonReentrant {
        if (balancerVault == address(0) || flashAssets == 0) revert InconsistentInput();
        if (job.market.loanToken == address(0) || job.swapPool == address(0)) revert ZeroAddress();
        address[] memory tokens = new address[](1);
        uint256[] memory amounts = new uint256[](1);
        tokens[0] = job.market.loanToken;
        amounts[0] = flashAssets;
        IBalancerVault(balancerVault).flashLoan(address(this), tokens, amounts, abi.encode(job));
    }

    function onMorphoFlashLoan(uint256 assets, bytes calldata data) external {
        if (msg.sender != morpho) revert BadCallback();
        if (_status != _ENTERED) revert Reentrancy();
        Job memory job = abi.decode(data, (Job));
        uint256 profit = _liquidateAndSwap(job, assets, 0);
        emit LiquidationExecuted(job.market.loanToken, assets, profit);
        // Morpho pulls `assets` with transferFrom after this returns. Premium is zero.
    }

    function receiveFlashLoan(
        address[] calldata tokens,
        uint256[] calldata amounts,
        uint256[] calldata feeAmounts,
        bytes calldata userData
    ) external {
        if (msg.sender != balancerVault) revert BadCallback();
        if (_status != _ENTERED) revert Reentrancy();
        if (tokens.length != 1 || amounts.length != 1 || feeAmounts.length != 1) revert BadCallback();
        Job memory job = abi.decode(userData, (Job));
        if (tokens[0] != job.market.loanToken) revert BadCallback();
        uint256 profit = _liquidateAndSwap(job, amounts[0], feeAmounts[0]);
        SafeTransfer.safeTransfer(tokens[0], balancerVault, amounts[0] + feeAmounts[0]);
        emit LiquidationExecuted(job.market.loanToken, amounts[0], profit);
    }

    function withdraw(address token, address to, uint256 amount) external onlyOwner nonReentrant {
        if (to == address(0) || token == address(0)) revert ZeroAddress();
        uint256 bal = IERC20(token).balanceOf(address(this));
        uint256 amt = amount == type(uint256).max ? bal : amount;
        SafeTransfer.safeTransfer(token, to, amt);
    }

    function _liquidateAndSwap(Job memory job, uint256 flashAssets, uint256 fee) internal returns (uint256 profit) {
        if ((job.seizedAssets == 0) == (job.repaidShares == 0)) revert InconsistentInput();
        SafeTransfer.forceApprove(job.market.loanToken, morpho, type(uint256).max);
        IMorphoBlue(morpho).liquidate(job.market, job.borrower, job.seizedAssets, job.repaidShares, "");

        uint256 seized = IERC20(job.market.collateralToken).balanceOf(address(this));
        if (seized == 0) revert Shortfall();
        uint256 out = _swapSolidly(job.swapPool, job.market.collateralToken, job.market.loanToken, seized, job.minSwapOut);

        uint256 bal = IERC20(job.market.loanToken).balanceOf(address(this));
        uint256 owed = flashAssets + fee;
        if (bal < owed + job.minProfit) revert Shortfall();
        profit = bal - owed;
        // `out` is retained so a zero-output swap cannot be optimized away from the check above.
        if (out < job.minSwapOut) revert Slippage();
    }

    function _swapSolidly(address pool, address tokenIn, address tokenOut, uint256 amountIn, uint256 minOut)
        internal
        returns (uint256 amountOut)
    {
        ISolidlyPair pair = ISolidlyPair(pool);
        address token0 = pair.token0();
        if (tokenIn != token0 && tokenIn != pair.token1()) revert BadCallback();
        address other = tokenIn == token0 ? pair.token1() : token0;
        if (other != tokenOut) revert BadCallback();
        amountOut = pair.getAmountOut(amountIn, tokenIn);
        if (amountOut < minOut || amountOut == 0) revert Slippage();
        bool zeroForOne = tokenIn == token0;
        SafeTransfer.safeTransfer(tokenIn, pool, amountIn);
        pair.swap(zeroForOne ? 0 : amountOut, zeroForOne ? amountOut : 0, address(this), "");
    }
}
