"""Judge every goal from attributable signals and feed what was observed back into each viewer's state.

Rules:
  * succeeded   - an event satisfying the goal's (current, possibly corrected) success predicate occurred inside
                  [started_at, deadline]. Events outside the window never count.
  * failed      - no such event and the whole window lies inside the observed data (deadline <= horizon).
  * inconclusive- no such event yet but the window extends past the observed data, or the goal was cancelled
                  because its premise became false. Missing evidence is not failure.
Outcomes already finalized (final=1) are never rewritten. A published message is not a delivered message; only a
`message_delivered` event shows delivery, and only `home_page_shown` with personalised=true shows a page was seen.
Goal success is descriptive per viewer; causal impact is estimated only by the randomized comparison.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .content_selector import Catalogue
from .goal_planner import judge_success
from .loader import read_jsonl
from .scheduler import iso, parse_ts
from .state_store import StateStore


def ingest_outcome_events(store: StateStore, path: Path) -> int:
    store.clear_outcome_events()
    rows = []
    for e in read_jsonl(path, None, ("event_id", "user_id", "event", "received_at")):
        rows.append((e["event_id"], e["user_id"], e["event"], e["received_at"], e))
    return store.insert_outcome_events(rows)


def _t(e: dict[str, Any]) -> datetime:
    return parse_ts(e["received_at"])


def judge_all(store: StateStore, catalogue: Catalogue, judged_at: datetime | None = None) -> list[dict[str, Any]]:
    horizon_s = store.outcome_horizon()
    horizon = parse_ts(horizon_s) if horizon_s else None
    judged_at = judged_at or horizon or datetime.now().astimezone()
    actions_by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in store.all_actions():
        actions_by_user[a["user_id"]].append(a)
    goals_by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for g in store.all_goals():
        goals_by_user[g["user_id"]].append(g)
    results = []
    for uid in sorted(goals_by_user):
        events = store.outcome_events_for_user(uid)
        events.sort(key=lambda e: (e["received_at"], e["event_id"]))
        acts = actions_by_user.get(uid, [])
        my_ids = {a["action_id"] for a in acts}
        exposures = _exposures(events, my_ids)
        learning = _learning(store.get_learning(uid), events, acts, my_ids, exposures)
        user_out = []
        for g in goals_by_user[uid]:
            o = _judge_goal(g, events, catalogue, horizon, judged_at, exposures)
            user_out.append(o)
            results.append(o)
        learning["goal_results"] = [{"goal_id": o["goal_id"], "instance_id": o["goal_instance_id"],
                                     "result": o["result"], "attribution": o["attribution"]} for o in user_out]
        with store.tx() as c:
            for o in user_out:
                store.upsert_outcome(c, o)
            store.put_learning(c, uid, learning)
    return store.outcomes() if results else []


def _exposures(events: list[dict[str, Any]], my_ids: set[str]) -> list[dict[str, Any]]:
    out = []
    for e in events:
        p = e.get("properties") or {}
        if e["event"] == "home_page_shown" and p.get("personalised") and p.get("page_action_id") in my_ids:
            out.append({"kind": "page_shown", "action_id": p["page_action_id"], "at": e["received_at"],
                        "ref": f"event:{e['event_id']}"})
        elif e["event"] in ("message_delivered", "message_clicked") and p.get("action_id") in my_ids:
            out.append({"kind": e["event"], "action_id": p["action_id"], "at": e["received_at"],
                        "ref": f"event:{e['event_id']}"})
    return out


def _judge_goal(g: dict[str, Any], events: list[dict[str, Any]], catalogue: Catalogue, horizon: datetime | None,
                judged_at: datetime, exposures: list[dict[str, Any]]) -> dict[str, Any]:
    start, deadline = parse_ts(g["started_at"]), parse_ts(g["deadline"])
    base = {"goal_instance_id": g["instance_id"], "user_id": g["user_id"], "goal_id": g["goal_id"],
            "arm": g.get("arm"), "rule_version": g["rule_version"], "success_signal": g["success_event"],
            "success_condition": g["success_condition"], "started_at": g["started_at"], "deadline": g["deadline"],
            "observed_until": iso(horizon), "judged_at": iso(judged_at), "decision_id": g["decision_id"],
            "goal_status_in_replay": g["status"], "rule_history": g.get("history", [])}
    if g["status"] == "cancelled":
        return base | {"result": "inconclusive", "final": True, "evidence": [],
                       "reason": f"goal cancelled during replay: {g.get('close_reason')}", "attribution": "n/a",
                       "exposures_before_success": []}
    hit, refs, unsupported = judge_success(g, events, catalogue, _t)
    if unsupported:
        return base | {"result": "inconclusive", "final": False, "evidence": [], "reason": unsupported,
                       "attribution": "n/a", "exposures_before_success": []}
    window_exp = [x for x in exposures if start <= parse_ts(x["at"]) <= deadline]
    if hit is not None:
        ht = _t(hit)
        before = [x for x in window_exp if parse_ts(x["at"]) <= ht]
        if g.get("arm") == "holdout":
            attribution = "holdout: no agent exposure possible"
        elif before:
            attribution = "exposed_before_success"
        else:
            attribution = "success_without_observed_exposure"
        mins = (ht - start).total_seconds() / 60
        return base | {"result": "succeeded", "final": True, "evidence": refs,
                       "reason": f"{hit['event']} at {hit['received_at']} ({mins:.0f} min after goal start) satisfies "
                                 f"'{g['success_condition']}' under {g['rule_version']}",
                       "attribution": attribution, "exposures_before_success": [x["ref"] for x in before][:5]}
    if horizon is not None and deadline <= horizon:
        return base | {"result": "failed", "final": True, "evidence": [],
                       "reason": f"no qualifying {g['success_event']} between {g['started_at']} and {g['deadline']}",
                       "attribution": "n/a", "exposures_window": [x["ref"] for x in window_exp][:5],
                       "exposures_before_success": []}
    return base | {"result": "inconclusive", "final": False, "evidence": [],
                   "reason": f"deadline {g['deadline']} is after the last observed event ({iso(horizon)}); "
                             "absence of success so far is not failure",
                   "attribution": "n/a", "exposures_before_success": []}


def _learning(prev: dict[str, Any], events: list[dict[str, Any]], acts: list[dict[str, Any]], my_ids: set[str],
              exposures: list[dict[str, Any]]) -> dict[str, Any]:
    """What the agent observed about this viewer's response to its own actions (no causal claims)."""
    L = dict(prev)
    by_type: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for a in acts:
        by_type[a["type"]][a["status"]] += 1
    msgs: dict[str, dict[str, Any]] = {}
    notes = []
    for e in events:
        p = e.get("properties") or {}
        aid = p.get("action_id")
        if aid not in my_ids:
            continue
        ch = p.get("channel")
        m = msgs.setdefault(ch, {"delivered": 0, "clicked": 0, "failed": {}})
        if e["event"] == "message_delivered":
            m["delivered"] += 1
        elif e["event"] == "message_clicked":
            m["clicked"] += 1
        elif e["event"] == "message_failed":
            r = p.get("reason")
            m["failed"][r] = m["failed"].get(r, 0) + 1
            if r == "token_invalid":
                L["push_token_invalid"] = True
                L["push_token_ref"] = f"event:{e['event_id']}"
                L["push_token_at"] = e["received_at"]
                notes.append("push token invalid: future push suppressed for this viewer")
    opt = [e for e in events if e["event"] == "notification_opt_out"]
    if opt:
        notes.append(f"opted out of {sorted({(e.get('properties') or {}).get('channel') for e in opt})} after the replay day")
    shown = [x for x in exposures if x["kind"] == "page_shown"]
    pages = sum(1 for a in acts if a["type"] == "home_page")
    if pages and not shown:
        notes.append("published page(s) were never shown: viewer did not reach a home page while a page was live")
    L.update({"actions": {k: dict(v) for k, v in by_type.items()}, "messages": msgs,
              "pages": {"published": pages, "shown_personalised": len(shown)}, "notes": notes})
    return L
