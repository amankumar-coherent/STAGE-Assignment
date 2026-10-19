"""One logical agent per viewer: decide -> verify -> publish, plus reactions to live and control events.

`AgentRuntime` holds shared, read-mostly dependencies (catalogue, rules, store, gates). All state that belongs to a
viewer (facts, goal, plan, decisions, actions, outcomes, learning) is persisted in the store keyed by user_id, and
every mutation for a user happens under that user's lock inside one database transaction.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .config import AgentConfig, PackConfig
from .content_selector import Catalogue
from .dependencies import StoreGate
from .fact_resolver import FactResolver, ViewerFacts
from .goal_planner import Eligibility, GoalPlanner, GoalRuleBook, judge_success
from .intervention_planner import InterventionPlanner, assess_confidence
from .model_provider import (AdviceResult, BudgetExceeded, CallContext, DecisionBudget, ModelClient,
                             StoreUnavailable)
from .models import Event
from .normalization import normalize_dialect
from .scheduler import IST_SOURCE, ist_day, iso, parse_ts
from .state_store import StateStore
from .verifier import ArmRegistry, Verifier, VerifyInput

log = logging.getLogger("stage_agent.agent")

SUBSCRIPTION_GOALS = ("G04", "G05", "G06", "G08", "G09")
MESSAGE_TYPES = ("push", "whatsapp")
ACTION_LABEL = {"home_page": "HOME_PAGE", "push": "PUSH", "whatsapp": "WHATSAPP", "in_app_message": "IN_APP"}


def _h(s: str, n: int = 12) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:n]


@dataclass
class DecisionResult:
    decision: dict[str, Any]
    service_ms: float
    actions: list[dict[str, Any]] = field(default_factory=list)


class AgentRuntime:
    def __init__(self, cfg: AgentConfig, pack: PackConfig, catalogue: Catalogue, store: StateStore,
                 arms: ArmRegistry, model: ModelClient, store_gate: StoreGate, history_start: datetime | None):
        self.cfg = cfg
        self.pack = pack
        self.cat = catalogue
        self.store = store
        self.arms = arms
        self.model = model
        self.store_gate = store_gate
        self.history_start = history_start
        self.book = GoalRuleBook(pack.goals)
        self.resolver = FactResolver(cfg, catalogue, pack.plans)
        self.goals = GoalPlanner(cfg, self.book, catalogue)
        self.planner = InterventionPlanner(cfg, pack, catalogue)
        self.verifier = Verifier(cfg, pack, catalogue, store, arms, self.book, history_start)
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()
        self.counters: dict[str, int] = defaultdict(int)
        for gid, rule in self.book.rules.items():
            store.put_goal_rule(gid, rule.revision, {"success_event": rule.success_event,
                                                     "condition": rule.success_condition,
                                                     "window_hours": rule.window_hours}, rule.source, None)
        for gid, rev in self._persisted_revisions().items():  # resume: re-apply corrections already in the store
            for r in self.store.goal_rules():
                if r["goal_id"] == gid and r["revision"] > self.book.current(gid).revision:
                    raw = r["raw"]
                    self.book.apply_correction({"goal_id": gid, "new_success": {
                        "event": raw["success_event"], "condition": raw["condition"],
                        "window_hours": raw["window_hours"]}}, parse_ts(r["effective_at"]))
        for tid, at in store.withdrawals().items():
            catalogue.withdraw(tid, at)

    def _persisted_revisions(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.store.goal_rules():
            out[r["goal_id"]] = max(out.get(r["goal_id"], 0), r["revision"])
        return {g: v for g, v in out.items() if v > 1}

    def lock(self, user_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks[user_id]

    # ================================================================== triggers
    def handle_trigger(self, trig: Event, now: datetime, seq: int, queue_depth: int = 0) -> DecisionResult:
        with self.lock(trig.user_id):
            try:
                return self._handle_trigger(trig, now, seq, queue_depth)
            except _AlreadyClaimed:
                original = self.store.processed_decision(trig.event_id)
                return self._duplicate(trig, now, seq, original or "unknown", time.perf_counter())

    def _duplicate(self, trig: Event, now: datetime, seq: int, original: str, t0: float) -> DecisionResult:
        n = self.store.mark_redelivery(trig.event_id)
        self.counters["duplicate_triggers"] += 1
        ms = (time.perf_counter() - t0) * 1000
        d = self._skeleton(f"{original}-dup-{_h(iso(trig.arrival) or '', 6)}", trig, now)
        d.update(action="NONE", status="DUPLICATE_IGNORED", decided_in_ms=round(ms, 1),
                 no_action_reason=f"duplicate delivery (#{n}) of trigger {trig.event_id}; already decided as {original}",
                 duplicate_of=original)
        d["goal"]["no_goal_reason"] = "duplicate delivery: see original decision"
        d["checks"]["passed"].append("decision:trigger_idempotency")
        d["assumptions"] = ["event_id identifies the same delivery; the original decision already covers it"]
        d["confidence_reason"] = "no new evidence: redelivery of an already-decided trigger"
        with self.store.tx() as c:
            self.store.insert_decision(c, d, seq)
        return DecisionResult(d, ms)

    def _skeleton(self, decision_id: str, trig: Event, now: datetime) -> dict[str, Any]:
        age = trig.buffer_delay_s + max(0.0, (now - trig.received_at).total_seconds())
        return {
            "decision_id": decision_id, "viewer_id": trig.user_id,
            "trigger": {"event": trig.event, "event_id": trig.event_id, "session_id": trig.session_id,
                        "received_at": iso(trig.received_at), "deliver_at": iso(trig.arrival),
                        "occurred_at_device": iso(trig.occurred_at), "est_occurred_at": iso(trig.time),
                        "buffer_delay_s": round(trig.buffer_delay_s, 1), "event_age_s": round(age, 1),
                        "stale": trig.buffer_delay_s > self.cfg.stale_trigger_seconds,
                        "anomalies": list(trig.anomalies)},
            "arm": self.arms.arm(trig.user_id),
            "goal": {"id": None, "instance_id": None, "started_at": None, "deadline": None, "success_signal": None,
                     "success_condition": None, "rule_version": None, "source": None, "no_goal_reason": None},
            "action": "NONE", "actions": [], "no_action_reason": None,
            "dialect": {"used": None, "source": None, "conflicts": []},
            "content": [], "confidence": 0.0, "confidence_reason": "", "evidence": [trig.ref()],
            "facts": {}, "assumptions": [], "conflicts": [], "checks": {"passed": [], "failed": []},
            "repairs": [], "goal_candidates": [], "model": {"status": "not_called", "notes": []},
            "fallback_used": False, "degradations": [], "model_calls": 0, "tool_calls": 0,
            "sim_dependency_ms": 0.0, "compute_ms": 0.0, "decided_in_ms": 0.0, "queue_wait_ms": 0.0,
            "latency_ms": None, "published_at": None, "status": "ABSTAINED", "ist_source": IST_SOURCE,
        }

    def _handle_trigger(self, trig: Event, now: datetime, seq: int, queue_depth: int) -> DecisionResult:
        t0 = time.perf_counter()
        original = self.store.processed_decision(trig.event_id)
        if original:
            return self._duplicate(trig, now, seq, original, t0)
        uid = trig.user_id
        decision_id = "D-" + _h(trig.event_id)
        d = self._skeleton(decision_id, trig, now)
        budget = DecisionBudget(self.cfg.max_model_calls, self.cfg.max_tool_calls)
        ctx = CallContext(trig.event_id, uid, now, budget)
        arm = d["arm"]
        stale = d["trigger"]["stale"]

        # ---------------- read the viewer's state (may be degraded)
        try:
            self.store_gate.read(ctx, "viewer_state")
            for tool in ("events", "profile", "goals", "actions", "learning"):
                budget.use_tool(tool)
        except (StoreUnavailable, BudgetExceeded) as exc:
            self.counters["store_failures"] += 1
            d["degradations"].append(f"user_data_store: {exc}")
            d["no_action_reason"] = (f"fail closed: viewer state could not be read ({exc}); without it eligibility, "
                                     "consent, caps and idempotency cannot be verified")
            d["goal"]["no_goal_reason"] = "viewer state unavailable"
            d["confidence_reason"] = "no evidence could be read"
            d["status"] = "HOLDOUT" if arm == "holdout" else "ABSTAINED"
            return self._commit(d, trig, now, seq, budget, t0, None, None, [])
        events = self.store.user_events(uid, now, self.cfg.clock_skew_flag_seconds)
        profiles = self.store.profile_rows(uid)
        learning = self.store.get_learning(uid)
        open_goal = self.store.open_goal(uid)
        prior_actions = self.store.actions_for_user(uid)

        vf = self.resolver.resolve(uid, now, trig, events, profiles, self.history_start, learning)
        d["facts"] = {k: f.to_dict() for k, f in sorted(vf.facts.items())}
        d["conflicts"] = vf.conflicts()
        dfact = vf.fact("dialect")
        d["dialect"] = {"used": dfact.value, "source": dfact.source, "rule": dfact.rule,
                        "conflicts": [f"{c['source']}:{c['value']}" for c in dfact.conflicts],
                        "conflict_detail": dfact.conflicts, "supported": dfact.supported}
        for name in ("dialect", "subscription_status", "signup_at", "profile_present"):
            f = vf.fact(name)
            if f and f.source_ref and f.source_ref not in d["evidence"]:
                d["evidence"].append(f.source_ref)

        # ---------------- goal: keep, revise or choose
        goal, goal_new, chosen_el, advice = self._goal(d, vf, trig, now, open_goal, stale, queue_depth, ctx, arm)

        if arm == "holdout":
            d["status"] = "HOLDOUT"
            d["no_action_reason"] = ("holdout arm: no agent action of any kind (goal tracked only so outcomes can be "
                                     "compared with the agent arm)")
            conf, why = assess_confidence(vf, chosen_el, d["fallback_used"])
            d["confidence"], d["confidence_reason"] = conf, why
            res = self.verifier.verify(self._vi(trig, now, now, goal, goal_new, d, advice, [], stale))
            self._record_checks(d, res)
            return self._commit(d, trig, now, seq, budget, t0, goal, goal_new, [])
        if stale:
            d["no_action_reason"] = (f"stale trigger: app open buffered offline for {trig.buffer_delay_s / 3600:.1f} h "
                                     f"(> {self.cfg.stale_trigger_seconds} s); the viewer is not in a live session, so a "
                                     "page or message now would not match their visit. Event kept for history.")
            d["confidence_reason"] = "trigger is not a live visit"
            return self._commit(d, trig, now, seq, budget, t0, goal, goal_new, [])
        if goal is None:
            d["no_action_reason"] = "no eligible, measurable goal: " + (d["goal"]["no_goal_reason"] or "")
            conf, why = assess_confidence(vf, None, d["fallback_used"])
            d["confidence"], d["confidence_reason"] = conf, why
            return self._commit(d, trig, now, seq, budget, t0, goal, goal_new, [])
        if vf.get("dialect") is None or not dfact.supported:
            d["no_action_reason"] = f"cannot act: {dfact.rule}"
            conf, why = assess_confidence(vf, chosen_el, d["fallback_used"])
            d["confidence"], d["confidence_reason"] = conf, why
            return self._commit(d, trig, now, seq, budget, t0, goal, goal_new, [])

        # ---------------- plan the intervention
        conf, why = assess_confidence(vf, chosen_el, d["fallback_used"])
        d["confidence"], d["confidence_reason"] = conf, why
        try:
            budget.use_tool("catalogue")
        except BudgetExceeded as exc:
            d["no_action_reason"] = f"tool budget exhausted: {exc}"
            return self._commit(d, trig, now, seq, budget, t0, goal, goal_new, [])
        elapsed_ms = (time.perf_counter() - t0) * 1000 + budget.sim_ms
        publish_est = now + timedelta(milliseconds=elapsed_ms + 2000)
        prop = self.planner.propose(vf, goal, chosen_el, trig.app_language, now, publish_est, conf, prior_actions)
        d["assumptions"] += prop.assumptions
        d["plan_notes"] = prop.notes
        actions = self._materialize(prop.actions, d, trig, goal)

        # ---------------- independent verification with explicit repair + re-verification
        res = self.verifier.verify(self._vi(trig, now, publish_est, goal, goal_new, d, advice, actions, stale))
        for _ in range(3):
            if res.decision_blocked:
                if actions:
                    d["repairs"].append({"dropped": [a["action_id"] for a in actions],
                                         "reason": "decision-level critical check failed: " +
                                         "; ".join(f["reason"] for f in res.failed() if f["severity"] == "critical")})
                actions = []
                break
            bad = [i for i, a in enumerate(actions) if f"a{i}:{a['type']}" in res.blocked]
            if not bad:
                break
            for i in bad:
                reasons = [f["reason"] for f in res.failed() if f["check"].startswith(f"a{i}:")]
                d["repairs"].append({"dropped": actions[i]["action_id"], "type": actions[i]["type"],
                                     "reason": "; ".join(reasons)})
            actions = [a for i, a in enumerate(actions) if i not in bad]
            self.counters["verifier_blocked_actions"] += len(bad)
            res = self.verifier.verify(self._vi(trig, now, publish_est, goal, goal_new, d, advice, actions, stale))
        self._record_checks(d, res)
        if res.error:
            d["degradations"].append(f"verifier error: {res.error}")
            actions = []
        if not actions:
            reason = "; ".join(prop.notes) if prop.notes else "planner proposed nothing"
            if d["repairs"]:
                d["status"] = "BLOCKED_BY_VERIFIER"
                reason = "all proposed actions failed verification: " + "; ".join(
                    str(r["reason"]) for r in d["repairs"])
            d["no_action_reason"] = reason
        return self._commit(d, trig, now, seq, budget, t0, goal, goal_new, actions, publish_est)

    # ------------------------------------------------------------------ goal selection
    def _goal(self, d, vf: ViewerFacts, trig: Event, now: datetime, open_goal, stale: bool, queue_depth: int,
              ctx: CallContext, arm: str):
        advice: AdviceResult | None = None
        el_list = None
        if open_goal is not None:
            invalid = None
            if parse_ts(open_goal["deadline"]) <= now:
                invalid = ("expired", "deadline passed; awaiting final judgement")
            elif open_goal["goal_id"] in SUBSCRIPTION_GOALS:
                allowed = self.book.current(open_goal["goal_id"]).eligibility.get("subscription_status") or []
                st = vf.fact("subscription_status")
                if st is not None and st.value is not None and st.value not in allowed:
                    invalid = ("cancelled", f"premise invalidated: subscription_status is now {st.value}")
            if invalid is None:
                self._fill_goal(d, open_goal, "kept")
                el = Eligibility(open_goal["goal_id"], True, open_goal["eligibility"]["reason"],
                                 open_goal["eligibility"]["evidence"], open_goal["eligibility"].get("context") or {})
                d["evidence"] += [r for r in el.evidence if r not in d["evidence"]]
                return open_goal, False, el, None
            with self.store.tx() as c:
                self.store.close_goal(c, open_goal["instance_id"], invalid[0], now, invalid[1])
                self.store.audit("goal_closed", now, {"instance_id": open_goal["instance_id"], "status": invalid[0],
                                                      "reason": invalid[1]}, trig.user_id, c)
            d["assumptions"].append(f"previous goal {open_goal['goal_id']} {invalid[0]}: {invalid[1]}")
        if stale:
            d["goal"]["no_goal_reason"] = "stale trigger: goal planning deferred to the next live visit"
            return None, False, None, None
        el_list = self.goals.evaluate(vf, now, vf.get("dialect"))
        d["goal_candidates"] = [e.to_dict() for e in el_list]
        ranked = self.goals.rank([e for e in el_list if e.eligible])
        if not ranked:
            unmeasurable = [e.goal_id for e in el_list if not e.measurable]
            d["goal"]["no_goal_reason"] = (f"none of the measurable goals is supported by evidence "
                                           f"(unmeasurable, never selectable: {unmeasurable})")
            return None, False, None, None
        chosen, source = ranked[0], "deterministic"
        if len(ranked) >= 2:
            shed = queue_depth > self.cfg.shed_queue_depth
            over = ctx.budget.sim_ms > self.cfg.decision_soft_budget_ms
            if shed or over:
                d["model"] = {"status": "skipped_load_shedding" if shed else "skipped_time_budget", "notes": [
                    f"queue depth {queue_depth}" if shed else f"{ctx.budget.sim_ms:.0f} ms already spent"]}
                d["degradations"].append(d["model"]["status"])
            else:
                cands = [{"goal_id": e.goal_id, "reason": e.reason, "evidence": e.evidence} for e in ranked]
                facts = {k: d["facts"][k]["value"] for k in ("subscription_status", "signup_at", "dialect")
                         if k in d["facts"]}
                advice = self.model.goal_advice(cands, facts, ctx)
                d["model"] = {"status": advice.status, "attempts": advice.attempts, "notes": advice.notes}
                picked = next((e for e in ranked if e.goal_id == advice.goal_id), None) if advice.usable else None
                if picked is not None:
                    chosen, source = picked, "model"
                elif advice.usable:  # defence in depth: never act on a goal outside the evidenced set
                    d["fallback_used"] = True
                    d["model"]["notes"].append(f"model goal {advice.goal_id} not in evidenced set; ignored")
                    d["degradations"].append("model returned an unevidenced goal: deterministic priority used")
                elif advice.status != "ok":
                    d["fallback_used"] = True
                    d["degradations"].append(f"model {advice.status}: deterministic priority used")
                    self.counters[f"model_{advice.status}"] += 1
        g = self.goals.new_instance(trig.user_id, chosen, d["decision_id"], now, arm)
        g["source"] = source
        self._fill_goal(d, g, "new")
        d["goal"]["source"] = source
        d["evidence"] += [r for r in chosen.evidence if r not in d["evidence"]]
        return g, True, chosen, advice

    @staticmethod
    def _fill_goal(d, g, how: str) -> None:
        d["goal"].update(id=g["goal_id"], instance_id=g["instance_id"], started_at=g["started_at"],
                         deadline=g["deadline"], success_signal=g["success_event"],
                         success_condition=g["success_condition"], rule_version=g["rule_version"],
                         eligibility_reason=g["eligibility"]["reason"], selection=how,
                         source=g.get("source", "kept"))

    def _vi(self, trig, now, publish_at, goal, goal_new, d, advice, actions, stale) -> VerifyInput:
        elig = [c["goal_id"] for c in d.get("goal_candidates", []) if c.get("eligible")]
        return VerifyInput(user_id=trig.user_id, trigger=trig, now=now, publish_at=publish_at, goal=goal,
                           goal_is_new=goal_new, deterministic_eligible=elig,
                           goal_source=d["goal"].get("source") or "deterministic",
                           model_status=advice.status if advice else None, actions=actions, trigger_stale=stale)

    @staticmethod
    def _record_checks(d, res) -> None:
        d["checks"] = {"passed": res.passed(), "failed": res.failed()}

    def _materialize(self, proposed: list[dict[str, Any]], d, trig: Event, goal) -> list[dict[str, Any]]:
        out = []
        for a in proposed:
            key = f"{trig.user_id}|{trig.event_id}|{a['type']}"
            out.append({"action_id": "A-" + _h(key), "decision_id": d["decision_id"], "user_id": trig.user_id,
                        "trigger_event_id": trig.event_id, "goal_instance_id": goal["instance_id"] if goal else None,
                        "goal_id": goal["goal_id"] if goal else None, "_idem": key, **a})
        return out

    # ------------------------------------------------------------------ commit
    def _commit(self, d, trig: Event, now: datetime, seq: int, budget: DecisionBudget, t0: float,
                goal, goal_new, actions: list[dict[str, Any]], publish_est: datetime | None = None) -> DecisionResult:
        self._baseline_assumptions(d)
        compute_ms = (time.perf_counter() - t0) * 1000
        service_ms = compute_ms + budget.sim_ms
        publish_at = now + timedelta(milliseconds=service_ms)
        if actions and publish_est is not None and publish_at > publish_est:
            d["degradations"].append("publish time exceeded the verified estimate; actions withheld")
            d["repairs"].append({"dropped": [a["action_id"] for a in actions], "reason": "late publication"})
            actions = []
            d["no_action_reason"] = "verification window exceeded before publication"
        d.update(model_calls=budget.model_calls, tool_calls=budget.tool_calls,
                 sim_dependency_ms=round(budget.sim_ms, 1), compute_ms=round(compute_ms, 2),
                 decided_in_ms=round(service_ms, 1), dependency_log=budget.log)
        pub_actions = []
        for a in actions:
            idem = a.pop("_idem")
            a["published_at"] = iso(publish_at)
            pub_actions.append((a, idem))
        if pub_actions:
            d["status"] = "PUBLISHED"
            d["published_at"] = iso(publish_at)
            labels = [ACTION_LABEL[a["type"]] for a, _ in pub_actions]
            d["action"] = "+".join(labels)
            d["actions"] = [{"action_id": a["action_id"], "type": a["type"], "send_at": a.get("send_at"),
                             "status": "scheduled" if a["type"] in MESSAGE_TYPES else "published"}
                            for a, _ in pub_actions]
            content = []
            for a, _ in pub_actions:
                ids = ([a["hero_title_id"]] if a.get("hero_title_id") else []) + \
                      [t for r in a.get("rows") or [] for t in r["title_ids"]] + \
                      ([a["title_id"]] if a.get("title_id") else [])
                content += [f"title:{t}" for t in ids if f"title:{t}" not in content]
                if a.get("plan_id"):
                    d["evidence"].append(f"pricing:{a['plan_id']}")
                if a.get("template_id"):
                    d["evidence"].append(f"config:interventions@{self.pack.interventions.get('version')}")
            d["content"] = content
            d["evidence"] += [f"catalogue:{c.split(':', 1)[1]}" for c in content[:12]]
            d["evidence"].append(f"config:interventions@{self.pack.interventions.get('version')}")
        if d["goal"]["rule_version"]:
            ref = f"goal_rule:{d['goal']['rule_version']}"
            if ref not in d["evidence"]:
                d["evidence"].append(ref)
        d["evidence"].append(f"arm:{trig.user_id}")
        d["evidence"] = list(dict.fromkeys(d["evidence"]))
        with self.store.tx() as c:
            if not self.store.claim_trigger(c, trig.event_id, d["decision_id"], trig.user_id):
                raise _AlreadyClaimed()
            if goal is not None and goal_new:
                self.store.insert_goal(c, goal)
            for a, idem in pub_actions:
                status = "scheduled" if a["type"] in MESSAGE_TYPES else "published"
                titles = [t.split(":", 1)[1] for t in d["content"]]
                self.store.insert_action(c, a, idem, status, a.get("goal_instance_id"), titles)
            self.store.insert_decision(c, d, seq)
        self.counters[f"status_{d['status']}"] += 1
        return DecisionResult(d, service_ms, [a for a, _ in pub_actions])

    @staticmethod
    def _baseline_assumptions(d) -> None:
        """Every decision states what it takes for granted, including abstentions."""
        a = d["assumptions"]
        t = d["trigger"]
        if t.get("stale"):
            a.append(f"device-reported buffering ({t['buffer_delay_s']:.0f} s) reflects when the viewer was really "
                     "active, so they are no longer in that session")
        if d["arm"] == "holdout":
            a.append("replay/arms.csv assignment is final for the whole replay; holdout viewers must see the default app")
        if d["dialect"].get("source") == "app_setting":
            a.append("the trigger's app_dialect is the setting the app will use to request the home page")
        elif d["dialect"].get("used") is None and d["trigger"]["event"] != "title_withdrawn":
            a.append("no other source reliably tells which dialect page the app will request")
        if d["goal"].get("id") and d["goal"].get("selection") == "kept":
            a.append(f"goal {d['goal']['id']} stays valid until its deadline unless new evidence invalidates it")
        a.append("facts are as of the decision time; events not yet delivered (buffered or late) may change them")
        d["assumptions"] = list(dict.fromkeys(a))

    # ================================================================== live events
    def on_live_event(self, e: Event, raw: dict[str, Any], now: datetime) -> dict[str, Any]:
        with self.lock(e.user_id):
            ins, dup = self.store.insert_events([(e, raw)])
            if dup:
                self.counters["duplicate_live_events"] += 1
                return {"duplicate": True}
            out: dict[str, Any] = {"duplicate": False, "cancelled": []}
            g = self.store.open_goal(e.user_id)
            if g is not None:
                evs = [self._as_dict(x) for x in self.store.user_events(e.user_id, now) if x.source in ("live",)]
                hit, refs, why = judge_success(g, evs, self.cat, lambda x: parse_ts(x["_time"]))
                if hit is not None:
                    out["cancelled"] += self._close_succeeded(g, hit, refs, now)
                    out["goal_succeeded"] = g["instance_id"]
            if e.event == "purchase_completed":
                out["cancelled"] += self._cancel_where(
                    e.user_id, now, lambda a: a.get("plan_id") is not None or a.get("template_id") == "wa_trial_ending_v1",
                    f"purchase {e.ref()}: price/trial messaging no longer applies")
                out["replan"] = self._replan_after_purchase(e, now, g if not out.get("goal_succeeded") else None)
                self.store.audit("purchase_after_decision", now, {"event": e.ref(), "cancelled": out["cancelled"],
                                                                  "goal_closed": out.get("goal_succeeded"),
                                                                  "replan": out["replan"]}, e.user_id)
            elif e.event == "notification_opt_out":
                ch = e.properties.get("channel")
                out["cancelled"] += self._cancel_where(e.user_id, now, lambda a: a["type"] == ch,
                                                      f"opt-out {e.ref()} for {ch}")
            return out

    def _replan_after_purchase(self, e: Event, now: datetime, g: dict[str, Any] | None) -> str:
        """Re-check the open goal's premise now that the viewer has paid (the profile still says otherwise)."""
        status = "trial_active" if e.properties.get("is_trial") else "active"
        if g is None:
            return f"subscription now {status} (event overrides the stale profile); no open goal left to revise"
        allowed = self.book.current(g["goal_id"]).eligibility.get("subscription_status")
        if allowed and status not in allowed:
            with self.store.tx() as c:
                self.store.close_goal(c, g["instance_id"], "cancelled", now,
                                      f"premise invalidated by {e.ref()}: subscription now {status}")
            cancelled = self._cancel_where(g["user_id"], now, lambda a: a.get("goal_instance_id") == g["instance_id"],
                                           f"goal {g['goal_id']} cancelled after purchase {e.ref()}")
            return (f"goal {g['goal_id']} cancelled (requires {allowed}, now {status}); cancelled {cancelled}; "
                    "a new goal is chosen at the next live trigger")
        return (f"goal {g['goal_id']} kept: its eligibility does not depend on subscription status; "
                f"subscription now {status} for later decisions")

    @staticmethod
    def _as_dict(e: Event) -> dict[str, Any]:
        return {"event_id": e.event_id, "event": e.event, "properties": e.properties, "_time": iso(e.time)}

    def _close_succeeded(self, g, hit, refs, now: datetime) -> list[str]:
        outcome = {"result": "succeeded", "success_event": refs, "judged_from": "live replay events",
                   "rule_version": g["rule_version"]}
        with self.store.tx() as c:
            if self.store.close_goal(c, g["instance_id"], "succeeded", now, f"success signal {refs[0]}", outcome):
                self.store.audit("goal_succeeded_live", now, {"instance_id": g["instance_id"], "evidence": refs},
                                 g["user_id"], c)
        return self._cancel_where(g["user_id"], now, lambda a: a.get("goal_instance_id") == g["instance_id"],
                                  f"goal {g['goal_id']} already achieved ({refs[0]})")

    def _cancel_where(self, user_id: str, now: datetime, pred, reason: str) -> list[str]:
        cancelled = []
        with self.store.tx() as c:
            for a in self.store.actions_for_user(user_id):
                if a["status"] in ("scheduled",) or (a["type"] == "in_app_message" and a["status"] == "published"):
                    if pred(a | a["raw"]):
                        if self.store.set_action_status(c, a["action_id"], "cancelled", reason, now):
                            cancelled.append(a["action_id"])
            if cancelled:
                self.store.audit("actions_cancelled", now, {"actions": cancelled, "reason": reason}, user_id, c)
        self.counters["cancelled_actions"] += len(cancelled)
        return cancelled

    # ================================================================== control events
    def on_withdrawal(self, ctrl: dict[str, Any], at: datetime, seq_fn) -> list[DecisionResult]:
        tid = ctrl["title_id"]
        ref = f"control:{iso(at)}:title_withdrawn:{tid}"
        self.cat.withdraw(tid, at)
        self.store.add_withdrawal(tid, at, ref)
        results = []
        cancelled_users = set()
        for a in self.store.all_actions():
            if a["status"] == "scheduled" and tid in a["title_ids"]:
                with self.store.tx() as c:
                    self.store.set_action_status(c, a["action_id"], "cancelled", f"{tid} withdrawn ({ref})", at)
                cancelled_users.add(a["user_id"])
                self.counters["cancelled_actions"] += 1
        ttl = timedelta(minutes=self.pack.page_ttl_minutes)
        live_pages = [a for a in self.store.all_actions() if a["type"] == "home_page" and a["status"] == "published"
                      and parse_ts(a["published_at"]) <= at < parse_ts(a["published_at"]) + ttl and tid in a["title_ids"]]
        for a in live_pages:
            with self.lock(a["user_id"]):
                results.append(self._repair_page(a, tid, at, ref, seq_fn()))
        self.store.audit("title_withdrawn", at, {"title_id": tid, "reason": ctrl.get("reason"),
                                                 "cancelled_message_users": sorted(cancelled_users),
                                                 "live_pages_repaired": [a["action_id"] for a in live_pages]})
        return results

    def _repair_page(self, old: dict[str, Any], tid: str, at: datetime, ref: str, seq: int) -> DecisionResult:
        t0 = time.perf_counter()
        raw = old["raw"]
        uid = old["user_id"]
        ctrl_event = Event(event_id=f"{ref}|{old['action_id']}", user_id=uid, event="title_withdrawn",
                           occurred_at=at, sent_at=at, received_at=at, session_id=None,
                           app_dialect_raw=raw.get("dialect"), app_language=None, platform=None, properties={},
                           source="control", deliver_at=at, est_occurred_at=at)
        d = self._skeleton("D-" + _h(ctrl_event.event_id), ctrl_event, at)
        budget = DecisionBudget(self.cfg.max_model_calls, self.cfg.max_tool_calls)
        hero = raw.get("hero_title_id")
        rows = [{"row_type": r["row_type"], "title_ids": [t for t in r["title_ids"] if t != tid]} for r in raw["rows"]]
        lo = int(self.pack.interventions["home_page"]["rows"]["min_titles_per_row"])
        rows = [r for r in rows if len(r["title_ids"]) >= lo]
        if hero == tid:
            hero = None
        goal = self.store.open_goal(uid)
        d["goal"]["no_goal_reason"] = None if goal else "goal no longer open"
        if goal:
            self._fill_goal(d, goal, "kept")
        expires = raw.get("expires_at")
        page = {"type": "home_page", "dialect": raw.get("dialect"), "hero_title_id": hero, "rows": rows,
                "expires_at": expires, "repair_of": old["action_id"]}
        key = f"{uid}|{ctrl_event.event_id}|home_page"
        act = {"action_id": "A-" + _h(key), "decision_id": d["decision_id"], "user_id": uid,
               "trigger_event_id": ctrl_event.event_id, "goal_instance_id": old["goal_instance_id"],
               "goal_id": raw.get("goal_id"), "_idem": key, **page}
        d["dialect"] = {"used": raw.get("dialect"), "source": "previous_page", "conflicts": []}
        with self.store.tx() as c:
            self.store.set_action_status(c, old["action_id"], "superseded", f"{tid} withdrawn ({ref})", at)
        vi = VerifyInput(user_id=uid, trigger=ctrl_event, now=at, publish_at=at + timedelta(seconds=2), goal=goal,
                         goal_is_new=False, deterministic_eligible=[], goal_source="kept", model_status=None,
                         actions=[act] if (rows or hero) else [], trigger_stale=False)
        res = self.verifier.verify(vi)
        blocked = any(k.startswith("a0") for k in res.blocked) or res.decision_blocked
        self._record_checks(d, res)
        d["assumptions"].append(f"repair of {old['action_id']}: {tid} withdrawn while the page was live")
        if goal is None or not (rows or hero) or blocked:
            d["no_action_reason"] = (f"page {old['action_id']} contained withdrawn {tid}; superseded, and no valid "
                                     "replacement could be verified" if (rows or hero) else
                                     f"page {old['action_id']} superseded; too few titles remain without {tid}")
            if goal is None and (rows or hero) and not blocked:
                d["no_action_reason"] = f"page superseded; goal no longer open, so no replacement is published"
            return self._commit_repair(d, ctrl_event, at, seq, budget, t0, [])
        return self._commit_repair(d, ctrl_event, at, seq, budget, t0, [act])

    def _commit_repair(self, d, ev, at, seq, budget, t0, actions) -> DecisionResult:
        d["evidence"] = [d["trigger"]["event_id"].split("|")[0]]
        d["confidence"], d["confidence_reason"] = (0.9, "deterministic repair of an already-verified page")
        return self._commit(d, ev, at, seq, budget, t0, None, False, actions,
                            at + timedelta(seconds=2) if actions else None)

    def on_rule_correction(self, ctrl: dict[str, Any], at: datetime) -> dict[str, Any]:
        gid = ctrl["goal_id"]
        old = self.book.current(gid).version
        rule = self.book.apply_correction(ctrl, at)
        self.store.put_goal_rule(gid, rule.revision, {"success_event": rule.success_event,
                                                      "condition": rule.success_condition,
                                                      "window_hours": rule.window_hours}, rule.source, at)
        affected = self.store.open_goals_for_goal_id(gid)
        total_open = self.store.conn.execute("SELECT COUNT(*) FROM goals WHERE status='open'").fetchone()[0]
        with self.store.tx() as c:
            for g in affected:
                self.store.revise_goal_rule(c, g["instance_id"], rule.version, rule.success_event,
                                            rule.success_condition, at,
                                            f"goal_rule_corrected: {old} -> {rule.version} ({ctrl.get('note', '')})")
            info = {"goal_id": gid, "from": old, "to": rule.version, "affected_open_goals": len(affected),
                    "open_goals_total": total_open, "untouched": total_open - len(affected),
                    "affected_instances": [g["instance_id"] for g in affected]}
            self.store.audit("goal_rule_corrected", at, info, None, c)
        self.counters["goals_revised"] += len(affected)
        return info

    # ================================================================== scheduled messages
    def send_time_check(self, a: dict[str, Any], now: datetime, final: bool = False) -> tuple[bool, str]:
        """Re-verify a scheduled message at send time (or, for `final`, against state at the end of replay)."""
        uid = a["user_id"]
        raw = a["raw"]
        send_at = parse_ts(a["send_at"])
        v = self.verifier
        v._withdrawn = None
        if self.arms.arm(uid) == "holdout":
            return False, "holdout"
        ok, why, _ = v._consent(uid, a["type"], now)
        if not ok:
            return False, why
        if v.quiet.contains(send_at):
            return False, "send time inside quiet hours"
        others = [r for r in v._messages_on_day(uid, ist_day(send_at), now) if r != f"action:{a['action_id']}"]
        if others:
            return False, f"daily cap: {others}"
        if raw.get("title_id"):
            ok, why = v._available(raw["title_id"], send_at)
            if not ok:
                return False, why
            last = [e for e in self.store.user_events(uid, now) if normalize_dialect(e.app_dialect_raw)]
            cur = normalize_dialect(last[-1].app_dialect_raw) if last else None
            t = self.cat.by_id.get(raw["title_id"])
            if cur is not None and t is not None and t.dialect != cur:
                return False, f"viewer's app dialect is now {cur}, message title is {t.dialect}"
        g = next((x for x in self.store.goals_for_user(uid) if x["instance_id"] == a["goal_instance_id"]), None)
        if g is None or g["status"] != "open":
            return False, f"goal no longer open ({g['status'] if g else 'missing'})"
        return True, "revalidated" + (" at end of replay" if final else " at send time")

    def dispatch_due(self, now: datetime) -> int:
        n = 0
        for a in self.store.scheduled_messages(due_by=now):
            with self.lock(a["user_id"]):
                ok, why = self.send_time_check(a, parse_ts(a["send_at"]))
                with self.store.tx() as c:
                    self.store.set_action_status(c, a["action_id"], "sent" if ok else "cancelled", why,
                                                 parse_ts(a["send_at"]))
                if not ok:
                    self.counters["cancelled_actions"] += 1
                n += 1
        return n

    def final_revalidation(self, end: datetime) -> dict[str, int]:
        kept = dropped = 0
        for a in self.store.scheduled_messages():
            ok, why = self.send_time_check(a, end, final=True)
            if not ok:
                with self.store.tx() as c:
                    self.store.set_action_status(c, a["action_id"], "cancelled", why + " (end-of-replay check)", end)
                dropped += 1
            else:
                kept += 1
        return {"kept_scheduled": kept, "cancelled": dropped}


class _AlreadyClaimed(Exception):
    """A concurrent worker decided this trigger first; the caller records a duplicate."""
