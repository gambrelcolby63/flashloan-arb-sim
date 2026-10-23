#!/usr/bin/env bash
# Start a local Anvil fork of the pinned mainnet block, run the demo, stop Anvil.
# The demo submits only to 127.0.0.1. It does not post to Flashbots.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export ETH_RPC_URL="${ETH_RPC_URL:-https://ethereum-rpc.publicnode.com}"
export FORK_RPC_URL="${FORK_RPC_URL:-http://127.0.0.1:8545}"
BLOCK="$(python3 -c 'import json; print(json.load(open("config/fork-blocks.json"))["mainnet"])')"

if ! command -v forge >/dev/null; then
  echo "forge is not installed" >&2
  exit 1
fi
forge build

started=0
if ! curl -sf -X POST -H 'content-type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' \
  "$FORK_RPC_URL" >/dev/null; then
  echo "starting anvil fork at block ${BLOCK}"
  anvil --fork-url "$ETH_RPC_URL" --fork-block-number "$BLOCK" --port 8545 --silent >anvil.log 2>&1 &
  echo $! >anvil.pid
  started=1
  for _ in $(seq 1 60); do
    if curl -sf -X POST -H 'content-type: application/json' \
      --data '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' \
      "$FORK_RPC_URL" >/dev/null; then
      break
    fi
    sleep 1
  done
fi

cleanup() {
  if [[ "$started" -eq 1 && -f anvil.pid ]]; then
    kill "$(cat anvil.pid)" >/dev/null 2>&1 || true
    rm -f anvil.pid
  fi
}
trap cleanup EXIT

PYTHONPATH=bot python3 -m flasharb.fork_demo
