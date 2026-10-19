"""Simulated user-data-store latency and failures for replay (control event `dependency_degraded`).

The pseudo-random draws are a deterministic hash of (seed, trigger, attempt), so replays are reproducible.
Outside a degraded window a read costs `store_base_read_ms`. Inside one, 20% of reads time out and the rest
follow a heavy-tailed latency whose 95th percentile is about 8 s, matching the control event's description.
Reads slower than `store_read_timeout_ms` are abandoned as timeouts.
"""
from __future__ import annotations

import hashlib
from datetime import datetime

from .config import AgentConfig
from .model_provider import CallContext, StoreUnavailable


def h01(*parts: object) -> float:
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:12], 16) / 16 ** 12


class StoreGate:
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self.windows: list[tuple[datetime, datetime, float]] = []  # start, end, timeout share
        self.stats = {"reads": 0, "timeouts": 0, "failed_reads": 0}

    def add_window(self, start: datetime, end: datetime, timeout_share: float = 0.2) -> None:
        self.windows.append((start, end, timeout_share))

    def read(self, ctx: CallContext, what: str) -> None:
        """Account for one logical read; raise StoreUnavailable when every attempt fails."""
        ctx.budget.use_tool(f"store:{what}")
        win = next((w for w in self.windows if w[0] <= ctx.now < w[1]), None)
        self.stats["reads"] += 1
        if win is None:
            ctx.budget.wait(self.cfg.store_base_read_ms, f"store:{what}")
            return
        for attempt in range(1, self.cfg.store_read_max_attempts + 1):
            if attempt > 1:
                ctx.budget.use_tool(f"store:{what}:retry")
            u = h01(self.cfg.seed, "store", ctx.trigger_event_id, what, attempt)
            if u < win[2]:
                latency_ms = float("inf")
            else:
                q = (u - win[2]) / (1 - win[2])
                latency_ms = 500 + 9000 * q ** 3
            if latency_ms <= self.cfg.store_read_timeout_ms:
                ctx.budget.wait(latency_ms, f"store:{what}:degraded")
                return
            self.stats["timeouts"] += 1
            ctx.budget.wait(self.cfg.store_read_timeout_ms, f"store:{what}:timeout")
        self.stats["failed_reads"] += 1
        raise StoreUnavailable(f"user_data_store read '{what}' failed after {self.cfg.store_read_max_attempts} attempts")
