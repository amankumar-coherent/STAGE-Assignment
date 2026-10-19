"""Late / duplicate triggers, concurrency, tool failures, model faults and idempotent replay."""
import threading
import unittest
from datetime import datetime, timedelta

from helpers import T0, UTC, Pack, ev, profile, standard_user_history

from stage_agent.normalization import normalize_event
from stage_agent.replay import run_replay


def std_pack(**kw):
    return Pack(events=standard_user_history("U-1"), profiles=[profile("U-1")], arms={"U-1": "agent"}, **kw)


class DuplicateAndConcurrency(unittest.TestCase):
    def test_duplicate_trigger_no_duplicate_action(self):
        p = std_pack()
        try:
            raw = ev("U-1", "app_open", T0, eid="trig-1")
            d1 = p.trigger(raw)
            self.assertEqual(d1["status"], "PUBLISHED")
            n_actions = len(p.actions("U-1"))
            self.assertGreaterEqual(n_actions, 1)
            later = dict(raw, deliver_at="2026-10-01T04:36:00.000Z", received_at="2026-10-01T04:36:00.000Z")
            d2 = p.trigger(later)
            self.assertEqual(d2["status"], "DUPLICATE_IGNORED")
            self.assertEqual(d2["duplicate_of"], d1["decision_id"])
            self.assertEqual(len(p.actions("U-1")), n_actions, "a redelivery must not publish again")
            row = p.store.conn.execute("SELECT deliveries FROM processed_triggers WHERE event_id='trig-1'").fetchone()
            self.assertEqual(row[0], 2, "the redelivery is counted against the single processed trigger")
            n_ev = p.store.conn.execute("SELECT COUNT(*) FROM events WHERE event_id='trig-1'").fetchone()[0]
            self.assertEqual(n_ev, 1)
        finally:
            p.close()

    def test_concurrent_same_trigger_single_decision(self):
        p = std_pack()
        try:
            raw = ev("U-1", "app_open", T0, eid="trig-c")
            e = normalize_event(raw, "trigger")
            p.store.insert_events([(e, raw)])
            out = []

            def work(k):
                out.append(p.rt.handle_trigger(e, e.arrival, 100 + k).decision["status"])

            threads = [threading.Thread(target=work, args=(k,)) for k in range(8)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual(out.count("PUBLISHED"), 1)
            self.assertEqual(out.count("DUPLICATE_IGNORED"), 7)
            types = [a["type"] for a in p.actions("U-1")]
            self.assertEqual(len(types), len(set(types)), "no duplicate action of any type")
            self.assertEqual(len(p.store.conn.execute("SELECT * FROM goals WHERE status='open'").fetchall()), 1)
        finally:
            p.close()


class Staleness(unittest.TestCase):
    def test_stale_buffered_trigger(self):
        p = std_pack()
        try:
            raw = ev("U-1", "app_open", T0 - timedelta(days=3), buffer_s=3 * 86400)  # opened 3 days ago, sent now
            d = p.trigger(raw)
            self.assertTrue(d["trigger"]["stale"])
            self.assertGreater(d["trigger"]["event_age_s"], 2 * 86400)
            self.assertEqual(d["status"], "ABSTAINED")
            self.assertIn("stale trigger", d["no_action_reason"])
            self.assertIsNone(d["goal"]["id"], "no goal window is started from a stale visit")
            self.assertEqual(p.actions("U-1"), [])
            self.assertTrue(p.store.has_event(raw["event_id"]), "the event is kept for history")
            # the buffered app_open counts at its true (server-anchored) time, 3 days ago
            e = [x for x in p.store.user_events("U-1", T0 + timedelta(minutes=1)) if x.event_id == raw["event_id"]][0]
            self.assertLess(e.time, T0 - timedelta(days=2))
        finally:
            p.close()

    def test_clock_skew_does_not_make_fresh_trigger_stale(self):
        p = std_pack()
        try:
            raw = ev("U-1", "app_open", T0, skew_s=-8 * 3600)  # phone clock 8 h behind, but sent immediately
            d = p.trigger(raw)
            self.assertFalse(d["trigger"]["stale"])
            self.assertIn("phone_clock_wrong", d["trigger"]["anomalies"])
            self.assertLess(d["trigger"]["event_age_s"], 5)
            self.assertEqual(d["status"], "PUBLISHED")
        finally:
            p.close()


class Failures(unittest.TestCase):
    def test_store_unavailable_fails_closed(self):
        p = std_pack()
        try:
            p.world.store_gate.add_window(T0 - timedelta(minutes=1), T0 + timedelta(minutes=5), timeout_share=1.0)
            d = p.trigger(ev("U-1", "app_open", T0))
            self.assertEqual(d["status"], "ABSTAINED")
            self.assertIn("fail closed", d["no_action_reason"])
            self.assertEqual(p.actions("U-1"), [])
            self.assertTrue(d["degradations"])
            # bounded: 2 attempts x 2.5 s timeout, then give up
            self.assertLessEqual(d["sim_dependency_ms"], p.cfg.store_read_max_attempts * p.cfg.store_read_timeout_ms)
            self.assertLessEqual(d["tool_calls"], p.cfg.max_tool_calls)
        finally:
            p.close()

    def test_model_timeout_bounded_fallback(self):
        raw = ev("U-1", "app_open", T0, eid="trig-timeout")
        p = std_pack(faults=[{"trigger_event_id": "trig-timeout", "user_id": "U-1", "fault": "timeout", "seconds": 45}])
        try:
            d = p.trigger(raw)
            self.assertEqual(d["model"]["status"], "timeout")
            self.assertEqual(d["model_calls"], p.cfg.model_max_attempts)
            self.assertTrue(d["fallback_used"])
            self.assertEqual(d["goal"]["id"], "G07", "deterministic priority used")
            self.assertEqual(d["status"], "PUBLISHED", "the fallback is itself independently verified")
            self.assertLess(d["decided_in_ms"], 60_000)
            self.assertGreaterEqual(d["sim_dependency_ms"], p.cfg.model_max_attempts * p.cfg.model_timeout_ms)
        finally:
            p.close()

    def test_broken_json_rejected_with_bounded_retries(self):
        raw = ev("U-1", "app_open", T0, eid="trig-bad")
        p = std_pack(faults=[{"trigger_event_id": "trig-bad", "user_id": "U-1", "fault": "malformed_output",
                              "model_output": "{\"goal_id\": \"G02\", \"eligible\": tr"}])
        try:
            d = p.trigger(raw)
            self.assertEqual(d["model"]["status"], "malformed")
            self.assertEqual(d["model"]["attempts"], p.cfg.model_max_attempts)
            self.assertEqual(d["model_calls"], p.cfg.model_max_attempts)
            self.assertTrue(d["fallback_used"])
            self.assertNotEqual(d["goal"]["id"], "G02")
        finally:
            p.close()

    def test_unsupported_model_claim_rejected(self):
        raw = ev("U-1", "app_open", T0, eid="trig-claim")
        p = std_pack(faults=[{"trigger_event_id": "trig-claim", "user_id": "U-1", "fault": "unsupported_claim",
                              "model_output": {"goal_id": "G04", "eligible": True, "confidence": 0.97,
                                               "reason": "The user is on an active trial (confirmed in billing)."}}])
        try:
            d = p.trigger(raw)
            self.assertEqual(d["model"]["status"], "rejected_unsupported_claim")
            self.assertEqual(d["model"]["attempts"], 1, "a rejected claim is not retried")
            self.assertNotEqual(d["goal"]["id"], "G04")
            self.assertIn("goal:model_claim_rejected", d["checks"]["passed"])
            g04 = [c for c in d["goal_candidates"] if c["goal_id"] == "G04"][0]
            self.assertFalse(g04["eligible"])
        finally:
            p.close()

    def test_model_outage_circuit_breaker(self):
        users = [f"U-{k}" for k in range(1, 6)]
        events = sum((standard_user_history(u) for u in users), [])
        p = Pack(events=events, profiles=[profile(u) for u in users], arms={u: "agent" for u in users})
        try:
            p.world.outage.add_window(T0 - timedelta(minutes=1), T0 + timedelta(minutes=10))
            statuses = [p.trigger(ev(u, "app_open", T0 + timedelta(seconds=k)))["model"]["status"]
                        for k, u in enumerate(users)]
            self.assertIn("unavailable", statuses)
            self.assertIn("skipped_circuit_open", statuses, "repeated 503s open the circuit")
            for a in p.actions():
                self.assertIn(a["type"], ("home_page", "push", "whatsapp", "in_app_message"))
            self.assertTrue(all(d["fallback_used"] for d in p.store.decisions()))
        finally:
            p.close()


class Burst(unittest.TestCase):
    def test_burst_prioritises_fresh_and_sheds_model_work(self):
        from stage_agent.config import AgentConfig
        users = [f"U-{k:03d}" for k in range(60)]
        hist = sum((standard_user_history(u) for u in users), [])
        at = T0 + timedelta(hours=10)  # evening peak
        # stale opens arrive 100 ms after the first fresh one, i.e. while fresh ones are already queued
        stale = [ev(u, "app_open", at - timedelta(days=2) + timedelta(milliseconds=100), buffer_s=2 * 86400)
                 for u in users[:20]]
        fresh = [ev(u, "app_open", at + timedelta(milliseconds=k)) for k, u in enumerate(users)]
        trig = sorted(stale + fresh, key=lambda e: e["deliver_at"])
        cfg = AgentConfig(workers=1, shed_queue_depth=10)
        p = Pack(events=hist, profiles=[profile(u) for u in users], triggers=trig,
                 arms={u: "agent" for u in users}, cfg=cfg)
        try:
            r = run_replay(p.world)
            lat = r["latency"]
            self.assertEqual(len(lat), 80)
            fresh_done = [x for x in lat if not x["stale"]]
            stale_done = [x for x in lat if x["stale"]]
            # with one worker the queue builds; no stale open is served while a fresh one is waiting
            order = [x["stale"] for x in lat]
            self.assertEqual(order, sorted(order), "all fresh triggers are dispatched before stale ones")
            self.assertIn("skipped_load_shedding", {x["model_status"] for x in fresh_done})
            self.assertLess(max(x["latency_ms"] for x in lat), 60_000)
            self.assertTrue(all(x["status"] == "PUBLISHED" for x in fresh_done))
            self.assertTrue(all(x["status"] == "ABSTAINED" for x in stale_done))
        finally:
            p.close()


class IdempotentReplay(unittest.TestCase):
    def test_replay_twice_same_database(self):
        trig = ev("U-1", "app_open", T0, eid="trig-r")
        dup = dict(trig, deliver_at="2026-10-01T04:40:00.000Z", received_at="2026-10-01T04:40:00.000Z")
        trig2 = ev("U-1", "app_open", T0 + timedelta(hours=3), eid="trig-r2")
        live = [ev("U-1", "play_started", T0 + timedelta(minutes=5), props={"title_id": "T1002",
                                                                              "episode_id": "s2-d1-s1e1"})]
        controls = [{"at": "2026-10-01T05:00:00.000Z", "type": "title_withdrawn", "title_id": "T1003",
                     "reason": "test"}]
        p = std_pack(triggers=[trig, dup, trig2], live=live, controls=controls)
        try:
            r1 = run_replay(p.world)
            acts1 = sorted((a["action_id"], a["status"]) for a in p.actions())
            decs1 = sorted(d["decision_id"] for d in p.store.decisions())
            from stage_agent.replay import build_world
            p.world.store.close()
            p.world = build_world(p.root, p.db, p.cfg)  # restart against the same durable state
            r2 = run_replay(p.world)
            acts2 = sorted((a["action_id"], a["status"]) for a in p.actions())
            decs2 = sorted(d["decision_id"] for d in p.store.decisions())
            self.assertEqual(acts1, acts2)
            self.assertEqual(decs1, decs2)
            self.assertEqual(r2["already_decided"], 3)
            self.assertEqual(len(r1["latency"]), 3)
            self.assertTrue(any(c.get("skipped") for c in r2["controls"]), "control events applied once")
        finally:
            p.close()


if __name__ == "__main__":
    unittest.main()
