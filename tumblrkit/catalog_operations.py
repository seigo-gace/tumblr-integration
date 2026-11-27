"""Owner operations for the original catalog actions that had no route.

Each operation reuses the existing draft, engagement, campaign, referral,
attachment, or event ledger. Nothing here publishes to Tumblr except the
explicit engagement undo, and nothing selects a paid model.
"""
import sqlite3
from datetime import datetime, timezone

from tumblrkit.attachments import AttachmentError
from tumblrkit.core import create_campaign
from tumblrkit.integration import CoreLinkStore


class WinnerNotice:
    """Deliver one existing winner row through the already bound Telegram owner."""

    def __init__(self, owner, destination):
        self.owner = owner
        self.destination = destination

    def publish(self, event_id, campaign_id, applicant_id, outcome):
        text = (f"企画「{campaign_id}」の結果です。対象: {applicant_id}。"
                f"結果: {outcome}。公開投稿は行っていません。")
        result = self.owner.perform(
            "telegram.publish_announcement", self.destination,
            {"text": text[:2000], "operation_key": event_id})
        if not isinstance(result, dict) or result.get("verified") is not True:
            return {"message_ref": None}
        reference = result.get("reference") or result.get("message_id")
        if not isinstance(reference, str) or not reference:
            return {"message_ref": None}
        return {"message_ref": reference}

    def readback(self, event_id, reference):
        return isinstance(reference, str) and bool(reference)


def _draft_id(value):
    if (not isinstance(value, str) or len(value) != 32
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError("INVALID_DRAFT")
    return value


def _ids(value, limit=5):
    if not isinstance(value, list) or len(value) > limit or len(set(value)) != len(value):
        raise ValueError("publisher_attachment_invalid")
    return value


def perform(action, payload, *, drafts, discovery=None, engagement=None,
            campaign_db=None, attachments=None, media=None, telegram=None,
            analytics=None):
    if not isinstance(action, str) or not isinstance(payload, dict):
        raise ValueError("catalog_operation_invalid")
    keys = set(payload)
    if action == "publisher.01" and keys == {"text"}:
        saved = drafts.save(payload["text"])
        drafts.set_meta(saved["id"], saved["text"], {"catalog": "publisher.01"})
        return {"verified": True, "draft_id": saved["id"], "text": saved["text"],
                "published": False}
    if action == "publisher.02" and keys == {"draft_id", "text", "expected_text"}:
        updated = drafts.update(payload["draft_id"], payload["text"], payload["expected_text"])
        return {"verified": True, "draft_id": updated["id"], "text": updated["text"],
                "published": False}
    if action == "publisher.03" and keys == {"draft_id"}:
        return {"verified": True, **drafts.reuse(_draft_id(payload["draft_id"]))}
    if action == "publisher.04" and keys == {"draft_id", "attachment_ids", "media_job_ids"}:
        return _attach(drafts, attachments, media, payload)
    if action == "publisher.05" and keys in ({"draft_id"}, {"draft_id", "apply"}):
        return _tags(drafts, discovery, payload)
    if action == "engagement.02" and keys == {"candidate_id", "comment"}:
        if engagement is None:
            raise ValueError("engagement_not_connected")
        return engagement.save_reblog_draft(payload["candidate_id"], payload["comment"], drafts)
    if action == "engagement.07" and keys == {"engagement_action", "target"}:
        if engagement is None:
            raise ValueError("engagement_not_connected")
        return engagement.undo(payload["engagement_action"], payload["target"])
    if action == "campaign.02" and keys == {"campaign_id"}:
        return _campaign_copy(campaign_db, drafts, payload["campaign_id"])
    if action == "campaign.08" and keys == {"campaign_id", "event_id"}:
        return _notify(campaign_db, telegram, payload["campaign_id"], payload["event_id"])
    if action == "referral.02" and keys == {"campaign_id", "referrer_id"}:
        return _share_text(campaign_db, drafts, payload["campaign_id"], payload["referrer_id"])
    if action == "referral.03" and keys == {"campaign_id", "source_post_id"}:
        return _share_link(campaign_db, payload["campaign_id"], payload["source_post_id"])
    if action == "referral.03" and keys == {"campaign_id", "source_post_id", "destination", "confirmed"}:
        return _share_send(campaign_db, telegram, payload)
    if action == "referral.04" and keys == {"campaign_id", "name"}:
        return _configure_referral(campaign_db, payload["campaign_id"], payload["name"])
    if action == "analytics.09" and keys == set():
        return _inflow(analytics, "telegram")
    if action == "analytics.10" and keys == set():
        return _inflow(analytics, "unclassified")
    raise ValueError("catalog_operation_invalid")


def _attach(drafts, attachments, media, payload):
    ident = _draft_id(payload["draft_id"])
    attachment_ids = _ids(payload["attachment_ids"])
    job_ids = _ids(payload["media_job_ids"])
    if not attachment_ids and not job_ids:
        raise ValueError("publisher_attachment_invalid")
    if len(attachment_ids) + len(job_ids) > 5:
        raise ValueError("publisher_attachment_invalid")
    current = drafts.get(ident)
    described = []
    if attachment_ids:
        if attachments is None:
            raise ValueError("publisher_attachment_store_missing")
        try:
            described = attachments.describe(attachment_ids)
        except AttachmentError as exc:
            raise ValueError("publisher_attachment_invalid") from exc
        if any(not item["media_type"].startswith("image/") for item in described):
            raise ValueError("publisher_attachment_invalid")
    jobs = []
    if job_ids:
        if media is None:
            raise ValueError("publisher_media_store_missing")
        by_id = {}
        offset = 0
        while offset <= 10000:
            listed = media.list(offset=offset, limit=100)
            for item in listed["jobs"]:
                if item["id"] in job_ids:
                    by_id[item["id"]] = item
            if len(by_id) == len(job_ids) or not listed["has_next"]:
                break
            offset += 100
        for job_id in job_ids:
            job = by_id.get(job_id)
            if not job or job["state"] != "completed":
                raise ValueError("publisher_attachment_invalid")
            jobs.append({"id": job["id"], "candidate_id": job["candidate_id"],
                         "output_name": job["output_name"]})
    meta = drafts.meta_of(ident)
    meta.update({"catalog": "publisher.04",
                 "attachment_ids": [item["id"] for item in described],
                 "media_job_ids": [item["id"] for item in jobs]})
    drafts.set_meta(ident, current["text"], meta)
    return {"verified": True, "draft_id": ident, "attachments": described,
            "media_jobs": jobs, "tumblr_upload": False, "published": False}


def _tags(drafts, discovery, payload):
    if discovery is None:
        raise ValueError("publisher_tags_not_connected")
    ident = _draft_id(payload["draft_id"])
    current = drafts.get(ident)
    suggestions = discovery.suggest_tags(current["text"])
    applied = payload.get("apply")
    if applied is None:
        return {"verified": True, "draft_id": ident, "suggestions": suggestions,
                "applied": [], "published": False}
    if (not isinstance(applied, list) or len(applied) > 8 or len(set(applied)) != len(applied)
            or any(tag not in suggestions for tag in applied)):
        raise ValueError("publisher_tags_invalid")
    meta = drafts.meta_of(ident)
    meta.update({"catalog": "publisher.05", "tags": applied})
    drafts.set_meta(ident, current["text"], meta)
    return {"verified": True, "draft_id": ident, "suggestions": suggestions,
            "applied": applied, "published": False}


def _campaign_copy(db, drafts, campaign_id):
    from tumblrkit.campaign_runtime import CampaignContest
    owner = CampaignContest(db)
    match = [row for row in owner.list_campaigns() if row["campaign_id"] == campaign_id]
    if len(match) != 1:
        raise ValueError("campaign_not_found")
    row = match[0]
    when = datetime.fromtimestamp(row["deadline"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = (f"{row['prize']}\n応募締切: {when}\n当選人数: {row['winner_count']}\n"
            f"受取期限: {row['claim_seconds']}秒\n"
            "この文面は保存済みの企画条件から作成した下書きです。"
            "公開と当選通知は別の操作です。")
    saved = drafts.save(text)
    drafts.set_meta(saved["id"], saved["text"],
                    {"catalog": "campaign.02", "campaign_id": campaign_id})
    return {"verified": True, "draft_id": saved["id"], "text": saved["text"],
            "published": False, "notified": False}


def _notify(db, telegram, campaign_id, event_id):
    from tumblrkit.campaign_runtime import CampaignContest
    owner = CampaignContest(db)
    if not any(item["event_id"] == event_id for item in owner.pending_notifications(campaign_id)):
        raise ValueError("campaign_notification_not_pending")
    topic = db.execute(
        "SELECT posting_topic_id FROM campaign_gate_topics WHERE campaign_id=?",
        (campaign_id,)).fetchone()
    if topic is None or telegram is None or not hasattr(telegram, "perform"):
        raise ValueError("campaign_messenger_not_connected")
    return owner.dispatch_notification(event_id, WinnerNotice(telegram, topic[0]))


def _share_text(db, drafts, campaign_id, referrer_id):
    from tumblrkit.referral_runtime import ReferralManager
    issued = ReferralManager(db).issue(campaign_id, referrer_id)
    text = (f"紹介コード: {issued['referral_code']}\n"
            f"キャンペーン: {issued['campaign_id']}\n"
            "このコードは紹介台帳に保存済みです。公開URLは含めていません。")
    saved = drafts.save(text)
    drafts.set_meta(saved["id"], saved["text"],
                    {"catalog": "referral.02", "campaign_id": campaign_id,
                     "referrer_id": referrer_id})
    return {"verified": True, "draft_id": saved["id"], "text": saved["text"],
            "referral_code": issued["referral_code"], "public_url": None,
            "published": False}


def _share_link(db, campaign_id, source_post_id):
    link_id = CoreLinkStore(db).create(campaign_id, source_post_id)
    text = (f"紹介リンク参照: {link_id}\nキャンペーン: {campaign_id}\n"
            "この値は台帳上の参照です。公開URLではなく、まだ共有していません。")
    return {"verified": True, "link_id": link_id, "text": text,
            "shared": False, "public_url": None}


def _share_send(db, telegram, payload):
    if payload.get("confirmed") is not True:
        raise ValueError("referral_share_confirmation_required")
    prepared = _share_link(db, payload["campaign_id"], payload["source_post_id"])
    destination = payload["destination"]
    if (telegram is None or not hasattr(telegram, "perform")
            or not isinstance(destination, str) or not destination.strip()
            or len(destination) > 64):
        raise ValueError("referral_share_not_connected")
    result = telegram.perform(
        "telegram.publish_announcement", destination.strip(),
        {"text": prepared["text"], "operation_key": prepared["link_id"]})
    verified = isinstance(result, dict) and result.get("verified") is True
    return {"verified": verified, "link_id": prepared["link_id"],
            "shared": verified, "public_url": None,
            "reference": result.get("reference") if isinstance(result, dict) else None}


def _configure_referral(db, campaign_id, name):
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 150:
        raise ValueError("referral_campaign_invalid")
    try:
        create_campaign(db, campaign_id, name.strip(), {})
    except sqlite3.IntegrityError:
        row = db.execute("SELECT name,active FROM campaigns WHERE id=?",
                         (campaign_id,)).fetchone()
        if row is None:
            raise
        return {"verified": True, "campaign_id": campaign_id, "name": row[0],
                "state": "already_configured", "active": bool(row[1])}
    return {"verified": True, "campaign_id": campaign_id, "name": name.strip(),
            "state": "configured", "active": True}


def _inflow(analytics, kind):
    if analytics is None:
        raise ValueError("analytics_not_connected")
    return analytics.inflow(kind)
