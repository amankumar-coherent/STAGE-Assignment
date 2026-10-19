"""Catalogue access and home-page / message content selection.

Every title chosen here is re-checked by the verifier; this module only proposes.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from .loader import read_csv
from .models import Event, Title
from .normalization import parse_title

log = logging.getLogger("stage_agent.content")


class Catalogue:
    def __init__(self, titles: Iterable[Title], episodes: list[dict[str, str]] | None = None):
        self.by_id: dict[str, Title] = {}
        self.by_dialect: dict[str, list[Title]] = defaultdict(list)
        self.by_slug: dict[str, list[Title]] = defaultdict(list)
        for t in titles:
            self.by_id[t.title_id] = t
            self.by_dialect[t.dialect].append(t)
            self.by_slug[t.content_slug].append(t)
        self.episodes: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
        for r in episodes or []:
            self.episodes[r["content_slug"]].append((int(r["season"]), int(r["episode"]), r["episode_id"]))
        for v in self.episodes.values():
            v.sort()
        self.episode_index: dict[str, tuple[str, int, int]] = {
            ep_id: (slug, s, e) for slug, eps in self.episodes.items() for (s, e, ep_id) in eps}
        self.withdrawn: dict[str, datetime] = {}
        self.popularity: dict[str, int] = {}
        self.suspicious = {t.title_id: t.suspicious_reason for t in self.by_id.values() if t.suspicious}
        for tid, why in sorted(self.suspicious.items()):
            log.warning("catalogue_text_flagged", extra={"title_id": tid, "matched": why,
                                                         "handling": "treated as content; title quarantined"})

    @classmethod
    def load(cls, catalogue_csv, episodes_csv) -> "Catalogue":
        return cls([parse_title(r) for r in read_csv(catalogue_csv)], read_csv(episodes_csv))

    @property
    def dialects(self) -> set[str]:
        return set(self.by_dialect)

    def withdraw(self, title_id: str, at: datetime) -> None:
        cur = self.withdrawn.get(title_id)
        if cur is None or at < cur:
            self.withdrawn[title_id] = at

    def available(self, title_id: str, at: datetime) -> bool:
        t = self.by_id.get(title_id)
        if t is None or t.available_from is None or at < t.available_from:
            return False
        if t.available_until is not None and at >= t.available_until:
            return False
        w = self.withdrawn.get(title_id)
        return not (w is not None and at >= w)

    def available_throughout(self, title_id: str, start: datetime, end: datetime) -> bool:
        """Available at `start` with no known end (licence or withdrawal) before `end`."""
        if not self.available(title_id, start):
            return False
        t = self.by_id[title_id]
        if t.available_until is not None and t.available_until <= end:
            return False
        w = self.withdrawn.get(title_id)
        return not (w is not None and w <= end)

    def promotable(self, title_id: str, dialect: str, start: datetime, end: datetime) -> bool:
        t = self.by_id.get(title_id)
        return (t is not None and t.dialect == dialect and not t.suspicious
                and self.available_throughout(title_id, start, end))

    def compute_popularity(self, events: Iterable[Event], until: datetime, days: int) -> None:
        """Distinct viewers who started a title in the `days` before `until` (history only)."""
        since = until - timedelta(days=days)
        seen: dict[str, set[str]] = defaultdict(set)
        for e in events:
            if e.event == "play_started" and since <= e.time < until:
                tid = e.properties.get("title_id")
                if tid in self.by_id:
                    seen[self.by_id[tid].content_slug].add(e.user_id)
        self.popularity = {slug: len(u) for slug, u in seen.items()}

    def pop(self, t: Title) -> int:
        return self.popularity.get(t.content_slug, 0)

    def next_episode_number(self, slug: str, furthest: tuple[int, int] | None) -> tuple[int, int, str] | None:
        eps = self.episodes.get(slug) or []
        for s, e, ep_id in eps:
            if furthest is None or (s, e) > furthest:
                return s, e, ep_id
        return None


@dataclass
class ContentContext:
    dialect: str
    language: str | None
    publish_at: datetime
    live_until: datetime
    is_paid: bool
    watched_slugs: set[str]
    top_genres: list[str]
    unfinished_slugs: list[str]  # most recent first
    goal_id: str | None


def _variant_for_language(variants: list[Title], language: str | None) -> Title:
    for t in variants:
        if t.language == language:
            return t
    return sorted(variants, key=lambda t: (t.language != "hin", t.title_id))[0]


class ContentSelector:
    def __init__(self, catalogue: Catalogue, interventions: dict[str, Any], new_release_days: int):
        self.cat = catalogue
        self.rules = interventions["home_page"]["rows"]
        self.new_release_days = new_release_days

    def candidates(self, ctx: ContentContext) -> list[Title]:
        """One promotable title per content_slug in the page dialect, preferring the app language variant."""
        by_slug: dict[str, list[Title]] = defaultdict(list)
        for t in self.cat.by_dialect.get(ctx.dialect, []):
            if self.cat.promotable(t.title_id, ctx.dialect, ctx.publish_at, ctx.live_until):
                by_slug[t.content_slug].append(t)
        chosen = [_variant_for_language(v, ctx.language) for v in by_slug.values()]
        return sorted(chosen, key=lambda t: (-self.cat.pop(t), t.title_id))

    def hero(self, ctx: ContentContext, pool: list[Title]) -> Title | None:
        if not pool:
            return None
        by_slug = {t.content_slug: t for t in pool}
        if ctx.goal_id == "G07":
            for slug in ctx.unfinished_slugs:
                if slug in by_slug:
                    return by_slug[slug]
        if ctx.goal_id == "G08":
            fresh = self._new_releases(pool, ctx.publish_at)
            if fresh:
                return fresh[0]
        if ctx.goal_id in ("G04", "G05", "G06"):
            prem = [t for t in pool if t.is_premium]
            if prem:
                return prem[0]
        if not ctx.is_paid:  # a non-subscriber can start a free title immediately
            free = [t for t in pool if not t.is_premium and t.content_slug not in ctx.watched_slugs]
            if free:
                return free[0]
        unseen = [t for t in pool if t.content_slug not in ctx.watched_slugs]
        return (unseen or pool)[0]

    def _new_releases(self, pool: list[Title], at: datetime) -> list[Title]:
        lo = (at - timedelta(days=self.new_release_days)).date()
        hi = at.date()
        return [t for t in pool if t.release_date is not None and lo <= t.release_date <= hi]

    def build_page(self, ctx: ContentContext, max_rows: int, per_row: int) -> dict[str, Any] | None:
        pool = self.candidates(ctx)
        if not pool:
            return None
        hero = self.hero(ctx, pool)
        used: set[str] = {hero.content_slug} if hero else set()
        allowed = list(self.rules["row_types"])
        lo, hi = int(self.rules["min_titles_per_row"]), int(self.rules["max_titles_per_row"])
        per_row = max(lo, min(hi, per_row))
        playable = [t for t in pool if ctx.is_paid or not t.is_premium] if ctx.goal_id not in ("G04", "G05", "G06") else pool
        by_slug = {t.content_slug: t for t in pool}

        def fill(cands: list[Title]) -> list[str]:
            out = []
            for t in cands:
                if t.content_slug in used or len(out) >= per_row:
                    continue
                out.append(t)
            if len(out) < lo:
                return []  # never publish a row the platform would drop
            used.update(t.content_slug for t in out)
            return [t.title_id for t in out]

        plan: list[tuple[str, list[Title]]] = []
        nxt = [by_slug[s] for s in ctx.unfinished_slugs if s in by_slug]
        plan.append(("next_episode", nxt))
        if ctx.top_genres:
            g0 = ctx.top_genres[0]
            byw = [t for t in playable if g0 in t.genres and t.content_slug not in ctx.watched_slugs]
            plan.append(("because_you_watched", byw))
        plan.append(("popular_in_dialect", [t for t in playable if t.content_slug not in ctx.watched_slugs] or playable))
        plan.append(("new_releases", self._new_releases(playable, ctx.publish_at)))
        genres = ctx.top_genres[1:3] if ctx.top_genres else []
        if not genres:
            gc = Counter(g for t in playable[:60] for g in t.genres)
            genres = [g for g, _ in gc.most_common(2)]
        for g in genres:
            plan.append(("genre", [t for t in playable if g in t.genres]))

        rows = []
        for row_type, cands in plan:
            if len(rows) >= max_rows:
                break
            if row_type not in allowed:
                continue
            ids = fill(cands)
            if ids:
                rows.append({"row_type": row_type, "title_ids": ids})
        if not rows and hero is None:
            return None
        return {"hero_title_id": hero.title_id if hero else None, "rows": rows}

    def message_title(self, ctx: ContentContext, send_at: datetime, prefer: list[str]) -> Title | None:
        """A title for a message: promotable in the user's dialect at send time."""
        for tid in prefer:
            t = self.cat.by_id.get(tid)
            if t and self.cat.promotable(tid, ctx.dialect, send_at, send_at + timedelta(hours=1)):
                return t
        return None
