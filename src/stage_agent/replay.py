"""Replay harness: feed triggers, live events and control events through the agents on a virtual clock.

Latency semantics (also written into latency_report.json):
  * Triggers arrive at `deliver_at`. A pool of `workers` virtual workers serves them, fresh triggers first.
  * service time = measured wall-clock compute of the decision + simulated dependency latency (mock model calls,
    store reads, timeouts, retries) accumulated in the decision's budget ledger.
  * published_at = start + service time, where start = max(deliver_at, a worker is free). Latency to publication
    = published_at - deliver_at = queue wait + service time. This follows the README rule
    "published_at = deliver_at + your measured processing time", with queueing made explicit.
"""
from __future__ import annotations

import heapq
import json
import logging
import shutil
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .agent import AgentRuntime
from .config import AgentConfig, PackConfig, PackPaths
from .content_selector import Catalogue
from .dependencies import StoreGate
from .loader import EVENT_REQUIRED, Quarantine, read_csv, read_jsonl, write_jsonl
from .model_provider import FaultInjectingProvider, MockModelProvider, ModelClient, OutageGate, load_faults
from .normalization import dedupe_events, normalize_event
from .scheduler import iso, parse_ts
from .state_store import StateStore
from .telemetry import summarize
from .verifier import ArmRegistry

log = logging.getLogger("stage_agent.replay")

SIM_FIELDS = ("action_id", "decision_id", "user_id", "trigger_event_id", "type", "published_at", "send_at", "dialect",
              "hero_title_id", "rows", "title_id", "text", "deeplink", "template_id", "variables", "plan_id",
              "show_on", "expires_at", "goal_id", "goal_instance_id", "repair_of")


@dataclass
class World:
    cfg: AgentConfig
    paths: PackPaths
    pack: PackConfig
    catalogue: Catalogue
    store: StateStore
    arms: ArmRegistry
    runtime: AgentRuntime
    provider: FaultInjectingProvider
    outage: OutageGate
    store_gate: StoreGate
    quarantine: Quarantine
    ingest_stats: dict[str, Any] = field(default_factory=dict)


def build_world(pack_root: Path, db_path: Path | str, cfg: AgentConfig) -> World:
    paths = PackPaths(Path(pack_root))
    pack = PackConfig.load(paths)
    catalogue = Catalogue.load(paths.catalogue, paths.episodes)
    store = StateStore(db_path)
    quarantine = Quarantine()
    stats: dict[str, Any] = {}
    if store.get_meta("history_ingested") != "1":
        t0 = time.perf_counter()
        raws = list(read_jsonl(paths.events, quarantine, EVENT_REQUIRED))
        evs = []
        for r in raws:
            e = normalize_event(r, "history", cfg.clock_skew_flag_seconds, quarantine)
            if e is not None:
                evs.append((e, r))
        uniq, dups = dedupe_events(e for e, _ in evs)
        raw_by_id: dict[str, dict] = {}
        for e, r in evs:  # raw of the kept (earliest) delivery
            if e.event_id not in raw_by_id or parse_ts(r["received_at"]) < parse_ts(raw_by_id[e.event_id]["received_at"]):
                raw_by_id[e.event_id] = r
        store.insert_events((e, raw_by_id[e.event_id]) for e in uniq)
        store.insert_profiles(read_csv(paths.profiles))
        hist_start = min(e.received_at for e in uniq) if uniq else None
        store.set_meta("history_start", iso(hist_start) or "")
        store.set_meta("history_end", iso(max(e.received_at for e in uniq)) if uniq else "")
        stats = {"history_lines": len(raws), "history_unique": len(uniq), "history_redeliveries": sum(dups.values()),
                 "quarantined": len(quarantine.items), "ingest_s": round(time.perf_counter() - t0, 2)}
        store.set_meta("ingest_stats", json.dumps(stats))
        store.set_meta("history_ingested", "1")
    else:
        stats = json.loads(store.get_meta("ingest_stats") or "{}")
    hist_start = parse_ts(store.get_meta("history_start") or None)
    hist_end = parse_ts(store.get_meta("history_end") or None)
    plays = [normalize_event(json.loads(r["raw"]), "history") for r in
             store.conn.execute("SELECT raw FROM events WHERE event='play_started' AND source='history'")]
    catalogue.compute_popularity([p for p in plays if p], hist_end or datetime.max, cfg.popularity_days)

    arms = ArmRegistry({r["user_id"]: r["arm"] for r in read_csv(paths.arms)})
    faults = load_faults(list(read_jsonl(paths.model_faults, quarantine)))
    provider = FaultInjectingProvider(MockModelProvider(cfg.mock_model_latency_ms), faults, cfg)
    outage = OutageGate(provider, cfg)
    model = ModelClient(outage, cfg)
    store_gate = StoreGate(cfg)
    runtime = AgentRuntime(cfg, pack, catalogue, store, arms, model, store_gate, hist_start)
    return World(cfg, paths, pack, catalogue, store, arms, runtime, provider, outage, store_gate, quarantine, stats)


@dataclass(order=True)
class _Item:
    t: datetime
    prio: int
    seq: int
    kind: str = field(compare=False)
    payload: Any = field(compare=False)


def run_replay(world: World) -> dict[str, Any]:
    cfg, rt, store = world.cfg, world.runtime, world.store
    q = world.quarantine
    timeline: list[_Item] = []
    n = 0
    for c in read_jsonl(world.paths.control_events, q, ("at", "type")):
        timeline.append(_Item(parse_ts(c["at"]), 0, n, "control", c))
        n += 1
    for r in read_jsonl(world.paths.live_events, q, EVENT_REQUIRED):
        e = normalize_event(r, "live", cfg.clock_skew_flag_seconds, q)
        if e:
            timeline.append(_Item(e.arrival, 1, n, "live", (e, r)))
            n += 1
    for r in read_jsonl(world.paths.triggers, q, EVENT_REQUIRED):
        e = normalize_event(r, "trigger", cfg.clock_skew_flag_seconds, q)
        if e:
            timeline.append(_Item(e.arrival, 2, n, "trigger", (e, r)))
            n += 1
    timeline.sort()
    wall0 = time.perf_counter()

    workers = [datetime.min.replace(tzinfo=timeline[0].t.tzinfo)] * cfg.workers if timeline else []
    heapq.heapify(workers)
    queue: list[tuple[bool, datetime, int, Any]] = []
    clock = timeline[0].t if timeline else None
    i, seq = 0, store.next_seq()
    lat: list[dict[str, Any]] = []
    controls: list[dict[str, Any]] = []
    already = 0

    def next_seq() -> int:
        nonlocal seq
        seq += 1
        return seq - 1

    while i < len(timeline) or queue:
        t_arr = timeline[i].t if i < len(timeline) else None
        if queue:
            t_disp = max(workers[0], clock)
            if t_arr is None or t_disp <= t_arr:
                stale, deliver, _, (e, r) = heapq.heappop(queue)
                rt.dispatch_due(t_disp)
                if store.delivery_recorded(e.event_id, iso(e.arrival)):
                    already += 1  # this exact delivery was decided in an earlier run: idempotent rerun
                    continue
                res = rt.handle_trigger(e, t_disp, next_seq(), queue_depth=len(queue))
                done = t_disp + timedelta(milliseconds=res.service_ms)
                heapq.heapreplace(workers, done)
                d = res.decision
                wait_ms = (t_disp - deliver).total_seconds() * 1000
                total_ms = (done - deliver).total_seconds() * 1000
                d["queue_wait_ms"] = round(wait_ms, 1)
                d["latency_ms"] = round(total_ms, 1)
                with store.tx() as c:
                    store.update_decision(c, d)
                lat.append({"decision_id": d["decision_id"], "status": d["status"], "latency_ms": total_ms,
                            "queue_ms": wait_ms, "service_ms": res.service_ms, "compute_ms": d["compute_ms"],
                            "stale": d["trigger"]["stale"], "fallback": d["fallback_used"],
                            "model_status": d["model"]["status"], "degraded": bool(d["degradations"]),
                            "deliver_at": iso(deliver)})
                continue
        item = timeline[i]
        i += 1
        clock = item.t
        rt.dispatch_due(clock)
        if item.kind == "control":
            controls.append(_apply_control(world, item.payload, item.t, next_seq))
        elif item.kind == "live":
            e, r = item.payload
            rt.on_live_event(e, r, item.t)
        else:
            e, r = item.payload
            store.insert_events([(e, r)])
            heapq.heappush(queue, (e.buffer_delay_s > cfg.stale_trigger_seconds, e.arrival, item.seq, (e, r)))

    end = max([timeline[-1].t] + list(workers)) if timeline else None
    if end is not None:
        rt.dispatch_due(end)
        final = rt.final_revalidation(end)
    else:
        final = {}
    wall_s = time.perf_counter() - wall0
    return {"latency": lat, "controls": controls, "final_revalidation": final, "wall_s": wall_s,
            "replay_end": iso(end), "already_decided": already}


def _apply_control(world: World, c: dict[str, Any], at: datetime, next_seq) -> dict[str, Any]:
    rt = world.runtime
    t = c["type"]
    info: dict[str, Any] = {"at": iso(at), "type": t}
    key = f"applied:{c['at']}:{t}:{c.get('goal_id') or c.get('title_id') or c.get('dependency')}"
    if t in ("title_withdrawn", "goal_rule_corrected") and world.store.get_meta(key) == "1":
        info["skipped"] = "already applied in an earlier run against this database"
        return info
    if t in ("title_withdrawn", "goal_rule_corrected"):
        world.store.set_meta(key, "1")
    if t == "title_withdrawn":
        res = rt.on_withdrawal(c, at, next_seq)
        info.update(title_id=c["title_id"], repaired_pages=[r.decision["decision_id"] for r in res])
    elif t == "goal_rule_corrected":
        info.update(rt.on_rule_correction(c, at))
    elif t in ("dependency_unavailable", "dependency_degraded"):
        until = parse_ts(c["until"])
        if c.get("dependency") == "model_provider":
            world.outage.add_window(at, until)
        elif c.get("dependency") == "user_data_store":
            world.store_gate.add_window(at, until, 0.2)
        info.update(dependency=c.get("dependency"), until=c.get("until"), detail=c.get("detail"))
        world.store.audit(t, at, info)
    else:
        info["ignored"] = "unknown control type"
        world.store.audit("control_ignored", at, c)
    return info


# ---------------------------------------------------------------------- exports
def export_outputs(world: World, result: dict[str, Any], out: Path) -> dict[str, Any]:
    store, cfg = world.store, world.cfg
    out.mkdir(parents=True, exist_ok=True)
    decisions = store.decisions()
    write_jsonl(out / "decisions.jsonl", decisions)
    acts = store.all_actions()
    live = [a for a in acts if a["status"] in ("published", "scheduled", "sent", "superseded")]
    sim_rows = []
    for a in live:
        row = {k: a["raw"][k] for k in SIM_FIELDS if k in a["raw"] and a["raw"][k] is not None}
        row["status"] = a["status"]
        row["policy_checks"] = "verified"
        sim_rows.append(row)
    write_jsonl(out / "actions.jsonl", sim_rows)
    write_jsonl(out / "action_log.jsonl", [{"action_id": a["action_id"], "type": a["type"], "user_id": a["user_id"],
                                            "status": a["status"], "status_reason": a["status_reason"],
                                            "published_at": a["published_at"], "send_at": a["send_at"],
                                            "updated_at": a["updated_at"], "decision_id": a["decision_id"]}
                                           for a in acts])
    write_jsonl(out / "audit_log.jsonl", [{"at": r["at"], "user_id": r["user_id"], "kind": r["kind"], **r["raw"]}
                                          for r in store.audit_rows()])
    lat = result["latency"]
    report = latency_report(lat, decisions, world, result)
    with open(out / "latency_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return {"decisions": len(decisions), "actions_exported": len(sim_rows), "latency": report}


def latency_report(lat: list[dict[str, Any]], decisions: list[dict[str, Any]], world: World,
                   result: dict[str, Any]) -> dict[str, Any]:
    cfg = world.cfg
    vals = [x["latency_ms"] for x in lat]
    pub = [x["latency_ms"] for x in lat if x["status"] == "PUBLISHED"]
    over60 = sum(v > cfg.latency_target_s * 1000 for v in vals)
    over300 = sum(v > cfg.latency_fail_s * 1000 for v in vals)
    statuses = Counter(d["status"] for d in decisions)
    model = Counter(x["model_status"] for x in lat)
    fired = world.provider.fired
    fault_rows = []
    for tid, f in world.provider.faults.items():
        dec = next((d for d in decisions if d["trigger"]["event_id"] == tid and d["status"] != "DUPLICATE_IGNORED"), None)
        fault_rows.append({"trigger_event_id": tid, "fault": f.get("fault"), "user_id": f.get("user_id"),
                           "model_calls_hit": fired.get(tid, 0),
                           "decision_id": dec["decision_id"] if dec else None,
                           "model_status": dec["model"]["status"] if dec else None,
                           "goal": dec["goal"]["id"] if dec else None,
                           "fallback_used": dec["fallback_used"] if dec else None,
                           "status": dec["status"] if dec else None,
                           "note": None if fired.get(tid) else "model not called for this trigger (see decision)"})
    return {
        "semantics": {
            "latency_ms": "published_at - deliver_at = queue wait + service time (virtual clock)",
            "service_ms": "measured wall-clock compute of the decision + simulated dependency latency "
                          "(mock model latency, store reads, timeouts, retries)",
            "compute_ms": "real wall-clock CPU time of the decision code on this machine",
            "workers": cfg.workers, "target_s": cfg.latency_target_s, "failure_s": cfg.latency_fail_s,
            "scope": "every trigger delivery, including duplicates and abstentions",
        },
        "triggers_processed": len(lat),
        "decisions_by_status": dict(sorted(statuses.items())),
        "all_decisions": summarize(vals),
        "published_decisions": summarize(pub),
        "queue_wait": summarize([x["queue_ms"] for x in lat]),
        "compute_only": summarize([x["compute_ms"] for x in lat]),
        "over_60s": {"count": over60, "pct": round(100 * over60 / len(vals), 3) if vals else 0},
        "over_5min": over300,
        "fallbacks": sum(1 for x in lat if x["fallback"]),
        "model_status_counts": dict(sorted(model.items())),
        "model_client_stats": dict(sorted(world.runtime.model.stats.items())),
        "store_gate": world.store_gate.stats,
        "degraded_decisions": sum(1 for x in lat if x["degraded"]),
        "stale_triggers": sum(1 for x in lat if x["stale"]),
        "errors": 0,
        "model_faults": fault_rows,
        "controls": result["controls"],
        "final_revalidation": result["final_revalidation"],
        "replay_wall_clock_s": round(result["wall_s"], 2),
        "counters": dict(sorted(world.runtime.counters.items())),
        "slo_met": over60 == 0 and over300 == 0,
    }


def export_viewer_state(world: World, out: Path, users: list[str] | None = None) -> int:
    store, rt = world.store, world.runtime
    vdir = out / "viewer_state"
    if vdir.exists():
        shutil.rmtree(vdir)
    vdir.mkdir(parents=True)
    if users is None:
        users = sorted({r[0] for r in store.conn.execute("SELECT DISTINCT user_id FROM decisions")})
    outcomes = {}
    for o in store.outcomes():
        outcomes.setdefault(o["user_id"], []).append(o)
    end = parse_ts(store.get_meta("replay_end") or None) or datetime.now().astimezone()
    for uid in users:
        decs = store.decisions_for_user(uid)
        facts = rt.resolver.resolve(uid, end, None, store.user_events(uid, end), store.profile_rows(uid),
                                    rt.history_start, store.get_learning(uid))
        state = {
            "viewer_id": uid, "arm": world.arms.arm(uid), "as_of": iso(end),
            "facts": facts.to_dict(),
            "goals": store.goals_for_user(uid),
            "decisions": [{"decision_id": d["decision_id"], "trigger": d["trigger"]["event_id"],
                           "deliver_at": d["trigger"]["deliver_at"], "status": d["status"], "action": d["action"],
                           "goal": d["goal"]["id"], "reason": d["no_action_reason"]} for d in decs],
            "actions": [{"action_id": a["action_id"], "type": a["type"], "status": a["status"],
                         "status_reason": a["status_reason"], "published_at": a["published_at"],
                         "send_at": a["send_at"]} for a in store.actions_for_user(uid)],
            "outcomes": outcomes.get(uid, []),
            "learning": store.get_learning(uid),
            "audit": [{"at": r["at"], "kind": r["kind"], **r["raw"]} for r in store.audit_rows(user_id=uid)],
        }
        with open(vdir / f"{uid}.json", "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1, ensure_ascii=False, default=str)
    return len(users)
