// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {FlashLoanLiquidator, IMorphoBlue} from "../src/FlashLoanLiquidator.sol";
import {IERC20} from "../src/interfaces/Interfaces.sol";
import {MockERC20} from "./mocks/Mocks.sol";

contract MockMorpho {
    MockERC20 public loan;
    MockERC20 public collateral;
    uint256 public repayAssets;
    uint256 public seizeAssets;

    constructor(MockERC20 _loan, MockERC20 _collateral, uint256 _repay, uint256 _seize) {
        loan = _loan;
        collateral = _collateral;
        repayAssets = _repay;
        seizeAssets = _seize;
    }

    function flashLoan(address token, uint256 assets, bytes calldata data) external {
        require(token == address(loan), "token");
        loan.transfer(msg.sender, assets);
        (bool ok, bytes memory ret) = msg.sender.call(abi.encodeWithSignature("onMorphoFlashLoan(uint256,bytes)", assets, data));
        require(ok, string(ret));
        loan.transferFrom(msg.sender, address(this), assets);
    }

    function liquidate(IMorphoBlue.MarketParams calldata, address, uint256, uint256, bytes calldata)
        external
        returns (uint256, uint256)
    {
        loan.transferFrom(msg.sender, address(this), repayAssets);
        collateral.transfer(msg.sender, seizeAssets);
        return (seizeAssets, repayAssets);
    }
}

contract MockBalancer {
    function flashLoan(address recipient, address[] calldata tokens, uint256[] calldata amounts, bytes calldata userData)
        external
    {
        uint256 beforeBal = IERC20(tokens[0]).balanceOf(address(this));
        IERC20(tokens[0]).transfer(recipient, amounts[0]);
        uint256[] memory fees = new uint256[](1);
        (bool ok, bytes memory ret) = recipient.call(
            abi.encodeWithSignature("receiveFlashLoan(address[],uint256[],uint256[],bytes)", tokens, amounts, fees, userData)
        );
        require(ok, string(ret));
        require(IERC20(tokens[0]).balanceOf(address(this)) >= beforeBal, "not repaid");
    }
}

contract MockSolidlyPool {
    address public token0;
    address public token1;
    MockERC20 public outToken;

    constructor(address _token0, address _token1, MockERC20 _out) {
        token0 = _token0;
        token1 = _token1;
        outToken = _out;
    }

    function getAmountOut(uint256 amountIn, address) external pure returns (uint256) {
        return amountIn;
    }

    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata) external {
        outToken.transfer(to, amount0Out + amount1Out);
    }
}

contract FlashLoanLiquidatorTest is Test {
    MockERC20 internal loan;
    MockERC20 internal collateral;
    MockMorpho internal morpho;
    MockBalancer internal balancer;
    MockSolidlyPool internal pool;
    FlashLoanLiquidator internal exec;

    uint256 internal constant REPAY = 100 ether;
    uint256 internal constant SEIZE = 120 ether;

    function setUp() public {
        loan = new MockERC20("loan", 18);
        collateral = new MockERC20("coll", 18);
        morpho = new MockMorpho(loan, collateral, REPAY, SEIZE);
        balancer = new MockBalancer();
        pool = new MockSolidlyPool(address(collateral), address(loan), loan);
        exec = new FlashLoanLiquidator(address(this), address(morpho), address(balancer));

        loan.mint(address(morpho), 1_000 ether);
        loan.mint(address(balancer), 1_000 ether);
        collateral.mint(address(morpho), SEIZE);
        loan.mint(address(pool), 1_000 ether);
    }

    function _job(uint256 minProfit) internal view returns (FlashLoanLiquidator.Job memory job) {
        job.market = IMorphoBlue.MarketParams({
            loanToken: address(loan),
            collateralToken: address(collateral),
            oracle: address(1),
            irm: address(1),
            lltv: 0.8 ether
        });
        job.borrower = address(0xBEEF);
        job.seizedAssets = 0;
        job.repaidShares = 1;
        job.swapPool = address(pool);
        job.minSwapOut = REPAY;
        job.minProfit = minProfit;
    }

    function test_morpho_flash_keeps_the_bonus() public {
        exec.liquidateWithMorphoFlash(_job(1), REPAY);
        // Swap is 1:1 on the mock, so 120 loan tokens come back for 100 repaid.
        // The flash repayment consumes the 100, and 20 stays.
        assertEq(loan.balanceOf(address(exec)), 20 ether);
        assertEq(collateral.balanceOf(address(exec)), 0);
    }

    function test_balancer_flash_keeps_the_bonus() public {
        exec.liquidateWithBalancerFlash(_job(1), REPAY);
        assertEq(loan.balanceOf(address(exec)), 20 ether);
        assertEq(loan.balanceOf(address(balancer)), 1_000 ether);
    }

    function test_shortfall_reverts_and_repays_nothing() public {
        uint256 beforeBal = loan.balanceOf(address(morpho));
        vm.expectRevert();
        exec.liquidateWithMorphoFlash(_job(21 ether), REPAY);
        assertEq(loan.balanceOf(address(morpho)), beforeBal);
        assertEq(loan.balanceOf(address(exec)), 0);
    }

    function test_non_owner_and_stray_callback_revert() public {
        vm.prank(address(0xCAFE));
        vm.expectRevert(FlashLoanLiquidator.NotOwner.selector);
        exec.liquidateWithMorphoFlash(_job(1), REPAY);

        vm.expectRevert(FlashLoanLiquidator.BadCallback.selector);
        exec.onMorphoFlashLoan(1, "");
    }
}
