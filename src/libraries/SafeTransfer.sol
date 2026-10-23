// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "../interfaces/Interfaces.sol";

/// @dev Handles tokens that return nothing (USDT) as well as tokens that return false.
library SafeTransfer {
    error TransferFailed();
    error ApproveFailed();

    function safeTransfer(address token, address to, uint256 amount) internal {
        (bool ok, bytes memory data) = token.call(abi.encodeWithSelector(IERC20.transfer.selector, to, amount));
        _check(ok, data);
    }

    function safeTransferFrom(address token, address from, address to, uint256 amount) internal {
        (bool ok, bytes memory data) =
            token.call(abi.encodeWithSelector(IERC20.transferFrom.selector, from, to, amount));
        _check(ok, data);
    }

    /// @notice Zeroes the allowance first so USDT-style tokens can be re-approved.
    function forceApprove(address token, address spender, uint256 amount) internal {
        (bool ok, bytes memory data) = token.call(abi.encodeWithSelector(IERC20.approve.selector, spender, 0));
        _checkApprove(ok, data);
        if (amount == 0) return;
        (ok, data) = token.call(abi.encodeWithSelector(IERC20.approve.selector, spender, amount));
        _checkApprove(ok, data);
    }

    function _check(bool ok, bytes memory data) private pure {
        if (!ok) _bubble(data, TransferFailed.selector);
        if (data.length != 0 && !abi.decode(data, (bool))) revert TransferFailed();
    }

    function _checkApprove(bool ok, bytes memory data) private pure {
        if (!ok) _bubble(data, ApproveFailed.selector);
        if (data.length != 0 && !abi.decode(data, (bool))) revert ApproveFailed();
    }

    function _bubble(bytes memory data, bytes4 fallbackSelector) private pure {
        if (data.length > 0) {
            assembly {
                revert(add(data, 32), mload(data))
            }
        }
        assembly {
            mstore(0x00, fallbackSelector)
            revert(0x00, 0x04)
        }
    }
}
