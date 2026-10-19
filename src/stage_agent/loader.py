"""Readers for the pack's JSONL and CSV files. Malformed lines are quarantined, never silently dropped."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


@dataclass
class Quarantine:
    items: list[dict[str, Any]] = field(default_factory=list)

    def add(self, path: Path | str, line_no: int, reason: str, raw: str) -> None:
        self.items.append({"file": str(path), "line": line_no, "reason": reason, "raw": raw[:300]})


def read_jsonl(path: Path | str, quarantine: Quarantine | None = None,
               required: tuple[str, ...] = ()) -> Iterator[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                if quarantine is not None:
                    quarantine.add(path, n, f"invalid JSON: {exc.msg}", line)
                continue
            if not isinstance(obj, dict):
                if quarantine is not None:
                    quarantine.add(path, n, "not a JSON object", line)
                continue
            missing = [k for k in required if k not in obj]
            if missing:
                if quarantine is not None:
                    quarantine.add(path, n, f"missing fields {missing}", line)
                continue
            yield obj


def read_csv(path: Path | str) -> list[dict[str, str]]:
    path = Path(path)
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_jsonl(path: Path | str, rows: list[dict[str, Any]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=False) + "\n")


EVENT_REQUIRED = ("event_id", "user_id", "event", "occurred_at", "sent_at", "received_at")
