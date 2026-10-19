"""Structured logging and latency statistics."""
from __future__ import annotations

import json
import logging
import math
from typing import Any, Iterable

_STD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        out.update({k: v for k, v in vars(record).items() if k not in _STD})
        return json.dumps(out, ensure_ascii=False, default=str)


def setup_logging(path: str | None = None, level: int = logging.INFO) -> None:
    root = logging.getLogger("stage_agent")
    root.setLevel(level)
    root.handlers.clear()
    h: logging.Handler = logging.FileHandler(path, mode="w", encoding="utf-8") if path else logging.StreamHandler()
    h.setFormatter(JsonFormatter())
    root.addHandler(h)
    root.propagate = False


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in [0, 100])."""
    if not values:
        return None
    s = sorted(values)
    k = max(1, math.ceil(q / 100 * len(s)))
    return s[k - 1]


def summarize(values: Iterable[float]) -> dict[str, Any]:
    v = list(values)
    if not v:
        return {"n": 0}
    return {"n": len(v), "median_ms": round(percentile(v, 50), 1), "p95_ms": round(percentile(v, 95), 1),
            "p99_ms": round(percentile(v, 99), 1), "max_ms": round(max(v), 1), "mean_ms": round(sum(v) / len(v), 1)}
