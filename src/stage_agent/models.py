"""Typed records shared across the agent."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

from .scheduler import iso

# The event vocabulary of the pack (README "data/events.jsonl" table) plus the two event types the simulator
# adds. A goal whose success event is not in this set cannot be measured.
OBSERVABLE_EVENTS = frozenset({
    "signup_completed", "login_success", "app_open", "app_dialect_changed", "home_viewed", "title_viewed",
    "search", "play_started", "play_progress", "episode_completed", "paywall_viewed", "purchase_completed",
    "subscription_renewed", "subscription_lapsed", "message_delivered", "message_clicked",
    "notification_opt_out", "home_page_shown", "message_failed",
})

TRIGGER_EVENTS = frozenset({"app_open", "login_success"})


@dataclass
class Event:
    event_id: str
    user_id: str
    event: str
    occurred_at: datetime
    sent_at: datetime
    received_at: datetime
    session_id: str | None
    app_dialect_raw: str | None
    app_language: str | None
    platform: str | None
    properties: dict[str, Any]
    source: str  # history | live | trigger | outcome
    deliver_at: datetime | None = None
    buffer_delay_s: float = 0.0  # sent_at - occurred_at: device-measured, immune to phone clock offset
    est_occurred_at: datetime | None = None  # received_at - buffer_delay: server-anchored occurrence estimate
    clock_skew_s: float = 0.0  # received_at - sent_at: large magnitude = wrong phone clock
    anomalies: tuple[str, ...] = ()

    @property
    def time(self) -> datetime:
        """Best estimate of when the event really happened (server-anchored)."""
        return self.est_occurred_at or self.received_at

    @property
    def arrival(self) -> datetime:
        return self.deliver_at or self.received_at

    def ref(self) -> str:
        return f"event:{self.event_id}"


@dataclass
class Title:
    title_id: str
    content_slug: str
    name: str
    dialect: str
    language: str
    content_type: str
    genres: tuple[str, ...]
    release_date: date | None
    available_from: datetime | None
    available_until: datetime | None
    is_premium: bool
    description: str
    suspicious: bool = False
    suspicious_reason: str | None = None


@dataclass
class Fact:
    name: str
    value: Any
    source: str  # app_setting | profile | event | derived | assignment | unknown
    source_ref: str | None
    source_time: datetime | None
    rule: str
    freshness: str = "unknown"  # fresh | aging | stale | unknown
    age_s: float | None = None
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    supported: bool = True  # False when evidence is insufficient or conflicts are unresolved

    def to_dict(self) -> dict[str, Any]:
        v = self.value
        if isinstance(v, datetime):
            v = iso(v)
        return {
            "name": self.name, "value": v, "source": self.source, "source_ref": self.source_ref,
            "source_time": iso(self.source_time), "freshness": self.freshness,
            "age_s": None if self.age_s is None else round(self.age_s, 1), "rule": self.rule,
            "conflicts": self.conflicts, "supported": self.supported,
        }


@dataclass
class Check:
    name: str
    passed: bool
    severity: str  # critical | warning | info
    reason: str
    evidence: list[str] = field(default_factory=list)
    subject: str = "decision"  # decision | goal | home_page | push | whatsapp | in_app_message

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GoalRule:
    goal_id: str
    name: str
    revision: int
    eligibility: dict[str, Any]
    success_event: str | None
    success_condition: str
    window_hours: int
    source: str  # config file version or control event reference

    @property
    def version(self) -> str:
        return f"{self.goal_id}-r{self.revision}"

    @property
    def measurable(self) -> bool:
        return self.success_event is not None and self.success_event in OBSERVABLE_EVENTS

    @property
    def unmeasurable_reason(self) -> str | None:
        if self.success_event is None:
            return f"{self.goal_id} has no success event ('{self.success_condition}' is not observable)"
        if self.success_event not in OBSERVABLE_EVENTS:
            return f"{self.goal_id} success event '{self.success_event}' never appears in the event stream"
        return None
