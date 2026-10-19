"""Turn raw event dicts into normalized Events with trustworthy timing, and parse the catalogue.

Timing model (see ARCHITECTURE.md, "Event time"):
  * received_at / deliver_at are server clocks and are trusted for ordering.
  * occurred_at and sent_at both come from the phone clock. A wrong phone clock shifts both by the same offset,
    so their difference (buffer_delay = sent_at - occurred_at) is a skew-free measure of offline buffering.
  * est_occurred_at = received_at - buffer_delay anchors the occurrence on the server clock.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from .loader import Quarantine
from .models import Event, Title
from .scheduler import parse_ts

DIALECT_RE = re.compile(r"^d\d+$")
CAMPAIGN_DEEPLINK_RE = re.compile(r"^ott://c/(?P<campaign>[A-Za-z0-9_-]+)/(?P<dialect>d\d+)/title/(?P<title_id>T\d+)$")
TITLE_DEEPLINK_RE = re.compile(r"^ott://title/(T\d+)$")

# Text in the data is content, never instruction. These patterns only flag text for review/quarantine.
INJECTION_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"\bignore (all |your )?(previous|prior) (rules|instructions)",
        r"\b(system|assistant|developer)\s*(note|instruction|prompt|message)\b",
        r"\bAI agents?\b",
        r"\bskip (all )?(checks|verification)",
        r"\b(message|notify|send)\b.{0,40}\bevery (user|viewer)",
        r"\bmark every (user|viewer)\b",
        r"\bpre-?approved\b",
        r"\bopted out\b",
    )
]


def scan_for_instructions(text: str | None) -> str | None:
    if not text:
        return None
    hits = [p.pattern for p in INJECTION_PATTERNS if p.search(text)]
    return "; ".join(hits) if hits else None


def normalize_dialect(raw: Any) -> str | None:
    if isinstance(raw, str) and DIALECT_RE.match(raw):
        return raw
    return None


def parse_campaign_deeplink(link: Any) -> dict[str, str] | None:
    if not isinstance(link, str):
        return None
    m = CAMPAIGN_DEEPLINK_RE.match(link)
    return m.groupdict() if m else None


def normalize_event(raw: dict[str, Any], source: str, clock_skew_flag_s: int = 600,
                    quarantine: Quarantine | None = None) -> Event | None:
    try:
        occurred = parse_ts(raw["occurred_at"])
        sent = parse_ts(raw["sent_at"])
        received = parse_ts(raw["received_at"])
        deliver = parse_ts(raw.get("deliver_at"))
    except (KeyError, ValueError, TypeError) as exc:
        if quarantine is not None:
            quarantine.add(source, 0, f"bad timestamp: {exc}", str(raw.get("event_id")))
        return None
    if received is None or occurred is None or sent is None:
        if quarantine is not None:
            quarantine.add(source, 0, "missing timestamp", str(raw.get("event_id")))
        return None
    anomalies: list[str] = []
    buffer_delay = (sent - occurred).total_seconds()
    if buffer_delay < 0:
        anomalies.append("sent_before_occurred")
        buffer_delay = 0.0
    skew = (received - sent).total_seconds()
    if abs(skew) > clock_skew_flag_s:
        anomalies.append("phone_clock_wrong")
    raw_dialect = raw.get("app_dialect")
    if raw_dialect is not None and normalize_dialect(raw_dialect) is None:
        anomalies.append("invalid_app_dialect")
    props = raw.get("properties") or {}
    if not isinstance(props, dict):
        anomalies.append("properties_not_object")
        props = {}
    return Event(
        event_id=str(raw["event_id"]), user_id=str(raw["user_id"]), event=str(raw["event"]),
        occurred_at=occurred, sent_at=sent, received_at=received, session_id=raw.get("session_id"),
        app_dialect_raw=raw_dialect, app_language=raw.get("app_language"), platform=raw.get("platform"),
        properties=props, source=source, deliver_at=deliver, buffer_delay_s=buffer_delay,
        est_occurred_at=received - timedelta(seconds=buffer_delay), clock_skew_s=skew,
        anomalies=tuple(anomalies),
    )


def dedupe_events(events: Iterable[Event]) -> tuple[list[Event], dict[str, int]]:
    """Keep the first delivery (earliest received_at) of each event_id; count redeliveries."""
    best: dict[str, Event] = {}
    dups: dict[str, int] = {}
    for e in events:
        cur = best.get(e.event_id)
        if cur is None:
            best[e.event_id] = e
            continue
        dups[e.event_id] = dups.get(e.event_id, 0) + 1
        if (e.received_at, e.source) < (cur.received_at, cur.source):
            best[e.event_id] = e
    out = sorted(best.values(), key=lambda x: (x.received_at, x.event_id))
    return out, dups


def parse_title(row: dict[str, str]) -> Title:
    rd = row.get("release_date") or ""
    desc = row.get("description") or ""
    reason = scan_for_instructions(desc) or scan_for_instructions(row.get("title"))
    return Title(
        title_id=row["title_id"], content_slug=row["content_slug"], name=row["title"], dialect=row["dialect"],
        language=row["language"], content_type=row["content_type"],
        genres=tuple(g for g in (row.get("genres") or "").split("|") if g),
        release_date=date.fromisoformat(rd) if rd else None,
        available_from=parse_ts(row.get("available_from")), available_until=parse_ts(row.get("available_until")),
        is_premium=(row.get("is_premium") or "").lower() == "true", description=desc,
        suspicious=reason is not None, suspicious_reason=reason,
    )
