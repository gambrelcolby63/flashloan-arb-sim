// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {
    IAaveV3Pool,
    IAlgebraSwapCallback,
    IBalancerVault,
    ICamelotPair,
    IERC20,
    ISolidlyPair,
    IUniswapV2Pair,
    IUniswapV3Pool,
    IUniswapV3SwapCallback
} from "./interfaces/Interfaces.sol";
import {SafeTransfer} from "./libraries/SafeTransfer.sol";
import {V2Math} from "./libraries/V2Math.sol";

/// @title FlashLoanArbExecutor
/// @notice Owner-only, non-upgradeable executor for a two-or-three hop DEX arbitrage
///         funded by an Aave V3 flash loan or a Balancer V2 (zero-fee) flash loan.
/// @dev The contract does not search for trades. The off-chain bot chooses the route,
///      the size, and `minProfit`. This contract borrows, swaps, repays, and reverts
///      unless the token balance increased by at least `minProfit` AFTER the flash-loan
///      premium. Gas is paid by the caller in ETH and is not visible on-chain; the bot
///      folds a gas cushion into `minProfit` when it wants one.
///
///      Trust model: `owner` is trusted (they pick pools). Pools and tokens are not.
///      There is no proxy, no delegatecall, and no owner-transfer function.
contract FlashLoanArbExecutor is IUniswapV3SwapCallback, IAlgebraSwapCallback {
    enum DexKind {
        UniV2,
        UniV3,
        Solidly,
        Camelot,
        Algebra
    }

    enum FlashSource {
        AaveV3,
        BalancerV2
    }

    struct SwapStep {
        DexKind kind;
        address pool;
        address tokenIn;
        address tokenOut;
        uint256 feeBps;
        uint256 amountOutMin;
        uint160 sqrtPriceLimitX96;
    }

    struct ArbPlan {
        FlashSource source;
        address flashAsset;
        uint256 flashAmount;
        uint256 minProfit;
        SwapStep[] steps;
    }

    uint256 private constant _NOT_ENTERED = 1;
    uint256 private constant _ENTERED = 2;
    uint256 public constant MAX_STEPS = 4;

    uint160 internal constant MIN_SQRT_RATIO = 4295128739;
    uint160 internal constant MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342;

    address public immutable owner;
    address public immutable aavePool;
    address public immutable balancerVault;

    uint256 private _status;
    address private _activePool;
    DexKind private _activeKind;

    address private _flashAsset;
    uint256 private _minProfit;
    uint256 private _beforeBal;

    error NotOwner();
    error Reentrancy();
    error BadCallback();
    error MinProfit();
    error Slippage();
    error ZeroAddress();
    error NoSteps();
    error TooManySteps();
    error UnexpectedToken();
    error SourceUnavailable();
    error AmountTooLarge();
    error FeeTooHigh();

    event ArbExecuted(address indexed asset, uint256 borrowed, uint256 premium, uint256 profit);
    event Withdrawn(address indexed token, address indexed to, uint256 amount);

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

    constructor(address _owner, address _aavePool, address _balancerVault) {
        if (_owner == address(0)) revert ZeroAddress();
        owner = _owner;
        aavePool = _aavePool;
        balancerVault = _balancerVault;
        _status = _NOT_ENTERED;
    }

    /// @notice Borrow `plan.flashAmount`, run the swap steps, repay, and keep the profit.
    function execute(ArbPlan calldata plan) external onlyOwner nonReentrant {
        if (plan.flashAsset == address(0)) revert ZeroAddress();
        if (plan.flashAmount == 0) revert AmountTooLarge();
        uint256 n = plan.steps.length;
        if (n == 0) revert NoSteps();
        if (n > MAX_STEPS) revert TooManySteps();
        if (plan.steps[0].tokenIn != plan.flashAsset) revert UnexpectedToken();
        if (plan.steps[n - 1].tokenOut != plan.flashAsset) revert UnexpectedToken();

        _flashAsset = plan.flashAsset;
        _minProfit = plan.minProfit;
        _beforeBal = IERC20(plan.flashAsset).balanceOf(address(this));

        bytes memory data = abi.encode(plan.steps);

        if (plan.source == FlashSource.AaveV3) {
            if (aavePool == address(0)) revert SourceUnavailable();
            IAaveV3Pool(aavePool).flashLoanSimple(address(this), plan.flashAsset, plan.flashAmount, data, 0);
        } else if (plan.source == FlashSource.BalancerV2) {
            if (balancerVault == address(0)) revert SourceUnavailable();
            address[] memory tokens = new address[](1);
            tokens[0] = plan.flashAsset;
            uint256[] memory amounts = new uint256[](1);
            amounts[0] = plan.flashAmount;
            IBalancerVault(balancerVault).flashLoan(address(this), tokens, amounts, data);
        } else {
            revert SourceUnavailable();
        }

        _flashAsset = address(0);
        _minProfit = 0;
        _beforeBal = 0;
    }

    /// @inheritdoc IUniswapV3SwapCallback
    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata data) external {
        _settleConcentrated(amount0Delta, amount1Delta, data, DexKind.UniV3);
    }

    /// @inheritdoc IAlgebraSwapCallback
    function algebraSwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata data) external {
        _settleConcentrated(amount0Delta, amount1Delta, data, DexKind.Algebra);
    }

    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool) {
        if (msg.sender != aavePool) revert BadCallback();
        if (initiator != address(this)) revert BadCallback();
        if (_status != _ENTERED) revert Reentrancy();
        _run(asset, amount, premium, params);
        SafeTransfer.forceApprove(asset, aavePool, amount + premium);
        return true;
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
        _run(tokens[0], amounts[0], feeAmounts[0], userData);
        SafeTransfer.safeTransfer(tokens[0], balancerVault, amounts[0] + feeAmounts[0]);
    }

    /// @notice Pull profits (or any token balance) to `to`. `amount == type(uint256).max` withdraws the full balance.
    function withdraw(address token, address to, uint256 amount) external onlyOwner nonReentrant {
        if (to == address(0) || token == address(0)) revert ZeroAddress();
        uint256 bal = IERC20(token).balanceOf(address(this));
        uint256 amt = amount == type(uint256).max ? bal : amount;
        SafeTransfer.safeTransfer(token, to, amt);
        emit Withdrawn(token, to, amt);
    }

    function withdrawETH(address payable to) external onlyOwner nonReentrant {
        if (to == address(0)) revert ZeroAddress();
        uint256 bal = address(this).balance;
        (bool ok,) = to.call{value: bal}("");
        if (!ok) revert SafeTransfer.TransferFailed();
        emit Withdrawn(address(0), to, bal);
    }

    receive() external payable {}

    function _run(address asset, uint256 amount, uint256 premium, bytes calldata params) internal {
        if (asset != _flashAsset) revert UnexpectedToken();
        SwapStep[] memory steps = abi.decode(params, (SwapStep[]));

        uint256 current = amount;
        address currentToken = asset;
        uint256 n = steps.length;
        for (uint256 i = 0; i < n; ++i) {
            if (steps[i].tokenIn != currentToken) revert UnexpectedToken();
            if (steps[i].pool == address(0) || steps[i].tokenOut == address(0)) revert ZeroAddress();
            current = _swap(steps[i], current);
            currentToken = steps[i].tokenOut;
        }
        if (currentToken != asset) revert UnexpectedToken();

        uint256 owed = amount + premium;
        uint256 afterBal = IERC20(asset).balanceOf(address(this));
        // `afterBal` still includes the borrowed principal. Net profit is what remains
        // after repayment, relative to the balance from before the flash loan.
        if (afterBal < _beforeBal + owed + _minProfit) revert MinProfit();
        emit ArbExecuted(asset, amount, premium, afterBal - _beforeBal - owed);
    }

    function _swap(SwapStep memory step, uint256 amountIn) internal returns (uint256 amountOut) {
        if (step.kind == DexKind.UniV2) return _swapUniV2(step, amountIn);
        if (step.kind == DexKind.Solidly) return _swapQuoted(step, amountIn, false);
        if (step.kind == DexKind.Camelot) return _swapQuoted(step, amountIn, true);
        if (step.kind == DexKind.UniV3 || step.kind == DexKind.Algebra) return _swapConcentrated(step, amountIn);
        revert SourceUnavailable();
    }

    function _swapUniV2(SwapStep memory step, uint256 amountIn) internal returns (uint256 amountOut) {
        if (step.feeBps >= 10_000) revert FeeTooHigh();
        IUniswapV2Pair pair = IUniswapV2Pair(step.pool);
        address token0 = pair.token0();
        bool zeroForOne = step.tokenIn == token0;
        address other = zeroForOne ? pair.token1() : token0;
        if (!zeroForOne && step.tokenIn != pair.token1()) revert UnexpectedToken();
        if (other != step.tokenOut) revert UnexpectedToken();

        (uint112 r0, uint112 r1,) = pair.getReserves();
        uint256 reserveIn = zeroForOne ? uint256(r0) : uint256(r1);
        uint256 reserveOut = zeroForOne ? uint256(r1) : uint256(r0);
        amountOut = V2Math.getAmountOut(amountIn, reserveIn, reserveOut, step.feeBps);
        if (amountOut < step.amountOutMin || amountOut == 0) revert Slippage();

        SafeTransfer.safeTransfer(step.tokenIn, step.pool, amountIn);
        pair.swap(zeroForOne ? 0 : amountOut, zeroForOne ? amountOut : 0, address(this), "");
    }

    function _swapQuoted(SwapStep memory step, uint256 amountIn, bool camelot) internal returns (uint256 amountOut) {
        address token0;
        if (camelot) {
            amountOut = ICamelotPair(step.pool).getAmountOut(amountIn, step.tokenIn);
            token0 = ICamelotPair(step.pool).token0();
            if (step.tokenIn != token0 && step.tokenIn != ICamelotPair(step.pool).token1()) revert UnexpectedToken();
        } else {
            amountOut = ISolidlyPair(step.pool).getAmountOut(amountIn, step.tokenIn);
            token0 = ISolidlyPair(step.pool).token0();
            if (step.tokenIn != token0 && step.tokenIn != ISolidlyPair(step.pool).token1()) revert UnexpectedToken();
        }
        address other = step.tokenIn == token0 ? _token1(step, camelot) : token0;
        if (other != step.tokenOut) revert UnexpectedToken();
        if (amountOut < step.amountOutMin || amountOut == 0) revert Slippage();

        bool zeroForOne = step.tokenIn == token0;
        SafeTransfer.safeTransfer(step.tokenIn, step.pool, amountIn);
        if (camelot) {
            ICamelotPair(step.pool).swap(zeroForOne ? 0 : amountOut, zeroForOne ? amountOut : 0, address(this), "");
        } else {
            ISolidlyPair(step.pool).swap(zeroForOne ? 0 : amountOut, zeroForOne ? amountOut : 0, address(this), "");
        }
    }

    function _token1(SwapStep memory step, bool camelot) private view returns (address) {
        if (camelot) return ICamelotPair(step.pool).token1();
        return ISolidlyPair(step.pool).token1();
    }

    function _swapConcentrated(SwapStep memory step, uint256 amountIn) internal returns (uint256 amountOut) {
        if (amountIn > uint256(type(int256).max)) revert AmountTooLarge();
        IUniswapV3Pool pool = IUniswapV3Pool(step.pool);
        address token0 = pool.token0();
        bool zeroForOne = step.tokenIn == token0;
        if (!zeroForOne && step.tokenIn != pool.token1()) revert UnexpectedToken();
        address other = zeroForOne ? pool.token1() : token0;
        if (other != step.tokenOut) revert UnexpectedToken();

        uint160 limit = step.sqrtPriceLimitX96;
        if (limit == 0) {
            limit = zeroForOne ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1;
        }

        _activePool = step.pool;
        _activeKind = step.kind;
        (int256 amount0, int256 amount1) =
            pool.swap(address(this), zeroForOne, int256(amountIn), limit, abi.encode(step.tokenIn));
        _activePool = address(0);

        int256 received = zeroForOne ? amount1 : amount0;
        if (received >= 0) revert Slippage();
        amountOut = uint256(-received);
        if (amountOut < step.amountOutMin) revert Slippage();
    }

    function _settleConcentrated(int256 amount0Delta, int256 amount1Delta, bytes calldata data, DexKind expected)
        internal
    {
        if (_status != _ENTERED) revert Reentrancy();
        if (msg.sender != _activePool || _activeKind != expected) revert BadCallback();
        // Clear before the token transfer so a malicious token cannot re-enter the callback and be paid twice.
        address pool = msg.sender;
        _activePool = address(0);

        if ((amount0Delta > 0) == (amount1Delta > 0)) revert BadCallback();
        uint256 pay = amount0Delta > 0 ? uint256(amount0Delta) : uint256(amount1Delta);
        address tokenIn = abi.decode(data, (address));
        SafeTransfer.safeTransfer(tokenIn, pool, pay);
    }
}
