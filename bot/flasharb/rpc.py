"""Tiny JSON-RPC client. Sends a user agent because several public endpoints reject the default urllib one."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


class RpcError(RuntimeError):
    pass


class Rpc:
    def __init__(self, url: str, timeout: float = 40.0, backups: list[str] | None = None):
        if not url:
            raise RpcError("empty RPC URL")
        self.urls = [url]
        for extra in backups or []:
            if extra and extra not in self.urls:
                self.urls.append(extra)
        self.url = url
        self.timeout = timeout
        self._id = 1

    def request(self, method: str, params: list[Any]) -> Any:
        last: Exception | None = None
        for url in self.urls:
            self.url = url
            for attempt in range(3):
                body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}).encode()
                self._id += 1
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "flashloan-arb-sim/0.1 (simulation; not a trader)",
                        "Accept": "application/json",
                    },
                )
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        payload = json.loads(resp.read().decode())
                except urllib.error.HTTPError as exc:
                    last = exc
                    if exc.code in {408, 429, 500, 502, 503, 504} and attempt < 2:
                        time.sleep(0.4 * (attempt + 1))
                        continue
                    break
                except (urllib.error.URLError, TimeoutError) as exc:
                    last = exc
                    if attempt < 2:
                        time.sleep(0.4 * (attempt + 1))
                        continue
                    break
                if "error" in payload:
                    message = str(payload["error"])
                    if any(token in message.lower() for token in ("rate", "limit", "timeout", "busy")) and attempt < 2:
                        time.sleep(0.4 * (attempt + 1))
                        last = RpcError(message)
                        continue
                    # A revert or a bad query will not succeed on another host. Surface it.
                    if method != "eth_getLogs":
                        raise RpcError(f"{method}: {payload['error']}")
                    last = RpcError(f"{method}: {payload['error']}")
                    break
                return payload["result"]
        raise RpcError(f"{method} failed against {self.urls}: {last}")

    def block_number(self) -> int:
        return int(self.request("eth_blockNumber", []), 16)

    def block(self, number: int) -> dict[str, Any]:
        return self.request("eth_getBlockByNumber", [hex(number), False])

    def eth_call(self, to: str, data: str, block: int | str = "latest") -> bytes:
        blk = block if isinstance(block, str) else hex(block)
        raw = self.request("eth_call", [{"to": to, "data": data}, blk])
        return bytes.fromhex(raw[2:]) if raw and raw != "0x" else b""

    def get_logs(
        self,
        address: list[str] | None,
        topics: list[Any],
        start: int,
        end: int,
    ) -> list[dict[str, Any]]:
        filt: dict[str, Any] = {
            "fromBlock": hex(start),
            "toBlock": hex(end),
            "topics": topics,
        }
        if address:
            filt["address"] = address
        return self.request("eth_getLogs", [filt])
