"""Block-level competition accounting.

An opportunity opens when a transaction moves two pools of the same pair from
"not profitable" to "profitable" for a directed cycle. It is treated as captured
by another bot only when a *later* transaction in the same block or the next
block touches both pools and the cycle is no longer profitable afterwards.
Closing the spread without an atomic two-pool transaction is recorded as
ambiguous (could be two unrelated swaps). A spread that is still profitable at
the end of the next block is uncaptured: that is the residual a slower bot
could still see.

Spreads that stay open are counted once. They are not re-opened on every
subsequent block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Hashable, Mapping


Key = tuple[Hashable, Hashable, Hashable]


@dataclass
class ReplayTx:
    block: int
    tx_index: int
    tx_hash: str
    pools: tuple[Hashable, ...]
    apply: Callable[[dict], None]


@dataclass
class EpisodeResult:
    key: Key
    opened_block: int
    opened_tx_index: int
    opened_profit: int
    outcome: str  # captured_same_block, captured_next_block, uncaptured, ambiguous
    closed_block: int
    remaining_profit: int
    tier: str = ""


@dataclass
class CompetitionReport:
    episodes: list[EpisodeResult] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {
            "opportunities": len(self.episodes),
            "captured_same_block": 0,
            "captured_next_block": 0,
            "uncaptured": 0,
            "ambiguous": 0,
        }
        for ep in self.episodes:
            out[ep.outcome] = out.get(ep.outcome, 0) + 1
        return out


ProfitFn = Callable[[dict, Hashable, Hashable, Hashable], int]


def classify(
    start_block: int,
    end_block: int,
    state: dict,
    txs: list[ReplayTx],
    profit_fn: ProfitFn,
    keys_for_pools: Callable[[set[Hashable]], list[Key]],
    tier_of: Callable[[Key], str] | None = None,
) -> CompetitionReport:
    """Walk `txs` in order. `end_block` is included so an open at end-1 can be judged.

    New episodes are not opened on `end_block` itself (there is no following block
    in the window). `profit_fn` must be pure with respect to anything except `state`.
    """
    by_block: dict[int, list[ReplayTx]] = {}
    for tx in txs:
        by_block.setdefault(tx.block, []).append(tx)
    for block_txs in by_block.values():
        block_txs.sort(key=lambda t: t.tx_index)

    open_eps: dict[Key, EpisodeResult] = {}
    suppressed: set[Key] = set()
    report = CompetitionReport()

    def profit(key: Key) -> int:
        return int(profit_fn(state, key[0], key[1], key[2]))

    for block in range(start_block, end_block + 1):
        for tx in by_block.get(block, []):
            touched = set(tx.pools)
            keys = list(keys_for_pools(touched))
            # Include already-open keys the tx might close even if keys_for_pools
            # only looks at touched pools. Both pools of an open key must be checked
            # when either is touched.
            for key, ep in open_eps.items():
                if key[0] in touched or key[1] in touched:
                    if key not in keys:
                        keys.append(key)
            pre = {key: profit(key) for key in keys}
            tx.apply(state)
            for key in keys:
                post = profit(key)
                both = key[0] in touched and key[1] in touched
                ep = open_eps.get(key)
                if ep is not None and both and post <= 0 and not (ep.opened_block == block and ep.opened_tx_index == tx.tx_index):
                    if block == ep.opened_block:
                        outcome = "captured_same_block"
                    elif block == ep.opened_block + 1:
                        outcome = "captured_next_block"
                    else:
                        # Outside the same-or-next window. Treat the residual as
                        # already classified at the deadline; ignore late captures.
                        continue
                    ep.outcome = outcome
                    ep.closed_block = block
                    ep.remaining_profit = 0
                    report.episodes.append(ep)
                    del open_eps[key]
                    suppressed.discard(key)
                    continue
                if (
                    ep is None
                    and post > 0
                    and pre.get(key, 0) <= 0
                    and key not in suppressed
                    and block < end_block
                ):
                    open_eps[key] = EpisodeResult(
                        key=key,
                        opened_block=block,
                        opened_tx_index=tx.tx_index,
                        opened_profit=post,
                        outcome="open",
                        closed_block=block,
                        remaining_profit=post,
                        tier=tier_of(key) if tier_of else "",
                    )
                if post <= 0:
                    suppressed.discard(key)

        finished: list[Key] = []
        for key, ep in open_eps.items():
            if block < ep.opened_block + 1:
                continue
            post = profit(key)
            if post > 0:
                ep.outcome = "uncaptured"
                ep.remaining_profit = post
                suppressed.add(key)
            else:
                ep.outcome = "ambiguous"
                ep.remaining_profit = 0
                suppressed.discard(key)
            ep.closed_block = block
            report.episodes.append(ep)
            finished.append(key)
        for key in finished:
            del open_eps[key]

    return report


def summarize_by_tier(episodes: list[EpisodeResult], usd_of: Callable[[EpisodeResult], float]) -> dict[str, dict]:
    tiers: dict[str, dict] = {}
    for ep in episodes:
        tier = ep.tier or "unknown"
        row = tiers.setdefault(
            tier,
            {
                "opportunities": 0,
                "captured_same_block": 0,
                "captured_next_block": 0,
                "uncaptured": 0,
                "ambiguous": 0,
                "uncaptured_net_usd": 0.0,
                "opened_net_usd": 0.0,
            },
        )
        row["opportunities"] += 1
        row[ep.outcome] = row.get(ep.outcome, 0) + 1
        if ep.outcome == "uncaptured":
            row["uncaptured_net_usd"] += float(usd_of(ep))
    return tiers


def empty_tier_row() -> dict[str, float | int]:
    return {
        "opportunities": 0,
        "captured_same_block": 0,
        "captured_next_block": 0,
        "uncaptured": 0,
        "ambiguous": 0,
        "uncaptured_net_usd": 0.0,
        "gross_opened_usd": 0.0,
    }
