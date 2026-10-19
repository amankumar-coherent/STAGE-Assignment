"""Independent verifier: the safety boundary between a proposed decision and publication.

It deliberately does NOT reuse the planner's conclusions (FactResolver output, Eligibility flags, the Catalogue's
availability cache). It re-reads raw profile rows, raw events, the arms file, catalogue rows, the withdrawals
table, templates and pricing, and re-derives what it needs with its own, simpler and more conservative logic.
A failed critical check blocks the subject it applies to; a failed decision-level critical check blocks
everything. Any exception inside the verifier fails closed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .config import AgentConfig, PackConfig
from .content_selector import Catalogue
from .models import Check, Event, OBSERVABLE_EVENTS
from .normalization import TITLE_DEEPLINK_RE, normalize_dialect
from .scheduler import QuietHours, ist_clock, ist_day, iso, parse_ts
from .state_store import StateStore

PRICE_RE = re.compile(r"(?:₹|\bRs\.?\s?|\bINR\s?)(\d+(?:\.\d+)?)", re.I)
MESSAGE_TYPES = ("push", "whatsapp")


class ArmRegistry:
    """Randomized assignment, read-only and separate from planning."""

    def __init__(self, arms: dict[str, str]):
        self._arms = dict(arms)

    def arm(self, user_id: str) -> str:
        # Unknown users are treated as holdout: never act on someone outside the randomized population.
        return self._arms.get(user_id, "holdout")

    def __len__(self) -> int:
        return len(self._arms)

    def items(self):
        return self._arms.items()


@dataclass
class VerifyInput:
    user_id: str
    trigger: Event
    now: datetime
    publish_at: datetime
    goal: dict[str, Any] | None
    goal_is_new: bool
    deterministic_eligible: list[str]
    goal_source: str  # deterministic | model
    model_status: str | None
    actions: list[dict[str, Any]]
    trigger_stale: bool


@dataclass
class VerifyResult:
    checks: list[Check] = field(default_factory=list)
    blocked: set[str] = field(default_factory=set)  # subjects: decision or action index "a0", "a1", ...
    error: str | None = None

    @property
    def decision_blocked(self) -> bool:
        return "decision" in self.blocked or self.error is not None

    def passed(self) -> list[str]:
        return [f"{c.subject}:{c.name}" for c in self.checks if c.passed]

    def failed(self) -> list[dict[str, Any]]:
        return [{"check": f"{c.subject}:{c.name}", "severity": c.severity, "reason": c.reason, "evidence": c.evidence}
                for c in self.checks if not c.passed]


class Verifier:
    def __init__(self, cfg: AgentConfig, pack: PackConfig, catalogue: Catalogue, store: StateStore,
                 arms: ArmRegistry, rulebook, history_start: datetime | None):
        self.cfg = cfg
        self.pack = pack
        self.titles = catalogue.by_id  # raw rows only; availability is recomputed here
        self.store = store
        self.arms = arms
        self.book = rulebook
        self.history_start = history_start
        q = pack.interventions["push"]["quiet_hours_ist"]
        self.quiet = QuietHours(q[0], q[1])
        self.ttl = timedelta(minutes=pack.page_ttl_minutes)
        self.prices = {float(p["price_inr"]) for p in pack.pricing["plans"]}
        self.templates = pack.approved_templates
        self.copy = pack.interventions["push"]["copy"]
        self.rows_cfg = pack.interventions["home_page"]["rows"]
        self._withdrawn: dict[str, datetime] | None = None

    # ------------------------------------------------------------------ public
    def verify(self, vi: VerifyInput) -> VerifyResult:
        res = VerifyResult()
        self._withdrawn = None  # re-read once per verification, never cached across decisions
        try:
            self._verify(vi, res)
        except Exception as exc:  # fail closed: an unverifiable decision is never published
            res.error = f"{type(exc).__name__}: {exc}"
            res.blocked.add("decision")
            res.checks.append(Check("verifier_error", False, "critical", f"verifier failed, fail closed: {res.error}"))
        return res

    # ------------------------------------------------------------------ helpers
    def _add(self, res: VerifyResult, subject: str, name: str, ok: bool, reason: str,
             evidence: list[str] | None = None, severity: str = "critical") -> None:
        res.checks.append(Check(name, ok, severity, reason, evidence or [], subject))
        if not ok and severity == "critical":
            res.blocked.add(subject)

    def _available(self, tid: str, start: datetime, end: datetime | None = None) -> tuple[bool, str]:
        t = self.titles.get(tid)
        if t is None:
            return False, f"{tid} not in catalogue"
        end = end or start
        if t.available_from is None or start < t.available_from:
            return False, f"{tid} not yet available at {iso(start)}"
        if t.available_until is not None and t.available_until <= end:
            return False, f"{tid} licence ends {iso(t.available_until)} before {iso(end)}"
        if self._withdrawn is None:
            self._withdrawn = self.store.withdrawals()
        w = self._withdrawn.get(tid)
        if w is not None and w <= end:
            return False, f"{tid} withdrawn at {iso(w)}"
        return True, "available"

    def _raw_events(self, user_id: str, until: datetime) -> list[Event]:
        return self.store.user_events(user_id, until, self.cfg.clock_skew_flag_seconds)

    def _consent(self, user_id: str, channel: str, until: datetime) -> tuple[bool, str, list[str]]:
        field_name = "notifications_opt_in" if channel == "push" else "whatsapp_opt_in"
        rows = sorted(self.store.profile_rows(user_id), key=lambda r: r.get("updated_at") or "", reverse=True)
        if not rows:
            return False, "no profile row: no consent on record", []
        ref = f"profile:{user_id}@{rows[0].get('updated_at')}"
        if rows[0].get(field_name) != "true":
            return False, f"{field_name} is not true", [ref]
        outs = [e for e in self._raw_events(user_id, until)
                if e.event == "notification_opt_out" and e.properties.get("channel") == channel]
        if outs:
            return False, f"opt-out event for {channel} overrides snapshot consent", [outs[-1].ref()]
        return True, f"{field_name}=true and no {channel} opt-out", [ref]

    def _sub_status(self, user_id: str, until: datetime) -> tuple[str | None, bool, list[str]]:
        rows = sorted(self.store.profile_rows(user_id), key=lambda r: r.get("updated_at") or "", reverse=True)
        evs = [e for e in self._raw_events(user_id, until)
               if e.event in ("purchase_completed", "subscription_renewed", "subscription_lapsed")]
        paid_ever = any(e.event == "subscription_renewed" or (e.event == "purchase_completed"
                        and not e.properties.get("is_trial")) for e in evs) or any(
            r.get("subscription_status") in ("active", "lapsed") for r in rows)
        status, refs = None, []
        if rows:
            status, refs = rows[0].get("subscription_status"), [f"profile:{user_id}@{rows[0].get('updated_at')}"]
        if evs:
            last = max(evs, key=lambda e: e.time)
            pt = parse_ts(rows[0].get("updated_at")) if rows else None
            if pt is None or last.time > pt:
                status = ("lapsed" if last.event == "subscription_lapsed" else
                          "trial_active" if last.event == "purchase_completed" and last.properties.get("is_trial")
                          else "active")
                refs = [last.ref()]
        return status, paid_ever, refs

    def _ref_resolves(self, ref: str, user_id: str) -> bool:
        kind, _, val = ref.partition(":")
        if kind == "event":
            return self.store.event_exists_ref(val)
        if kind == "profile":
            uid, _, upd = val.partition("@")
            return uid == user_id and any(r.get("updated_at") == upd for r in self.store.profile_rows(uid))
        if kind in ("catalogue", "title"):
            return val in self.titles
        if kind == "goal_rule":
            gid, _, rev = val.partition("-r")
            return gid in self.book.rules and rev.isdigit() and 1 <= int(rev) <= self.book.current(gid).revision
        if kind == "pricing":
            return val in self.pack.plans
        return kind in ("config", "arm", "control")

    def _prices_honest(self, text: str) -> tuple[bool, str]:
        found = [float(x) for x in PRICE_RE.findall(text or "")]
        bad = [x for x in found if x not in self.prices]
        return (not bad), (f"prices {found} all exist in pricing.json" if not bad else f"invented price(s) {bad}")

    def _messages_on_day(self, user_id: str, day, until: datetime) -> list[str]:
        refs = []
        for a in self.store.actions_for_user(user_id):
            if a["type"] in MESSAGE_TYPES and a["status"] != "cancelled":
                if ist_day(parse_ts(a["send_at"] or a["published_at"])) == day:
                    refs.append(f"action:{a['action_id']}")
        for e in self._raw_events(user_id, until):
            if e.event == "message_delivered" and e.properties.get("channel") in MESSAGE_TYPES and ist_day(e.time) == day:
                refs.append(e.ref())
        return refs

    # ------------------------------------------------------------------ the checks
    def _verify(self, vi: VerifyInput, res: VerifyResult) -> None:
        uid, acts = vi.user_id, vi.actions
        arm = self.arms.arm(uid)
        self._add(res, "decision", "holdout_exclusion", not (arm == "holdout" and acts),
                  f"arm={arm}; {len(acts)} proposed action(s)", [f"arm:{uid}"])
        if acts:
            self._add(res, "decision", "trigger_fresh", not vi.trigger_stale,
                      f"buffer delay {vi.trigger.buffer_delay_s:.0f}s vs limit {self.cfg.stale_trigger_seconds}s",
                      [vi.trigger.ref()])
            prior = [a for a in self.store.actions_for_user(uid)
                     if a["raw"].get("trigger_event_id") == vi.trigger.event_id]
            self._add(res, "decision", "trigger_idempotency", not prior,
                      "no earlier action for this trigger" if not prior else f"{len(prior)} action(s) already exist",
                      [vi.trigger.ref()])

        g = vi.goal
        if g is None:
            if acts:
                self._add(res, "decision", "goal_present", False, "an action needs an active measurable goal")
        else:
            self._verify_goal(vi, res, g)

        trig_dialect = normalize_dialect(vi.trigger.app_dialect_raw)
        n_msgs = sum(1 for a in acts if a["type"] in MESSAGE_TYPES)
        if n_msgs > 1:
            self._add(res, "decision", "one_message_channel", False, f"{n_msgs} messages proposed in one decision")
        for i, a in enumerate(acts):
            subj = f"a{i}:{a['type']}"
            t = a["type"]
            if t == "home_page":
                self._verify_page(res, subj, a, trig_dialect, vi.publish_at)
            elif t in MESSAGE_TYPES:
                self._verify_message(res, subj, a, uid, trig_dialect, vi, g)
            elif t == "in_app_message":
                self._verify_in_app(res, subj, a, uid, vi)
            else:
                self._add(res, subj, "action_type_allowed", False, f"unknown action type {t}")

    def _verify_goal(self, vi: VerifyInput, res: VerifyResult, g: dict[str, Any]) -> None:
        gid = g["goal_id"]
        rule = self.book.rules.get(gid)
        if rule is None:
            self._add(res, "goal", "goal_known", False, f"unknown goal {gid}")
            return
        self._add(res, "goal", "goal_measurable", rule.measurable and g.get("success_event") in OBSERVABLE_EVENTS,
                  rule.unmeasurable_reason or f"success event {g.get('success_event')} is observable",
                  [f"goal_rule:{rule.version}"])
        self._add(res, "goal", "goal_rule_current", g["rule_version"] == rule.version,
                  f"goal uses {g['rule_version']}, current {rule.version}", [f"goal_rule:{rule.version}"])
        start, deadline = parse_ts(g["started_at"]), parse_ts(g["deadline"])
        ok = deadline == start + timedelta(hours=rule.window_hours) and deadline > vi.publish_at
        self._add(res, "goal", "goal_deadline_valid", ok,
                  f"deadline {g['deadline']} = start + {rule.window_hours}h and in the future")
        refs = (g.get("eligibility") or {}).get("evidence") or []
        bad = [r for r in refs if not self._ref_resolves(r, vi.user_id)]
        self._add(res, "goal", "goal_evidence_resolves", bool(refs) and not bad,
                  "all eligibility evidence resolves to source records" if refs and not bad
                  else f"unresolvable evidence {bad or 'none supplied'}", refs)
        if vi.goal_source == "model":
            self._add(res, "goal", "model_choice_evidenced", gid in vi.deterministic_eligible,
                      f"model-chosen {gid} must be in the evidenced set {vi.deterministic_eligible}")
        if vi.model_status == "rejected_unsupported_claim":
            self._add(res, "goal", "model_claim_rejected", True,
                      "model asserted eligibility without evidence; the claim was rejected and not used",
                      severity="info")
        if vi.goal_is_new:
            ok, why, ev = self._independent_eligibility(vi.user_id, gid, vi.now, vi.trigger, g)
            self._add(res, "goal", "goal_eligibility_independent", ok, why, ev)

    def _independent_eligibility(self, uid: str, gid: str, now: datetime, trig: Event,
                                 g: dict[str, Any]) -> tuple[bool, str, list[str]]:
        rule = self.book.current(gid)
        evs = [e for e in self._raw_events(uid, now) if e.event_id != trig.event_id]
        rows = sorted(self.store.profile_rows(uid), key=lambda r: r.get("updated_at") or "", reverse=True)
        allowed = rule.eligibility.get("subscription_status")
        if allowed:
            status, paid_ever, refs = self._sub_status(uid, now)
            if status not in allowed:
                return False, f"independent status {status!r} not in {allowed}", refs
            if gid == "G05" and paid_ever:
                return False, "independent check found a paid plan in the user's records", refs
            if gid == "G09":
                lo = now - timedelta(days=self.cfg.habit_lookback_days)
                days = {ist_day(e.time) for e in evs if e.event == "play_started" and lo <= e.time < now}
                if len(days) > 1:
                    return False, f"played on {len(days)} days in the lookback", refs
            return True, f"independent status {status!r} in {allowed}", refs
        if gid in ("G01", "G03"):
            within = int(rule.eligibility.get("signed_up_within_days", 0))
            sign = [e for e in evs if e.event == "signup_completed"]
            st = min(e.time for e in sign) if sign else parse_ts(rows[0].get("signup_at")) if rows else None
            if st is None or now - st > timedelta(days=within):
                return False, f"signup {iso(st)} not within {within} days", []
            if gid == "G01" and any(e.event == "play_started" for e in evs):
                return False, "a play_started exists", []
            return True, f"signup {iso(st)} within {within} days", [sign[0].ref()] if sign else []
        if gid == "G02":
            if any(e.event == "episode_completed" for e in evs):
                return False, "an episode_completed exists", []
            sign = [e for e in evs if e.event == "signup_completed"]
            st = min(e.time for e in sign) if sign else parse_ts(rows[0].get("signup_at")) if rows else None
            if st is None or self.history_start is None or st < self.history_start:
                return False, "lifetime not observable, 'never completed' unsupported", []
            return True, "no completion in the full observed lifetime", []
        if gid == "G07":
            slug = ((g.get("eligibility") or {}).get("context") or {}).get("series_slug")
            plays = [e for e in evs if e.event in ("play_started", "play_progress", "episode_completed")
                     and (t := self.titles.get(e.properties.get("title_id"))) is not None and t.content_slug == slug]
            if not plays:
                return False, f"no plays of {slug}", []
            idle = now - max(e.time for e in plays)
            return idle >= timedelta(days=self.cfg.resume_idle_days), f"{slug} idle {idle.days} days", [plays[-1].ref()]
        if gid == "G12":
            lo = now - timedelta(days=self.cfg.absence_days)
            recent = [e for e in evs if e.event == "app_open" and e.session_id != trig.session_id and lo <= e.time < trig.time]
            return (not recent), ("no app_open in the absence window" if not recent else "recent app_open found"), \
                [recent[-1].ref()] if recent else []
        return False, f"no independent check for {gid}: not allowed to act", []

    def _verify_page(self, res: VerifyResult, subj: str, a: dict[str, Any], trig_dialect: str | None,
                     publish_at: datetime) -> None:
        d = a.get("dialect")
        self._add(res, subj, "dialect_matches_app_setting", d is not None and d == trig_dialect,
                  f"page dialect {d} vs current app setting {trig_dialect}")
        end = publish_at + self.ttl
        exp = parse_ts(a.get("expires_at")) if a.get("expires_at") else None
        self._add(res, subj, "page_ttl", exp is not None and exp <= end, f"expires {a.get('expires_at')} within TTL")
        seen: set[str] = set()
        hero = a.get("hero_title_id")
        if hero:
            ok, why = self._available(hero, publish_at, end)
            t = self.titles.get(hero)
            ok = ok and t is not None and t.dialect == d and not t.suspicious
            self._add(res, subj, "hero_valid", ok, f"hero {hero}: {why}", [f"catalogue:{hero}"])
            if t:
                seen.add(t.content_slug)
        rows = a.get("rows") or []
        self._add(res, subj, "row_count", len(rows) <= int(self.rows_cfg["max_rows"]),
                  f"{len(rows)} rows (max {self.rows_cfg['max_rows']})")
        allowed = set(self.rows_cfg["row_types"])
        lo, hi = int(self.rows_cfg["min_titles_per_row"]), int(self.rows_cfg["max_titles_per_row"])
        for k, r in enumerate(rows):
            rt = r.get("row_type")
            self._add(res, subj, f"row{k}_type_allowed", rt in allowed and "continue" not in str(rt).lower(),
                      f"row type {rt}")
            ids = r.get("title_ids") or []
            self._add(res, subj, f"row{k}_size", lo <= len(ids) <= hi, f"{len(ids)} titles (allowed {lo}-{hi})")
            for tid in ids:
                t = self.titles.get(tid)
                ok, why = self._available(tid, publish_at, end)
                if t is None or t.dialect != d:
                    self._add(res, subj, f"row{k}_dialect", False, f"{tid} not in page dialect {d}", [f"catalogue:{tid}"])
                if not ok:
                    self._add(res, subj, f"row{k}_availability", False, why, [f"catalogue:{tid}"])
                if t is not None and t.suspicious:
                    self._add(res, subj, f"row{k}_quarantine", False, f"{tid} has instruction-like text, quarantined")
                if t is not None:
                    if t.content_slug in seen:
                        self._add(res, subj, "content_slug_unique", False, f"{t.content_slug} appears twice")
                    seen.add(t.content_slug)
        if not any(c.subject == subj and not c.passed for c in res.checks):
            self._add(res, subj, "page_titles_valid", True,
                      f"{len(seen)} unique slugs, all in {d} and available {iso(publish_at)}-{iso(end)}")

    def _verify_title_for_message(self, res, subj, tid, dialect, send_at) -> None:
        if tid is None:
            return
        t = self.titles.get(tid)
        ok, why = self._available(tid, send_at)
        self._add(res, subj, "message_title_available", ok, why, [f"catalogue:{tid}"])
        self._add(res, subj, "message_title_dialect", t is not None and t.dialect == dialect and not t.suspicious,
                  f"{tid} dialect {t.dialect if t else None} vs viewer dialect {dialect}")

    def _verify_message(self, res: VerifyResult, subj: str, a: dict[str, Any], uid: str, trig_dialect: str | None,
                        vi: VerifyInput, g: dict[str, Any] | None) -> None:
        t = a["type"]
        ok, why, ev = self._consent(uid, t, vi.now)
        self._add(res, subj, "consent", ok, why, ev)
        send_at = parse_ts(a.get("send_at"))
        if send_at is None:
            self._add(res, subj, "send_time", False, "missing send_at")
            return
        self._add(res, subj, "send_after_publish", send_at >= vi.publish_at, f"send_at {a.get('send_at')}")
        self._add(res, subj, "quiet_hours_ist", not self.quiet.contains(send_at),
                  f"send_at {ist_clock(send_at)} vs quiet hours 22:00-08:00 IST")
        same_day = self._messages_on_day(uid, ist_day(send_at), vi.now)
        self._add(res, subj, "daily_message_cap", not same_day,
                  f"{len(same_day)} push/WhatsApp already on IST day {ist_day(send_at)}", same_day)
        if g is not None:
            self._add(res, subj, "before_goal_deadline", send_at < parse_ts(g["deadline"]), "send before deadline")
        self._verify_title_for_message(res, subj, a.get("title_id"), trig_dialect, send_at)
        if t == "push":
            text = a.get("text") or {}
            title, body = str(text.get("title") or ""), str(text.get("body") or "")
            self._add(res, subj, "push_length", 0 < len(title) <= int(self.copy["title_max_chars"])
                      and 0 < len(body) <= int(self.copy["body_max_chars"]),
                      f"title {len(title)}/{self.copy['title_max_chars']}, body {len(body)}/{self.copy['body_max_chars']}")
            low = (title + " " + body).lower()
            bad = [p for p in self.copy["forbidden_phrases"] if p.lower() in low]
            self._add(res, subj, "forbidden_phrases", not bad, f"forbidden phrases found: {bad}" if bad else "none")
            ok, why = self._prices_honest(title + " " + body)
            self._add(res, subj, "pricing_honest", ok, why)
            dl = a.get("deeplink")
            if a.get("title_id"):
                m = TITLE_DEEPLINK_RE.match(dl or "")
                self._add(res, subj, "deeplink_format", bool(m) and m.group(1) == a["title_id"],
                          f"deeplink {dl!r} must be ott://title/{a['title_id']}")
                tname = self.titles[a["title_id"]].name if a["title_id"] in self.titles else None
                self._add(res, subj, "names_real_title", tname is not None and tname in title + body,
                          "copy names the catalogue title")
            else:
                self._add(res, subj, "deeplink_format", dl is None, "no deeplink without a title")
        else:
            tpl = self.templates.get(a.get("template_id"))
            self._add(res, subj, "template_approved", tpl is not None, f"template {a.get('template_id')}")
            if tpl is None:
                return
            vars_ = a.get("variables") or {}
            self._add(res, subj, "template_variables", set(vars_) == set(tpl["variables"]),
                      f"variables {sorted(vars_)} vs {tpl['variables']}")
            ok, why = True, "variables come from catalogue/pricing"
            for k, v in vars_.items():
                tt = self.titles.get(a.get("title_id"))
                if k == "title_name":
                    ok &= tt is not None and v == tt.name
                elif k == "dialect_name":
                    ok &= tt is not None and v == tt.dialect
                elif k == "plan_price":
                    m = PRICE_RE.fullmatch(str(v))
                    ok &= bool(m) and float(m.group(1)) in self.prices
                elif k == "episode_number":
                    ok = False
                    why = "episode_number claims a *new* episode; no episode release dates exist, so it is unverifiable"
                else:
                    ok = False
            self._add(res, subj, "template_values_sourced", ok, why if ok or "episode" in why else
                      "a variable is not a catalogue/pricing value")
            if a.get("template_id") == "wa_trial_ending_v1":
                status, _, refs = self._sub_status(uid, vi.now)
                self._add(res, subj, "trial_claim_true", status == "trial_active", f"status {status}", refs)

    def _verify_in_app(self, res: VerifyResult, subj: str, a: dict[str, Any], uid: str, vi: VerifyInput) -> None:
        day = ist_day(vi.publish_at)
        same = [x for x in self.store.actions_for_user(uid) if x["type"] == "in_app_message"
                and x["status"] != "cancelled" and ist_day(parse_ts(x["published_at"])) == day]
        limit = int(self.pack.interventions["in_app_message"].get("max_per_day", 1))
        self._add(res, subj, "in_app_daily_limit", len(same) < limit, f"{len(same)} in-app today (max {limit})")
        ok, why = self._prices_honest(a.get("text", ""))
        self._add(res, subj, "pricing_honest", ok and a.get("plan_id") in self.pack.plans, why)
        low = (a.get("text") or "").lower()
        bad = [p for p in self.copy["forbidden_phrases"] if p.lower() in low]
        self._add(res, subj, "forbidden_phrases", not bad, f"forbidden: {bad}" if bad else "none")
