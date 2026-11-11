"""Real owner-only analytics over the *existing* Discovery/Library SQLite state.

This is not Tumblr follower analytics, GATE conversion or a provider estimate.
Only observed, already persisted candidate and Library state is reported.
"""
import time
from contextlib import closing

from tumblrkit.library import Library


class AnalyticsAdapter:
    PERIODS = {
        "all": (None, "全期間"),
        "today": (86400, "過去24時間"),
        "week": (7 * 86400, "過去7日間"),
        "month": (30 * 86400, "過去30日間"),
    }

    def __init__(self, discovery_store, clock=None):
        self.store = discovery_store
        self.clock = clock or time.time

    def perform(self, action, target, payload):
        if action != "analytics.get_summary":
            raise ValueError("analytics_action_unlisted")
        if payload not in ({}, None):
            raise ValueError("analytics_payload_unsupported")
        if not isinstance(target, str) or target not in self.PERIODS:
            raise ValueError("analytics_period_invalid")
        seconds, period = self.PERIODS[target]
        cutoff = None if seconds is None else int(self.clock()) - seconds
        # Library._open initializes its own existing table in the same private
        # Discovery DB; no separate database, provider or fake metrics.
        with closing(Library(self.store)._open()) as db:
            where = "p.state='visible' AND COALESCE(b.blocked,0)=0"
            params = ()
            if cutoff is not None:
                where += " AND p.first_seen>=?"
                params = (cutoff,)
            row = db.execute(
                "SELECT COUNT(*), "
                "COALESCE(SUM(CASE WHEN s.seen=1 THEN 1 ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN s.favorite=1 THEN 1 ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN s.watch_later=1 THEN 1 ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN p.post_type='video' THEN 1 ELSE 0 END),0) "
                "FROM discovery_posts p "
                "LEFT JOIN discovery_blogs b ON b.name=p.blog "
                "LEFT JOIN library_state s ON s.candidate_id=p.candidate_id "
                "WHERE " + where, params,
            ).fetchone()
        labels = (
            "確認済み候補数",
            "閲覧済み候補数",
            "お気に入り候補数",
            "あとで見る候補数",
            "動画分類の候補数",
        )
        return {
            "verified": True,
            "period": period,
            "basis": "owner_private_discovery_library",
            "rows": [
                {"label": label, "value": count, "period": period, "url": ""}
                for label, count in zip(labels, row)
            ],
            "not_measured": [
                "Tumblrフォロワー数", "Tumblr反応総数",
                "Telegram参加転換率", "キャンペーン成果",
            ],
        }
