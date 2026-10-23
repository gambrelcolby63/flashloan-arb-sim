"""Private-orderflow submission, exercised only against a local fork.

Modes
-----
simulate
    Default. Build the private payload and stop. No ``eth_sendRawTransaction``.
fork
    Record the Flashbots Protect (mainnet) or sequencer-direct (L2) payload,
    then deliver the signed transaction to a loopback Anvil only.
live
    Disabled unless ``ARB_MODE=live`` and
    ``ARB_ALLOW_LIVE=i-understand-live-trading-is-not-recommended``.
    Not recommended. Searchers will take the trade, and this code path is not
    a promise of profit or of privacy.

L2 notes
--------
Base has no public mempool and no Flashbots bundles. A transaction sent to the
sequencer (``https://mainnet-sequencer.base.org``) is ordered by arrival time.
That is a latency race, not a sealed bundle.

Arbitrum also sequences on arrival. The sequencer feed is public after
acceptance. Priority is sold through Timeboost, not through Flashbots. Posting
to the sequencer does not win that auction.

Mainnet Flashbots Protect (``eth_sendPrivateTransaction`` via
``https://rpc.flashbots.net``) hides the raw transaction from the public
mempool. It does not stop another searcher's bundle from landing first.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

LIVE_ACK = "i-understand-live-trading-is-not-recommended"

FLASHBOTS_PROTECT_RPC = "https://rpc.flashbots.net"
SEQUENCER_RPC = {
    "base": "https://mainnet-sequencer.base.org",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
}

NOTES = {
    "mainnet": (
        "Flashbots Protect hides the tx from the public mempool. It does not "
        "reserve the state. Another searcher's bundle can still backrun the "
        "same pools in an earlier position."
    ),
    "base": (
        "Base has no public mempool and no Flashbots bundles. Direct sequencer "
        "submission is a latency race against other searchers."
    ),
    "arbitrum": (
        "Arbitrum orders via the sequencer. The feed is public after acceptance. "
        "Timeboost, not a private bundle, sells the right to go first. Direct "
        "submission does not enter that auction."
    ),
}

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

Transport = Callable[[str, dict[str, Any]], Any]
SendRaw = Callable[[bytes], str]


class SubmitRefused(RuntimeError):
    pass


@dataclass
class SubmissionResult:
    mode: str
    chain: str
    endpoint: str
    method: str
    note: str
    payload: dict[str, Any]
    tx_hash: str | None = None
    posted: bool = False


@dataclass
class PrivateSubmitter:
    mode: str = "simulate"
    submit_url: str = "http://127.0.0.1:8545"
    allow_live: str = ""
    transport: Transport | None = None
    send_raw: SendRaw | None = None
    last_payload: dict[str, Any] | None = field(default=None, init=False)

    @classmethod
    def from_env(cls, send_raw: SendRaw | None = None, transport: Transport | None = None) -> "PrivateSubmitter":
        return cls(
            mode=os.environ.get("ARB_MODE", "simulate").strip().lower() or "simulate",
            submit_url=os.environ.get("FORK_RPC_URL", "http://127.0.0.1:8545"),
            allow_live=os.environ.get("ARB_ALLOW_LIVE", ""),
            transport=transport,
            send_raw=send_raw,
        )

    def build_payload(self, raw_tx: bytes, chain: str, target_block: int) -> dict[str, Any]:
        raw_hex = raw_tx if isinstance(raw_tx, str) else "0x" + raw_tx.hex()
        chain = chain.lower()
        if chain == "mainnet":
            rpc = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_sendPrivateTransaction",
                "params": [{"tx": raw_hex, "maxBlockNumber": hex(target_block)}],
            }
            endpoint = FLASHBOTS_PROTECT_RPC
        elif chain in SEQUENCER_RPC:
            rpc = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_sendRawTransaction",
                "params": [raw_hex],
            }
            endpoint = SEQUENCER_RPC[chain]
        else:
            raise SubmitRefused(f"no private route configured for chain {chain}")
        return {
            "rpc": rpc,
            "endpoint": endpoint,
            "note": NOTES[chain],
            "chain": chain,
            "target_block": target_block,
        }

    def submit(self, raw_tx: bytes, chain: str, target_block: int) -> SubmissionResult:
        if self.mode not in {"simulate", "fork", "live"}:
            raise SubmitRefused(f"unknown ARB_MODE {self.mode!r}")
        payload = self.build_payload(raw_tx, chain, target_block)
        self.last_payload = payload
        method = payload["rpc"]["method"]
        if self.mode == "simulate":
            return SubmissionResult(
                mode="simulate",
                chain=chain,
                endpoint="not-sent",
                method=method,
                note=payload["note"],
                payload=payload,
                posted=False,
            )
        if self.mode == "fork":
            self._assert_loopback(self.submit_url)
            if self.send_raw is None:
                raise SubmitRefused("fork mode has no loopback send_raw transport")
            # The private payload is built and recorded, then the raw tx is
            # delivered to Anvil. Nothing is posted to Flashbots or a sequencer.
            tx_hash = self.send_raw(raw_tx)
            return SubmissionResult(
                mode="fork",
                chain=chain,
                endpoint=self.submit_url,
                method=method,
                note=payload["note"],
                payload=payload,
                tx_hash=tx_hash,
                posted=False,
            )
        self._assert_live_allowed()
        if self.transport is None:
            raise SubmitRefused("live mode has no transport; refusing to send")
        endpoint = payload["endpoint"]
        self._reject_if_read_rpc(endpoint)
        self.transport(endpoint, payload["rpc"])
        return SubmissionResult(
            mode="live",
            chain=chain,
            endpoint=endpoint,
            method=method,
            note=payload["note"] + " LIVE MODE IS NOT RECOMMENDED.",
            payload=payload,
            posted=True,
        )

    @staticmethod
    def _assert_loopback(url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        if host not in LOOPBACK_HOSTS:
            raise SubmitRefused(
                f"fork mode refuses non-loopback submit URL {url!r}. "
                "Start Anvil and point FORK_RPC_URL at 127.0.0.1."
            )

    def _assert_live_allowed(self) -> None:
        if self.allow_live != LIVE_ACK:
            raise SubmitRefused(
                "live mode is not recommended and is disabled. "
                f"Set ARB_ALLOW_LIVE={LIVE_ACK} to arm it. Do not."
            )

    @staticmethod
    def _reject_if_read_rpc(endpoint: str) -> None:
        # Live submission must target the private endpoint the payload names,
        # never an ambient read RPC that happens to be a public mempool.
        host = (urlparse(endpoint).hostname or "").lower()
        if host in LOOPBACK_HOSTS:
            raise SubmitRefused("live mode refuses loopback; that is fork mode")
