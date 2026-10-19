"""Propose a proportionate intervention for a viewer's active goal: home page, at most one message, or nothing.

Proportionality rules (ARCHITECTURE.md, "Intervention planning"):
  * no goal, unknown dialect, weak evidence or an already-live page -> no new page;
  * messages only for goals that need the viewer to come back later (re-engagement / trial ending), at most one
    per goal, never in quiet hours, never on an IST day that already has a push/WhatsApp, only with consent;
  * the viewer is in the app right now, so a message is a follow-up scheduled after the page's lifetime;
  * in-app messages only for monetisation goals, priced strictly from pricing.json, at most one per IST day.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .config import AgentConfig, PackConfig
from .content_selector import Catalogue, ContentContext, ContentSelector
from .fact_resolver import ViewerFacts
from .goal_planner import Eligibility
from .scheduler import QuietHours, ist_day, iso, parse_ts, plan_message_slot

MESSAGE_TYPES = ("push", "whatsapp")


@dataclass
class Proposal:
    actions: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.actions


def assess_confidence(vf: ViewerFacts, goal: Eligibility | None, fallback_used: bool) -> tuple[float, str]:
    """Confidence that the decision rests on current, consistent evidence (not a success probability)."""
    score, why = 0.9, []
    if not vf.profile_present:
        score -= 0.2
        why.append("no profile row (-0.20)")
    d = vf.fact("dialect")
    if d is None or d.value is None or not d.supported:
        score -= 0.4
        why.append("current dialect unknown or without content (-0.40)")
    elif d.conflicts:
        score -= 0.05
        why.append(f"dialect conflicts with {len(d.conflicts)} other source(s), app setting used (-0.05)")
    sub = vf.fact("subscription_status")
    if sub is None or not sub.supported:
        score -= 0.15
        why.append("subscription status unsupported (-0.15)")
    elif sub.conflicts:
        score -= 0.1
        why.append("subscription status had conflicting sources (-0.10)")
    elif sub.freshness == "stale":
        score -= 0.1
        why.append("subscription fact is stale (-0.10)")
    if goal is None:
        score -= 0.3
        why.append("no eligible goal (-0.30)")
    if fallback_used:
        score -= 0.05
        why.append("deterministic fallback after a model fault (-0.05)")
    score = round(max(0.05, min(0.95, score)), 2)
    base = "current app setting, consistent profile/event facts" if not why else "; ".join(why)
    return score, base


class InterventionPlanner:
    def __init__(self, cfg: AgentConfig, pack: PackConfig, catalogue: Catalogue):
        self.cfg = cfg
        self.pack = pack
        self.cat = catalogue
        self.selector = ContentSelector(catalogue, pack.interventions, cfg.new_release_days)
        q = pack.interventions["push"]["quiet_hours_ist"]
        self.quiet = QuietHours(q[0], q[1])
        self.ttl = timedelta(minutes=pack.page_ttl_minutes)

    def content_context(self, vf: ViewerFacts, dialect: str, language: str | None, publish_at: datetime,
                        goal_id: str | None) -> ContentContext:
        sub = vf.get("subscription_status")
        series = [s.slug for s in sorted(vf.series.values(), key=lambda s: s.last_play, reverse=True)
                  if s.has_unwatched and s.content_type in ("show", "micro_drama")]
        return ContentContext(dialect=dialect, language=language, publish_at=publish_at,
                              live_until=publish_at + self.ttl, is_paid=sub in ("active",),
                              watched_slugs=set(vf.watched_slugs), top_genres=list(vf.top_genres),
                              unfinished_slugs=series, goal_id=goal_id)

    def message_days_taken(self, vf: ViewerFacts, prior_actions: list[dict[str, Any]]) -> set:
        days = set(vf.message_days)
        for a in prior_actions:
            if a["type"] in MESSAGE_TYPES and a["status"] != "cancelled":
                days.add(ist_day(parse_ts(a["send_at"] or a["published_at"])))
        return days

    def propose(self, vf: ViewerFacts, goal: dict[str, Any], el: Eligibility | None, trigger_lang: str | None,
                now: datetime, publish_at: datetime, confidence: float,
                prior_actions: list[dict[str, Any]]) -> Proposal:
        p = Proposal()
        dialect = vf.get("dialect")
        gid = goal["goal_id"]
        if confidence < self.cfg.min_confidence_page:
            p.notes.append(f"confidence {confidence} below page threshold {self.cfg.min_confidence_page}")
            return p
        ctx = self.content_context(vf, dialect, trigger_lang, publish_at, gid)

        # ------------------------------------------------ home page
        live = [a for a in prior_actions if a["type"] == "home_page" and a["status"] == "published"
                and parse_ts(a["published_at"]) + self.ttl > publish_at]
        live_same = [a for a in live if a["raw"].get("dialect") == dialect]
        page = None
        if live_same:
            exp = parse_ts(live_same[-1]["published_at"]) + self.ttl
            p.notes.append(f"a personalised page for {dialect} is already live until {iso(exp)}; not republished")
        else:
            page = self.selector.build_page(ctx, self.cfg.max_rows_used, self.cfg.target_titles_per_row)
            if page is None:
                p.notes.append(f"no valid page: too few promotable titles in {dialect} for {iso(publish_at)}-"
                               f"{iso(publish_at + self.ttl)}")
            else:
                p.actions.append({"type": "home_page", "dialect": dialect, **page,
                                  "expires_at": iso(publish_at + self.ttl)})
                if live:
                    p.notes.append(f"app dialect changed since the live page ({live[-1]['raw'].get('dialect')}"
                                   f" -> {dialect}); new page for the current dialect")
                if any(r["row_type"] == "next_episode" for r in page["rows"]):
                    p.assumptions.append("viewer may resume an unfinished series shown in the next_episode row")
                hero_t = self.cat.by_id.get(page.get("hero_title_id") or "")
                if hero_t is not None and hero_t.content_slug in ctx.unfinished_slugs:
                    p.assumptions.append(f"hero {hero_t.title_id} is the viewer's unfinished series; assumes they "
                                         "have not finished it on another device (no cross-device evidence)")
                p.assumptions.append("viewer reaches the home page within the page lifetime "
                                     f"({self.pack.page_ttl_minutes} min)")

        # ------------------------------------------------ one follow-up message
        msg = self._message(vf, goal, el, ctx, now, publish_at, confidence, prior_actions, page, p)
        if msg:
            p.actions.append(msg)

        # ------------------------------------------------ in-app message
        if gid in self.cfg.in_app_goals:
            today = ist_day(publish_at)
            done_today = any(a["type"] == "in_app_message" and a["status"] != "cancelled"
                             and ist_day(parse_ts(a["published_at"])) == today for a in prior_actions)
            already_for_goal = any(a["type"] == "in_app_message" and a["status"] != "cancelled"
                                   and a.get("goal_instance_id") == goal.get("instance_id") for a in prior_actions)
            plan = self._cheapest_paid_plan()
            if done_today or already_for_goal:
                p.notes.append("in-app message skipped: one already queued today / for this goal")
            elif plan is not None:
                lang = trigger_lang or "hin"
                text = (f"Plans start at ₹{plan['price_inr']} for {plan['days']} days." if lang == "en" else
                        f"Plan sirf ₹{plan['price_inr']} mein, {plan['days']} din ke liye.")
                p.actions.append({"type": "in_app_message", "text": text, "plan_id": plan["plan_id"],
                                  "show_on": "next_app_open"})
        return p

    def _cheapest_paid_plan(self) -> dict[str, Any] | None:
        paid = [pl for pl in self.pack.pricing["plans"] if pl["plan_id"] != "P_TRIAL" and pl["price_inr"] > 1]
        return min(paid, key=lambda pl: pl["price_inr"]) if paid else None

    def _message(self, vf: ViewerFacts, goal: dict[str, Any], el: Eligibility | None, ctx: ContentContext,
                 now: datetime, publish_at: datetime, confidence: float, prior: list[dict[str, Any]],
                 page: dict[str, Any] | None, p: Proposal) -> dict[str, Any] | None:
        gid = goal["goal_id"]
        if gid not in self.cfg.message_goals:
            return None
        if confidence < self.cfg.min_confidence_message:
            p.notes.append(f"no message: confidence {confidence} < {self.cfg.min_confidence_message}")
            return None
        if any(a["type"] in MESSAGE_TYPES and a["status"] != "cancelled"
               and a.get("goal_instance_id") == goal.get("instance_id") for a in prior):
            p.notes.append("no message: this goal already has one")
            return None
        push_ok = vf.get("push_consent") is True and vf.get("push_deliverable") is not False
        wa_ok = vf.get("whatsapp_consent") is True
        if not push_ok and not wa_ok:
            p.notes.append("no message: no push or WhatsApp consent")
            return None
        deadline = parse_ts(goal["deadline"])
        earliest = publish_at + timedelta(hours=self.cfg.message_followup_hours)
        slot = plan_message_slot(earliest, deadline, self.quiet, self.message_days_taken(vf, prior),
                                 self.cfg.message_morning_hour_ist)
        if slot is None:
            p.notes.append("no message: no slot outside quiet hours and the daily cap before the goal deadline")
            return None
        lang = ctx.language or "hin"
        plan = self._cheapest_paid_plan()

        if gid == "G04":
            ends = vf.get("trial_ends_at")
            if ends is None or not (timedelta(0) <= ends - slot <= timedelta(days=self.cfg.trial_ending_within_days)):
                p.notes.append("no message: trial end not evidenced within the 'ending soon' window")
                return None
            renew = self.pack.plans.get("P_TRIAL", {}).get("renews_to")
            plan = self.pack.plans.get(renew) or plan
            if plan is None:
                return None
            price = f"₹{plan['price_inr']}"
            if wa_ok:
                return {"type": "whatsapp", "send_at": iso(slot), "template_id": "wa_trial_ending_v1",
                        "variables": {"plan_price": price}, "title_id": None, "plan_id": plan["plan_id"]}
            title = "Aapka trial" if lang != "en" else "Your trial"
            body = (f"Trial ke baad {price} mein dekhte rahiye." if lang != "en"
                    else f"Keep watching for {price} after your trial.")
            return {"type": "push", "send_at": iso(slot), "title_id": None, "text": {"title": title, "body": body},
                    "deeplink": None, "plan_id": plan["plan_id"]}

        prefer: list[str] = []
        if gid == "G07" and el is not None:
            slug = el.context.get("series_slug")
            prefer += [t.title_id for t in self.cat.by_slug.get(slug, []) if t.dialect == ctx.dialect]
        if page:
            if page.get("hero_title_id"):
                prefer.append(page["hero_title_id"])
            for r in page["rows"]:
                prefer += r["title_ids"]
        prefer += [t.title_id for t in self.selector.candidates(ctx)[:20]]
        t = self.selector.message_title(ctx, slot, prefer)
        if t is None:
            p.notes.append("no message: no title promotable in the viewer's dialect at the send time")
            return None
        if push_ok:
            if gid == "G07":
                title, body = (t.name, f"{t.name} wahin se dekhiye jahan chhoda tha.") if lang != "en" else \
                              (t.name, f"Pick up {t.name} where you left off.")
            else:
                title, body = (t.name, f"Aapke liye: {t.name}. Abhi dekhiye.") if lang != "en" else \
                              (t.name, f"Picked for you: {t.name}. Watch now.")
            return {"type": "push", "send_at": iso(slot), "title_id": t.title_id,
                    "text": {"title": title[:40], "body": body}, "deeplink": f"ott://title/{t.title_id}"}
        return {"type": "whatsapp", "send_at": iso(slot), "template_id": "wa_recommend_title_v1",
                "variables": {"title_name": t.name, "dialect_name": t.dialect}, "title_id": t.title_id}
