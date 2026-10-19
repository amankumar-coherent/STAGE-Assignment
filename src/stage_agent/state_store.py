"""Durable per-viewer state in SQLite.

Idempotency is enforced by the schema, not only by application code:
  * events.event_id is the primary key (duplicate deliveries are counted, not stored twice);
  * processed_triggers.event_id makes a trigger decide at most once;
  * actions.idempotency_key is UNIQUE (retrying a trigger cannot publish twice);
  * a partial UNIQUE index allows at most one open goal per user.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import Event
from .normalization import normalize_event
from .scheduler import iso, parse_ts

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, event TEXT NOT NULL, received_at TEXT NOT NULL,
    arrival TEXT NOT NULL, est_time TEXT NOT NULL, source TEXT NOT NULL, buffer_delay_s REAL NOT NULL,
    raw TEXT NOT NULL, dup_count INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_events_user ON events(user_id, arrival);
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, updated_at TEXT, raw TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_profiles_user ON profiles(user_id);
CREATE TABLE IF NOT EXISTS goal_rules (
    goal_id TEXT NOT NULL, revision INTEGER NOT NULL, effective_at TEXT, source TEXT NOT NULL,
    raw TEXT NOT NULL, PRIMARY KEY (goal_id, revision));
CREATE TABLE IF NOT EXISTS goals (
    instance_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, goal_id TEXT NOT NULL, rule_version TEXT NOT NULL,
    status TEXT NOT NULL, started_at TEXT NOT NULL, deadline TEXT NOT NULL, success_event TEXT,
    success_condition TEXT, decision_id TEXT, arm TEXT, eligibility TEXT NOT NULL, closed_at TEXT,
    close_reason TEXT, outcome TEXT, history TEXT NOT NULL DEFAULT '[]');
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_open_goal ON goals(user_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS ix_goals_goal ON goals(goal_id, status);
CREATE TABLE IF NOT EXISTS processed_triggers (
    event_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, user_id TEXT NOT NULL, deliveries INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY, trigger_event_id TEXT NOT NULL, user_id TEXT NOT NULL, seq INTEGER NOT NULL,
    deliver_at TEXT NOT NULL, status TEXT NOT NULL, raw TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_decisions_user ON decisions(user_id, seq);
CREATE INDEX IF NOT EXISTS ix_decisions_trigger ON decisions(trigger_event_id, deliver_at);
CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, decision_id TEXT NOT NULL,
    user_id TEXT NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL, published_at TEXT NOT NULL,
    send_at TEXT, goal_instance_id TEXT, title_ids TEXT NOT NULL, raw TEXT NOT NULL, status_reason TEXT,
    updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_actions_user ON actions(user_id, type, status);
CREATE INDEX IF NOT EXISTS ix_actions_due ON actions(status, send_at);
CREATE TABLE IF NOT EXISTS withdrawals (title_id TEXT PRIMARY KEY, at TEXT NOT NULL, ref TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outcome_events (
    event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, event TEXT NOT NULL, time TEXT NOT NULL, raw TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_outcome_user ON outcome_events(user_id, time);
CREATE TABLE IF NOT EXISTS outcomes (
    instance_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, goal_id TEXT NOT NULL, result TEXT NOT NULL,
    final INTEGER NOT NULL, judged_at TEXT NOT NULL, raw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learning (user_id TEXT PRIMARY KEY, raw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, user_id TEXT, kind TEXT NOT NULL, raw TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_audit_user ON audit(user_id);
"""


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


class StateStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL" if self.path != ":memory:" else "PRAGMA journal_mode=MEMORY")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ transactions
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serializable write transaction (BEGIN IMMEDIATE takes the write lock up front)."""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))

    # ------------------------------------------------------------------ events
    def insert_events(self, events: Iterable[tuple[Event, dict[str, Any]]]) -> tuple[int, int]:
        """Insert (event, raw) pairs. Returns (inserted, duplicates). First delivery wins."""
        ins = dup = 0
        with self.tx() as c:
            for e, raw in events:
                cur = c.execute(
                    "INSERT INTO events(event_id,user_id,event,received_at,arrival,est_time,source,buffer_delay_s,raw)"
                    " VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING",
                    (e.event_id, e.user_id, e.event, iso(e.received_at), iso(e.arrival), iso(e.time), e.source,
                     e.buffer_delay_s, _dumps(raw)))
                if cur.rowcount == 1:
                    ins += 1
                else:
                    dup += 1
                    c.execute("UPDATE events SET dup_count = dup_count + 1 WHERE event_id=?", (e.event_id,))
        return ins, dup

    def has_event(self, event_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone() is not None

    def user_events(self, user_id: str, known_at: datetime, clock_skew_flag_s: int = 600) -> list[Event]:
        """Events for a user that had reached the agent by `known_at`, ordered by estimated occurrence."""
        rows = self.conn.execute(
            "SELECT raw, source FROM events WHERE user_id=? AND arrival<=? ORDER BY est_time, event_id",
            (user_id, iso(known_at))).fetchall()
        out = []
        for r in rows:
            e = normalize_event(json.loads(r["raw"]), r["source"], clock_skew_flag_s)
            if e is not None:
                out.append(e)
        return out

    def event_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def event_exists_ref(self, event_id: str) -> bool:
        return (self.has_event(event_id) or self.conn.execute(
            "SELECT 1 FROM outcome_events WHERE event_id=?", (event_id,)).fetchone() is not None)

    # ------------------------------------------------------------------ profiles
    def insert_profiles(self, rows: list[dict[str, str]]) -> None:
        with self.tx() as c:
            c.executemany("INSERT INTO profiles(user_id, updated_at, raw) VALUES(?,?,?)",
                          [(r["user_id"], r.get("updated_at"), _dumps(r)) for r in rows])

    def profile_rows(self, user_id: str) -> list[dict[str, str]]:
        rows = self.conn.execute("SELECT raw FROM profiles WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
        return [json.loads(r["raw"]) for r in rows]

    def profile_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]

    # ------------------------------------------------------------------ goal rules
    def put_goal_rule(self, goal_id: str, revision: int, raw: dict[str, Any], source: str,
                      effective_at: datetime | None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO goal_rules(goal_id,revision,effective_at,source,raw) VALUES(?,?,?,?,?)"
                      " ON CONFLICT(goal_id,revision) DO NOTHING",
                      (goal_id, revision, iso(effective_at), source, _dumps(raw)))

    def goal_rules(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM goal_rules ORDER BY goal_id, revision").fetchall()
        return [dict(r) | {"raw": json.loads(r["raw"])} for r in rows]

    # ------------------------------------------------------------------ goals
    def open_goal(self, user_id: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM goals WHERE user_id=? AND status='open'", (user_id,)).fetchone()
        return self._goal_row(r) if r else None

    def goals_for_user(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM goals WHERE user_id=? ORDER BY started_at", (user_id,)).fetchall()
        return [self._goal_row(r) for r in rows]

    def all_goals(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM goals ORDER BY started_at, instance_id").fetchall()
        return [self._goal_row(r) for r in rows]

    def open_goals_for_goal_id(self, goal_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM goals WHERE goal_id=? AND status='open' ORDER BY instance_id",
                                 (goal_id,)).fetchall()
        return [self._goal_row(r) for r in rows]

    @staticmethod
    def _goal_row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["eligibility"] = json.loads(d["eligibility"])
        d["history"] = json.loads(d["history"] or "[]")
        d["outcome"] = json.loads(d["outcome"]) if d["outcome"] else None
        return d

    def insert_goal(self, c: sqlite3.Connection, g: dict[str, Any]) -> None:
        c.execute(
            "INSERT INTO goals(instance_id,user_id,goal_id,rule_version,status,started_at,deadline,success_event,"
            "success_condition,decision_id,arm,eligibility,history) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (g["instance_id"], g["user_id"], g["goal_id"], g["rule_version"], "open", g["started_at"],
             g["deadline"], g["success_event"], g["success_condition"], g["decision_id"], g.get("arm"),
             _dumps(g["eligibility"]), _dumps(g.get("history", []))))

    def close_goal(self, c: sqlite3.Connection, instance_id: str, status: str, at: datetime, reason: str,
                   outcome: dict[str, Any] | None = None) -> bool:
        cur = c.execute(
            "UPDATE goals SET status=?, closed_at=?, close_reason=?, outcome=? WHERE instance_id=? AND status='open'",
            (status, iso(at), reason, _dumps(outcome) if outcome else None, instance_id))
        if cur.rowcount:
            self._append_goal_history(c, instance_id, {"at": iso(at), "change": f"status->{status}", "reason": reason})
        return cur.rowcount == 1

    def revise_goal_rule(self, c: sqlite3.Connection, instance_id: str, rule_version: str, success_event: str,
                         success_condition: str, at: datetime, reason: str) -> None:
        c.execute("UPDATE goals SET rule_version=?, success_event=?, success_condition=? WHERE instance_id=?",
                  (rule_version, success_event, success_condition, instance_id))
        self._append_goal_history(c, instance_id, {"at": iso(at), "change": f"rule->{rule_version}", "reason": reason})

    def _append_goal_history(self, c: sqlite3.Connection, instance_id: str, entry: dict[str, Any]) -> None:
        r = c.execute("SELECT history FROM goals WHERE instance_id=?", (instance_id,)).fetchone()
        hist = json.loads(r["history"] or "[]") if r else []
        hist.append(entry)
        c.execute("UPDATE goals SET history=? WHERE instance_id=?", (_dumps(hist), instance_id))

    # ------------------------------------------------------------------ triggers / decisions
    def processed_decision(self, event_id: str) -> str | None:
        r = self.conn.execute("SELECT decision_id FROM processed_triggers WHERE event_id=?", (event_id,)).fetchone()
        return r["decision_id"] if r else None

    def delivery_recorded(self, event_id: str, deliver_at: str) -> bool:
        return self.conn.execute("SELECT 1 FROM decisions WHERE trigger_event_id=? AND deliver_at=?",
                                 (event_id, deliver_at)).fetchone() is not None

    def mark_redelivery(self, event_id: str) -> int:
        with self.tx() as c:
            c.execute("UPDATE processed_triggers SET deliveries = deliveries + 1 WHERE event_id=?", (event_id,))
            r = c.execute("SELECT deliveries FROM processed_triggers WHERE event_id=?", (event_id,)).fetchone()
        return int(r["deliveries"]) if r else 0

    def claim_trigger(self, c: sqlite3.Connection, event_id: str, decision_id: str, user_id: str) -> bool:
        cur = c.execute("INSERT INTO processed_triggers(event_id,decision_id,user_id) VALUES(?,?,?)"
                        " ON CONFLICT(event_id) DO NOTHING", (event_id, decision_id, user_id))
        return cur.rowcount == 1

    def insert_decision(self, c: sqlite3.Connection, d: dict[str, Any], seq: int) -> bool:
        cur = c.execute("INSERT INTO decisions(decision_id,trigger_event_id,user_id,seq,deliver_at,status,raw)"
                        " VALUES(?,?,?,?,?,?,?) ON CONFLICT(decision_id) DO NOTHING",
                        (d["decision_id"], d["trigger"]["event_id"], d["viewer_id"], seq, d["trigger"]["deliver_at"],
                         d["status"], _dumps(d)))
        return cur.rowcount == 1

    def update_decision(self, c: sqlite3.Connection, d: dict[str, Any]) -> None:
        c.execute("UPDATE decisions SET raw=?, status=? WHERE decision_id=?", (_dumps(d), d["status"], d["decision_id"]))

    def decisions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT raw FROM decisions ORDER BY seq, decision_id").fetchall()
        return [json.loads(r["raw"]) for r in rows]

    def decisions_for_user(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT raw FROM decisions WHERE user_id=? ORDER BY seq", (user_id,)).fetchall()
        return [json.loads(r["raw"]) for r in rows]

    def next_seq(self) -> int:
        r = self.conn.execute("SELECT COALESCE(MAX(seq), 0) FROM decisions").fetchone()
        return int(r[0]) + 1

    # ------------------------------------------------------------------ actions
    def insert_action(self, c: sqlite3.Connection, a: dict[str, Any], idem_key: str, status: str,
                      goal_instance_id: str | None, title_ids: list[str]) -> bool:
        cur = c.execute(
            "INSERT INTO actions(action_id,idempotency_key,decision_id,user_id,type,status,published_at,send_at,"
            "goal_instance_id,title_ids,raw,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(idempotency_key) DO NOTHING",
            (a["action_id"], idem_key, a["decision_id"], a["user_id"], a["type"], status, a["published_at"],
             a.get("send_at"), goal_instance_id, _dumps(title_ids), _dumps(a), a["published_at"]))
        return cur.rowcount == 1

    def set_action_status(self, c: sqlite3.Connection, action_id: str, status: str, reason: str,
                          at: datetime) -> bool:
        cur = c.execute("UPDATE actions SET status=?, status_reason=?, updated_at=? WHERE action_id=? AND status!=?",
                        (status, reason, iso(at), action_id, status))
        return cur.rowcount == 1

    def actions_for_user(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM actions WHERE user_id=? ORDER BY published_at, action_id",
                                 (user_id,)).fetchall()
        return [self._action_row(r) for r in rows]

    def all_actions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM actions ORDER BY published_at, action_id").fetchall()
        return [self._action_row(r) for r in rows]

    def scheduled_messages(self, due_by: datetime | None = None) -> list[dict[str, Any]]:
        if due_by is None:
            rows = self.conn.execute("SELECT * FROM actions WHERE status='scheduled' ORDER BY send_at, action_id").fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM actions WHERE status='scheduled' AND send_at<=? ORDER BY send_at,"
                                     " action_id", (iso(due_by),)).fetchall()
        return [self._action_row(r) for r in rows]

    def action_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]

    @staticmethod
    def _action_row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["raw"] = json.loads(d["raw"])
        d["title_ids"] = json.loads(d["title_ids"])
        return d

    # ------------------------------------------------------------------ withdrawals
    def add_withdrawal(self, title_id: str, at: datetime, ref: str) -> bool:
        with self.tx() as c:
            cur = c.execute("INSERT INTO withdrawals(title_id,at,ref) VALUES(?,?,?) ON CONFLICT(title_id) DO NOTHING",
                            (title_id, iso(at), ref))
        return cur.rowcount == 1

    def withdrawals(self) -> dict[str, datetime]:
        return {r["title_id"]: parse_ts(r["at"]) for r in self.conn.execute("SELECT * FROM withdrawals")}

    # ------------------------------------------------------------------ outcomes / learning / audit
    def insert_outcome_events(self, rows: Iterable[tuple[str, str, str, str, dict[str, Any]]]) -> int:
        n = 0
        with self.tx() as c:
            for eid, uid, ev, t, raw in rows:
                cur = c.execute("INSERT INTO outcome_events(event_id,user_id,event,time,raw) VALUES(?,?,?,?,?)"
                                " ON CONFLICT(event_id) DO NOTHING", (eid, uid, ev, t, _dumps(raw)))
                n += cur.rowcount
        return n

    def clear_outcome_events(self) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM outcome_events")

    def outcome_events_for_user(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT raw FROM outcome_events WHERE user_id=? ORDER BY time, event_id",
                                 (user_id,)).fetchall()
        return [json.loads(r["raw"]) for r in rows]

    def outcome_horizon(self) -> str | None:
        r = self.conn.execute("SELECT MAX(time) FROM outcome_events").fetchone()
        return r[0]

    def upsert_outcome(self, c: sqlite3.Connection, o: dict[str, Any]) -> None:
        c.execute("INSERT INTO outcomes(instance_id,user_id,goal_id,result,final,judged_at,raw) VALUES(?,?,?,?,?,?,?)"
                  " ON CONFLICT(instance_id) DO UPDATE SET result=excluded.result, final=excluded.final,"
                  " judged_at=excluded.judged_at, raw=excluded.raw WHERE outcomes.final=0",
                  (o["goal_instance_id"], o["user_id"], o["goal_id"], o["result"], int(o["final"]), o["judged_at"],
                   _dumps(o)))

    def outcomes(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT raw FROM outcomes ORDER BY instance_id").fetchall()
        return [json.loads(r["raw"]) for r in rows]

    def get_learning(self, user_id: str) -> dict[str, Any]:
        r = self.conn.execute("SELECT raw FROM learning WHERE user_id=?", (user_id,)).fetchone()
        return json.loads(r["raw"]) if r else {}

    def put_learning(self, c: sqlite3.Connection, user_id: str, data: dict[str, Any]) -> None:
        c.execute("INSERT INTO learning(user_id,raw) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET raw=excluded.raw",
                  (user_id, _dumps(data)))

    def audit(self, kind: str, at: datetime, payload: dict[str, Any], user_id: str | None = None,
              c: sqlite3.Connection | None = None) -> None:
        sql = "INSERT INTO audit(at,user_id,kind,raw) VALUES(?,?,?,?)"
        args = (iso(at), user_id, kind, _dumps(payload))
        if c is not None:
            c.execute(sql, args)
        else:
            with self.tx() as cc:
                cc.execute(sql, args)

    def audit_rows(self, kind: str | None = None, user_id: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM audit WHERE 1=1", []
        if kind:
            q += " AND kind=?"
            args.append(kind)
        if user_id:
            q += " AND user_id=?"
            args.append(user_id)
        rows = self.conn.execute(q + " ORDER BY id", args).fetchall()
        return [dict(r) | {"raw": json.loads(r["raw"])} for r in rows]
