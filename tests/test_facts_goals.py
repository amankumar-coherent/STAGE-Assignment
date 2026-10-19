"""Conflicting sources, precedence, missing/duplicate profiles, goal selection, corrections, purchases."""
import unittest
from datetime import datetime, timedelta

from helpers import T0, UTC, Pack, ev, profile, standard_user_history

from stage_agent.config import AgentConfig
from stage_agent.goal_planner import GoalRuleBook, judge_success
from stage_agent.scheduler import iso, parse_ts


class Facts(unittest.TestCase):
    def test_three_way_dialect_conflict(self):
        p = Pack(events=standard_user_history("U-1"), profiles=[profile("U-1", dialect="d2")], arms={"U-1": "agent"})
        try:
            raw = ev("U-1", "app_open", T0, dialect="d1", props={"deeplink": "ott://c/cmp9/d4/title/T2001"})
            d = p.trigger(raw)
            self.assertEqual(d["dialect"]["used"], "d1")
            self.assertEqual(d["dialect"]["source"], "app_setting")
            self.assertCountEqual(d["dialect"]["conflicts"], ["profile:d2", "campaign_link:d4"])
            pages = [a for a in p.actions("U-1") if a["type"] == "home_page"]
            self.assertEqual(len(pages), 1)
            self.assertEqual(pages[0]["raw"]["dialect"], "d1")
            ids = [pages[0]["raw"]["hero_title_id"]] + [t for r in pages[0]["raw"]["rows"] for t in r["title_ids"]]
            self.assertTrue(all(p.world.catalogue.by_id[t].dialect == "d1" for t in ids))
            self.assertTrue(any(c["fact"] == "dialect" for c in d["conflicts"]))
        finally:
            p.close()

    def test_later_opt_out_overrides_stale_consent(self):
        hist = standard_user_history("U-1") + [
            ev("U-1", "notification_opt_out", datetime(2026, 9, 30, 9, tzinfo=UTC), props={"channel": "push"},
               deliver=False)]
        p = Pack(events=hist, profiles=[profile("U-1", push="true")], arms={"U-1": "agent"})
        try:
            d = p.trigger(ev("U-1", "app_open", T0))
            self.assertFalse(d["facts"]["push_consent"]["value"])
            self.assertEqual(d["facts"]["push_consent"]["source"], "event")
            self.assertTrue(d["facts"]["push_consent"]["conflicts"], "the overridden profile consent stays visible")
            self.assertFalse(any(a["type"] == "push" for a in p.actions("U-1")))
        finally:
            p.close()

    def test_duplicate_profile_rows_newest_wins(self):
        rows = [profile("U-1", status="lapsed", dialect="d2", updated="2026-09-10T00:00:00.000Z"),
                profile("U-1", status="never", dialect="d1", updated="2026-09-29T00:00:00.000Z")]
        p = Pack(events=standard_user_history("U-1"), profiles=rows, arms={"U-1": "agent"})
        try:
            d = p.trigger(ev("U-1", "app_open", T0))
            sub = d["facts"]["subscription_status"]
            self.assertEqual(sub["value"], "never")
            self.assertIn("2026-09-29", sub["source_ref"])
            self.assertFalse(sub["supported"], "'never' contradicted by an older 'lapsed' row is not evidence")
            g05 = [c for c in d["goal_candidates"] if c["goal_id"] == "G05"][0]
            self.assertFalse(g05["eligible"])
            # deterministic: same answer when rows arrive in the other order
            p2 = Pack(events=standard_user_history("U-1"), profiles=list(reversed(rows)), arms={"U-1": "agent"})
            try:
                d2 = p2.trigger(ev("U-1", "app_open", T0))
                self.assertEqual(d2["facts"]["subscription_status"]["value"], "never")
                self.assertEqual(d2["goal"]["id"], d["goal"]["id"])
            finally:
                p2.close()
        finally:
            p.close()

    def test_missing_profile(self):
        signup = datetime(2026, 9, 30, 10, tzinfo=UTC)
        hist = [ev("U-new", "signup_completed", signup, props={"method": "otp"}, deliver=False)]
        p = Pack(events=hist, profiles=[], arms={"U-new": "agent"})
        try:
            d = p.trigger(ev("U-new", "app_open", T0))
            self.assertFalse(d["facts"]["profile_present"]["value"])
            self.assertEqual(d["facts"]["signup_at"]["source"], "event")
            self.assertEqual(d["facts"]["subscription_status"]["value"], "never")
            self.assertEqual(d["facts"]["subscription_status"]["source"], "derived")
            self.assertFalse(d["facts"]["push_consent"]["value"], "consent is never inferred")
            self.assertEqual(d["goal"]["id"], "G01")
            self.assertFalse(any(a["type"] in ("push", "whatsapp") for a in p.actions("U-new")))
        finally:
            p.close()

    def test_unknown_dialect_abstains(self):
        p = Pack(events=standard_user_history("U-1") + standard_user_history("U-2"),
                 profiles=[profile("U-1"), profile("U-2")], arms={"U-1": "agent", "U-2": "agent"})
        try:
            d = p.trigger(ev("U-1", "app_open", T0, dialect=None))
            self.assertEqual(d["status"], "ABSTAINED")
            self.assertIsNone(d["dialect"]["used"])
            self.assertTrue(d["dialect"]["conflict_detail"], "alternatives listed but not substituted")
            d7 = p.trigger(ev("U-2", "app_open", T0, dialect="d7"))
            self.assertEqual(d7["status"], "ABSTAINED")
            self.assertEqual(p.actions(), [])
        finally:
            p.close()


class Goals(unittest.TestCase):
    def test_unmeasurable_goal_never_selected(self):
        cfg = AgentConfig(goal_priority=("G10", "G11", "G07", "G05"))
        p = Pack(events=standard_user_history("U-1"), profiles=[profile("U-1", status="active")],
                 arms={"U-1": "agent"}, cfg=cfg)
        try:
            d = p.trigger(ev("U-1", "app_open", T0))
            by = {c["goal_id"]: c for c in d["goal_candidates"]}
            self.assertFalse(by["G10"]["measurable"])
            self.assertFalse(by["G11"]["measurable"])
            self.assertIn("tv_app_open", by["G11"]["reason"])
            self.assertNotIn(d["goal"]["id"], ("G10", "G11"))
            self.assertIsNotNone(d["goal"]["success_signal"])
        finally:
            p.close()

    def test_g06_recent_lapse_interpretation(self):
        recent = [ev("U-r", "app_open", datetime(2026, 9, 25, tzinfo=UTC), deliver=False),
                  ev("U-r", "subscription_lapsed", datetime(2026, 9, 21, tzinfo=UTC), deliver=False)]
        old = [ev("U-o", "app_open", datetime(2026, 9, 25, tzinfo=UTC), deliver=False)]
        p = Pack(events=recent + old,
                 profiles=[profile("U-r", status="lapsed", updated="2026-09-22T00:00:00.000Z"),
                           profile("U-o", status="lapsed", plan_ends="2026-08-10T00:00:00.000Z")],
                 arms={"U-r": "agent", "U-o": "agent"})
        try:
            dr = p.trigger(ev("U-r", "app_open", T0))
            do = p.trigger(ev("U-o", "app_open", T0))
            self.assertEqual(dr["goal"]["id"], "G06")
            g06 = [c for c in do["goal_candidates"] if c["goal_id"] == "G06"][0]
            self.assertFalse(g06["eligible"])
            self.assertIn("recently", g06["reason"])
        finally:
            p.close()

    def test_existing_goal_kept_across_triggers(self):
        p = Pack(events=standard_user_history("U-1"), profiles=[profile("U-1")], arms={"U-1": "agent"})
        try:
            d1 = p.trigger(ev("U-1", "app_open", T0))
            d2 = p.trigger(ev("U-1", "app_open", T0 + timedelta(hours=3)))
            self.assertEqual(d2["goal"]["instance_id"], d1["goal"]["instance_id"])
            self.assertEqual(d2["goal"]["selection"], "kept")
            self.assertEqual(d2["model_calls"], 0, "no model call to re-pick an existing valid goal")
        finally:
            p.close()

    def test_purchase_after_decision_cancels_and_replans(self):
        hist = [ev("U-L", "app_open", datetime(2026, 9, 25, tzinfo=UTC), deliver=False),
                ev("U-L", "subscription_lapsed", datetime(2026, 9, 26, tzinfo=UTC), deliver=False)]
        p = Pack(events=hist, profiles=[profile("U-L", status="lapsed", updated="2026-09-27T00:00:00.000Z")],
                 arms={"U-L": "agent"})
        try:
            d = p.trigger(ev("U-L", "app_open", T0))
            self.assertEqual(d["goal"]["id"], "G06")
            in_app = [a for a in p.actions("U-L") if a["type"] == "in_app_message"]
            self.assertEqual(len(in_app), 1)
            self.assertIn("149", in_app[0]["raw"]["text"])
            out = p.live(ev("U-L", "purchase_completed", T0 + timedelta(minutes=2),
                            props={"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False}))
            self.assertEqual(out["goal_succeeded"], d["goal"]["instance_id"])
            self.assertIn(in_app[0]["action_id"], out["cancelled"])
            st = {a["action_id"]: a["status"] for a in p.actions("U-L")}
            self.assertEqual(st[in_app[0]["action_id"]], "cancelled")
            d2 = p.trigger(ev("U-L", "app_open", T0 + timedelta(hours=4)))
            self.assertIn(d2["goal"]["id"], ("G08", "G09"), "re-planned for an active subscriber")
            self.assertEqual(d2["facts"]["subscription_status"]["value"], "active")
            self.assertEqual(d2["facts"]["subscription_status"]["source"], "event")
        finally:
            p.close()


    def test_trial_purchase_invalidates_first_paid_goal(self):
        hist = [ev("U-N", "app_open", datetime(2026, 9, 25, tzinfo=UTC), deliver=False)]
        p = Pack(events=hist, profiles=[profile("U-N", status="never")], arms={"U-N": "agent"})
        try:
            d = p.trigger(ev("U-N", "app_open", T0))
            self.assertEqual(d["goal"]["id"], "G05")
            out = p.live(ev("U-N", "purchase_completed", T0 + timedelta(minutes=2),
                            props={"plan_id": "P_TRIAL", "price_inr": 1, "is_trial": True}))
            self.assertNotIn("goal_succeeded", out, "a trial is not a paid plan")
            self.assertIn("cancelled (requires ['never'], now trial_active)", out["replan"])
            self.assertIsNone(p.store.open_goal("U-N"))
            audit = p.store.audit_rows("purchase_after_decision", "U-N")
            self.assertEqual(audit[0]["raw"]["replan"], out["replan"])
            d2 = p.trigger(ev("U-N", "app_open", T0 + timedelta(hours=2)))
            self.assertEqual(d2["goal"]["id"], "G04", "re-planned from the purchase event, not the stale profile")
        finally:
            p.close()


class Corrections(unittest.TestCase):
    def _new_user_history(self, u):
        s = datetime(2026, 9, 30, 6, tzinfo=UTC)
        return [ev(u, "signup_completed", s, deliver=False),
                ev(u, "play_started", s + timedelta(minutes=2), props={"title_id": "T1009", "episode_id": "s9-d1-s1e1"}, deliver=False),
                ev(u, "episode_completed", s + timedelta(minutes=40), props={"title_id": "T1009", "episode_id": "s9-d1-s1e1"}, deliver=False)]

    def test_corrected_goal_rule_only_affects_open_goals(self):
        users = ["U-n1", "U-n2"]
        hist = sum((self._new_user_history(u) for u in users), []) + standard_user_history("U-x")
        p = Pack(events=hist, profiles=[profile("U-x")], arms={"U-n1": "agent", "U-n2": "holdout", "U-x": "agent"})
        try:
            g = {u: p.trigger(ev(u, "app_open", T0))["goal"] for u in users + ["U-x"]}
            self.assertEqual(g["U-n1"]["id"], "G03")
            self.assertEqual(g["U-n2"]["id"], "G03")
            self.assertEqual(g["U-x"]["id"], "G07")
            # finalize U-n2's goal before the correction: it must not be rewritten
            with p.store.tx() as c:
                p.store.close_goal(c, g["U-n2"]["instance_id"], "succeeded", T0 + timedelta(hours=1), "test")
            ctrl = {"at": "2026-10-01T08:30:00.000Z", "type": "goal_rule_corrected", "goal_id": "G03",
                    "old_success": {"event": "app_open"}, "new_success": {"event": "play_started",
                    "condition": "on a later IST calendar day within the window"}, "note": "test"}
            info = p.rt.on_rule_correction(ctrl, parse_ts(ctrl["at"]))
            self.assertEqual(info["affected_open_goals"], 1)
            self.assertEqual(info["untouched"], 1, "the G07 goal is not re-evaluated")
            goals = {x["user_id"]: x for x in p.store.all_goals()}
            self.assertEqual(goals["U-n1"]["rule_version"], "G03-r2")
            self.assertEqual(goals["U-n1"]["success_event"], "play_started")
            self.assertEqual(goals["U-n2"]["rule_version"], "G03-r1", "finalized goal keeps its rule")
            self.assertEqual(goals["U-x"]["rule_version"], "G07-r1")
            self.assertEqual(goals["U-x"]["history"][-1]["change"], "opened")
            # judging uses the corrected rule: a next-day app_open alone no longer counts
            g1 = goals["U-n1"]
            nxt = parse_ts(g1["started_at"]) + timedelta(days=1)
            opens = [{"event_id": "o1", "event": "app_open", "properties": {}, "received_at": iso(nxt)}]
            plays = opens + [{"event_id": "p1", "event": "play_started", "properties": {"title_id": "T1002"},
                              "received_at": iso(nxt + timedelta(minutes=5))}]
            t = lambda e: parse_ts(e["received_at"])
            self.assertIsNone(judge_success(g1, opens, p.world.catalogue, t)[0])
            self.assertEqual(judge_success(g1, plays, p.world.catalogue, t)[0]["event_id"], "p1")
            # new G03 goals opened after the correction start on r2
            self.assertEqual(p.rt.book.current("G03").version, "G03-r2")
        finally:
            p.close()


if __name__ == "__main__":
    unittest.main()
