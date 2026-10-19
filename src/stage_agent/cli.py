"""Command-line entry point.

    python -m stage_agent run                    # replay + simulator + judging + reports (mock model, no API key)
    python -m stage_agent run --seeds a,b,c      # first seed is the headline; others are sensitivity runs
    python -m stage_agent inventory              # data-quality inventory only
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .config import AgentConfig, PackPaths, default_pack_root
from .data_quality import measure, render_md
from .goal_planner import judge_success  # noqa: F401  (re-exported for tests)
from .impact_reporter import audit_actions, pilot_audit, render_impact_md, replay_itt, triggered_users
from .loader import read_jsonl, write_jsonl
from .outcome_judge import _exposures, _judge_goal, ingest_outcome_events, judge_all
from .replay import build_world, export_outputs, export_viewer_state, run_replay
from .scheduler import parse_ts
from .telemetry import setup_logging

DEFAULT_SEEDS = "oua-practice,seed-a,seed-b,seed-c,seed-d"
GENERATED = ("decisions.jsonl", "actions.jsonl", "action_log.jsonl", "audit_log.jsonl", "outcomes.jsonl",
             "latency_report.json", "impact_report.md", "final_report.md", "data_quality_report.md",
             "data_quality.json", "impact.json", "agent.log", "state.db", "state.db-wal", "state.db-shm",
             "viewer_state", "sim", "sim_seeds", "run_summary.json")


def run_simulator(paths: PackPaths, actions: Path, out: Path, seed: str) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(paths.simulator), "--pack", str(paths.root), "--actions", str(actions),
           "--out", str(out), "--seed", seed]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"simulator failed ({proc.returncode}): {proc.stderr[-2000:]}")
    with open(out / "simulator_report.json", encoding="utf-8") as f:
        rep = json.load(f)
    rep["_command"] = " ".join(cmd)
    rep["_stderr"] = proc.stderr.strip()[-300:]
    return rep


def judge_in_memory(world, events_path: Path) -> list[dict[str, Any]]:
    """Judge all goals against one simulator output without touching persisted outcomes (seed sensitivity)."""
    by_user: dict[str, list] = defaultdict(list)
    horizon = None
    for e in read_jsonl(events_path):
        by_user[e["user_id"]].append(e)
        t = parse_ts(e["received_at"])
        horizon = t if horizon is None or t > horizon else horizon
    acts = defaultdict(set)
    for a in world.store.all_actions():
        acts[a["user_id"]].add(a["action_id"])
    out = []
    for g in world.store.all_goals():
        evs = sorted(by_user.get(g["user_id"], []), key=lambda e: (e["received_at"], e["event_id"]))
        out.append(_judge_goal(g, evs, world.catalogue, horizon, horizon, _exposures(evs, acts[g["user_id"]])))
    return out


def cmd_run(a: argparse.Namespace) -> int:
    t0 = time.perf_counter()
    pack = Path(a.pack).resolve()
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if not a.keep:
        for name in GENERATED:
            p = out / name
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
    setup_logging(str(out / "agent.log"))
    cfg = AgentConfig.load(a.config)
    paths = PackPaths(pack)
    db = Path(a.db).resolve() if a.db else out / "state.db"
    print(f"[1/7] loading pack {pack} into {db}", flush=True)
    world = build_world(pack, db, cfg)
    print(f"      ingest: {world.ingest_stats}", flush=True)
    print("[2/7] replaying triggers, live and control events", flush=True)
    result = run_replay(world)
    world.store.set_meta("replay_end", result["replay_end"] or "")
    summary = export_outputs(world, result, out)
    lat = summary["latency"]
    print(f"      decisions={summary['decisions']} actions_exported={summary['actions_exported']} "
          f"p95={lat['all_decisions'].get('p95_ms')}ms over60s={lat['over_60s']['count']} "
          f"already_decided={result['already_decided']}", flush=True)
    seeds = [s for s in a.seeds.split(",") if s]
    print(f"[3/7] running the supplied simulator for seeds {seeds}", flush=True)
    sim_reports = {}
    for i, seed in enumerate(seeds):
        sdir = out / "sim" if i == 0 else out / "sim_seeds" / seed
        sim_reports[seed] = run_simulator(paths, out / "actions.jsonl", sdir, seed)
    print("[4/7] judging goal outcomes", flush=True)
    arms = {u: arm for u, arm in world.arms.items()}
    trig = triggered_users(paths.triggers)
    seed_rows = []
    for i, seed in enumerate(seeds[1:], 1):
        ev = out / "sim_seeds" / seed / "outcome_events.jsonl"
        r = replay_itt(arms, trig, judge_in_memory(world, ev), None)
        seed_rows.append({"seed": seed, "primary": r["primary"]})
        if not a.keep_seed_events:  # reproducible with the same seed; simulator_report.json is kept as proof
            ev.unlink()
    ingest_outcome_events(world.store, out / "sim" / "outcome_events.jsonl")
    outcomes = judge_all(world.store, world.catalogue)
    write_jsonl(out / "outcomes.jsonl", outcomes)
    print(f"      outcomes={len(outcomes)} {dict(Counter(o['result'] for o in outcomes))}", flush=True)
    print("[5/7] impact (randomized ITT) and pilot audit", flush=True)
    itt = replay_itt(arms, trig, outcomes, out / "sim" / "outcome_events.jsonl")
    seed_rows.insert(0, {"seed": seeds[0], "primary": itt["primary"]})
    pilot = pilot_audit(paths.pilot_assignment, paths.pilot_outcomes)
    exported = list(read_jsonl(out / "actions.jsonl"))
    policy = audit_actions([x for x in exported if x.get("status") != "superseded"] , arms, world.catalogue,
                           world.pack, paths.triggers)
    md = render_impact_md(itt, pilot, seed_rows, sim_reports[seeds[0]], seeds[0], policy)
    (out / "impact_report.md").write_text(md, encoding="utf-8")
    with open(out / "impact.json", "w", encoding="utf-8") as f:
        json.dump({"itt": itt, "pilot": pilot, "seeds": seed_rows, "policy_audit": policy,
                   "simulator": {s: {k: v for k, v in r.items() if k != "policy_problems"} for s, r in sim_reports.items()}},
                  f, indent=2, default=str)
    print("[6/7] viewer state and data-quality inventory", flush=True)
    n_users = export_viewer_state(world, out)
    dq = measure(paths, cfg)
    with open(out / "data_quality.json", "w", encoding="utf-8") as f:
        json.dump(dq, f, indent=2, default=str)
    (out / "data_quality_report.md").write_text(render_md(dq), encoding="utf-8")
    print("[7/7] final report", flush=True)
    from .final_report import render_final
    final = render_final(out, summary, result, itt, pilot, seed_rows, sim_reports[seeds[0]], policy, outcomes, dq,
                         world, seeds[0], time.perf_counter() - t0)
    (out / "final_report.md").write_text(final, encoding="utf-8")
    run_summary = {"pack": str(pack), "seeds": seeds, "decisions": summary["decisions"],
                   "actions": summary["actions_exported"], "viewer_state_files": n_users,
                   "primary": itt["primary"], "slo_met": lat["slo_met"], "wall_s": round(time.perf_counter() - t0, 1),
                   "simulator_ran": {s: r["events_written"] for s, r in sim_reports.items()}}
    with open(out / "run_summary.json", "w", encoding="utf-8") as f:
        json.dump(run_summary, f, indent=2, default=str)
    p = itt["primary"]
    print(f"done in {run_summary['wall_s']} s: agent {p['agent_k']}/{p['agent_n']} vs holdout "
          f"{p['holdout_k']}/{p['holdout_n']}, diff {p['diff_pp']:+.2f} pp "
          f"[{p['ci_lo_pp']:+.2f}, {p['ci_hi_pp']:+.2f}]; outputs in {out}", flush=True)
    world.store.close()
    return 0


def cmd_inventory(a: argparse.Namespace) -> int:
    m = measure(PackPaths(Path(a.pack)), AgentConfig.load(a.config))
    print(render_md(m))
    return 0


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(prog="stage_agent", description="One User, One Agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="full replay -> simulator -> judging -> reports")
    r.add_argument("--pack", default=str(default_pack_root()), help="data pack root (default: ./pack or $STAGE_PACK)")
    r.add_argument("--out", default=str(root / "sample_run"))
    r.add_argument("--db", default=None, help="SQLite path (default: <out>/state.db)")
    r.add_argument("--config", default=str(root / "config" / "agent_config.json"))
    r.add_argument("--seeds", default=DEFAULT_SEEDS, help="comma-separated simulator seeds; first is the headline")
    r.add_argument("--keep", action="store_true", help="keep existing outputs/DB (idempotent rerun)")
    r.add_argument("--keep-seed-events", action="store_true",
                   help="keep outcome_events.jsonl of the sensitivity seeds (large; reproducible)")
    r.set_defaults(fn=cmd_run)
    i = sub.add_parser("inventory", help="print the measured data-quality inventory")
    i.add_argument("--pack", default=str(default_pack_root()))
    i.add_argument("--config", default=str(root / "config" / "agent_config.json"))
    i.set_defaults(fn=cmd_inventory)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
