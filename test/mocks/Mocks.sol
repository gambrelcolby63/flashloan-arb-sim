// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20, IUniswapV3SwapCallback, IAlgebraSwapCallback} from "../../src/interfaces/Interfaces.sol";
import {V2Math} from "../../src/libraries/V2Math.sol";

contract MockERC20 {
    string public symbol;
    uint8 public decimals;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    constructor(string memory s, uint8 d) {
        symbol = s;
        decimals = d;
    }

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
        totalSupply += amount;
    }

    function approve(address spender, uint256 amount) external virtual returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    address public reenterHook;
    bool public armReenter;

    function setReenter(address hook) external {
        reenterHook = hook;
        armReenter = true;
    }

    function transfer(address to, uint256 amount) external virtual returns (bool) {
        _move(msg.sender, to, amount);
        _maybeReenter();
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external virtual returns (bool) {
        uint256 alw = allowance[from][msg.sender];
        if (alw != type(uint256).max) {
            require(alw >= amount, "allow");
            allowance[from][msg.sender] = alw - amount;
        }
        _move(from, to, amount);
        return true;
    }

    function _move(address from, address to, uint256 amount) internal {
        require(balanceOf[from] >= amount, "bal");
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
    }

    function _maybeReenter() internal {
        if (armReenter && reenterHook != address(0)) {
            armReenter = false;
            (bool ok, bytes memory data) = reenterHook.call(abi.encodeWithSignature("onTokenTransfer()"));
            if (!ok) {
                assembly {
                    revert(add(data, 32), mload(data))
                }
            }
        }
    }
}

/// @notice USDT-shaped token: transfer/approve succeed with empty return data.
contract MockNoReturnERC20 is MockERC20 {
    constructor(string memory s, uint8 d) MockERC20(s, d) {}

    function approve(address spender, uint256 amount) external override returns (bool) {
        allowance[msg.sender][spender] = amount;
        assembly {
            return(0, 0)
        }
    }

    function transfer(address to, uint256 amount) external override returns (bool) {
        _move(msg.sender, to, amount);
        assembly {
            return(0, 0)
        }
    }

    function transferFrom(address from, address to, uint256 amount) external override returns (bool) {
        uint256 alw = allowance[from][msg.sender];
        if (alw != type(uint256).max) {
            require(alw >= amount, "allow");
            allowance[from][msg.sender] = alw - amount;
        }
        _move(from, to, amount);
        assembly {
            return(0, 0)
        }
    }
}

library MockSafe {
    function safeTransfer(address token, address to, uint256 amount) internal {
        (bool ok, bytes memory data) = token.call(abi.encodeWithSelector(IERC20.transfer.selector, to, amount));
        require(ok && (data.length == 0 || abi.decode(data, (bool))), "transfer");
    }

    function safeTransferFrom(address token, address from, address to, uint256 amount) internal {
        (bool ok, bytes memory data) =
            token.call(abi.encodeWithSelector(IERC20.transferFrom.selector, from, to, amount));
        require(ok && (data.length == 0 || abi.decode(data, (bool))), "transferFrom");
    }
}

contract MockV2Pair {
    address public token0;
    address public token1;
    uint112 public reserve0;
    uint112 public reserve1;
    uint256 public feeBps;

    constructor(address _token0, address _token1, uint256 r0, uint256 r1, uint256 _feeBps) {
        token0 = _token0;
        token1 = _token1;
        feeBps = _feeBps;
        reserve0 = uint112(r0);
        reserve1 = uint112(r1);
        MockERC20(_token0).mint(address(this), r0);
        MockERC20(_token1).mint(address(this), r1);
    }

    function getReserves() external view returns (uint112, uint112, uint32) {
        return (reserve0, reserve1, 0);
    }

    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata) external {
        require(amount0Out > 0 || amount1Out > 0, "out");
        require(amount0Out < reserve0 && amount1Out < reserve1, "liq");
        if (amount0Out > 0) MockSafe.safeTransfer(token0, to, amount0Out);
        if (amount1Out > 0) MockSafe.safeTransfer(token1, to, amount1Out);
        uint256 bal0 = IERC20(token0).balanceOf(address(this));
        uint256 bal1 = IERC20(token1).balanceOf(address(this));
        uint256 amount0In = bal0 > uint256(reserve0) - amount0Out ? bal0 - (uint256(reserve0) - amount0Out) : 0;
        uint256 amount1In = bal1 > uint256(reserve1) - amount1Out ? bal1 - (uint256(reserve1) - amount1Out) : 0;
        require(amount0In > 0 || amount1In > 0, "in");
        uint256 bal0Adj = bal0 * 10_000 - amount0In * feeBps;
        uint256 bal1Adj = bal1 * 10_000 - amount1In * feeBps;
        require(bal0Adj * bal1Adj >= uint256(reserve0) * uint256(reserve1) * (10_000 ** 2), "K");
        reserve0 = uint112(bal0);
        reserve1 = uint112(bal1);
    }
}

contract MockSolidlyPair {
    address public token0;
    address public token1;
    uint256 public reserve0;
    uint256 public reserve1;
    bool public stable;
    uint256 public feeBps;

    constructor(address _token0, address _token1, uint256 r0, uint256 r1, uint256 _feeBps) {
        token0 = _token0;
        token1 = _token1;
        reserve0 = r0;
        reserve1 = r1;
        feeBps = _feeBps;
        MockERC20(_token0).mint(address(this), r0);
        MockERC20(_token1).mint(address(this), r1);
    }

    function getReserves() external view returns (uint256, uint256, uint256) {
        return (reserve0, reserve1, 0);
    }

    function getAmountOut(uint256 amountIn, address tokenIn) external view returns (uint256) {
        uint256 rin = tokenIn == token0 ? reserve0 : reserve1;
        uint256 rout = tokenIn == token0 ? reserve1 : reserve0;
        uint256 kept = amountIn - (amountIn * feeBps) / 10_000;
        return (kept * rout) / (rin + kept);
    }

    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata) external {
        if (amount0Out > 0) MockSafe.safeTransfer(token0, to, amount0Out);
        if (amount1Out > 0) MockSafe.safeTransfer(token1, to, amount1Out);
        uint256 bal0 = IERC20(token0).balanceOf(address(this));
        uint256 bal1 = IERC20(token1).balanceOf(address(this));
        reserve0 = bal0;
        reserve1 = bal1;
    }
}

contract MockCamelotPair is MockSolidlyPair {
    constructor(address _token0, address _token1, uint256 r0, uint256 r1, uint256 _feeBps)
        MockSolidlyPair(_token0, _token1, r0, r1, _feeBps)
    {}
}

contract MockV3Pool {
    address public token0;
    address public token1;
    uint112 public reserve0;
    uint112 public reserve1;
    uint256 public feeBps;
    bool public algebra;

    constructor(address _token0, address _token1, uint256 r0, uint256 r1, uint256 _feeBps, bool _algebra) {
        token0 = _token0;
        token1 = _token1;
        reserve0 = uint112(r0);
        reserve1 = uint112(r1);
        feeBps = _feeBps;
        algebra = _algebra;
        MockERC20(_token0).mint(address(this), r0);
        MockERC20(_token1).mint(address(this), r1);
    }

    function swap(address recipient, bool zeroForOne, int256 amountSpecified, uint160, bytes calldata data)
        external
        returns (int256 amount0, int256 amount1)
    {
        require(amountSpecified > 0, "exact in");
        uint256 amountIn = uint256(amountSpecified);
        uint256 rin = zeroForOne ? reserve0 : reserve1;
        uint256 rout = zeroForOne ? reserve1 : reserve0;
        uint256 amountOut = V2Math.getAmountOut(amountIn, rin, rout, feeBps);
        require(amountOut > 0 && amountOut < rout, "liq");

        if (algebra) {
            IAlgebraSwapCallback(msg.sender).algebraSwapCallback(
                zeroForOne ? int256(amountIn) : -int256(amountOut),
                zeroForOne ? -int256(amountOut) : int256(amountIn),
                data
            );
        } else {
            IUniswapV3SwapCallback(msg.sender).uniswapV3SwapCallback(
                zeroForOne ? int256(amountIn) : -int256(amountOut),
                zeroForOne ? -int256(amountOut) : int256(amountIn),
                data
            );
        }

        address tokenIn = zeroForOne ? token0 : token1;
        require(IERC20(tokenIn).balanceOf(address(this)) >= rin + amountIn, "unpaid");
        MockSafe.safeTransfer(zeroForOne ? token1 : token0, recipient, amountOut);

        reserve0 = uint112(IERC20(token0).balanceOf(address(this)));
        reserve1 = uint112(IERC20(token1).balanceOf(address(this)));
        amount0 = zeroForOne ? int256(amountIn) : -int256(amountOut);
        amount1 = zeroForOne ? -int256(amountOut) : int256(amountIn);
    }
}

interface IFlashLoanSimpleReceiver {
    function executeOperation(address asset, uint256 amount, uint256 premium, address initiator, bytes calldata params)
        external
        returns (bool);
}

interface IBalancerRecipient {
    function receiveFlashLoan(
        address[] calldata tokens,
        uint256[] calldata amounts,
        uint256[] calldata feeAmounts,
        bytes calldata userData
    ) external;
}

contract MockAavePool {
    uint256 public premiumBps;

    constructor(uint256 _premiumBps) {
        premiumBps = _premiumBps;
    }

    function FLASHLOAN_PREMIUM_TOTAL() external view returns (uint128) {
        return uint128(premiumBps);
    }

    function flashLoanSimple(address receiver, address asset, uint256 amount, bytes calldata params, uint16) external {
        uint256 premium = (amount * premiumBps) / 10_000;
        MockERC20(asset).mint(receiver, amount);
        bool ok = IFlashLoanSimpleReceiver(receiver).executeOperation(asset, amount, premium, msg.sender, params);
        require(ok, "aave callback");
        MockSafe.safeTransferFrom(asset, receiver, address(this), amount + premium);
    }
}

contract MockBalancerVault {
    function flashLoan(address recipient, address[] calldata tokens, uint256[] calldata amounts, bytes calldata userData)
        external
    {
        uint256[] memory fees = new uint256[](tokens.length);
        uint256[] memory beforeBal = new uint256[](tokens.length);
        for (uint256 i = 0; i < tokens.length; ++i) {
            beforeBal[i] = IERC20(tokens[i]).balanceOf(address(this));
            MockERC20(tokens[i]).mint(recipient, amounts[i]);
            fees[i] = 0;
        }
        IBalancerRecipient(recipient).receiveFlashLoan(tokens, amounts, fees, userData);
        for (uint256 i = 0; i < tokens.length; ++i) {
            require(IERC20(tokens[i]).balanceOf(address(this)) >= beforeBal[i] + amounts[i] + fees[i], "balancer repay");
        }
    }
}
