"""Outcome judging, impact statistics, pilot audit, output schema and evidence references."""
import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from helpers import T0, UTC, Pack, ev, profile, standard_user_history

from stage_agent.content_selector import Catalogue
from stage_agent.loader import read_jsonl
from stage_agent.impact_reporter import newcombe_diff, pilot_audit, replay_itt, wilson
from stage_agent.outcome_judge import _judge_goal, ingest_outcome_events, judge_all
from stage_agent.replay import export_outputs, run_replay
from stage_agent.scheduler import iso, parse_ts

REQUIRED = ("decision_id", "viewer_id", "trigger", "goal", "action", "no_action_reason", "dialect", "content",
            "confidence", "confidence_reason", "evidence", "assumptions", "conflicts", "checks", "fallback_used",
            "model_calls", "tool_calls", "decided_in_ms", "published_at", "status", "facts", "latency_ms")


def goal(start, hours=24, event="play_started", cond="any title", gid="G01", arm="agent"):
    return {"instance_id": "GI-t", "user_id": "U-1", "goal_id": gid, "rule_version": f"{gid}-r1", "status": "open",
            "started_at": iso(start), "deadline": iso(start + timedelta(hours=hours)), "success_event": event,
            "success_condition": cond, "decision_id": "D-t", "arm": arm, "eligibility": {"context": {}}, "history": []}


def oe(eid, event, at, **props):
    return {"event_id": eid, "user_id": "U-1", "event": event, "received_at": iso(at), "properties": props}


class Judge(unittest.TestCase):
    cat = Catalogue([], [])

    def test_events_outside_window_not_credited(self):
        g = goal(T0)
        evs = [oe("before", "play_started", T0 - timedelta(hours=1)), oe("after", "play_started", T0 + timedelta(hours=25))]
        o = _judge_goal(g, evs, self.cat, T0 + timedelta(days=14), T0, [])
        self.assertEqual(o["result"], "failed")
        self.assertTrue(o["final"])
        o2 = _judge_goal(g, evs + [oe("in", "play_started", T0 + timedelta(hours=2))], self.cat,
                         T0 + timedelta(days=14), T0, [])
        self.assertEqual(o2["result"], "succeeded")
        self.assertEqual(o2["evidence"], ["event:in"])

    def test_missing_evidence_is_inconclusive_not_failed(self):
        g = goal(T0, hours=336)
        o = _judge_goal(g, [], self.cat, T0 + timedelta(days=3), T0, [])
        self.assertEqual(o["result"], "inconclusive")
        self.assertFalse(o["final"])

    def test_paid_purchase_condition_and_attribution(self):
        g = goal(T0, 168, "purchase_completed", "is_trial = false", "G05")
        trial = oe("t", "purchase_completed", T0 + timedelta(hours=1), is_trial=True)
        paid = oe("p", "purchase_completed", T0 + timedelta(hours=2), is_trial=False)
        shown = {"kind": "page_shown", "action_id": "A-1", "at": iso(T0 + timedelta(minutes=1)), "ref": "event:s"}
        o = _judge_goal(g, [trial, paid], self.cat, T0 + timedelta(days=14), T0, [shown])
        self.assertEqual(o["evidence"], ["event:p"], "a trial purchase is not a paid plan")
        self.assertEqual(o["attribution"], "exposed_before_success")
        o2 = _judge_goal(g, [paid], self.cat, T0 + timedelta(days=14), T0, [])
        self.assertEqual(o2["attribution"], "success_without_observed_exposure")

    def test_three_distinct_ist_days(self):
        g = goal(T0, 336, "play_started", "on 3 distinct IST days", "G09")
        two = [oe("a", "play_started", T0 + timedelta(hours=1)), oe("b", "play_started", T0 + timedelta(hours=2)),
               oe("c", "play_started", T0 + timedelta(days=1))]
        self.assertEqual(_judge_goal(g, two, self.cat, T0 + timedelta(days=15), T0, [])["result"], "failed")
        three = two + [oe("d", "play_started", T0 + timedelta(days=4))]
        self.assertEqual(_judge_goal(g, three, self.cat, T0 + timedelta(days=15), T0, [])["evidence"][-1], "event:d")

    def test_finalized_outcome_preserved_and_learning(self):
        p = Pack(events=standard_user_history("U-1"), profiles=[profile("U-1")], arms={"U-1": "agent"})
        try:
            d = p.trigger(ev("U-1", "app_open", T0))
            push = [a for a in p.actions("U-1") if a["type"] == "push"][0]
            path = Path(p.tmp.name) / "oe.jsonl"
            rows = [oe("f1", "message_failed", parse_ts(push["send_at"]), channel="push", reason="token_invalid",
                       action_id=push["action_id"]),
                    oe("x1", "play_started", T0 + timedelta(days=1), title_id="T1001"),
                    oe("end", "app_open", T0 + timedelta(days=14))]
            path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            ingest_outcome_events(p.store, path)
            out = judge_all(p.store, p.world.catalogue)
            self.assertEqual(out[0]["result"], "succeeded")  # G07: play of the same slug inside 72 h
            learning = p.store.get_learning("U-1")
            self.assertTrue(learning["push_token_invalid"])
            self.assertEqual(learning["messages"]["push"]["failed"], {"token_invalid": 1})
            self.assertEqual(learning["messages"]["push"]["delivered"], 0, "published is not delivered")
            # finalized outcomes are not rewritten by a later judgement
            path.write_text(json.dumps(rows[-1]), encoding="utf-8")
            ingest_outcome_events(p.store, path)
            judge_all(p.store, p.world.catalogue)
            self.assertEqual(p.store.outcomes()[0]["result"], "succeeded")
            d2 = p.trigger(ev("U-1", "app_open", T0 + timedelta(days=4)))
            self.assertFalse(d2["facts"].get("push_deliverable", {}).get("value", True),
                             "learning feeds back into the next decision")
        finally:
            p.close()


class Stats(unittest.TestCase):
    def test_newcombe_known_values(self):
        # Newcombe (1998) Statistics in Medicine 17:873, example (a): 56/70 vs 48/80 -> 0.2000 (0.0524, 0.3339)
        d, lo, hi = newcombe_diff(56, 70, 48, 80)
        self.assertAlmostEqual(d, 0.2, places=6)
        self.assertAlmostEqual(lo, 0.0524, places=3)
        self.assertAlmostEqual(hi, 0.3339, places=3)
        lo_w, hi_w = wilson(0, 10)
        self.assertEqual(lo_w, 0.0)
        self.assertAlmostEqual(hi_w, 0.2775, places=3)

    def test_itt_counts_every_randomized_user(self):
        arms = {"a1": "agent", "a2": "agent", "a3": "agent", "h1": "holdout", "h2": "holdout", "x": "holdout"}
        trig = {u: T0 for u in ("a1", "a2", "a3", "h1", "h2")}
        outs = [{"user_id": "a1", "goal_id": "G01", "result": "succeeded", "started_at": iso(T0), "goal_instance_id": "1"},
                {"user_id": "a1", "goal_id": "G08", "result": "failed", "started_at": iso(T0 + timedelta(hours=1)), "goal_instance_id": "2"},
                {"user_id": "a2", "goal_id": "G01", "result": "failed", "started_at": iso(T0), "goal_instance_id": "3"},
                {"user_id": "h1", "goal_id": "G01", "result": "succeeded", "started_at": iso(T0), "goal_instance_id": "4"}]
        r = replay_itt(arms, trig, outs, None)["primary"]
        self.assertEqual((r["agent_k"], r["agent_n"], r["holdout_k"], r["holdout_n"]), (1, 3, 1, 2),
                         "a3 (no goal) stays in the agent denominator; x never triggered")
        self.assertAlmostEqual(r["diff_pp"], 100 * (1 / 3 - 1 / 2))

    def test_pilot_audit_reproduces_and_corrects(self):
        rows = []
        for i in range(10):  # served users: high-baseline goal
            rows.append({"user_id": f"s{i}", "arm": "treatment", "served": "true", "goal_id": "G09",
                         "goal_started_at": "", "succeeded": "true" if i < 6 else "false"})
        for i in range(30):
            rows.append({"user_id": f"t{i}", "arm": "treatment", "served": "false", "goal_id": "G05",
                         "goal_started_at": "", "succeeded": "false"})
        for i in range(40):
            g = "G09" if i < 10 else "G05"
            rows.append({"user_id": f"h{i}", "arm": "holdout", "served": "false", "goal_id": g,
                         "goal_started_at": "", "succeeded": "true" if (g == "G09" and i < 6) else "false"})
        with tempfile.TemporaryDirectory() as d:
            a, o = Path(d) / "a.csv", Path(d) / "o.csv"
            for path, fields in ((a, ["user_id", "arm", "served"]), (o, list(rows[0]))):
                with open(path, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=fields)
                    w.writeheader()
                    for r in rows:
                        w.writerow({k: r[k] for k in fields})
            res = pilot_audit(a, o)
        self.assertAlmostEqual(res["flawed"]["agent_rate"], 0.6)
        self.assertAlmostEqual(res["flawed"]["holdout_rate"], 6 / 70)
        self.assertAlmostEqual(res["itt"]["diff_pp"], 0.0, msg="randomized arms show no effect")
        self.assertAlmostEqual(res["served_expected_without_agent"], 0.6, msg="selection explains the gap")


class OutputSchema(unittest.TestCase):
    def test_every_decision_has_required_fields_and_resolvable_evidence(self):
        users = ["U-1", "U-2", "U-3"]
        hist = sum((standard_user_history(u) for u in users), [])
        trig = [ev("U-1", "app_open", T0, eid="t1"), ev("U-2", "app_open", T0 + timedelta(minutes=1), dialect=None),
                ev("U-3", "login_success", T0 + timedelta(minutes=2)),
                ev("U-1", "app_open", T0 - timedelta(days=2), buffer_s=2 * 86400 + 300)]
        trig.append(dict(trig[0], deliver_at=iso(T0 + timedelta(minutes=3)), received_at=iso(T0 + timedelta(minutes=3))))
        trig.sort(key=lambda e: e["deliver_at"])
        p = Pack(events=hist, profiles=[profile(u) for u in users], triggers=trig,
                 arms={"U-1": "agent", "U-2": "agent", "U-3": "holdout"})
        try:
            r = run_replay(p.world)
            out = Path(p.tmp.name) / "out"
            export_outputs(p.world, r, out)
            decisions = list(read_jsonl(out / "decisions.jsonl"))
            self.assertEqual(len(decisions), len(trig))
            for d in decisions:
                for k in REQUIRED:
                    self.assertIn(k, d, f"{d['decision_id']} missing {k}")
                self.assertTrue(d["goal"]["id"] or d["goal"]["no_goal_reason"] or d["status"] == "DUPLICATE_IGNORED")
                self.assertTrue(d["action"] != "NONE" or d["no_action_reason"])
                self.assertTrue(d["confidence_reason"] or d["status"] == "DUPLICATE_IGNORED")
                if d["goal"]["id"]:
                    self.assertTrue(d["goal"]["deadline"] and d["goal"]["success_signal"] and d["goal"]["rule_version"])
                for ref in d["evidence"]:
                    self.assertTrue(p.rt.verifier._ref_resolves(ref, d["viewer_id"]), f"unresolvable {ref}")
            acts = list(read_jsonl(out / "actions.jsonl"))
            for a in acts:
                for k in ("action_id", "decision_id", "user_id", "trigger_event_id", "type", "published_at"):
                    self.assertIn(k, a)
                self.assertIn(a["type"], ("home_page", "push", "whatsapp", "in_app_message"))
                self.assertNotEqual(a["user_id"], "U-3")
            lat = json.loads((out / "latency_report.json").read_text(encoding="utf-8"))
            for k in ("triggers_processed", "all_decisions", "over_60s", "over_5min", "fallbacks", "semantics"):
                self.assertIn(k, lat)
            self.assertEqual(lat["triggers_processed"], len(trig))
        finally:
            p.close()


if __name__ == "__main__":
    unittest.main()
