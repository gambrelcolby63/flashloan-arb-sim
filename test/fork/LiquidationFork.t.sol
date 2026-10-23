// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test, console2} from "forge-std/Test.sol";
import {stdJson} from "forge-std/StdJson.sol";
import {FlashLoanLiquidator, IMorphoBlue} from "../../src/FlashLoanLiquidator.sol";
import {IERC20} from "../../src/interfaces/Interfaces.sol";

interface IMorphoPosition {
    function position(bytes32 id, address user) external view returns (uint256 supplyShares, uint128 borrowShares, uint128 collateral);
}

/// @notice Replays one historical Morpho Blue liquidation on Base, funded by Morpho's own
///         zero-fee flash loan, and sells the seized AERO on the Aerodrome volatile pool.
///         Skips when the RPC cannot serve the block. Does not broadcast.
contract LiquidationForkTest is Test {
    using stdJson for string;

    function test_historical_aero_usdc_liquidation() public {
        string memory rpc = vm.envOr("BASE_RPC_URL", string(""));
        if (bytes(rpc).length == 0) {
            vm.skip(true);
        }
        string memory json = vm.readFile("config/liquidation-fork.json");
        vm.createSelectFork(rpc, json.readBytes32(".tx"));

        address morpho = json.readAddress(".morpho");
        address borrower = json.readAddress(".borrower");
        bytes32 marketId = json.readBytes32(".market_id");
        ( , uint128 borrowShares, ) = IMorphoPosition(morpho).position(marketId, borrower);
        require(borrowShares > 0, "position already gone before the historical tx");

        FlashLoanLiquidator exec = new FlashLoanLiquidator(address(this), morpho, address(0));
        FlashLoanLiquidator.Job memory job;
        job.market = IMorphoBlue.MarketParams({
            loanToken: json.readAddress(".loan_token"),
            collateralToken: json.readAddress(".collateral_token"),
            oracle: json.readAddress(".oracle"),
            irm: json.readAddress(".irm"),
            lltv: json.readUint(".lltv")
        });
        job.borrower = borrower;
        job.seizedAssets = 0;
        job.repaidShares = borrowShares;
        job.swapPool = json.readAddress(".swap_pool");
        job.minSwapOut = 1;
        job.minProfit = 1_000_000; // 1 USDC. Gas is not part of this check.

        uint256 flashAssets = json.readUint(".flash_assets");
        exec.liquidateWithMorphoFlash(job, flashAssets);
        uint256 profit = IERC20(job.market.loanToken).balanceOf(address(exec));
        console2.log("aero liquidation profit usdc6", profit);
        assertGe(profit, 1_000_000);
        assertEq(IERC20(job.market.collateralToken).balanceOf(address(exec)), 0);
    }
}
