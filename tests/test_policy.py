"""Verifier and policy: holdout, consent, quiet hours, caps, dialect, availability, slugs, rows, injection, copy."""
import logging
import unittest
from datetime import datetime, timedelta

from helpers import FIX, T0, UTC, Pack, ev, profile, standard_user_history

from stage_agent.content_selector import Catalogue
from stage_agent.normalization import normalize_event
from stage_agent.scheduler import IST, QuietHours, ist_day, iso, parse_ts, plan_message_slot
from stage_agent.verifier import VerifyInput


def verify(p, user, actions, at=None, dialect="d1", goal="open"):
    raw = ev(user, "app_open", at or T0 + timedelta(minutes=30), dialect=dialect)
    e = normalize_event(raw, "trigger")
    g = p.store.open_goal(user) if goal == "open" else goal
    vi = VerifyInput(user_id=user, trigger=e, now=e.arrival, publish_at=e.arrival + timedelta(seconds=2), goal=g,
                     goal_is_new=False, deterministic_eligible=[], goal_source="kept", model_status=None,
                     actions=actions, trigger_stale=False)
    return p.rt.verifier.verify(vi)


def failed(res, name, subject_prefix=""):
    return any((not c.passed) and c.name == name and c.subject.startswith(subject_prefix) for c in res.checks)


def page(dialect="d1", hero="T1005", rows=None, at=T0):
    return {"type": "home_page", "dialect": dialect, "hero_title_id": hero,
            "rows": rows if rows is not None else [{"row_type": "popular_in_dialect", "title_ids": ["T1006", "T1007", "T1008"]}],
            "expires_at": iso(at + timedelta(minutes=150))}


def push(title_id="T1005", send_at="2026-10-01T09:30:00.000Z", title=None, body=None, deeplink=None):
    name = f"Title {title_id}"
    return {"type": "push", "send_at": send_at, "title_id": title_id,
            "text": {"title": title or name, "body": body or f"Aapke liye: {name}. Abhi dekhiye."},
            "deeplink": deeplink or f"ott://title/{title_id}"}


def wa(title_id="T1005", send_at="2026-10-01T09:30:00.000Z", template="wa_recommend_title_v1", variables=None):
    return {"type": "whatsapp", "send_at": send_at, "template_id": template, "title_id": title_id,
            "variables": variables if variables is not None else {"title_name": f"Title {title_id}", "dialect_name": "d1"}}


class Holdout(unittest.TestCase):
    def test_holdout_zero_interventions(self):
        p = Pack(events=standard_user_history("U-H"), profiles=[profile("U-H", wa="true")], arms={"U-H": "holdout"})
        try:
            for k in range(3):
                d = p.trigger(ev("U-H", "app_open", T0 + timedelta(hours=k)))
                self.assertEqual(d["status"], "HOLDOUT")
                self.assertEqual(d["action"], "NONE")
                self.assertEqual(d["actions"], [])
            self.assertEqual(p.actions("U-H"), [], "no page, push, WhatsApp or in-app action")
            g = p.store.open_goal("U-H")
            self.assertEqual(g["arm"], "holdout", "goal recorded for measurement only")
            res = verify(p, "U-H", [page()])
            self.assertTrue(failed(res, "holdout_exclusion"))
            self.assertTrue(res.decision_blocked)
            res2 = verify(p, "U-H", [{"type": "in_app_message", "text": "Plan sirf ₹149", "plan_id": "P_MONTH"}])
            self.assertTrue(res2.decision_blocked, "in-app is not a loophole")
        finally:
            p.close()

    def test_unknown_user_treated_as_holdout(self):
        p = Pack(events=standard_user_history("U-?"), profiles=[profile("U-?")], arms={})
        try:
            d = p.trigger(ev("U-?", "app_open", T0))
            self.assertEqual(d["status"], "HOLDOUT")
            self.assertEqual(p.actions(), [])
        finally:
            p.close()


class Consent(unittest.TestCase):
    def setUp(self):
        self.p = Pack(events=standard_user_history("U-1") + standard_user_history("U-2"),
                      profiles=[profile("U-1", push="false", wa="false"), profile("U-2", push="true", wa="true")],
                      arms={"U-1": "agent", "U-2": "agent"})
        self.p.trigger(ev("U-1", "app_open", T0))

    def tearDown(self):
        self.p.close()

    def test_push_without_consent_rejected(self):
        self.assertFalse(any(a["type"] == "push" for a in self.p.actions("U-1")), "planner proposes none")
        res = verify(self.p, "U-1", [push()])
        self.assertTrue(failed(res, "consent", "a0:push"))
        self.assertIn("a0:push", res.blocked)

    def test_whatsapp_without_consent_rejected(self):
        res = verify(self.p, "U-1", [wa()])
        self.assertTrue(failed(res, "consent", "a0:whatsapp"))
        self.assertIn("a0:whatsapp", res.blocked)

    def test_consented_message_passes(self):
        self.p.trigger(ev("U-2", "app_open", T0))
        res = verify(self.p, "U-2", [wa(send_at="2026-10-02T04:00:00.000Z")])
        self.assertFalse(failed(res, "consent"))


class TimingAndCaps(unittest.TestCase):
    def test_quiet_hours_scheduler(self):
        q = QuietHours("22:00", "08:00")
        late = datetime(2026, 10, 1, 17, 30, tzinfo=UTC)  # 23:00 IST
        self.assertTrue(q.contains(late))
        slot = plan_message_slot(late, late + timedelta(days=3), q, set(), 9)
        self.assertEqual(slot.astimezone(IST).strftime("%Y-%m-%d %H:%M"), "2026-10-02 09:00")
        self.assertFalse(q.contains(datetime(2026, 10, 1, 2, 30, tzinfo=UTC)))  # 08:00 IST allowed
        self.assertTrue(q.contains(datetime(2026, 10, 1, 2, 29, tzinfo=UTC)))  # 07:59 IST quiet
        self.assertTrue(q.contains(datetime(2026, 10, 1, 16, 30, tzinfo=UTC)))  # 22:00 IST quiet
        self.assertIsNone(plan_message_slot(late, late + timedelta(hours=5), q, set(), 9), "never past the deadline")

    def test_quiet_hours_verifier(self):
        p = Pack(events=standard_user_history("U-1"), profiles=[profile("U-1")], arms={"U-1": "agent"})
        try:
            p.trigger(ev("U-1", "app_open", T0))
            res = verify(p, "U-1", [push(send_at="2026-10-02T18:00:00.000Z")])  # 23:30 IST
            self.assertTrue(failed(res, "quiet_hours_ist"))
            for a in p.actions("U-1"):
                if a["type"] in ("push", "whatsapp"):
                    self.assertFalse(QuietHours().contains(parse_ts(a["send_at"])))
        finally:
            p.close()

    def test_combined_daily_cap(self):
        p = Pack(events=standard_user_history("U-1"), profiles=[profile("U-1", wa="true")], arms={"U-1": "agent"})
        try:
            p.trigger(ev("U-1", "app_open", T0))
            pushes = [a for a in p.actions("U-1") if a["type"] == "push"]
            self.assertEqual(len(pushes), 1)
            day = ist_day(parse_ts(pushes[0]["send_at"]))
            same_day = iso(datetime(day.year, day.month, day.day, 15, 0, tzinfo=IST))
            res = verify(p, "U-1", [wa(send_at=same_day)])
            self.assertTrue(failed(res, "daily_message_cap", "a0:whatsapp"), "push + WhatsApp share one daily cap")
            res2 = verify(p, "U-1", [push(), wa(send_at="2026-10-03T04:00:00.000Z")])
            self.assertTrue(failed(res2, "one_message_channel"))
        finally:
            p.close()

    def test_cap_uses_ist_day_across_utc_midnight(self):
        # delivered 00:10 IST on Oct 1 = 18:40 UTC on Sep 30: same IST day as 09:00 IST Oct 1, different UTC day
        hist = standard_user_history("U-1") + [ev("U-1", "message_delivered", datetime(2026, 9, 30, 18, 40, tzinfo=UTC),
                                                  props={"channel": "push", "campaign_id": "c1", "title_id": "T1005"},
                                                  deliver=False)]
        p = Pack(events=hist, profiles=[profile("U-1")], arms={"U-1": "agent"})
        try:
            d = p.trigger(ev("U-1", "app_open", T0))
            res = verify(p, "U-1", [push(send_at="2026-10-01T03:30:00.000Z")])
            self.assertTrue(failed(res, "daily_message_cap"))
            msgs = [a for a in p.actions("U-1") if a["type"] == "push"]
            for a in msgs:
                self.assertNotEqual(ist_day(parse_ts(a["send_at"])).isoformat(), "2026-10-01")
            slot = plan_message_slot(datetime(2026, 10, 1, 3, 30, tzinfo=UTC), datetime(2026, 10, 4, tzinfo=UTC),
                                     QuietHours(), {ist_day(datetime(2026, 9, 30, 18, 40, tzinfo=UTC))}, 9)
            self.assertEqual(slot.astimezone(IST).strftime("%Y-%m-%d %H:%M"), "2026-10-02 09:00")
        finally:
            p.close()


class Content(unittest.TestCase):
    def setUp(self):
        self.p = Pack(events=standard_user_history("U-1"), profiles=[profile("U-1")], arms={"U-1": "agent"})
        self.d = self.p.trigger(ev("U-1", "app_open", T0))

    def tearDown(self):
        self.p.close()

    def page_ids(self, a):
        return ([a["raw"]["hero_title_id"]] if a["raw"].get("hero_title_id") else []) + \
               [t for r in a["raw"]["rows"] for t in r["title_ids"]]

    def test_dialect_mismatch_rejected(self):
        res = verify(self.p, "U-1", [page("d2", "T2001", [{"row_type": "popular_in_dialect",
                                                          "title_ids": ["T2002", "T2003", "T2004"]}])])
        self.assertTrue(failed(res, "dialect_matches_app_setting"))
        res2 = verify(self.p, "U-1", [page(rows=[{"row_type": "genre", "title_ids": ["T1006", "T1007", "T2001"]}])])
        self.assertTrue(failed(res2, "row0_dialect"))
        res3 = verify(self.p, "U-1", [push(title_id="T2001")])
        self.assertTrue(failed(res3, "message_title_dialect"))

    def test_withdrawn_title_never_published(self):
        pg = [a for a in self.p.actions("U-1") if a["type"] == "home_page"][0]
        self.assertIn("T1001", self.page_ids(pg), "fixture: the unfinished series is the hero")
        msg = [a for a in self.p.actions("U-1") if a["type"] == "push"][0]
        self.assertEqual(msg["raw"]["title_id"], "T1001")
        at = T0 + timedelta(minutes=10)
        res = self.p.rt.on_withdrawal({"title_id": "T1001", "reason": "licence"}, at, lambda: 999)
        st = {a["action_id"]: a for a in self.p.actions("U-1")}
        self.assertEqual(st[msg["action_id"]]["status"], "cancelled")
        self.assertEqual(st[pg["action_id"]]["status"], "superseded")
        repaired = [a for a in self.p.actions("U-1") if a["raw"].get("repair_of") == pg["action_id"]]
        self.assertEqual(len(repaired), 1)
        self.assertNotIn("T1001", self.page_ids(repaired[0]))
        self.assertEqual(res[0].decision["status"], "PUBLISHED")
        bad = verify(self.p, "U-1", [page(hero="T1001")], at=at + timedelta(minutes=5))
        self.assertTrue(failed(bad, "hero_valid"))
        d2 = self.p.trigger(ev("U-1", "app_open", T0 + timedelta(hours=3), dialect="d1"))
        for a in self.p.actions("U-1"):
            if a["status"] in ("published", "scheduled") and parse_ts(a["published_at"]) >= at:
                self.assertNotIn("T1001", a["title_ids"])
        self.assertEqual(self.p.rt.send_time_check(st[msg["action_id"]], at)[0], False)

    def test_licence_ending_inside_page_ttl_excluded(self):
        pg = [a for a in self.p.actions("U-1") if a["type"] == "home_page"][0]
        self.assertNotIn("T1012", self.page_ids(pg), "T1012's licence ends 90 min after publication")
        res = verify(self.p, "U-1", [page(rows=[{"row_type": "genre", "title_ids": ["T1006", "T1007", "T1012"]}])])
        self.assertTrue(failed(res, "row0_availability"))

    def test_duplicate_slug_removed(self):
        pg = [a for a in self.p.actions("U-1") if a["type"] == "home_page"][0]
        ids = self.page_ids(pg)
        self.assertFalse({"T1001", "T1013"} <= set(ids), "language variants share one slug")
        slugs = [self.p.world.catalogue.by_id[t].content_slug for t in ids]
        self.assertEqual(len(slugs), len(set(slugs)))
        res = verify(self.p, "U-1", [page(hero="T1001", rows=[{"row_type": "genre", "title_ids": ["T1013", "T1006", "T1007"]}])])
        self.assertTrue(failed(res, "content_slug_unique"))

    def test_short_row_dropped(self):
        pg = [a for a in self.p.actions("U-1") if a["type"] == "home_page"][0]
        self.assertTrue(all(3 <= len(r["title_ids"]) <= 10 for r in pg["raw"]["rows"]))
        self.assertLessEqual(len(pg["raw"]["rows"]), 6)
        self.assertNotIn("continue_watching", [r["row_type"] for r in pg["raw"]["rows"]])
        res = verify(self.p, "U-1", [page(rows=[{"row_type": "genre", "title_ids": ["T1006", "T1007"]}])])
        self.assertTrue(failed(res, "row0_size"))
        res2 = verify(self.p, "U-1", [page(rows=[{"row_type": "continue_watching", "title_ids": ["T1006", "T1007", "T1008"]}])])
        self.assertTrue(failed(res2, "row0_type_allowed"))

    def test_prompt_injection_ignored(self):
        cat = self.p.world.catalogue
        self.assertTrue(cat.by_id["T1014"].suspicious)
        self.assertEqual(max(cat.popularity, key=cat.popularity.get), "s14-d1", "it is the most popular title")
        with self.assertLogs("stage_agent.content", level="WARNING") as logs:
            Catalogue.load(self.p.world.paths.catalogue, self.p.world.paths.episodes)
        self.assertTrue(any("T1014" in str(r.__dict__) for r in logs.records))
        for a in self.p.actions():
            self.assertNotIn("T1014", a["title_ids"])
        self.assertEqual({a["user_id"] for a in self.p.actions()}, {"U-1"}, "no mass messaging")
        self.assertEqual([a for a in self.p.actions() if a["type"] == "whatsapp"], [])
        res = verify(self.p, "U-1", [page(rows=[{"row_type": "genre", "title_ids": ["T1006", "T1007", "T1014"]}])])
        self.assertTrue(failed(res, "row0_quarantine"))

    def test_copy_rules(self):
        res = verify(self.p, "U-1", [push(body="Hurry! Title T1005 only today for ₹99, guaranteed.")])
        for name in ("forbidden_phrases", "pricing_honest"):
            self.assertTrue(failed(res, name), name)
        res = verify(self.p, "U-1", [push(title="x" * 41)])
        self.assertTrue(failed(res, "push_length"))
        res = verify(self.p, "U-1", [push(deeplink="ott://c/cmp1/d1/title/T1005")])
        self.assertTrue(failed(res, "deeplink_format"))
        res = verify(self.p, "U-1", [wa(template="wa_free_text")])
        self.assertTrue(failed(res, "template_approved"))
        res = verify(self.p, "U-1", [wa(variables={"title_name": "Watch now, it's free!", "dialect_name": "d1"})])
        self.assertTrue(failed(res, "template_values_sourced"))
        res = verify(self.p, "U-1", [wa(template="wa_new_episode_v2",
                                        variables={"title_name": "Title T1005", "episode_number": "3"})])
        self.assertTrue(failed(res, "template_values_sourced"), "unverifiable 'new episode' claim")

    def test_verifier_failure_fails_closed(self):
        v = self.p.rt.verifier
        orig = v._verify

        def boom(*a, **k):
            raise RuntimeError("verifier crashed")

        v._verify = boom
        try:
            d = self.p.trigger(ev("U-1", "app_open", T0 + timedelta(hours=5)))
        finally:
            v._verify = orig
        new = [a for a in self.p.actions("U-1") if a["decision_id"] == d["decision_id"]]
        self.assertEqual(new, [])
        self.assertTrue(any("verifier" in x for x in d["degradations"]))
        self.assertTrue(any(f["check"].endswith("verifier_error") for f in d["checks"]["failed"]))


if __name__ == "__main__":
    unittest.main()
