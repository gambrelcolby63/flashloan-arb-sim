// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {FlashLoanArbExecutor} from "../src/FlashLoanArbExecutor.sol";
import {V2Math} from "../src/libraries/V2Math.sol";
import {
    MockAavePool,
    MockBalancerVault,
    MockCamelotPair,
    MockERC20,
    MockNoReturnERC20,
    MockSolidlyPair,
    MockV2Pair,
    MockV3Pool
} from "./mocks/Mocks.sol";

contract OwnerAttacker {
    FlashLoanArbExecutor public exec;
    FlashLoanArbExecutor.ArbPlan internal stored;
    bool public attack = true;
    bool public withdrawInstead;

    constructor(address _exec) {
        exec = FlashLoanArbExecutor(payable(_exec));
    }

    function go(FlashLoanArbExecutor.ArbPlan calldata plan) external {
        stored = plan;
        exec.execute(plan);
    }

    function onTokenTransfer() external {
        if (!attack) return;
        attack = false;
        if (withdrawInstead) {
            exec.withdraw(address(0), address(this), 0);
            return;
        }
        FlashLoanArbExecutor.ArbPlan memory plan = stored;
        exec.execute(plan);
    }
}

contract FlashLoanArbExecutorTest is Test {
    uint256 internal constant FEE = 30;

    MockERC20 internal weth;
    MockERC20 internal usdc;
    MockV2Pair internal cheap;
    MockV2Pair internal expensive;
    MockAavePool internal aave;
    MockBalancerVault internal balancer;
    FlashLoanArbExecutor internal exec;

    function setUp() public {
        weth = new MockERC20("WETH", 18);
        usdc = new MockERC20("USDC", 18);
        // WETH is cheap here: 2 WETH per 1 USDC.
        cheap = new MockV2Pair(address(weth), address(usdc), 2_000_000 ether, 1_000_000 ether, FEE);
        // WETH is expensive here: 2 USDC per 1 WETH.
        expensive = new MockV2Pair(address(weth), address(usdc), 1_000_000 ether, 2_000_000 ether, FEE);
        aave = new MockAavePool(5);
        balancer = new MockBalancerVault();
        exec = new FlashLoanArbExecutor(address(this), address(aave), address(balancer));
    }

    function test_v2_math_matches_live_pool_vectors() public pure {
        // Camelot WETH/USDC volatile, 0.001 WETH in, fee stored as 300/1e5 == 30 bps.
        assertEq(V2Math.getAmountOut(1e15, 137871552368452388, 331117098, 30), 2377239);
        // BaseSwap WETH/USDC, 0.1 WETH in, 25 bps. Matches the router at the block we measured.
        assertEq(V2Math.getAmountOut(1e17, 4411712615487859411, 10611377089, 25), 234621220);
    }

    function test_profitable_aave_v2_exact_profit() public {
        uint256 amount = 10_000 ether;
        uint256 expected = _expectedProfit(amount);
        assertGt(expected, 0);

        uint256 endBal = _run(amount, expected, FlashLoanArbExecutor.FlashSource.AaveV3);
        assertEq(endBal, expected, "profit after premium");
    }

    function test_profit_below_min_reverts() public {
        uint256 amount = 10_000 ether;
        uint256 expected = _expectedProfit(amount);
        vm.expectRevert(FlashLoanArbExecutor.MinProfit.selector);
        exec.execute(_plan(amount, expected + 1, FlashLoanArbExecutor.FlashSource.AaveV3, 1, 1));
    }

    function test_unprofitable_round_trip_reverts() public {
        MockV2Pair a = new MockV2Pair(address(weth), address(usdc), 1_000_000 ether, 1_000_000 ether, FEE);
        MockV2Pair b = new MockV2Pair(address(weth), address(usdc), 1_000_000 ether, 1_000_000 ether, FEE);
        FlashLoanArbExecutor.ArbPlan memory plan = _planOn(
            address(a),
            FlashLoanArbExecutor.DexKind.UniV2,
            FEE,
            address(b),
            FlashLoanArbExecutor.DexKind.UniV2,
            FEE,
            1_000 ether,
            1,
            FlashLoanArbExecutor.FlashSource.AaveV3
        );
        vm.expectRevert(FlashLoanArbExecutor.MinProfit.selector);
        exec.execute(plan);
    }

    function test_slippage_reverts() public {
        FlashLoanArbExecutor.ArbPlan memory plan =
            _plan(1_000 ether, 0, FlashLoanArbExecutor.FlashSource.AaveV3, type(uint256).max, 1);
        vm.expectRevert(FlashLoanArbExecutor.Slippage.selector);
        exec.execute(plan);
    }

    function test_non_owner_reverts() public {
        vm.expectRevert(FlashLoanArbExecutor.NotOwner.selector);
        vm.prank(address(0xBEEF));
        exec.execute(_plan(1 ether, 0, FlashLoanArbExecutor.FlashSource.AaveV3, 1, 1));
    }

    function test_stranger_cannot_drive_callbacks() public {
        vm.expectRevert(FlashLoanArbExecutor.BadCallback.selector);
        exec.executeOperation(address(usdc), 1, 0, address(exec), "");

        address[] memory tokens = new address[](1);
        tokens[0] = address(usdc);
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = 1;
        uint256[] memory fees = new uint256[](1);
        vm.expectRevert(FlashLoanArbExecutor.BadCallback.selector);
        exec.receiveFlashLoan(tokens, amounts, fees, "");

        vm.prank(address(aave));
        vm.expectRevert(FlashLoanArbExecutor.Reentrancy.selector);
        exec.executeOperation(address(usdc), 1, 0, address(exec), "");
    }

    function test_balancer_zero_premium_beats_aave() public {
        uint256 amount = 10_000 ether;
        uint256 aaveProfit = _run(amount, 1, FlashLoanArbExecutor.FlashSource.AaveV3);

        // Fresh pools and executor so the first trade does not eat the gap.
        cheap = new MockV2Pair(address(weth), address(usdc), 2_000_000 ether, 1_000_000 ether, FEE);
        expensive = new MockV2Pair(address(weth), address(usdc), 1_000_000 ether, 2_000_000 ether, FEE);
        uint256 balProfit = _run(amount, 1, FlashLoanArbExecutor.FlashSource.BalancerV2);
        assertGt(balProfit, aaveProfit);
        assertEq(balProfit - aaveProfit, (amount * 5) / 10_000);
    }

    function test_solidly_and_camelot_kinds() public {
        MockSolidlyPair solidlyCheap = new MockSolidlyPair(address(weth), address(usdc), 2_000_000 ether, 1_000_000 ether, FEE);
        MockCamelotPair camelotRich = new MockCamelotPair(address(weth), address(usdc), 1_000_000 ether, 2_000_000 ether, FEE);
        uint256 before = usdc.balanceOf(address(exec));
        exec.execute(
            _planOn(
                address(solidlyCheap),
                FlashLoanArbExecutor.DexKind.Solidly,
                FEE,
                address(camelotRich),
                FlashLoanArbExecutor.DexKind.Camelot,
                FEE,
                5_000 ether,
                1,
                FlashLoanArbExecutor.FlashSource.BalancerV2
            )
        );
        assertGt(usdc.balanceOf(address(exec)) - before, 0);
    }

    function test_v3_and_algebra_kinds() public {
        MockV3Pool v3Cheap = new MockV3Pool(address(weth), address(usdc), 2_000_000 ether, 1_000_000 ether, FEE, false);
        MockV3Pool algebraRich = new MockV3Pool(address(weth), address(usdc), 1_000_000 ether, 2_000_000 ether, FEE, true);
        uint256 before = usdc.balanceOf(address(exec));
        exec.execute(
            _planOn(
                address(v3Cheap),
                FlashLoanArbExecutor.DexKind.UniV3,
                FEE,
                address(algebraRich),
                FlashLoanArbExecutor.DexKind.Algebra,
                FEE,
                5_000 ether,
                1,
                FlashLoanArbExecutor.FlashSource.AaveV3
            )
        );
        assertGt(usdc.balanceOf(address(exec)) - before, 0);
    }

    function test_withdraw_profits_owner_only() public {
        uint256 amount = 1_000 ether;
        uint256 profit = _run(amount, 1, FlashLoanArbExecutor.FlashSource.AaveV3);
        address alice = address(0xA11CE);
        exec.withdraw(address(usdc), alice, profit);
        assertEq(usdc.balanceOf(alice), profit);
        assertEq(usdc.balanceOf(address(exec)), 0);

        vm.expectRevert(FlashLoanArbExecutor.NotOwner.selector);
        vm.prank(alice);
        exec.withdraw(address(usdc), alice, 1);
    }

    function test_withdraw_max_and_no_return_token() public {
        MockNoReturnERC20 usdt = new MockNoReturnERC20("USDT", 6);
        usdt.mint(address(exec), 50e6);
        exec.withdraw(address(usdt), address(this), type(uint256).max);
        assertEq(usdt.balanceOf(address(this)), 50e6);
    }

    function test_no_return_token_full_aave_cycle() public {
        MockNoReturnERC20 token = new MockNoReturnERC20("USDT", 18);
        MockERC20 other = new MockERC20("WETH", 18);
        MockV2Pair a = new MockV2Pair(address(other), address(token), 2_000_000 ether, 1_000_000 ether, FEE);
        MockV2Pair b = new MockV2Pair(address(other), address(token), 1_000_000 ether, 2_000_000 ether, FEE);
        FlashLoanArbExecutor.SwapStep[] memory steps = new FlashLoanArbExecutor.SwapStep[](2);
        steps[0] = _step(FlashLoanArbExecutor.DexKind.UniV2, address(a), address(token), address(other), FEE, 1);
        steps[1] = _step(FlashLoanArbExecutor.DexKind.UniV2, address(b), address(other), address(token), FEE, 1);
        exec.execute(
            FlashLoanArbExecutor.ArbPlan({
                source: FlashLoanArbExecutor.FlashSource.AaveV3,
                flashAsset: address(token),
                flashAmount: 1_000 ether,
                minProfit: 1,
                steps: steps
            })
        );
        assertGt(token.balanceOf(address(exec)), 0);
    }

    function test_reentrancy_on_execute_reverts() public {
        RealAttacker attacker = new RealAttacker(address(aave), address(balancer));
        usdc.setReenter(address(attacker));
        vm.expectRevert(FlashLoanArbExecutor.Reentrancy.selector);
        attacker.go(_plan(1_000 ether, 0, FlashLoanArbExecutor.FlashSource.AaveV3, 1, 1));
    }

    function test_owner_is_immutable_and_contract_has_no_proxy_surface() public view {
        assertEq(exec.owner(), address(this));
        assertEq(exec.aavePool(), address(aave));
        assertEq(exec.MAX_STEPS(), 4);
    }

    function _expectedProfit(uint256 amount) internal view returns (uint256) {
        (uint112 c0, uint112 c1,) = cheap.getReserves();
        (uint112 e0, uint112 e1,) = expensive.getReserves();
        // cheap: token0 = WETH, token1 = USDC. Buying WETH spends USDC.
        uint256 mid = V2Math.getAmountOut(amount, c1, c0, FEE);
        uint256 back = V2Math.getAmountOut(mid, e0, e1, FEE);
        uint256 premium = (amount * 5) / 10_000;
        return back - amount - premium;
    }

    function _run(uint256 amount, uint256 minProfit, FlashLoanArbExecutor.FlashSource source) internal returns (uint256) {
        uint256 before = usdc.balanceOf(address(exec));
        exec.execute(_plan(amount, minProfit, source, 1, 1));
        return usdc.balanceOf(address(exec)) - before;
    }

    function _plan(
        uint256 amount,
        uint256 minProfit,
        FlashLoanArbExecutor.FlashSource source,
        uint256 minOut0,
        uint256 minOut1
    ) internal view returns (FlashLoanArbExecutor.ArbPlan memory) {
        FlashLoanArbExecutor.SwapStep[] memory steps = new FlashLoanArbExecutor.SwapStep[](2);
        steps[0] = _step(FlashLoanArbExecutor.DexKind.UniV2, address(cheap), address(usdc), address(weth), FEE, minOut0);
        steps[1] =
            _step(FlashLoanArbExecutor.DexKind.UniV2, address(expensive), address(weth), address(usdc), FEE, minOut1);
        return FlashLoanArbExecutor.ArbPlan({
            source: source,
            flashAsset: address(usdc),
            flashAmount: amount,
            minProfit: minProfit,
            steps: steps
        });
    }

    function _planOn(
        address poolA,
        FlashLoanArbExecutor.DexKind kindA,
        uint256 feeA,
        address poolB,
        FlashLoanArbExecutor.DexKind kindB,
        uint256 feeB,
        uint256 amount,
        uint256 minProfit,
        FlashLoanArbExecutor.FlashSource source
    ) internal view returns (FlashLoanArbExecutor.ArbPlan memory) {
        FlashLoanArbExecutor.SwapStep[] memory steps = new FlashLoanArbExecutor.SwapStep[](2);
        steps[0] = _step(kindA, poolA, address(usdc), address(weth), feeA, 1);
        steps[1] = _step(kindB, poolB, address(weth), address(usdc), feeB, 1);
        return FlashLoanArbExecutor.ArbPlan({
            source: source,
            flashAsset: address(usdc),
            flashAmount: amount,
            minProfit: minProfit,
            steps: steps
        });
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
}

/// @dev Owner and reenter hook in one contract, constructed with its own executor.
contract RealAttacker is OwnerAttacker {
    constructor(address aave, address balancer) OwnerAttacker(address(0)) {
        exec = new FlashLoanArbExecutor(address(this), aave, balancer);
    }
}
