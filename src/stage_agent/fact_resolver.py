"""Assemble what is known about one viewer, with provenance, freshness and explicit precedence rules.

Precedence (documented in ARCHITECTURE.md, "Fact precedence"):
  dialect        current app setting on the trigger > (never) profile / campaign link; conflicts kept visible.
  signup_at      signup_completed event > profile.signup_at.
  subscription   subscription events newer than the profile row > newest profile row > lifetime-observed history.
  consent        any notification_opt_out for the channel blocks; else newest profile row; missing profile = no consent.
  watch progress play/episode events only (the profile has none).
Duplicate profile rows: the row with the newest updated_at is used; the others are recorded as conflicts.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .config import AgentConfig
from .content_selector import Catalogue
from .models import Event, Fact
from .normalization import normalize_dialect, parse_campaign_deeplink
from .scheduler import ist_day, iso, parse_ts

PAID_EVENTS = ("purchase_completed", "subscription_renewed", "subscription_lapsed")


def freshness(now: datetime, t: datetime | None, stale_days: int) -> tuple[str, float | None]:
    if t is None:
        return "unknown", None
    age = (now - t).total_seconds()
    if age < 86400:
        return "fresh", age
    if age < stale_days * 86400:
        return "aging", age
    return "stale", age


@dataclass
class SeriesProgress:
    slug: str
    content_type: str
    furthest: tuple[int, int]
    last_episode: tuple[int, int]
    last_play: datetime
    evidence: list[str]

    @property
    def has_unwatched(self) -> bool:
        return self.furthest < self.last_episode


@dataclass
class ViewerFacts:
    user_id: str
    now: datetime
    facts: dict[str, Fact] = field(default_factory=dict)
    profile_present: bool = False
    profile_ref: str | None = None
    history_start: datetime | None = None
    covers_lifetime: bool = False
    events_seen: int = 0
    plays: list[Event] = field(default_factory=list)
    completions: list[Event] = field(default_factory=list)
    app_opens_before: list[Event] = field(default_factory=list)
    series: dict[str, SeriesProgress] = field(default_factory=dict)
    watched_slugs: set[str] = field(default_factory=set)
    top_genres: list[str] = field(default_factory=list)
    play_days_recent: set[date] = field(default_factory=set)
    message_days: dict[date, list[str]] = field(default_factory=dict)
    sub_events: list[Event] = field(default_factory=list)
    signup_event: Event | None = None
    learning: dict[str, Any] = field(default_factory=dict)

    def get(self, name: str) -> Any:
        f = self.facts.get(name)
        return f.value if f else None

    def fact(self, name: str) -> Fact | None:
        return self.facts.get(name)

    def conflicts(self) -> list[dict[str, Any]]:
        out = []
        for f in self.facts.values():
            for c in f.conflicts:
                out.append({"fact": f.name, "used": f.to_dict()["value"], "used_source": f.source, **c})
        return out

    def unfinished_series(self, idle_days: int) -> list[SeriesProgress]:
        cutoff = self.now - timedelta(days=idle_days)
        out = [s for s in self.series.values()
               if s.content_type in ("show", "micro_drama") and s.has_unwatched and s.last_play <= cutoff]
        return sorted(out, key=lambda s: s.last_play, reverse=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id, "as_of": iso(self.now), "profile_present": self.profile_present,
            "history_start": iso(self.history_start), "history_covers_lifetime": self.covers_lifetime,
            "events_seen": self.events_seen, "facts": {k: v.to_dict() for k, v in sorted(self.facts.items())},
            "watch": {
                "plays": len(self.plays), "completions": len(self.completions),
                "watched_slugs": sorted(self.watched_slugs)[:50], "top_genres": self.top_genres[:5],
                "play_days_recent": sorted(d.isoformat() for d in self.play_days_recent),
                "series": [{"slug": s.slug, "furthest": list(s.furthest), "last_episode": list(s.last_episode),
                            "last_play": iso(s.last_play)} for s in self.series.values()],
            },
            "learning": self.learning,
        }


class FactResolver:
    def __init__(self, cfg: AgentConfig, catalogue: Catalogue, plans: dict[str, dict[str, Any]]):
        self.cfg = cfg
        self.cat = catalogue
        self.plans = plans

    def resolve(self, user_id: str, now: datetime, trigger: Event | None, events: list[Event],
                profile_rows: list[dict[str, str]], history_start: datetime | None,
                learning: dict[str, Any] | None = None) -> ViewerFacts:
        vf = ViewerFacts(user_id=user_id, now=now, history_start=history_start, learning=learning or {})
        vf.events_seen = len(events)
        stale_days = self.cfg.profile_stale_days

        # ---------------- profile rows: newest updated_at wins, others become conflicts
        rows = sorted(profile_rows, key=lambda r: (r.get("updated_at") or "", r.get("snapshot_at") or ""), reverse=True)
        prof = rows[0] if rows else None
        others = rows[1:]
        prof_time = parse_ts(prof.get("updated_at")) if prof else None
        vf.profile_present = prof is not None
        vf.profile_ref = f"profile:{user_id}@{prof['updated_at']}" if prof else None
        fr, age = freshness(now, prof_time, stale_days)
        vf.facts["profile_present"] = Fact(
            "profile_present", prof is not None, "profile" if prof else "unknown", vf.profile_ref, prof_time,
            "newest updated_at among snapshot rows" + (f"; {len(others)} older row(s) kept as conflicts" if others else ""),
            fr, age, [{"value": True, "source": "profile", "source_ref": f"profile:{user_id}@{o.get('updated_at')}",
                       "note": "older duplicate row"} for o in others])

        # ---------------- scan events once
        signup_events, optouts = [], defaultdict(list)
        for e in events:
            if trigger is not None and e.event_id == trigger.event_id:
                continue
            if e.event == "signup_completed":
                signup_events.append(e)
            elif e.event in PAID_EVENTS:
                vf.sub_events.append(e)
            elif e.event == "notification_opt_out":
                optouts[str(e.properties.get("channel"))].append(e)
            elif e.event == "play_started":
                vf.plays.append(e)
            elif e.event == "episode_completed":
                vf.completions.append(e)
            elif e.event == "app_open":
                if trigger is None or (e.session_id != trigger.session_id and e.time < trigger.time):
                    vf.app_opens_before.append(e)
            elif e.event == "message_delivered" and e.properties.get("channel") in ("push", "whatsapp"):
                vf.message_days.setdefault(ist_day(e.time), []).append(e.ref())
            if e.event in ("play_started", "play_progress", "episode_completed"):
                self._track_progress(vf, e)

        # ---------------- dialect
        self._resolve_dialect(vf, trigger, prof, events, now)

        # ---------------- signup
        vf.signup_event = min(signup_events, key=lambda e: e.time) if signup_events else None
        prof_signup = parse_ts(prof.get("signup_at")) if prof and prof.get("signup_at") else None
        if vf.signup_event is not None:
            conf = []
            if prof_signup and abs((prof_signup - vf.signup_event.time).total_seconds()) > 3600:
                conf.append({"value": iso(prof_signup), "source": "profile", "source_ref": vf.profile_ref,
                             "resolution": "signup event is first-hand and server-anchored"})
            f, a = freshness(now, vf.signup_event.time, stale_days)
            vf.facts["signup_at"] = Fact("signup_at", vf.signup_event.time, "event", vf.signup_event.ref(),
                                         vf.signup_event.time, "signup_completed event > profile.signup_at", f, a, conf)
        elif prof_signup is not None:
            f, a = freshness(now, prof_time, stale_days)
            vf.facts["signup_at"] = Fact("signup_at", prof_signup, "profile", vf.profile_ref, prof_time,
                                         "profile.signup_at (no signup event observed)", f, a)
        else:
            vf.facts["signup_at"] = Fact("signup_at", None, "unknown", None, None,
                                         "no profile row and no signup_completed event", supported=False)
        signup = vf.get("signup_at")
        vf.covers_lifetime = bool(signup and history_start and signup >= history_start)
        vf.facts["history_covers_lifetime"] = Fact(
            "history_covers_lifetime", vf.covers_lifetime, "derived", vf.facts["signup_at"].source_ref,
            vf.facts["signup_at"].source_time,
            f"signup_at >= first observable event ({iso(history_start)}); 'never did X' claims need full coverage")

        # ---------------- subscription
        self._resolve_subscription(vf, prof, others, prof_time, now)

        # ---------------- consent
        for name, field_name, channel in (("push_consent", "notifications_opt_in", "push"),
                                          ("whatsapp_consent", "whatsapp_opt_in", "whatsapp")):
            self._resolve_consent(vf, name, field_name, channel, prof, others, prof_time, optouts.get(channel, []), now)
        if vf.learning.get("push_token_invalid"):
            vf.facts["push_deliverable"] = Fact("push_deliverable", False, "derived", vf.learning.get("push_token_ref"),
                                                parse_ts(vf.learning.get("push_token_at")),
                                                "learned from an earlier message_failed(token_invalid)")

        # ---------------- engagement summaries
        gc: Counter[str] = Counter()
        for e in vf.plays:
            t = self.cat.by_id.get(e.properties.get("title_id"))
            if t:
                vf.watched_slugs.add(t.content_slug)
                gc.update(t.genres)
        vf.top_genres = [g for g, _ in sorted(gc.items(), key=lambda kv: (-kv[1], kv[0]))]
        lo = now - timedelta(days=self.cfg.habit_lookback_days)
        vf.play_days_recent = {ist_day(e.time) for e in vf.plays if lo <= e.time < now}
        last_open = max(vf.app_opens_before, key=lambda e: e.time) if vf.app_opens_before else None
        f, a = freshness(now, last_open.time if last_open else None, stale_days)
        vf.facts["last_app_open_before_trigger"] = Fact(
            "last_app_open_before_trigger", last_open.time if last_open else None,
            "event" if last_open else "derived", last_open.ref() if last_open else None,
            last_open.time if last_open else None,
            "latest app_open in another session, ordered by server-anchored occurrence time", f, a)
        return vf

    # ------------------------------------------------------------------ helpers
    def _track_progress(self, vf: ViewerFacts, e: Event) -> None:
        ep = self.cat.episode_index.get(str(e.properties.get("episode_id")))
        if ep is None:
            return
        slug, s, n = ep
        titles = self.cat.by_slug.get(slug) or []
        if not titles:
            return
        eps = self.cat.episodes.get(slug) or []
        last = (eps[-1][0], eps[-1][1]) if eps else (s, n)
        cur = vf.series.get(slug)
        if cur is None:
            vf.series[slug] = SeriesProgress(slug, titles[0].content_type, (s, n), last, e.time, [e.ref()])
        else:
            if (s, n) > cur.furthest:
                cur.furthest = (s, n)
            if e.time > cur.last_play:
                cur.last_play = e.time
            if len(cur.evidence) < 5:
                cur.evidence.append(e.ref())

    def _resolve_dialect(self, vf: ViewerFacts, trigger: Event | None, prof: dict[str, str] | None,
                         events: list[Event], now: datetime) -> None:
        alternatives = []
        if prof and prof.get("preferred_dialect"):
            alternatives.append({"value": prof["preferred_dialect"], "source": "profile", "source_ref": vf.profile_ref})
        link = parse_campaign_deeplink((trigger.properties if trigger else {}).get("deeplink"))
        if link:
            alternatives.append({"value": link["dialect"], "source": "campaign_link",
                                 "source_ref": trigger.ref() if trigger else None, "campaign": link["campaign"]})
        cur = normalize_dialect(trigger.app_dialect_raw) if trigger else None
        if cur is not None:
            conflicts = [a | {"resolution": "app setting decides which page the app requests"}
                         for a in alternatives if a["value"] != cur]
            in_cat = cur in self.cat.dialects
            vf.facts["dialect"] = Fact(
                "dialect", cur, "app_setting", trigger.ref(), trigger.received_at,
                "current app_dialect on the trigger (the app shows the page of its current setting)"
                + ("" if in_cat else f"; {cur} has no titles in the catalogue"),
                "fresh", (now - trigger.received_at).total_seconds(), conflicts, supported=in_cat)
            return
        prior = None
        for e in reversed(events):
            d = normalize_dialect(e.app_dialect_raw)
            if d and (trigger is None or e.event_id != trigger.event_id):
                prior = (d, e)
                break
        if prior:
            alternatives.append({"value": prior[0], "source": "app_setting_earlier", "source_ref": prior[1].ref()})
        raw = trigger.app_dialect_raw if trigger else None
        vf.facts["dialect"] = Fact(
            "dialect", None, "unknown", trigger.ref() if trigger else None, trigger.received_at if trigger else None,
            f"trigger carries no valid app_dialect (raw={raw!r}); the page the app will request is unknown, "
            "so no alternative is substituted", "fresh" if trigger else "unknown", None,
            [a | {"resolution": "not used: cannot confirm it is the current app setting"} for a in alternatives],
            supported=False)

    def _resolve_subscription(self, vf: ViewerFacts, prof: dict[str, str] | None, others: list[dict[str, str]],
                              prof_time: datetime | None, now: datetime) -> None:
        stale_days = self.cfg.profile_stale_days
        conflicts: list[dict[str, Any]] = []
        for o in others:
            if o.get("subscription_status") != (prof or {}).get("subscription_status"):
                conflicts.append({"value": o.get("subscription_status"), "source": "profile",
                                  "source_ref": f"profile:{vf.user_id}@{o.get('updated_at')}",
                                  "resolution": "older duplicate row; newest updated_at wins"})
        ever_paid_refs = [e.ref() for e in vf.sub_events
                          if e.event == "subscription_renewed"
                          or (e.event == "purchase_completed" and not e.properties.get("is_trial"))]
        ever_paid_refs += [f"profile:{vf.user_id}@{r.get('updated_at')}" for r in ([prof] if prof else []) + others
                           if r.get("subscription_status") in ("active", "lapsed")]
        latest = max(vf.sub_events, key=lambda e: e.time) if vf.sub_events else None

        def status_of(e: Event) -> str:
            if e.event == "subscription_lapsed":
                return "lapsed"
            if e.event == "purchase_completed" and e.properties.get("is_trial"):
                return "trial_active"
            return "active"

        if latest is not None and (prof_time is None or latest.time > prof_time):
            value, source, ref, t = status_of(latest), "event", latest.ref(), latest.time
            rule = "subscription event newer than the profile row overrides it"
            if prof:
                conflicts.append({"value": prof.get("subscription_status"), "source": "profile",
                                  "source_ref": vf.profile_ref, "resolution": "older than " + latest.event})
        elif prof is not None:
            value, source, ref, t = prof.get("subscription_status"), "profile", vf.profile_ref, prof_time
            rule = "newest profile row (no newer subscription event)"
        elif vf.covers_lifetime:
            value, source, ref, t = "never", "derived", vf.facts["signup_at"].source_ref, vf.facts["signup_at"].source_time
            rule = "no profile; full lifetime observed since signup with no purchase"
        else:
            vf.facts["subscription_status"] = Fact("subscription_status", None, "unknown", None, None,
                                                   "no profile row and lifetime not observable", supported=False)
            vf.facts["ever_paid"] = Fact("ever_paid", None, "unknown", None, None, "not observable", supported=False)
            return
        supported = True
        if value == "never" and ever_paid_refs:
            supported = False
            conflicts.append({"value": "paid before", "source": "event/profile", "source_ref": ever_paid_refs[0],
                              "resolution": "unresolved: 'never paid' contradicted; status treated as unsupported"})
        f, a = freshness(now, t, stale_days)
        vf.facts["subscription_status"] = Fact("subscription_status", value, source, ref, t, rule, f, a, conflicts,
                                               supported=supported)
        ever_known = bool(ever_paid_refs) or vf.covers_lifetime or value in ("active", "lapsed", "trial_active")
        vf.facts["ever_paid"] = Fact(
            "ever_paid", bool(ever_paid_refs) or value in ("active", "lapsed"),
            "event" if ever_paid_refs and ever_paid_refs[0].startswith("event:") else source,
            ever_paid_refs[0] if ever_paid_refs else ref, t,
            "any paid purchase/renewal event or a paid status on any profile row", f, a, supported=ever_known or value == "never")
        if value == "lapsed":
            lapse_e = max((e for e in vf.sub_events if e.event == "subscription_lapsed"), key=lambda e: e.time, default=None)
            ends = parse_ts(prof.get("plan_ends_at")) if prof and prof.get("plan_ends_at") else None
            if lapse_e is not None:
                vf.facts["lapsed_at"] = Fact("lapsed_at", lapse_e.time, "event", lapse_e.ref(), lapse_e.time,
                                             "subscription_lapsed event")
            elif ends is not None:
                vf.facts["lapsed_at"] = Fact("lapsed_at", ends, "profile", vf.profile_ref, prof_time,
                                             "profile.plan_ends_at")
        if value == "trial_active":
            ends = parse_ts(prof.get("plan_ends_at")) if prof and prof.get("plan_ends_at") and source == "profile" else None
            if ends is None and source == "event" and latest is not None:
                days = int(self.plans.get("P_TRIAL", {}).get("days", 0)) or None
                ends = latest.time + timedelta(days=days) if days else None
            if ends is not None:
                vf.facts["trial_ends_at"] = Fact("trial_ends_at", ends, source, ref, t,
                                                 "profile.plan_ends_at or trial purchase + P_TRIAL days")

    def _resolve_consent(self, vf: ViewerFacts, name: str, field_name: str, channel: str,
                         prof: dict[str, str] | None, others: list[dict[str, str]], prof_time: datetime | None,
                         optouts: list[Event], now: datetime) -> None:
        stale_days = self.cfg.profile_stale_days
        conflicts = [{"value": o.get(field_name) == "true", "source": "profile",
                      "source_ref": f"profile:{vf.user_id}@{o.get('updated_at')}",
                      "resolution": "older duplicate row; newest updated_at wins"}
                     for o in others if prof and o.get(field_name) != prof.get(field_name)]
        if optouts:
            last = max(optouts, key=lambda e: e.time)
            if prof and prof.get(field_name) == "true":
                conflicts.append({"value": True, "source": "profile", "source_ref": vf.profile_ref,
                                  "resolution": "opt-out event overrides positive snapshot consent"
                                  + ("" if prof_time is None or last.time > prof_time else
                                     " (opt-out predates the row; re-consent is not inferred)")})
            f, a = freshness(now, last.time, stale_days)
            vf.facts[name] = Fact(name, False, "event", last.ref(), last.time,
                                  f"notification_opt_out(channel={channel}) blocks the channel", f, a, conflicts)
            return
        if prof is None:
            vf.facts[name] = Fact(name, False, "unknown", None, None,
                                  "no profile row: consent is never inferred (e.g. from a phone number)")
            return
        f, a = freshness(now, prof_time, stale_days)
        vf.facts[name] = Fact(name, prof.get(field_name) == "true", "profile", vf.profile_ref, prof_time,
                              f"newest profile row {field_name}", f, a, conflicts)
