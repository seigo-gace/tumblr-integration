"""Real owner-only analytics over the *existing* Discovery/Library SQLite state.

This is not Tumblr follower analytics, GATE conversion or a provider estimate.
Only observed, already persisted candidate and Library state is reported.
"""
import time
from datetime import datetime, timezone
from contextlib import closing

from tumblrkit.library import Library


class AnalyticsAdapter:
    PERIODS = {
        "all": (None, "全期間"),
        "today": (86400, "過去24時間"),
        "week": (7 * 86400, "過去7日間"),
        "month": (30 * 86400, "過去30日間"),
    }

    def __init__(self, discovery_store, clock=None, campaign_db=None):
        self.store = discovery_store
        self.clock = clock or time.time
        # Explicit owner-owned campaign ledger only. Without this injection
        # campaign totals remain unmeasured, never guessed from Discovery.
        self.campaign_db = campaign_db

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
            # The engagement module shares this exact private SQLite file.
            # A missing table means no confirmed activity has been recorded,
            # not simulated engagement; reserved/sent/uncertain are excluded.
            table=db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='engagement_receipts'").fetchone()
            engagement=[]
            for kind in ("engagement.like","engagement.reblog","engagement.follow"):
                if not table:
                    engagement.append(0)
                    continue
                statement=("SELECT COUNT(*) FROM engagement_receipts "
                           "WHERE action=? AND state='verified'")
                if cutoff is None:
                    observed=db.execute(statement,(kind,)).fetchone()[0]
                else:
                    observed=db.execute(
                        statement+" AND updated_at>=?",(kind,cutoff)).fetchone()[0]
                engagement.append(observed)
        campaign_rows=[]
        if self.campaign_db is not None:
            db=self.campaign_db
            required={"campaign_links","events","memberships","grants",
                      "announcement_receipts"}
            present={row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if not required.issubset(present):
                raise ValueError("analytics_campaign_source_unavailable")
            since=None if cutoff is None else datetime.fromtimestamp(
                cutoff, timezone.utc).isoformat()
            window="" if since is None else " AND {column}>=?"
            args=() if since is None else (since,)
            clicks=db.execute(
                "SELECT COUNT(*) FROM events e JOIN campaign_links l "
                "ON l.link_id=e.source WHERE e.kind='entry'"+
                window.format(column="e.at"),args).fetchone()[0]
            members=db.execute(
                "SELECT COUNT(*) FROM memberships WHERE 1=1"+
                window.format(column="verified_at"),args).fetchone()[0]
            tiers=db.execute(
                "SELECT COUNT(*) FROM grants WHERE 1=1"+
                window.format(column="awarded_at"),args).fetchone()[0]
            announcements=db.execute(
                "SELECT COUNT(*) FROM announcement_receipts "
                "WHERE state='verified'"+window.format(column="updated_at"),
                args).fetchone()[0]
            campaign_rows=[{"label":label,"value":count,"period":period,"url":""}
                           for label,count in zip((
                               "アプリ記録済み紹介クリック数",
                               "アプリ記録済みGATE参加確認レコード数",
                               "アプリ記録済み特典付与レコード数",
                               "MPB結果照合済み告知レコード数",
                           ),(clicks,members,tiers,announcements))]
        labels = (
            "確認済み候補数",
            "閲覧済み候補数",
            "お気に入り候補数",
            "あとで見る候補数",
            "動画分類の候補数",
            "本アプリから確認済みのいいね操作数",
            "本アプリから確認済みのリブログ操作数",
            "本アプリから確認済みのフォロー操作数",
        )
        return {
            "verified": True,
            "period": period,
            "basis": "owner_private_discovery_library",
            "rows": [
                {"label": label, "value": count, "period": period, "url": ""}
                for label, count in zip(labels, (*row,*engagement))
            ] + campaign_rows,
            "campaign_basis": ("local_campaign_records_not_external_conversion"
                               if self.campaign_db is not None else "not_connected"),
            "engagement_basis":"official_oauth_readback_receipts",
            "not_measured": [
                "Tumblrフォロワー数", "Tumblr反応総数",
                "Telegram参加転換率", "キャンペーン成果",
            ],
        }
