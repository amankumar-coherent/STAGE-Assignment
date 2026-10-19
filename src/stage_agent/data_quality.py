"""Measure the pack's data-quality issues. Every count is computed from the files at run time."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any

from .config import AgentConfig, PackPaths
from .goal_planner import GoalRuleBook
from .loader import Quarantine, read_csv, read_jsonl
from .normalization import normalize_dialect, normalize_event, parse_campaign_deeplink, parse_title
from .scheduler import IST, iso, parse_ts


def measure(paths: PackPaths, cfg: AgentConfig) -> dict[str, Any]:
    q = Quarantine()
    m: dict[str, Any] = {}
    cat = [parse_title(r) for r in read_csv(paths.catalogue)]
    cat_dialects = {t.dialect for t in cat}
    ids: Counter = Counter()
    copies: dict[str, set] = defaultdict(set)
    buffered, skew, bad_dialect, max_buf = 0, 0, Counter(), 0.0
    n = 0
    for r in read_jsonl(paths.events, q):
        n += 1
        ids[r["event_id"]] += 1
        copies[r["event_id"]].add(r["received_at"])
        e = normalize_event(r, "history", cfg.clock_skew_flag_seconds, q)
        if e is None:
            continue
        if e.buffer_delay_s > cfg.stale_trigger_seconds:
            buffered += 1
            max_buf = max(max_buf, e.buffer_delay_s)
        if "phone_clock_wrong" in e.anomalies:
            skew += 1
        d = r.get("app_dialect")
        if d is None or d not in cat_dialects:
            bad_dialect[str(d)] += 1
    dup_ids = [k for k, v in ids.items() if v > 1]
    m["events"] = {"lines": n, "duplicate_event_ids": len(dup_ids), "redelivered_rows": sum(ids[k] - 1 for k in dup_ids),
                   "duplicates_differing_only_in_received_at": sum(1 for k in dup_ids if len(copies[k]) > 1),
                   "buffered_over_stale_threshold": buffered, "max_buffer_hours": round(max_buf / 3600, 1),
                   "phone_clock_wrong": skew, "app_dialect_outside_catalogue": dict(bad_dialect),
                   "quarantined_lines": len(q.items)}
    prof = read_csv(paths.profiles)
    pc: dict[str, list] = defaultdict(list)
    for r in prof:
        pc[r["user_id"]].append(r)
    dups = {u: rs for u, rs in pc.items() if len(rs) > 1}
    conflict_fields = Counter()
    for rs in dups.values():
        for f in ("subscription_status", "notifications_opt_in", "whatsapp_opt_in", "preferred_dialect"):
            if len({r[f] for r in rs}) > 1:
                conflict_fields[f] += 1
    arms = {r["user_id"]: r["arm"] for r in read_csv(paths.arms)}
    trig = list(read_jsonl(paths.triggers, q))
    trig_users = {e["user_id"] for e in trig}
    m["profiles"] = {"rows": len(prof), "users": len(pc), "users_with_duplicate_rows": len(dups),
                     "duplicate_rows_conflicting_on": dict(conflict_fields),
                     "empty_preferred_dialect": sum(1 for r in prof if not r["preferred_dialect"]),
                     "preferred_dialect_outside_catalogue": sum(1 for r in prof if r["preferred_dialect"]
                                                                and r["preferred_dialect"] not in cat_dialects),
                     "arms_users_without_profile": len(set(arms) - set(pc)),
                     "triggered_users_without_profile": len(trig_users - set(pc)),
                     "snapshot_at_values": sorted({r["snapshot_at"] for r in prof})}
    tids = Counter(e["event_id"] for e in trig)
    newest = {u: max(rs, key=lambda r: r["updated_at"]) for u, rs in pc.items()}
    app_vs_prof = app_vs_link = three = 0
    stale_t, skew_t = 0, 0
    minute = Counter()
    for e in trig:
        ev = normalize_event(e, "trigger", cfg.clock_skew_flag_seconds)
        if ev and ev.buffer_delay_s > cfg.stale_trigger_seconds:
            stale_t += 1
        if ev and "phone_clock_wrong" in ev.anomalies:
            skew_t += 1
        minute[parse_ts(e["deliver_at"]).astimezone(IST).strftime("%H:%M")] += 1
        app = normalize_dialect(e.get("app_dialect"))
        p = newest.get(e["user_id"], {}).get("preferred_dialect") or None
        link = parse_campaign_deeplink((e.get("properties") or {}).get("deeplink"))
        ld = link["dialect"] if link else None
        if app and p and app != p:
            app_vs_prof += 1
        if app and ld and app != ld:
            app_vs_link += 1
        if app and p and ld and len({app, p, ld}) == 3:
            three += 1
    per_min = sorted(minute.values())
    m["triggers"] = {"lines": len(trig), "users": len(trig_users), "redelivered": sum(v - 1 for v in tids.values()),
                     "stale_offline_buffered": stale_t, "phone_clock_wrong": skew_t,
                     "app_vs_profile_dialect_conflicts": app_vs_prof, "app_vs_campaign_link_conflicts": app_vs_link,
                     "three_way_dialect_conflicts": three,
                     "peak_minute": max(minute.items(), key=lambda kv: kv[1]) if minute else None,
                     "median_per_active_minute": per_min[len(per_min) // 2] if per_min else 0,
                     "by_arm": dict(Counter(arms.get(u, "unknown") for u in trig_users))}
    live = list(read_jsonl(paths.live_events, q))
    lid = Counter(e["event_id"] for e in live)
    m["live_events"] = {"lines": len(live), "redelivered": sum(v - 1 for v in lid.values()),
                        "by_event": dict(Counter(e["event"] for e in live).most_common())}
    slugs: dict[str, set] = defaultdict(set)
    for t in cat:
        slugs[(t.content_slug, t.dialect)].add(t.title_id)
    start = min((parse_ts(e["deliver_at"]) for e in trig), default=None)
    end = start + timedelta(days=1) if start else None
    m["catalogue"] = {"titles": len(cat), "content_slugs": len({t.content_slug for t in cat}),
                      "slugs_with_language_variants": sum(1 for v in slugs.values() if len(v) > 1),
                      "titles_by_dialect": dict(sorted(Counter(t.dialect for t in cat).items())),
                      "unavailable_at_replay_start": sum(1 for t in cat if start and not (
                          t.available_from and t.available_from <= start and (t.available_until is None or start < t.available_until))),
                      "licence_ends_during_replay_day": sum(1 for t in cat if start and t.available_until
                                                           and start <= t.available_until < end),
                      "instruction_like_descriptions": {t.title_id: t.suspicious_reason for t in cat if t.suspicious}}
    with open(paths.goals, encoding="utf-8") as f:
        book = GoalRuleBook(json.load(f))
    m["goals"] = {"unmeasurable": {g: r.unmeasurable_reason for g, r in book.rules.items() if not r.measurable},
                  "underspecified_interpreted": {
                      "G06": "'recently lapsed' has no threshold -> lapse evidenced within 30 days",
                      "G02": "'never completed' only supported when history covers the whole lifetime",
                      "G10": "'engages meaningfully' has no event -> never selected"}}
    m["controls"] = list(read_jsonl(paths.control_events, q))
    m["model_faults"] = [{k: v for k, v in f.items() if k != "model_output"} for f in read_jsonl(paths.model_faults, q)]
    pilot = read_csv(paths.pilot_outcomes)
    m["pilot"] = {"rows": len(pilot), "served": sum(r["served"] == "true" for r in pilot),
                  "arms": dict(Counter(r["arm"] for r in pilot))}
    m["quarantine"] = q.items[:20]
    return m


def render_md(m: dict[str, Any]) -> str:
    e, p, t, c = m["events"], m["profiles"], m["triggers"], m["catalogue"]
    rows = [
        ("Duplicate event IDs (history)", f"{e['duplicate_event_ids']} ids / {e['redelivered_rows']} extra rows; "
         f"{e['duplicates_differing_only_in_received_at']} differ in received_at",
         "event_id PK; first delivery (earliest received_at) kept", "test_duplicate_trigger_no_duplicate_action"),
        ("Duplicate trigger deliveries (replay)", f"{t['redelivered']}",
         "processed_triggers PK + action idempotency key; recorded as DUPLICATE_IGNORED", "test_duplicate_trigger_no_duplicate_action"),
        ("Offline-buffered events (sent-occurred > threshold)", f"history {e['buffered_over_stale_threshold']} "
         f"(max {e['max_buffer_hours']} h); triggers {t['stale_offline_buffered']}",
         "trigger -> no goal commit, no action; event kept for history at server-anchored time", "test_stale_buffered_trigger"),
        ("Phone clock wrong (|received-sent| > 10 min)", f"history {e['phone_clock_wrong']}; triggers {t['phone_clock_wrong']}",
         "age = sent-occurred (skew-free); occurrence = received - age", "test_clock_skew_does_not_make_fresh_trigger_stale"),
        ("app_dialect outside catalogue (None / NA / d7)", str(e["app_dialect_outside_catalogue"]),
         "no page/message: the page the app requests is unknown or has no content", "test_unknown_dialect_abstains"),
        ("Duplicate profile rows", f"{p['users_with_duplicate_rows']} users; conflicting on {p['duplicate_rows_conflicting_on']}",
         "newest updated_at wins; older rows kept as conflicts; 'never' contradicted by paid evidence = unsupported",
         "test_duplicate_profile_rows_newest_wins"),
        ("Missing profiles", f"triggered users: {p['triggered_users_without_profile']}; arms users: {p['arms_users_without_profile']}",
         "facts rebuilt from events; no consent inferred; subscription only if lifetime observed", "test_missing_profile"),
        ("Dialect conflicts on triggers", f"app vs profile {t['app_vs_profile_dialect_conflicts']}; app vs campaign "
         f"{t['app_vs_campaign_link_conflicts']}; three-way {t['three_way_dialect_conflicts']}",
         "current app setting used; others listed in decision.dialect.conflicts", "test_three_way_dialect_conflict"),
        ("Language variants sharing a content_slug", f"{c['slugs_with_language_variants']} slugs",
         "one title per slug per page (app-language variant preferred)", "test_duplicate_slug_removed"),
        ("Unavailable / expiring titles", f"{c['unavailable_at_replay_start']} unavailable at replay start; "
         f"{c['licence_ends_during_replay_day']} licences end during the replay day",
         "titles must be available for the whole page TTL / at send time", "test_withdrawn_title_never_published"),
        ("Instruction-like catalogue text", ", ".join(c["instruction_like_descriptions"]) or "none",
         "flagged in logs, title quarantined, text never enters prompts", "test_prompt_injection_ignored"),
        ("Unmeasurable goals", "; ".join(f"{g}: {r}" for g, r in m["goals"]["unmeasurable"].items()),
         "rejected before eligibility; never selected", "test_unmeasurable_goal_never_selected"),
        ("Underspecified goals", "; ".join(f"{g}: {r}" for g, r in m["goals"]["underspecified_interpreted"].items()),
         "conservative interpretation documented", "test_g06_recent_lapse_interpretation"),
        ("Pilot headline compares served vs everyone else", f"{m['pilot']['served']} served of {m['pilot']['rows']}",
         "re-estimated by randomized arm (impact_report.md section 6)", "test_pilot_audit_reproduces_and_corrects"),
        ("Replay fault fixtures and corrections", f"{len(m['controls'])} control events, {len(m['model_faults'])} model faults",
         "see latency_report.json controls / model_faults", "test_model_timeout_bounded_fallback, test_corrected_goal_rule_only_affects_open_goals"),
    ]
    out = ["# Data-quality inventory (measured)", "",
           "Generated by `stage_agent.data_quality` from the pack at run time.", "",
           "| Issue | Measured | Handling | Test |", "|---|---|---|---|"]
    out += [f"| {a} | {b} | {c_} | `{d}` |" for a, b, c_, d in rows]
    out += ["", f"Trigger peak minute (IST): {t['peak_minute']}; median triggers per active minute: "
            f"{t['median_per_active_minute']}. Triggered users by arm: {t['by_arm']}.",
            f"Catalogue titles by dialect: {c['titles_by_dialect']} (d3 is thin; d7 has none).",
            f"Profile snapshot_at values: {p['snapshot_at_values']}; empty preferred_dialect: {p['empty_preferred_dialect']}.",
            f"Quarantined malformed lines: {e['quarantined_lines']}.", ""]
    return "\n".join(out)
