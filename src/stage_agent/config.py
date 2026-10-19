"""Agent configuration (policy limits, budgets, timeouts) and the pack's own configuration files."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentConfig:
    # --- freshness ---------------------------------------------------------------------------------------
    # Device-measured buffering delay (sent_at - occurred_at) above which a trigger is not a live session.
    # The data has a natural gap between 5 and 10 minutes (see DATA_QUALITY.md).
    stale_trigger_seconds: int = 600
    # |received_at - sent_at| above this marks the phone clock as wrong (event still usable via buffer delay).
    clock_skew_flag_seconds: int = 600
    profile_stale_days: int = 7

    # --- budgets -------------------------------------------------------------------------------------------
    max_model_calls: int = 8
    max_tool_calls: int = 20
    model_timeout_ms: int = 3000
    model_max_attempts: int = 2
    mock_model_latency_ms: int = 700
    model_unavailable_latency_ms: int = 150
    circuit_breaker_failures: int = 3
    circuit_breaker_cooldown_s: int = 60
    store_base_read_ms: int = 15
    store_read_timeout_ms: int = 2500
    store_read_max_attempts: int = 2
    decision_soft_budget_ms: int = 20000  # optional model work is skipped beyond this

    # --- real-time ----------------------------------------------------------------------------------------
    workers: int = 8
    shed_queue_depth: int = 40  # queue depth above which optional model work is skipped
    latency_target_s: int = 60
    latency_fail_s: int = 300

    # --- decision policy ------------------------------------------------------------------------------------
    goal_priority: tuple[str, ...] = ("G04", "G07", "G01", "G02", "G03", "G12", "G09", "G08", "G06", "G05")
    recent_lapse_days: int = 30  # interpretation of the underspecified "recently lapsed" (G06)
    resume_idle_days: int = 3
    habit_lookback_days: int = 14
    absence_days: int = 14
    min_confidence_page: float = 0.40
    min_confidence_message: float = 0.60
    message_goals: tuple[str, ...] = ("G03", "G07", "G12", "G04")
    in_app_goals: tuple[str, ...] = ("G04", "G06")
    message_followup_hours: int = 3
    message_morning_hour_ist: int = 9
    trial_ending_within_days: int = 2
    popularity_days: int = 14
    new_release_days: int = 30
    target_titles_per_row: int = 8
    max_rows_used: int = 5

    seed: str = "stage-agent-v1"

    @classmethod
    def load(cls, path: str | os.PathLike | None) -> "AgentConfig":
        if path is None or not Path(path).exists():
            return cls()
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        known = {f.name for f in fields(cls)}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"unknown agent config keys: {sorted(unknown)}")
        coerced = {k: tuple(v) if isinstance(v, list) else v for k, v in raw.items()}
        return replace(cls(), **coerced)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PackPaths:
    root: Path

    def p(self, rel: str) -> Path:
        return self.root / rel

    @property
    def events(self) -> Path:
        return self.p("data/events.jsonl")

    @property
    def profiles(self) -> Path:
        return self.p("data/profiles_snapshot.csv")

    @property
    def catalogue(self) -> Path:
        return self.p("data/catalogue.csv")

    @property
    def episodes(self) -> Path:
        return self.p("data/episodes.csv")

    @property
    def goals(self) -> Path:
        return self.p("config/goals.json")

    @property
    def interventions(self) -> Path:
        return self.p("config/interventions.json")

    @property
    def pricing(self) -> Path:
        return self.p("config/pricing.json")

    @property
    def triggers(self) -> Path:
        return self.p("replay/triggers.jsonl")

    @property
    def live_events(self) -> Path:
        return self.p("replay/live_events.jsonl")

    @property
    def control_events(self) -> Path:
        return self.p("replay/control_events.jsonl")

    @property
    def model_faults(self) -> Path:
        return self.p("replay/model_faults.jsonl")

    @property
    def arms(self) -> Path:
        return self.p("replay/arms.csv")

    @property
    def pilot_assignment(self) -> Path:
        return self.p("pilot/pilot_assignment.csv")

    @property
    def pilot_outcomes(self) -> Path:
        return self.p("pilot/pilot_outcomes.csv")

    @property
    def simulator(self) -> Path:
        return self.p("simulator/simulate_outcomes.py")


@dataclass
class PackConfig:
    goals: dict[str, Any]
    interventions: dict[str, Any]
    pricing: dict[str, Any]

    @classmethod
    def load(cls, paths: PackPaths) -> "PackConfig":
        def rd(p: Path) -> dict[str, Any]:
            with open(p, encoding="utf-8") as f:
                return json.load(f)

        cfg = cls(goals=rd(paths.goals), interventions=rd(paths.interventions), pricing=rd(paths.pricing))
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """Fail fast on a configuration the agent cannot honour safely."""
        hp = self.interventions["home_page"]
        rows = hp["rows"]
        assert rows["min_titles_per_row"] <= rows["max_titles_per_row"], "row bounds inverted"
        assert hp["hero"]["max_titles"] >= 1
        assert self.interventions["push"]["copy"]["title_max_chars"] > 0
        assert self.interventions["whatsapp"]["approved_templates"], "no approved WhatsApp templates"
        for plan in self.pricing["plans"]:
            assert isinstance(plan["price_inr"], (int, float)) and plan["price_inr"] > 0
        ids = [g["goal_id"] for g in self.goals["goals"]]
        assert len(ids) == len(set(ids)), "duplicate goal ids"

    @property
    def page_ttl_minutes(self) -> int:
        return int(self.interventions["home_page"]["ttl_minutes"])

    @property
    def approved_templates(self) -> dict[str, dict[str, Any]]:
        return {t["template_id"]: t for t in self.interventions["whatsapp"]["approved_templates"]}

    @property
    def plans(self) -> dict[str, dict[str, Any]]:
        return {p["plan_id"]: p for p in self.pricing["plans"]}


def default_pack_root() -> Path:
    env = os.environ.get("STAGE_PACK")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "pack"
