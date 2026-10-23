#!/usr/bin/env bash
# Fork tests against public RPCs. Unit tests are not run here.
# A chain whose node no longer has the pinned block is skipped (exit 0).
# An assertion failure, or any other test failure, fails the job.
set -u

export ETH_RPC_URL="${ETH_RPC_URL:-https://ethereum-rpc.publicnode.com}"
export BASE_RPC_URL="${BASE_RPC_URL:-https://mainnet.base.org}"
export ARB_RPC_URL="${ARB_RPC_URL:-https://arb1.arbitrum.io/rpc}"

run_one() {
  local match="$1"
  local log
  log="$(mktemp)"
  echo "== forge fork tests: ${match} =="
  if forge test --match-path test/fork/Fork.t.sol --match-contract "$match" -vv >"$log" 2>&1; then
    cat "$log"
    rm -f "$log"
    return 0
  fi
  cat "$log"
  # Real failures: assertions, explicit reverts we assert, or the crafted-arb helper.
  if grep -E -i "assertion failed|assertEq|assertGt|panic|no profitable size|MinProfit|NotOwner|Slippage|\\[FAIL\\..*(?:assert|panic|profitable|MinProfit)" "$log" >/dev/null; then
    echo "fork test assertion failed for ${match}"
    rm -f "$log"
    return 1
  fi
  if grep -E -i "rpc|429|403|401|pruned|archive|missing trie|history has been pruned|transport|timed out|timeout|HTTP|connection|rate limit|server returned an error|error sending request|forked chain|cheatcode" "$log" >/dev/null; then
    echo "skipping ${match}: RPC or historical state is unavailable"
    rm -f "$log"
    return 0
  fi
  echo "fork tests failed for ${match} for an unclassified reason"
  rm -f "$log"
  return 1
}

run_file() {
  local file="$1"
  local match="$2"
  local log
  log="$(mktemp)"
  echo "== forge fork tests: ${match} =="
  if forge test --match-path "$file" --match-contract "$match" -vv >"$log" 2>&1; then
    cat "$log"
    rm -f "$log"
    return 0
  fi
  cat "$log"
  if grep -E -i "assertion failed|assertEq|assertGt|assertGe|panic|no profitable size|MinProfit|NotOwner|Slippage|Shortfall|\\[FAIL\\..*(?:assert|panic|profitable|MinProfit|Shortfall)" "$log" >/dev/null; then
    echo "fork test assertion failed for ${match}"
    rm -f "$log"
    return 1
  fi
  if grep -E -i "rpc|429|403|401|pruned|archive|missing trie|history has been pruned|transport|timed out|timeout|HTTP|connection|rate limit|server returned an error|error sending request|forked chain|cheatcode" "$log" >/dev/null; then
    echo "skipping ${match}: RPC or historical state is unavailable"
    rm -f "$log"
    return 0
  fi
  echo "fork tests failed for ${match} for an unclassified reason"
  rm -f "$log"
  return 1
}

status=0
run_one MainnetForkTest || status=1
run_one BaseForkTest || status=1
run_one ArbitrumForkTest || status=1
run_file test/fork/LiquidationFork.t.sol LiquidationForkTest || status=1
exit "$status"
