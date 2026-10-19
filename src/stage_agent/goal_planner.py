"""Goal rules, eligibility, selection and success predicates.

Interpretations of underspecified rules (also in ARCHITECTURE.md / KNOWN_LIMITATIONS.md):
  * G10 meaningful_engagement_7d: success event is null -> unmeasurable, never selected.
  * G11 tv_activation_14d: success event 'tv_app_open' never appears in the event stream -> never selected.
  * G06 reactivate_lapsed_14d: "recently lapsed" has no threshold -> lapsed within cfg.recent_lapse_days (30, one
    billing cycle and the span of observable history) with the lapse time evidenced by an event or plan_ends_at.
  * G02 first_full_episode_3d: "has never completed an episode" is only supported when the observable history
    covers the user's whole lifetime (signup on/after the first observable event); otherwise unsupported.
  * "any paid plan" (G06) is read as purchase_completed with is_trial = false, consistent with G04/G05.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from .config import AgentConfig
from .content_selector import Catalogue
from .fact_resolver import ViewerFacts
from .models import GoalRule
from .scheduler import ist_day, iso, parse_ts


class GoalRuleBook:
    def __init__(self, goals_cfg: dict[str, Any]):
        self.config_version = goals_cfg.get("version", "unknown")
        self.rules: dict[str, GoalRule] = {}
        self.corrections: list[dict[str, Any]] = []
        for g in goals_cfg["goals"]:
            s = g.get("success") or {}
            self.rules[g["goal_id"]] = GoalRule(
                goal_id=g["goal_id"], name=g["name"], revision=1, eligibility=g.get("eligibility") or {},
                success_event=s.get("event"), success_condition=s.get("condition") or "",
                window_hours=int(s.get("window_hours") or 0), source=f"config/goals.json@{self.config_version}")

    def current(self, goal_id: str) -> GoalRule:
        return self.rules[goal_id]

    def apply_correction(self, control: dict[str, Any], at: datetime) -> GoalRule:
        gid = control["goal_id"]
        old = self.rules[gid]
        new = control["new_success"]
        rule = GoalRule(goal_id=gid, name=old.name, revision=old.revision + 1, eligibility=old.eligibility,
                        success_event=new.get("event"), success_condition=new.get("condition") or "",
                        window_hours=int(new.get("window_hours") or old.window_hours),
                        source=f"control:goal_rule_corrected@{iso(at)}")
        self.rules[gid] = rule
        self.corrections.append({"goal_id": gid, "from": old.version, "to": rule.version, "at": iso(at),
                                 "applies_to": control.get("applies_to"), "note": control.get("note")})
        return rule


@dataclass
class Eligibility:
    goal_id: str
    eligible: bool
    reason: str
    evidence: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    measurable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"goal_id": self.goal_id, "eligible": self.eligible, "reason": self.reason,
                "evidence": self.evidence, "context": self.context, "measurable": self.measurable}


def _ref(vf: ViewerFacts, name: str) -> list[str]:
    f = vf.fact(name)
    return [f.source_ref] if f and f.source_ref else []


class GoalPlanner:
    def __init__(self, cfg: AgentConfig, rulebook: GoalRuleBook, catalogue: Catalogue):
        self.cfg = cfg
        self.book = rulebook
        self.cat = catalogue
        self._checks: dict[str, Callable[[ViewerFacts, datetime], Eligibility]] = {
            "G01": self._g01, "G02": self._g02, "G03": self._g03, "G04": self._g04, "G05": self._g05,
            "G06": self._g06, "G07": self._g07, "G08": self._g08, "G09": self._g09, "G12": self._g12,
        }

    # ------------------------------------------------------------------ evaluation
    def evaluate(self, vf: ViewerFacts, now: datetime, dialect: str | None) -> list[Eligibility]:
        out = []
        for gid in sorted(self.book.rules):
            rule = self.book.current(gid)
            if not rule.measurable:
                out.append(Eligibility(gid, False, f"not measurable: {rule.unmeasurable_reason}",
                                       [f"goal_rule:{rule.version}"], measurable=False))
                continue
            fn = self._checks.get(gid)
            if fn is None:
                out.append(Eligibility(gid, False, "no eligibility implementation for this goal",
                                       [f"goal_rule:{rule.version}"]))
                continue
            el = fn(vf, now)
            el.evidence = [f"goal_rule:{rule.version}"] + el.evidence
            if gid == "G07" and el.eligible and dialect is not None:
                slug = el.context.get("series_slug")
                if not any(t.dialect == dialect for t in self.cat.by_slug.get(slug, [])):
                    el.eligible = False
                    el.reason += f"; but the series is not offered in the app dialect {dialect}, so it cannot be promoted"
            out.append(el)
        return out

    def rank(self, eligible: list[Eligibility]) -> list[Eligibility]:
        order = {g: i for i, g in enumerate(self.cfg.goal_priority)}
        return sorted(eligible, key=lambda e: (order.get(e.goal_id, 999), e.goal_id))

    def new_instance(self, user_id: str, el: Eligibility, decision_id: str, started_at: datetime,
                     arm: str) -> dict[str, Any]:
        rule = self.book.current(el.goal_id)
        iid = "GI-" + hashlib.sha256(f"{user_id}|{decision_id}|{el.goal_id}".encode()).hexdigest()[:12]
        return {
            "instance_id": iid, "user_id": user_id, "goal_id": el.goal_id, "rule_version": rule.version,
            "started_at": iso(started_at), "deadline": iso(started_at + timedelta(hours=rule.window_hours)),
            "success_event": rule.success_event, "success_condition": rule.success_condition,
            "decision_id": decision_id, "arm": arm,
            "eligibility": {"reason": el.reason, "evidence": el.evidence, "context": el.context},
            "history": [{"at": iso(started_at), "change": "opened", "reason": el.reason}],
        }

    # ------------------------------------------------------------------ per-goal eligibility
    def _signup_within(self, vf: ViewerFacts, now: datetime, days: int) -> tuple[bool | None, str]:
        f = vf.fact("signup_at")
        if f is None or f.value is None:
            return None, "signup time unknown"
        age = now - f.value
        return age <= timedelta(days=days), f"signed up {age.total_seconds() / 86400:.1f} days ago ({f.source})"

    def _g01(self, vf: ViewerFacts, now: datetime) -> Eligibility:
        ok, why = self._signup_within(vf, now, int(self.book.current("G01").eligibility.get("signed_up_within_days", 7)))
        if ok is None:
            return Eligibility("G01", False, f"unsupported: {why}")
        if not ok:
            return Eligibility("G01", False, why, _ref(vf, "signup_at"))
        if vf.plays:
            return Eligibility("G01", False, f"{why}, but has started a play", [vf.plays[0].ref()])
        return Eligibility("G01", True, f"{why} and no play_started since signup ({vf.events_seen} events checked)",
                           _ref(vf, "signup_at"))

    def _g02(self, vf: ViewerFacts, now: datetime) -> Eligibility:
        if vf.completions:
            return Eligibility("G02", False, "has completed an episode", [vf.completions[0].ref()])
        if not vf.covers_lifetime:
            return Eligibility("G02", False, "unsupported: observable history does not cover the user's lifetime, "
                               "so 'never completed' cannot be evidenced", _ref(vf, "signup_at"))
        refs = _ref(vf, "signup_at") + ([vf.plays[-1].ref()] if vf.plays else [])
        return Eligibility("G02", True, f"no episode_completed in full lifetime history ({len(vf.plays)} plays)", refs)

    def _g03(self, vf: ViewerFacts, now: datetime) -> Eligibility:
        ok, why = self._signup_within(vf, now, int(self.book.current("G03").eligibility.get("signed_up_within_days", 2)))
        if ok is None:
            return Eligibility("G03", False, f"unsupported: {why}")
        return Eligibility("G03", bool(ok), why, _ref(vf, "signup_at"))

    def _status_goal(self, gid: str, vf: ViewerFacts) -> Eligibility:
        f = vf.fact("subscription_status")
        want = self.book.current(gid).eligibility.get("subscription_status") or []
        if f is None or f.value is None:
            return Eligibility(gid, False, "unsupported: subscription status unknown")
        if not f.supported:
            return Eligibility(gid, False, f"unsupported: subscription status '{f.value}' has unresolved conflicts",
                               _ref(vf, "subscription_status"))
        ok = f.value in want
        return Eligibility(gid, ok, f"subscription_status={f.value} ({f.source}, {f.freshness})",
                           _ref(vf, "subscription_status"))

    def _g04(self, vf: ViewerFacts, now: datetime) -> Eligibility:
        el = self._status_goal("G04", vf)
        if el.eligible and vf.fact("trial_ends_at") is not None:
            el.context["trial_ends_at"] = iso(vf.get("trial_ends_at"))
            el.evidence += _ref(vf, "trial_ends_at")
        return el

    def _g05(self, vf: ViewerFacts, now: datetime) -> Eligibility:
        el = self._status_goal("G05", vf)
        if el.eligible and vf.get("ever_paid"):
            return Eligibility("G05", False, "status says never, but a paid plan is evidenced", _ref(vf, "ever_paid"))
        return el

    def _g06(self, vf: ViewerFacts, now: datetime) -> Eligibility:
        el = self._status_goal("G06", vf)
        if not el.eligible:
            return el
        f = vf.fact("lapsed_at")
        if f is None or f.value is None:
            return Eligibility("G06", False, "unsupported: lapsed, but the lapse time is not evidenced", el.evidence)
        days = (now - f.value).total_seconds() / 86400
        if days > self.cfg.recent_lapse_days:
            return Eligibility("G06", False, f"lapsed {days:.0f} days ago (> {self.cfg.recent_lapse_days}-day "
                               "interpretation of 'recently')", el.evidence + _ref(vf, "lapsed_at"))
        return Eligibility("G06", True, f"lapsed {days:.1f} days ago (<= {self.cfg.recent_lapse_days} days)",
                           el.evidence + _ref(vf, "lapsed_at"))

    def _g07(self, vf: ViewerFacts, now: datetime) -> Eligibility:
        series = vf.unfinished_series(self.cfg.resume_idle_days)
        if not series:
            return Eligibility("G07", False, f"no show/micro-drama with unwatched episodes idle >= "
                               f"{self.cfg.resume_idle_days} days")
        s = series[0]
        idle = (now - s.last_play).total_seconds() / 86400
        return Eligibility("G07", True, f"series {s.slug} reached S{s.furthest[0]}E{s.furthest[1]} of "
                           f"S{s.last_episode[0]}E{s.last_episode[1]}, idle {idle:.1f} days", s.evidence[-3:],
                           {"series_slug": s.slug, "furthest": list(s.furthest)})

    def _g08(self, vf: ViewerFacts, now: datetime) -> Eligibility:
        return self._status_goal("G08", vf)

    def _g09(self, vf: ViewerFacts, now: datetime) -> Eligibility:
        el = self._status_goal("G09", vf)
        if not el.eligible:
            return el
        n = len(vf.play_days_recent)
        if n > 1:
            return Eligibility("G09", False, f"active, but played on {n} IST days in the last "
                               f"{self.cfg.habit_lookback_days}", el.evidence)
        return Eligibility("G09", True, f"active subscriber with plays on {n} IST day(s) in the last "
                           f"{self.cfg.habit_lookback_days} days", el.evidence)

    def _g12(self, vf: ViewerFacts, now: datetime) -> Eligibility:
        signup = vf.get("signup_at")
        lo = now - timedelta(days=self.cfg.absence_days)
        if vf.history_start is None or vf.history_start > lo:
            return Eligibility("G12", False, "unsupported: observable history shorter than the absence window")
        recent = [e for e in vf.app_opens_before if e.time >= lo]
        if recent:
            return Eligibility("G12", False, f"app_open within the last {self.cfg.absence_days} days",
                               [recent[-1].ref()])
        if signup is None and vf.events_seen == 0:
            return Eligibility("G12", False, "unsupported: no evidence the user existed before the window")
        if signup is not None and signup > lo:
            return Eligibility("G12", False, f"signed up within the last {self.cfg.absence_days} days, "
                               "so not a returning user", _ref(vf, "signup_at"))
        last = vf.fact("last_app_open_before_trigger")
        refs = _ref(vf, "last_app_open_before_trigger") or _ref(vf, "signup_at")
        when = iso(last.value) if last and last.value else "none in observable history"
        return Eligibility("G12", True, f"no app_open in the {self.cfg.absence_days} days before the trigger "
                           f"(last: {when})", refs)


# ---------------------------------------------------------------------- success predicates
def judge_success(goal: dict[str, Any], events: list[dict[str, Any]], catalogue: Catalogue,
                  time_of: Callable[[dict[str, Any]], datetime]) -> tuple[dict[str, Any] | None, list[str], str | None]:
    """Find the event that satisfies a goal's success predicate inside [started_at, deadline].

    Returns (success_event, supporting_refs, unsupported_reason). `events` must be sorted by time.
    """
    start, deadline = parse_ts(goal["started_at"]), parse_ts(goal["deadline"])
    ev_name, cond = goal.get("success_event"), (goal.get("success_condition") or "").strip().lower()
    if ev_name is None:
        return None, [], "goal has no success event"
    window = [e for e in events if e.get("event") == ev_name and start <= time_of(e) <= deadline]
    ctx = (goal.get("eligibility") or {}).get("context") or {}

    def title(e):
        return catalogue.by_id.get((e.get("properties") or {}).get("title_id"))

    if cond in ("any title", "any episode", "any", ""):
        hits = window
    elif cond in ("is_trial = false", "any paid plan"):
        hits = [e for e in window if (e.get("properties") or {}).get("is_trial") is False]
    elif cond.startswith("on a later ist calendar day"):
        hits = [e for e in window if ist_day(time_of(e)) > ist_day(start)]
    elif cond == "same content_slug as the unfinished series":
        slug = ctx.get("series_slug")
        hits = [e for e in window if (t := title(e)) is not None and t.content_slug == slug]
    elif cond.startswith("a title released in the 30 days before the goal start"):
        lo = (start - timedelta(days=30)).date()
        hits = [e for e in window if (t := title(e)) is not None and t.release_date is not None
                and lo <= t.release_date <= start.date()]
    elif cond == "on 3 distinct ist days":
        days, hits = set(), []
        for e in window:
            d = ist_day(time_of(e))
            if d not in days:
                days.add(d)
                hits.append(e)
            if len(days) >= 3:
                return e, [f"event:{h['event_id']}" for h in hits], None
        return None, [], None
    else:
        return None, [], f"unrecognised success condition {cond!r}"
    if hits:
        return hits[0], [f"event:{hits[0]['event_id']}"], None
    return None, [], None
