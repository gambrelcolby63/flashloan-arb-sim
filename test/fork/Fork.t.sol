// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test, console2} from "forge-std/Test.sol";
import {FlashLoanArbExecutor} from "../../src/FlashLoanArbExecutor.sol";
import {IERC20} from "../../src/interfaces/Interfaces.sol";
import {V2Math} from "../../src/libraries/V2Math.sol";

interface IV2Factory {
    function getPair(address, address) external view returns (address);
}

interface IV3Factory {
    function getPool(address, address, uint24) external view returns (address);
}

interface ISolidlyFactory {
    function getPool(address, address, bool stable) external view returns (address);
}

interface IV2PairView {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function getReserves() external view returns (uint112, uint112, uint32);
    function swap(uint256, uint256, address, bytes calldata) external;
}

interface IQuotedPair {
    function token0() external view returns (address);
    function getAmountOut(uint256 amountIn, address tokenIn) external view returns (uint256);
    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata data) external;
}

interface ISolidlyReserves {
    function getReserves() external view returns (uint256, uint256, uint256);
    function token0() external view returns (address);
}

/// @dev Shared fork helpers. Chain tests fork a pinned block and skip when no RPC is configured.
abstract contract ForkFixture is Test {
    FlashLoanArbExecutor internal exec;
    address internal weth;
    address internal quote;

    function _forkOrSkip(string memory envKey, uint256 pinned) internal returns (bool ready) {
        string memory rpc = vm.envOr(envKey, string(""));
        if (bytes(rpc).length == 0) {
            vm.skip(true);
            return false;
        }
        vm.createSelectFork(rpc, pinned);
        return true;
    }

    function _deploy(address aave, address balancer) internal {
        exec = new FlashLoanArbExecutor(address(this), aave, balancer);
        vm.label(address(exec), "executor");
    }

    modifier onlyForked() {
        if (address(exec) == address(0)) {
            vm.skip(true);
            return;
        }
        _;
    }

    function _wethReserveV2(address pair) internal view returns (uint256) {
        (uint112 r0, uint112 r1,) = IV2PairView(pair).getReserves();
        return IV2PairView(pair).token0() == weth ? uint256(r0) : uint256(r1);
    }

    function _dumpV2(address pair, uint256 amountIn, uint256 feeBps) internal {
        deal(weth, address(this), amountIn);
        IERC20(weth).transfer(pair, amountIn);
        IV2PairView p = IV2PairView(pair);
        (uint112 r0, uint112 r1,) = p.getReserves();
        bool zeroForOne = weth == p.token0();
        uint256 amountOut = V2Math.getAmountOut(amountIn, zeroForOne ? r0 : r1, zeroForOne ? r1 : r0, feeBps);
        p.swap(zeroForOne ? 0 : amountOut, zeroForOne ? amountOut : 0, address(this), "");
    }

    function _dumpQuoted(address pair, uint256 amountIn) internal {
        deal(weth, address(this), amountIn);
        uint256 amountOut = IQuotedPair(pair).getAmountOut(amountIn, weth);
        IERC20(weth).transfer(pair, amountIn);
        bool zeroForOne = weth == IQuotedPair(pair).token0();
        IQuotedPair(pair).swap(zeroForOne ? 0 : amountOut, zeroForOne ? amountOut : 0, address(this), "");
    }

    function _step(
        FlashLoanArbExecutor.DexKind kind,
        address pool,
        address tokenIn,
        address tokenOut,
        uint256 feeBps,
        uint256 minOut
    ) internal pure returns (FlashLoanArbExecutor.SwapStep memory) {
        return FlashLoanArbExecutor.SwapStep({
            kind: kind,
            pool: pool,
            tokenIn: tokenIn,
            tokenOut: tokenOut,
            feeBps: feeBps,
            amountOutMin: minOut,
            sqrtPriceLimitX96: 0
        });
    }

    struct Leg {
        FlashLoanArbExecutor.DexKind kind;
        address pool;
        uint256 fee;
    }

    function _plan(
        FlashLoanArbExecutor.FlashSource source,
        Leg memory sell,
        Leg memory buy,
        uint256 amount,
        uint256 minProfit,
        uint256 minOut
    ) internal view returns (FlashLoanArbExecutor.ArbPlan memory plan) {
        FlashLoanArbExecutor.SwapStep[] memory steps = new FlashLoanArbExecutor.SwapStep[](2);
        // Sell WETH where it is expensive, buy it back where the skew made it cheap.
        steps[0] = _step(sell.kind, sell.pool, weth, quote, sell.fee, minOut);
        steps[1] = _step(buy.kind, buy.pool, quote, weth, buy.fee, minOut);
        plan = FlashLoanArbExecutor.ArbPlan({
            source: source,
            flashAsset: weth,
            flashAmount: amount,
            minProfit: minProfit,
            steps: steps
        });
    }

    function _trySizes(FlashLoanArbExecutor.FlashSource source, Leg memory sell, Leg memory buy, uint256 minProfit)
        internal
        returns (uint256)
    {
        uint256 beforeBal = IERC20(weth).balanceOf(address(exec));
        uint256 profit = _tryDirection(source, sell, buy, minProfit, beforeBal);
        if (profit > 0) return profit;
        profit = _tryDirection(source, buy, sell, minProfit, beforeBal);
        if (profit > 0) return profit;
        revert("no profitable size");
    }

    function _tryDirection(
        FlashLoanArbExecutor.FlashSource source,
        Leg memory sell,
        Leg memory buy,
        uint256 minProfit,
        uint256 beforeBal
    ) internal returns (uint256) {
        uint256[4] memory sizes = [uint256(0.05 ether), uint256(0.25 ether), uint256(1 ether), uint256(3 ether)];
        for (uint256 i = 0; i < sizes.length; ++i) {
            FlashLoanArbExecutor.ArbPlan memory plan = _plan(source, sell, buy, sizes[i], minProfit, 1);
            try exec.execute(plan) {
                return IERC20(weth).balanceOf(address(exec)) - beforeBal;
            } catch {}
        }
        return 0;
    }

    function _expectRoundTripLoss(address pair, uint256 feeBps) internal {
        FlashLoanArbExecutor.SwapStep[] memory steps = new FlashLoanArbExecutor.SwapStep[](2);
        steps[0] = _step(FlashLoanArbExecutor.DexKind.UniV2, pair, weth, quote, feeBps, 1);
        steps[1] = _step(FlashLoanArbExecutor.DexKind.UniV2, pair, quote, weth, feeBps, 1);
        FlashLoanArbExecutor.ArbPlan memory plan = FlashLoanArbExecutor.ArbPlan({
            source: FlashLoanArbExecutor.FlashSource.AaveV3,
            flashAsset: weth,
            flashAmount: 0.2 ether,
            minProfit: 1,
            steps: steps
        });
        vm.expectRevert(FlashLoanArbExecutor.MinProfit.selector);
        exec.execute(plan);
    }

    function _expectNonOwner(address pair, uint256 feeBps) internal {
        FlashLoanArbExecutor.ArbPlan memory plan = _plan(
            FlashLoanArbExecutor.FlashSource.AaveV3, _oneLeg(pair, feeBps), _oneLeg(pair, feeBps), 0.1 ether, 0, 1
        );
        vm.expectRevert(FlashLoanArbExecutor.NotOwner.selector);
        vm.prank(address(0xBEEF));
        exec.execute(plan);
    }

    function _oneLeg(address pair, uint256 feeBps) internal pure returns (Leg memory) {
        return Leg(FlashLoanArbExecutor.DexKind.UniV2, pair, feeBps);
    }

    function _expectSlippage(address pair, uint256 feeBps) internal {
        FlashLoanArbExecutor.ArbPlan memory plan = _plan(
            FlashLoanArbExecutor.FlashSource.AaveV3,
            _oneLeg(pair, feeBps),
            _oneLeg(pair, feeBps),
            0.1 ether,
            0,
            type(uint128).max
        );
        vm.expectRevert(FlashLoanArbExecutor.Slippage.selector);
        exec.execute(plan);
    }
}

contract MainnetForkTest is ForkFixture {
    uint256 internal constant BLOCK = 26_252_880;
    address internal constant AAVE = 0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2;
    address internal constant BALANCER = 0xBA12222222228d8Ba445958a75a0704d566BF2C8;
    address internal constant UNI_V2_FACTORY = 0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f;
    address internal constant SUSHI_FACTORY = 0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac;
    address internal constant UNI_V3_FACTORY = 0x1F98431c8aD98523631AE4a59f267346ea31F984;

    address internal uni;
    address internal sushi;
    address internal uniV3;

    function setUp() public {
        uint256 blockNo = vm.envOr("FORK_BLOCK_MAINNET", BLOCK);
        if (!_forkOrSkip("ETH_RPC_URL", blockNo)) return;
        weth = 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2;
        quote = 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48;
        uni = IV2Factory(UNI_V2_FACTORY).getPair(weth, quote);
        sushi = IV2Factory(SUSHI_FACTORY).getPair(weth, quote);
        uniV3 = IV3Factory(UNI_V3_FACTORY).getPool(weth, quote, 500);
        _deploy(AAVE, BALANCER);
    }

    function test_crafted_v2_aave_profit_and_withdraw() public onlyForked {
        uint256 dumpAmt = _wethReserveV2(sushi) / 4;
        _dumpV2(sushi, dumpAmt, 30);
        uint256 profit = _trySizes(
            FlashLoanArbExecutor.FlashSource.AaveV3,
            Leg(FlashLoanArbExecutor.DexKind.UniV2, uni, 30),
            Leg(FlashLoanArbExecutor.DexKind.UniV2, sushi, 30),
            1
        );
        console2.log("mainnet v2/v2 profit wei", profit);
        assertGt(profit, 0.01 ether, "crafted arb should clear 0.01 WETH");

        address alice = address(0xA11CE);
        exec.withdraw(weth, alice, profit);
        assertEq(IERC20(weth).balanceOf(alice), profit);

        vm.expectRevert(FlashLoanArbExecutor.NotOwner.selector);
        vm.prank(alice);
        exec.withdraw(weth, alice, 1);
    }

    function test_crafted_v3_leg_profit() public onlyForked {
        _dumpV2(sushi, _wethReserveV2(sushi) / 4, 30);
        uint256 profit = _trySizes(
            FlashLoanArbExecutor.FlashSource.AaveV3,
            Leg(FlashLoanArbExecutor.DexKind.UniV3, uniV3, 0),
            Leg(FlashLoanArbExecutor.DexKind.UniV2, sushi, 30),
            1
        );
        console2.log("mainnet v3/v2 profit wei", profit);
        assertGt(profit, 0.01 ether);
    }

    function test_crafted_balancer_zero_premium() public onlyForked {
        _dumpV2(sushi, _wethReserveV2(sushi) / 5, 30);
        uint256 profit = _trySizes(
            FlashLoanArbExecutor.FlashSource.BalancerV2,
            Leg(FlashLoanArbExecutor.DexKind.UniV2, uni, 30),
            Leg(FlashLoanArbExecutor.DexKind.UniV2, sushi, 30),
            1
        );
        console2.log("mainnet balancer profit wei", profit);
        assertGt(profit, 0.01 ether);
    }

    function test_unprofitable_non_owner_and_slippage_revert() public onlyForked {
        _expectRoundTripLoss(uni, 30);
        _expectNonOwner(uni, 30);
        _expectSlippage(uni, 30);
    }
}

contract BaseForkTest is ForkFixture {
    uint256 internal constant BLOCK = 52_969_900;
    address internal constant AAVE = 0xA238Dd80C259a72e81d7e4664a9801593F98d1c5;
    address internal constant BALANCER = 0xBA12222222228d8Ba445958a75a0704d566BF2C8;
    address internal constant AERO_FACTORY = 0x420DD381b31aEf6683db6B902084cB0FFECe40Da;
    address internal constant BASESWAP_FACTORY = 0xFDa619b6d20975be80A10332cD39b9a4b0FAa8BB;

    address internal aero;
    address internal baseswap;

    function setUp() public {
        uint256 blockNo = vm.envOr("FORK_BLOCK_BASE", BLOCK);
        if (!_forkOrSkip("BASE_RPC_URL", blockNo)) return;
        weth = 0x4200000000000000000000000000000000000006;
        quote = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;
        aero = ISolidlyFactory(AERO_FACTORY).getPool(weth, quote, false);
        baseswap = IV2Factory(BASESWAP_FACTORY).getPair(weth, quote);
        _deploy(AAVE, BALANCER);
    }

    function test_crafted_aerodrome_baseswap_profit_and_withdraw() public onlyForked {
        _dumpV2(baseswap, _wethReserveV2(baseswap) / 4, 25);
        uint256 profit = _trySizes(
            FlashLoanArbExecutor.FlashSource.AaveV3,
            Leg(FlashLoanArbExecutor.DexKind.Solidly, aero, 0),
            Leg(FlashLoanArbExecutor.DexKind.UniV2, baseswap, 25),
            1
        );
        console2.log("base aero/baseswap profit wei", profit);
        assertGt(profit, 0.005 ether, "long-tail baseswap skew should pay");

        address alice = address(0xA11CE);
        exec.withdraw(weth, alice, type(uint256).max);
        assertEq(IERC20(weth).balanceOf(alice), profit);
    }

    function test_balancer_on_skewed_baseswap() public onlyForked {
        _dumpV2(baseswap, _wethReserveV2(baseswap) / 5, 25);
        uint256 profit = _trySizes(
            FlashLoanArbExecutor.FlashSource.BalancerV2,
            Leg(FlashLoanArbExecutor.DexKind.Solidly, aero, 0),
            Leg(FlashLoanArbExecutor.DexKind.UniV2, baseswap, 25),
            1
        );
        console2.log("base balancer profit wei", profit);
        assertGt(profit, 0.005 ether);
    }

    function test_unprofitable_non_owner_and_slippage_revert() public onlyForked {
        _expectRoundTripLoss(baseswap, 25);
        _expectNonOwner(baseswap, 25);
        _expectSlippage(baseswap, 25);
    }
}

contract ArbitrumForkTest is ForkFixture {
    uint256 internal constant BLOCK = 517_431_000;
    address internal constant AAVE = 0x794a61358D6845594F94dc1DB02A252b5b4814aD;
    address internal constant BALANCER = 0xBA12222222228d8Ba445958a75a0704d566BF2C8;
    address internal constant SUSHI_FACTORY = 0xc35DADB65012eC5796536bD9864eD8773aBc74C4;
    address internal constant CAMELOT_FACTORY = 0x6EcCab422D763aC031210895C81787E87B43A652;

    address internal sushi;
    address internal camelot;

    function setUp() public {
        uint256 blockNo = vm.envOr("FORK_BLOCK_ARBITRUM", BLOCK);
        if (!_forkOrSkip("ARB_RPC_URL", blockNo)) return;
        weth = 0x82aF49447D8a07e3bd95BD0d56f35241523fBab1;
        // USDC.e: both Sushi and Camelot have a real WETH book here. Native USDC books are dust.
        quote = 0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8;
        sushi = IV2Factory(SUSHI_FACTORY).getPair(weth, quote);
        camelot = IV2Factory(CAMELOT_FACTORY).getPair(weth, quote);
        _deploy(AAVE, BALANCER);
    }

    function test_crafted_camelot_sushi_profit_and_withdraw() public onlyForked {
        // Camelot getReserves returns 4 words, so don't use the V2 decoder.
        uint256 dumpAmt = _camelotWeth() / 4;
        _dumpQuoted(camelot, dumpAmt);
        uint256 profit = _trySizes(
            FlashLoanArbExecutor.FlashSource.AaveV3,
            Leg(FlashLoanArbExecutor.DexKind.UniV2, sushi, 30),
            Leg(FlashLoanArbExecutor.DexKind.Camelot, camelot, 0),
            1
        );
        console2.log("arbitrum camelot/sushi profit wei", profit);
        assertGt(profit, 0.005 ether);

        address alice = address(0xA11CE);
        exec.withdraw(weth, alice, profit);
        assertEq(IERC20(weth).balanceOf(alice), profit);
    }

    function test_unprofitable_non_owner_and_slippage_revert() public onlyForked {
        _expectRoundTripLoss(sushi, 30);
        _expectNonOwner(sushi, 30);
        _expectSlippage(sushi, 30);
    }

    function _camelotWeth() internal view returns (uint256) {
        // 4-word getReserves. Decode manually so the 3-word V2 decoder is not used.
        (bool ok, bytes memory data) = camelot.staticcall(abi.encodeWithSignature("getReserves()"));
        require(ok && data.length >= 64, "camelot reserves");
        uint256 r0 = abi.decode(data, (uint256));
        uint256 r1;
        assembly {
            r1 := mload(add(data, 64))
        }
        return IQuotedPair(camelot).token0() == weth ? r0 : r1;
    }
}
