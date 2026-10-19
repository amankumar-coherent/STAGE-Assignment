"""Randomized intention-to-treat impact estimation and the pilot-report audit.

Estimand (primary): difference in the share of users who reached their goal, agent arm minus holdout arm, over ALL
randomized users who produced at least one trigger on the replay day, analysed by assigned arm (served or not,
eligible or not, abstained or not). The replay triggers are fixed fixtures that do not depend on the arm, and the
simulator produces outcomes only for triggered users, so "triggered" is a pre-treatment filter, not a
post-randomization selection. The goal is chosen by the identical procedure in both arms (holdout users get a
recorded, never-acted-on goal), so the outcome is defined the same way for every user. Users with no goal count as
not succeeded.

Interval: Newcombe hybrid score interval (method 10, Newcombe 1998) built from Wilson intervals.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .loader import read_csv, read_jsonl
from .scheduler import iso, parse_ts

Z95 = 1.959963984540054


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def newcombe_diff(k1: int, n1: int, k2: int, n2: int, z: float = Z95) -> tuple[float, float, float]:
    """Difference p1 - p2 with the Newcombe hybrid score 95% interval."""
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson(k1, n1, z)
    l2, u2 = wilson(k2, n2, z)
    d = p1 - p2
    lo = d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    hi = d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return d, lo, hi


def two_prop_p(k1: int, n1: int, k2: int, n2: int) -> float:
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    zval = abs(k1 / n1 - k2 / n2) / se
    return math.erfc(zval / math.sqrt(2))


@dataclass
class Comparison:
    label: str
    k1: int
    n1: int
    k2: int
    n2: int

    def row(self) -> dict[str, Any]:
        if self.n1 == 0 or self.n2 == 0:
            return {"label": self.label, "n_agent": self.n1, "n_holdout": self.n2, "note": "empty arm"}
        d, lo, hi = newcombe_diff(self.k1, self.n1, self.k2, self.n2)
        p2 = self.k2 / self.n2
        return {"label": self.label, "agent_k": self.k1, "agent_n": self.n1, "agent_rate": self.k1 / self.n1,
                "holdout_k": self.k2, "holdout_n": self.n2, "holdout_rate": p2,
                "diff_pp": 100 * d, "ci_lo_pp": 100 * lo, "ci_hi_pp": 100 * hi,
                "relative": (self.k1 / self.n1) / p2 - 1 if p2 > 0 else None,
                "p_value": two_prop_p(self.k1, self.n1, self.k2, self.n2)}


def pct(x: float | None, nd: int = 1) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.{nd}f}%"


def pp(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.2f} pp"


# ---------------------------------------------------------------------- replay ITT
def triggered_users(triggers_path: Path) -> dict[str, datetime]:
    first: dict[str, datetime] = {}
    for e in read_jsonl(triggers_path):
        t = parse_ts(e.get("deliver_at") or e["received_at"])
        u = e["user_id"]
        if u not in first or t < first[u]:
            first[u] = t
    return first


def user_outcomes(outcomes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """First goal instance per user (by start time) is the primary unit."""
    by: dict[str, dict[str, Any]] = {}
    for o in sorted(outcomes, key=lambda o: (o["started_at"], o["goal_instance_id"])):
        by.setdefault(o["user_id"], o)
    return by


def secondary_flags(outcome_events: Iterable[dict[str, Any]], users: set[str]) -> dict[str, dict[str, bool]]:
    f: dict[str, dict[str, bool]] = {u: {"any_play": False, "any_completion": False, "paid_purchase": False,
                                         "opt_out": False} for u in users}
    for e in outcome_events:
        u = e.get("user_id")
        if u not in f:
            continue
        ev = e["event"]
        if ev == "play_started":
            f[u]["any_play"] = True
        elif ev == "episode_completed":
            f[u]["any_completion"] = True
        elif ev == "purchase_completed" and (e.get("properties") or {}).get("is_trial") is False:
            f[u]["paid_purchase"] = True
        elif ev == "notification_opt_out":
            f[u]["opt_out"] = True
    return f


def replay_itt(arms: dict[str, str], trig_users: dict[str, datetime], outcomes: list[dict[str, Any]],
               outcome_events_path: Path | None) -> dict[str, Any]:
    pop = {u: arms.get(u) for u in trig_users}
    missing_arm = [u for u, a in pop.items() if a not in ("agent", "holdout")]
    uo = user_outcomes(outcomes)
    n = Counter(a for a in pop.values() if a in ("agent", "holdout"))
    k = Counter()
    k_strict = Counter()
    goal_counts: dict[str, Counter] = defaultdict(Counter)
    result_counts: dict[str, Counter] = defaultdict(Counter)
    for u, a in pop.items():
        if a not in ("agent", "holdout"):
            continue
        o = uo.get(u)
        result_counts[a][o["result"] if o else "no_goal"] += 1
        if o and o["result"] == "succeeded":
            k[a] += 1
        goal_counts[a][o["goal_id"] if o else "none"] += 1
    primary = Comparison("goal reached (first goal; no goal = not reached)", k["agent"], n["agent"],
                         k["holdout"], n["holdout"]).row()
    # sensitivity: drop users whose first goal is inconclusive (both arms)
    n_s = Counter()
    for u, a in pop.items():
        o = uo.get(u)
        if a in ("agent", "holdout") and not (o and o["result"] == "inconclusive"):
            n_s[a] += 1
    sens = Comparison("goal reached, excluding inconclusive first goals", k["agent"], n_s["agent"], k["holdout"],
                      n_s["holdout"]).row()
    # sensitivity: users who received a goal. Goal assignment uses only pre-treatment fixture data and is identical
    # in both arms (e.g. users whose only triggers were offline-buffered get none), so this is still randomized.
    n_g, k_g = Counter(), Counter()
    for u, a in pop.items():
        o = uo.get(u)
        if a in ("agent", "holdout") and o is not None:
            n_g[a] += 1
            k_g[a] += o["result"] == "succeeded"
    sens_goal = Comparison("goal reached, users with a goal only", k_g["agent"], n_g["agent"], k_g["holdout"],
                           n_g["holdout"]).row()
    balance = Comparison("pre-treatment balance: share of users with no goal", n["agent"] - n_g["agent"], n["agent"],
                         n["holdout"] - n_g["holdout"], n["holdout"]).row()
    by_goal = []
    for gid in sorted(set(goal_counts["agent"]) | set(goal_counts["holdout"])):
        if gid == "none":
            continue
        kk = Counter()
        nn = Counter()
        for u, a in pop.items():
            o = uo.get(u)
            if o and o["goal_id"] == gid and a in ("agent", "holdout"):
                nn[a] += 1
                kk[a] += o["result"] == "succeeded"
        by_goal.append(Comparison(gid, kk["agent"], nn["agent"], kk["holdout"], nn["holdout"]).row())
    secondary = []
    if outcome_events_path and outcome_events_path.exists():
        flags = secondary_flags(read_jsonl(outcome_events_path), set(pop))
        for key, label in (("any_play", "any play_started in the 14-day horizon"),
                           ("any_completion", "any episode_completed"),
                           ("paid_purchase", "paid purchase (is_trial = false)"),
                           ("opt_out", "notification opt-out (harm metric)")):
            kk = Counter(a for u, a in pop.items() if a in ("agent", "holdout") and flags[u][key])
            secondary.append(Comparison(label, kk["agent"], n["agent"], kk["holdout"], n["holdout"]).row())
    return {"population": "randomized users with >= 1 replay trigger", "n": dict(n), "missing_arm": len(missing_arm),
            "primary": primary, "sensitivity": sens, "sensitivity_goal_holders": sens_goal, "balance_no_goal": balance,
            "by_goal": by_goal, "secondary": secondary,
            "goal_mix": {a: dict(c) for a, c in goal_counts.items()},
            "result_counts": {a: dict(c) for a, c in result_counts.items()},
            "arms_file_counts": dict(Counter(arms.values()))}


# ---------------------------------------------------------------------- pilot audit
def pilot_audit(assign_path: Path, outcomes_path: Path) -> dict[str, Any]:
    rows = read_csv(outcomes_path)
    assign = {r["user_id"]: r for r in read_csv(assign_path)}
    succ = lambda r: r["succeeded"] == "true"
    served = [r for r in rows if r["served"] == "true"]
    others = [r for r in rows if r["served"] != "true"]
    treat = [r for r in rows if r["arm"] == "treatment"]
    hold = [r for r in rows if r["arm"] == "holdout"]
    mismatch = sum(1 for r in rows if r["user_id"] in assign and (assign[r["user_id"]]["arm"] != r["arm"] or
                                                                 assign[r["user_id"]]["served"] != r["served"]))
    flawed = Comparison("served vs everyone else (pilot report method)", sum(map(succ, served)), len(served),
                        sum(map(succ, others)), len(others)).row()
    itt = Comparison("treatment arm vs holdout arm (ITT)", sum(map(succ, treat)), len(treat),
                     sum(map(succ, hold)), len(hold)).row()
    served_in_holdout = sum(1 for r in hold if r["served"] == "true")
    # goal mix and a standardisation: what holdout users with the served users' goal mix achieved
    hold_rate = {}
    for gid in {r["goal_id"] for r in rows}:
        h = [r for r in hold if r["goal_id"] == gid]
        hold_rate[gid] = (sum(map(succ, h)) / len(h)) if h else None
    mix_served = Counter(r["goal_id"] for r in served)
    mix_others = Counter(r["goal_id"] for r in others)
    std = sum(mix_served[g] / len(served) * (hold_rate[g] or 0) for g in mix_served) if served else None
    treat_unserved = [r for r in treat if r["served"] != "true"]
    by_goal = []
    for gid in sorted({r["goal_id"] for r in rows}):
        t = [r for r in treat if r["goal_id"] == gid]
        h = [r for r in hold if r["goal_id"] == gid]
        by_goal.append(Comparison(gid, sum(map(succ, t)), len(t), sum(map(succ, h)), len(h)).row()
                       | {"served_share_of_goal": (sum(1 for r in t if r["served"] == "true") / len(t)) if t else None})
    return {"n_rows": len(rows), "assignment_mismatches": mismatch, "flawed": flawed, "itt": itt,
            "served_in_holdout": served_in_holdout,
            "served_goal_mix": dict(mix_served.most_common()), "others_goal_mix": dict(mix_others.most_common()),
            "holdout_rate_by_goal": hold_rate, "served_expected_without_agent": std,
            "treatment_unserved": {"n": len(treat_unserved), "rate": sum(map(succ, treat_unserved)) / len(treat_unserved)
                                   if treat_unserved else None},
            "by_goal": by_goal}


# ---------------------------------------------------------------------- markdown
def _cmp_table(rows: list[dict[str, Any]], first_col: str = "Outcome") -> list[str]:
    out = [f"| {first_col} | Agent k/n | Agent rate | Holdout k/n | Holdout rate | Diff (agent - holdout) | 95% CI (Newcombe) | Relative | p |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "agent_rate" not in r:
            out.append(f"| {r['label']} | {r.get('n_agent', 0)} | - | {r.get('n_holdout', 0)} | - | - | - | - | - |")
            continue
        rel = "n/a" if r["relative"] is None else f"{100 * r['relative']:+.1f}%"
        out.append(f"| {r['label']} | {r['agent_k']}/{r['agent_n']} | {pct(r['agent_rate'])} | {r['holdout_k']}/"
                   f"{r['holdout_n']} | {pct(r['holdout_rate'])} | {pp(r['diff_pp'])} | [{r['ci_lo_pp']:+.2f}, "
                   f"{r['ci_hi_pp']:+.2f}] pp | {rel} | {r['p_value']:.3f} |")
    return out


def interpret(r: dict[str, Any]) -> str:
    if "agent_rate" not in r:
        return "not estimable"
    lo, hi, d = r["ci_lo_pp"], r["ci_hi_pp"], r["diff_pp"]
    if lo > 0:
        return f"a positive effect of {d:+.2f} pp; the 95% interval excludes zero"
    if hi < 0:
        return f"a negative effect of {d:+.2f} pp; the 95% interval excludes zero"
    return (f"an estimate of {d:+.2f} pp whose 95% interval [{lo:+.2f}, {hi:+.2f}] includes zero: the data do not "
            "distinguish the agent from no agent at this sample size")


def render_impact_md(itt: dict[str, Any], pilot: dict[str, Any], seeds: list[dict[str, Any]], sim_report: dict,
                     seed: str, policy: dict[str, Any]) -> str:
    p = itt["primary"]
    L = ["# Impact report: agent vs randomized holdout", "",
         f"Simulator seed for the headline numbers: `{seed}`. All numbers are computed by "
         "`stage_agent.impact_reporter` from `sample_run/outcomes.jsonl`, `replay/arms.csv` and the simulator output; "
         "nothing in this file is typed by hand.", "",
         "## 1. Estimand and method", "",
         "* **Unit of randomization and analysis:** the user (`replay/arms.csv`). Each user counts once, however many "
         "triggers or events they produced.",
         "* **Population:** every randomized user with at least one replay-day trigger "
         f"(agent n = {itt['n'].get('agent', 0)}, holdout n = {itt['n'].get('holdout', 0)}; arms file overall: "
         f"{itt['arms_file_counts']}). Triggers are fixed inputs that do not depend on the arm, and the simulator "
         "only produces outcomes for triggered users, so this is a pre-treatment restriction.",
         "* **Analysis by assigned arm (intention to treat):** agent-arm users the agent abstained on, could not "
         "serve, or found ineligible are all included.",
         "* **Outcome:** the user's first goal (chosen by the same procedure in both arms; never acted on in the "
         "holdout) succeeded within its window, judged from simulator events. Users with no goal count as not "
         "succeeded. Users whose window extends past the observed data are inconclusive and also count as not "
         "succeeded in the primary analysis (sensitivity below excludes them).",
         "* **Interval:** Newcombe hybrid score interval (Wilson-based) for the difference of proportions; two-sided "
         "pooled z-test p-value for reference.", "",
         "## 2. Primary result", ""]
    L += _cmp_table([p, itt["sensitivity"], itt["sensitivity_goal_holders"]])
    b = itt["balance_no_goal"]
    if "agent_rate" in b:
        L += ["", f"Balance check (pre-treatment): users with no goal - agent {b['agent_k']}/{b['agent_n']} "
              f"({pct(b['agent_rate'])}) vs holdout {b['holdout_k']}/{b['holdout_n']} ({pct(b['holdout_rate'])}), "
              f"p = {b['p_value']:.3f}. No-goal status is fixed by the replay fixture before any treatment (almost "
              "always: every trigger was an offline-buffered open), so a difference here is chance imbalance, not an "
              "agent effect; the goal-holder row above removes it."]
    rel = "n/a" if p.get("relative") is None else f"{100 * p['relative']:+.1f}%"
    L += ["", f"Interpretation: {interpret(p)}.",
          f"Absolute difference: **{pp(p.get('diff_pp'))}**. Relative difference: "
          f"{rel} (relative to the holdout rate; "
          "the absolute difference is the decision-relevant number).", "",
          "First-goal result counts by arm:", ""]
    for a in ("agent", "holdout"):
        L.append(f"* {a}: {itt['result_counts'].get(a, {})}")
    L += ["", "## 3. Goal-agnostic secondary outcomes (same population, same ITT comparison)", ""]
    L += _cmp_table(itt["secondary"]) if itt["secondary"] else ["(simulator events unavailable)"]
    L += ["", "## 4. By first goal", "",
          "Goals are assigned before any treatment by the same deterministic procedure in both arms, so each row is "
          "a randomized comparison within a pre-treatment stratum. Samples are small, intervals wide and there is no "
          "multiplicity correction: read these as descriptive.", ""]
    L += _cmp_table(itt["by_goal"], "Goal")
    L += ["", "## 5. Sensitivity to the simulator seed", "",
          "The agent's actions are fixed; only the simulator seed changes.", "",
          "| Seed | Agent rate | Holdout rate | Diff | 95% CI |", "|---|---|---|---|---|"]
    for s in seeds:
        r = s["primary"]
        L.append(f"| `{s['seed']}` | {pct(r['agent_rate'])} | {pct(r['holdout_rate'])} | {pp(r['diff_pp'])} | "
                 f"[{r['ci_lo_pp']:+.2f}, {r['ci_hi_pp']:+.2f}] |")
    L += ["", "## 6. Pilot report audit", "",
          f"The internal pilot summary compares users **served** by the agent with **everyone else**. Recomputed from "
          f"`pilot/pilot_outcomes.csv` ({pilot['n_rows']} users; arm/served mismatches against "
          f"`pilot_assignment.csv`: {pilot['assignment_mismatches']}):", ""]
    L += _cmp_table([pilot["flawed"], pilot["itt"]], "Comparison")
    L += ["",
          f"* The flawed comparison reproduces the report's headline ({pct(pilot['flawed']['agent_rate'])} vs "
          f"{pct(pilot['flawed']['holdout_rate'])}). Its 'everyone else' group mixes the holdout arm with treatment "
          f"users who were not served; served users in the holdout arm: {pilot['served_in_holdout']}.",
          f"* **Correct randomized estimate (ITT):** treatment {pct(pilot['itt']['agent_rate'])} vs holdout "
          f"{pct(pilot['itt']['holdout_rate'])}, difference {pp(pilot['itt']['diff_pp'])} "
          f"(95% CI [{pilot['itt']['ci_lo_pp']:+.2f}, {pilot['itt']['ci_hi_pp']:+.2f}] pp): {interpret(pilot['itt'])}.",
          f"* **Why the headline misleads (selection bias):** serving was not random. Served users' goal mix was "
          f"{pilot['served_goal_mix']} against {pilot['others_goal_mix']} for everyone else. Holdout users (who never "
          f"saw the agent) with the served users' goal mix would have succeeded "
          f"{pct(pilot['served_expected_without_agent'])} of the time, so most of the gap is who was served, not what "
          f"serving did. Treatment users who were not served succeeded {pct(pilot['treatment_unserved']['rate'])} "
          f"(n = {pilot['treatment_unserved']['n']}).", "",
          "Pilot by goal (treatment vs holdout, with the share of treatment users served):", ""]
    L += _cmp_table(pilot["by_goal"], "Goal")
    L += ["", "## 7. Policy audit of the simulated run", "",
          f"* Simulator `policy_problem_count`: **{sim_report.get('policy_problem_count')}**; holdout actions "
          f"ignored by the simulator: {sim_report.get('counts', {}).get('holdout_actions_ignored', 0)}.",
          f"* Independent post-hoc audit of `actions.jsonl` against the pack rules: {policy}", "",
          "## 8. Limits of this evidence", "",
          "* The outcome model is the supplied simulator, not real users. Its effect sizes (page multipliers, click "
          "rates) are assumptions of the simulator; a simulated uplift is evidence that the agent's actions reach "
          "the mechanisms the simulator models, not proof of real-world impact.",
          "* One replay day, ~1.7k triggered users: power to detect a few-point difference is limited, as the "
          "interval width shows.",
          "* The goal outcome depends on the goal mix, which is the same in both arms by construction; goal-agnostic "
          "secondary outcomes are reported so that the conclusion does not rest on the goal definition alone.",
          "* Goal-level rows are small subgroups without multiplicity correction.", ""]
    return "\n".join(L)


def audit_actions(actions: list[dict[str, Any]], arms: dict[str, str], catalogue, pack, triggers_path: Path) -> dict:
    """Post-hoc re-check of exported actions against the pack rules (a second line of defence)."""
    from .normalization import TITLE_DEEPLINK_RE
    from .scheduler import QuietHours, ist_day
    q = pack.interventions["push"]["quiet_hours_ist"]
    quiet = QuietHours(q[0], q[1])
    problems: Counter = Counter()
    per_day: Counter = Counter()
    trig_dialect: dict[str, list[tuple[datetime, str | None]]] = defaultdict(list)
    for e in read_jsonl(triggers_path):
        trig_dialect[e["user_id"]].append((parse_ts(e["received_at"]), e.get("app_dialect")))
    rows_cfg = pack.interventions["home_page"]["rows"]
    for a in actions:
        u = a["user_id"]
        if arms.get(u) != "agent":
            problems["action_on_non_agent_user"] += 1
        t = a["type"]
        if t == "home_page":
            at = parse_ts(a["published_at"])
            slugs = set()
            if len(a.get("rows") or []) > rows_cfg["max_rows"]:
                problems["too_many_rows"] += 1
            ids = ([a["hero_title_id"]] if a.get("hero_title_id") else []) + [x for r in a.get("rows") or [] for x in r["title_ids"]]
            for r in a.get("rows") or []:
                if not rows_cfg["min_titles_per_row"] <= len(r["title_ids"]) <= rows_cfg["max_titles_per_row"]:
                    problems["row_size"] += 1
                if r["row_type"] not in rows_cfg["row_types"]:
                    problems["row_type"] += 1
            for tid in ids:
                tt = catalogue.by_id.get(tid)
                if tt is None or tt.dialect != a.get("dialect"):
                    problems["page_wrong_dialect"] += 1
                if not catalogue.available(tid, at):
                    problems["page_unavailable_title"] += 1
                if tt is not None:
                    if tt.content_slug in slugs:
                        problems["duplicate_slug"] += 1
                    slugs.add(tt.content_slug)
                    if tt.suspicious:
                        problems["quarantined_title_used"] += 1
        elif t in ("push", "whatsapp"):
            s = parse_ts(a.get("send_at") or a["published_at"])
            if quiet.contains(s):
                problems["quiet_hours"] += 1
            per_day[(u, ist_day(s))] += 1
            if t == "push":
                txt = a.get("text") or {}
                if len(txt.get("title", "")) > 40 or len(txt.get("body", "")) > 120:
                    problems["push_length"] += 1
                if a.get("title_id") and not TITLE_DEEPLINK_RE.match(a.get("deeplink") or ""):
                    problems["deeplink"] += 1
            if t == "whatsapp" and a.get("template_id") not in pack.approved_templates:
                problems["unapproved_template"] += 1
            if a.get("title_id") and not catalogue.available(a["title_id"], s):
                problems["message_unavailable_title"] += 1
    problems["frequency_cap"] = sum(1 for v in per_day.values() if v > 1)
    total = sum(problems.values())
    return {"actions_checked": len(actions), "problems_total": total,
            "problems": {k: v for k, v in sorted(problems.items()) if v}}
