"""Model provider abstraction, deterministic mock, replay fault injection and a bounded, validating client.

Where the model is used: choosing between two or more goals that the deterministic planner has already proven
eligible. The model can only *choose among* evidenced candidates; any goal or eligibility it asserts outside
that set is rejected and logged, and its free-text reason is never treated as evidence. Prompts contain only
structured facts (no catalogue descriptions, search queries or deeplinks).

The default provider is a deterministic mock (no network, no API key). Its answer mirrors the configured
goal priority, so in mock mode the model never changes the outcome of a healthy decision; it exists to exercise
the contract, budgets and failure handling exactly as a live provider would.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from .config import AgentConfig
from .scheduler import iso, parse_ts


class ModelTimeout(Exception):
    pass


class ModelUnavailable(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class StoreUnavailable(Exception):
    pass


@dataclass
class DecisionBudget:
    """Per-decision call budget and simulated dependency latency ledger."""
    max_model_calls: int
    max_tool_calls: int
    model_calls: int = 0
    tool_calls: int = 0
    sim_ms: float = 0.0
    log: list[dict[str, Any]] = field(default_factory=list)

    def use_model(self) -> None:
        if self.model_calls >= self.max_model_calls:
            raise BudgetExceeded(f"model call budget {self.max_model_calls} exhausted")
        self.model_calls += 1

    def use_tool(self, name: str) -> None:
        if self.tool_calls >= self.max_tool_calls:
            raise BudgetExceeded(f"tool call budget {self.max_tool_calls} exhausted at {name}")
        self.tool_calls += 1

    def wait(self, ms: float, what: str) -> None:
        self.sim_ms += ms
        self.log.append({"what": what, "sim_ms": round(ms, 1)})


@dataclass
class CallContext:
    trigger_event_id: str
    user_id: str
    now: datetime
    budget: DecisionBudget


class ModelProvider(Protocol):
    name: str

    def complete(self, task: str, payload: dict[str, Any], ctx: CallContext) -> str: ...


class MockModelProvider:
    """Deterministic stand-in for an LLM. Returns JSON text; costs simulated latency."""
    name = "mock"

    def __init__(self, latency_ms: int):
        self.latency_ms = latency_ms

    def complete(self, task: str, payload: dict[str, Any], ctx: CallContext) -> str:
        ctx.budget.wait(self.latency_ms, f"model:{task}")
        if task != "goal_advice":
            raise ValueError(f"unknown task {task}")
        cands = payload.get("candidates") or []
        if not cands:
            return json.dumps({"goal_id": None, "eligible": False, "confidence": 0.0, "reason": "no candidates"})
        top = cands[0]
        return json.dumps({"goal_id": top["goal_id"], "eligible": True, "confidence": 0.6,
                           "reason": f"highest-priority evidenced candidate ({top['goal_id']})"})


class FaultInjectingProvider:
    """Replays replay/model_faults.jsonl: at a named trigger, return the given output or time out."""

    def __init__(self, inner: ModelProvider, faults: dict[str, dict[str, Any]], cfg: AgentConfig):
        self.inner = inner
        self.faults = faults
        self.cfg = cfg
        self.name = f"{inner.name}+faults"
        self.fired: dict[str, int] = {}

    def complete(self, task: str, payload: dict[str, Any], ctx: CallContext) -> str:
        f = self.faults.get(ctx.trigger_event_id)
        if f is None:
            return self.inner.complete(task, payload, ctx)
        self.fired[ctx.trigger_event_id] = self.fired.get(ctx.trigger_event_id, 0) + 1
        if f.get("fault") == "timeout":
            waited = min(float(f.get("seconds", 60)) * 1000, self.cfg.model_timeout_ms)
            ctx.budget.wait(waited, "model:timeout")
            raise ModelTimeout(f"model did not answer within {self.cfg.model_timeout_ms} ms (fault fixture)")
        ctx.budget.wait(self.cfg.mock_model_latency_ms, f"model:{task}:fault")
        out = f.get("model_output")
        return out if isinstance(out, str) else json.dumps(out)


class OutageGate:
    """Makes the wrapped provider fail (HTTP 503) during dependency_unavailable windows."""

    def __init__(self, inner: ModelProvider, cfg: AgentConfig):
        self.inner = inner
        self.cfg = cfg
        self.name = inner.name
        self.windows: list[tuple[datetime, datetime]] = []

    def add_window(self, start: datetime, end: datetime) -> None:
        self.windows.append((start, end))

    def complete(self, task: str, payload: dict[str, Any], ctx: CallContext) -> str:
        if any(s <= ctx.now < e for s, e in self.windows):
            ctx.budget.wait(self.cfg.model_unavailable_latency_ms, "model:503")
            raise ModelUnavailable("HTTP 503 from model provider (dependency_unavailable window)")
        return self.inner.complete(task, payload, ctx)


ADVICE_SCHEMA = {"goal_id": (str, type(None)), "eligible": (bool,), "confidence": (int, float), "reason": (str,)}


def parse_advice(text: str) -> dict[str, Any]:
    """Strict parse: valid JSON object with exactly the expected field types. Raises ValueError otherwise."""
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("advice is not a JSON object")
    for k, types in ADVICE_SCHEMA.items():
        if k not in obj:
            raise ValueError(f"advice missing '{k}'")
        if not isinstance(obj[k], types) or (k == "confidence" and isinstance(obj[k], bool)):
            raise ValueError(f"advice field '{k}' has wrong type")
    if not 0.0 <= float(obj["confidence"]) <= 1.0:
        raise ValueError("confidence out of range")
    return obj


@dataclass
class AdviceResult:
    status: str  # ok | rejected_unsupported_claim | malformed | timeout | unavailable | skipped_* | budget_exhausted
    goal_id: str | None = None
    attempts: int = 0
    notes: list[str] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.status == "ok" and self.goal_id is not None


class ModelClient:
    """Bounded retries, strict parsing, claim validation and a circuit breaker over a ModelProvider."""

    def __init__(self, provider: ModelProvider, cfg: AgentConfig):
        self.provider = provider
        self.cfg = cfg
        self.consecutive_failures = 0
        self.open_until: datetime | None = None
        self.stats: dict[str, int] = {}

    def _bump(self, k: str) -> None:
        self.stats[k] = self.stats.get(k, 0) + 1

    def goal_advice(self, candidates: list[dict[str, Any]], facts: dict[str, Any], ctx: CallContext) -> AdviceResult:
        res = AdviceResult(status="skipped")
        if self.open_until is not None and ctx.now < self.open_until:
            res.status = "skipped_circuit_open"
            res.notes.append(f"circuit open until {iso(self.open_until)} after repeated provider failures")
            self._bump(res.status)
            return res
        allowed = {c["goal_id"] for c in candidates}
        payload = {"candidates": candidates, "facts": facts}
        for attempt in range(1, self.cfg.model_max_attempts + 1):
            try:
                ctx.budget.use_model()
            except BudgetExceeded as exc:
                res.status = "budget_exhausted"
                res.notes.append(str(exc))
                break
            res.attempts = attempt
            try:
                text = self.provider.complete("goal_advice", payload, ctx)
            except ModelTimeout as exc:
                res.status = "timeout"
                res.notes.append(f"attempt {attempt}: {exc}")
                self._failure(ctx.now)
                continue
            except ModelUnavailable as exc:
                res.status = "unavailable"
                res.notes.append(f"attempt {attempt}: {exc}")
                self._failure(ctx.now)
                if self.open_until is not None:
                    break
                continue
            res.raw.append(text[:500])
            try:
                adv = parse_advice(text)
            except (ValueError, json.JSONDecodeError) as exc:
                res.status = "malformed"
                res.notes.append(f"attempt {attempt}: rejected malformed output ({exc})")
                continue
            self.consecutive_failures = 0
            gid = adv["goal_id"]
            if not adv["eligible"] or gid is None:
                res.status = "ok"
                res.goal_id = None
                res.notes.append("model declined to choose")
                break
            if gid not in allowed:
                res.status = "rejected_unsupported_claim"
                res.notes.append(f"model claimed {gid} eligible (confidence {adv['confidence']}) with reason "
                                 f"{adv['reason']!r}; no deterministic evidence supports {gid}, claim rejected "
                                 "(model text is not evidence)")
                break  # a deterministic provider would repeat the claim; retrying only burns budget
            res.status, res.goal_id = "ok", gid
            break
        self._bump(res.status)
        return res

    def _failure(self, now: datetime) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.cfg.circuit_breaker_failures:
            self.open_until = now + timedelta(seconds=self.cfg.circuit_breaker_cooldown_s)
            self.consecutive_failures = 0
            self._bump("circuit_opened")


def load_faults(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["trigger_event_id"]: r for r in rows if r.get("trigger_event_id")}
