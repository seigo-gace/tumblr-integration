"""Run a catalog operation through the stores and adapters that already exist.

Bridge preview and confirmation stay in the assistant. This module performs
the local operations and the discovery scan that has no separate Bridge id.
"""
import hashlib
import re

from tumblrkit.conversation import FIELDS

_BLANK = frozenset({"なし", "なし。", "ない", "-"})
_INTS = frozenset({"deadline", "winners", "claim_seconds", "publish_at", "media_index", "threshold"})
_LISTS = frozenset({"attachment_ids", "media_job_ids", "apply"})
_OPTIONAL = frozenset({"apply", "destination", "gate_topic_id"})


def _need(services, name):
    value = services.get(name)
    if value is None:
        raise ValueError("operation_not_connected")
    return value


def _blank(value):
    return isinstance(value, str) and value.strip() in _BLANK


def _integer(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("operation_number_invalid")
    if isinstance(value, int):
        return value
    if not re.fullmatch(r"[0-9]{1,12}", value.strip()):
        raise ValueError("operation_number_invalid")
    return int(value.strip())


def _strings(value):
    if isinstance(value, list):
        return value
    if _blank(value):
        return []
    parts = [part.strip() for part in value.replace("、", ",").split(",")]
    return [part for part in parts if part and part not in _BLANK]


def normalize(action, payload):
    if not isinstance(payload, dict):
        raise ValueError("catalog_operation_invalid")
    clean = {}
    for key in FIELDS[action]:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, str):
            value = value.strip()
        if key in _OPTIONAL and _blank(value):
            continue
        if key in _INTS:
            value = _integer(value)
        elif key in _LISTS:
            value = _strings(value)
        elif key == "rights":
            if value != "承諾":
                raise ValueError("media_rights_required")
        elif not isinstance(value, str) or not value or len(value) > 2000:
            raise ValueError("operation_input_invalid")
        clean[key] = value
    missing = [key for key in FIELDS[action] if key not in clean and key not in _OPTIONAL]
    if missing:
        raise ValueError("operation_input_invalid")
    return clean


def _done(summary, **extra):
    body = {"verified": True, "summary": summary[:500]}
    body.update(extra)
    return body


def _count(value):
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in ("items", "jobs", "rows", "total"):
            if isinstance(value.get(key), list):
                return len(value[key])
            if key == "total" and isinstance(value.get(key), int):
                return value[key]
    return 0


def _catalog(services, action, payload):
    from tumblrkit.catalog_operations import perform
    prepared = dict(payload)
    if action == "publisher.05" and not prepared.get("apply"):
        prepared.pop("apply", None)
    if action == "referral.03" and prepared.get("destination"):
        prepared["confirmed"] = True
    result = perform(
        action, prepared,
        drafts=_need(services, "drafts"),
        discovery=services.get("discovery"),
        engagement=services.get("engagement"),
        campaign_db=services.get("campaign_db"),
        attachments=services.get("attachments"),
        media=services.get("media"),
        telegram=services.get("telegram"),
        analytics=services.get("analytics"))
    if not isinstance(result, dict):
        raise ValueError("catalog_operation_invalid")
    result = dict(result)
    result["summary"] = _catalog_summary(action, result)
    return result


def _catalog_summary(action, result):
    if result.get("published") is False or result.get("tumblr_upload") is False:
        text = "既存の保存先で実行しました。Tumblrへは公開していません。"
    elif action in ("analytics.09", "analytics.10"):
        text = "記録済みの流入です。新しい計測はしていません。件数: " + str(result.get("total", 0)) + "。"
    else:
        text = "操作を実行しました。"
    if isinstance(result.get("draft_id"), str):
        text += "下書きID: " + result["draft_id"] + "。"
    if isinstance(result.get("text"), str):
        text += "\n" + result["text"][:400]
    return text


def _account_view(services, payload, kind):
    status = _need(services, "account").status()
    connected = status.get("connected") is True
    if kind == "permission":
        text = "投稿権限: " + ("あります。" if status.get("can_publish") is True else "ありません。")
    else:
        text = "Tumblr接続: " + ("接続済みです。" if connected else "未接続です。")
    text += "リモートの解除はしていません。"
    return _done(text, connected=connected)


def _account_start(services, payload):
    url = _need(services, "account").start()
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("oauth_start_invalid")
    return _done("接続を開始しました。次のTumblr認可画面を開いてください。\n" + url[:500], external=True)


def _account_disconnect(services, payload):
    result = _need(services, "account").disconnect("disconnect_local_tumblr")
    return _done("この端末のTumblr接続情報を削除しました。Tumblr側の遠隔解除はしていません。", **result)


def _scan_target(value):
    text = value.strip()
    tagged = re.fullmatch(r"タグ\s*[:：]?\s*(\S+)", text)
    if tagged:
        return "tag:" + tagged.group(1)
    named = re.fullmatch(r"ブログ\s*[:：]?\s*(\S+)", text)
    if named:
        return "blog:" + named.group(1)
    if text in ("followers", "following") or re.fullmatch(r"(tag|blog|related):[^\s:]{1,64}", text):
        return text
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", text):
        return "tag:" + text
    return text


def _discovery_scan(services, payload):
    result = _need(services, "tumblr").perform("tumblr.discover", _scan_target(payload["target"]), {})
    if not isinstance(result, dict) or result.get("verified") is not True:
        return {"verified": False, "summary": "巡回結果を確認できません。完了扱いしません。"}
    result = dict(result)
    result["summary"] = "新着の巡回結果を取得しました。"
    result["external"] = True
    return result


def _discovery_membership(services, payload):
    store = _need(services, "discovery")
    if payload["mode"].startswith("解除"):
        result = store.include_blog(payload["target"])
        return _done("対象の登録を解除しました。再取得するまで古い投稿は戻しません。", **result)
    store.exclude_blog(payload["target"])
    return _done("対象を巡回対象外として登録しました。Tumblr側のフォローは変更していません。")


def _discovery_checkpoint(services, payload):
    point = _need(services, "discovery").checkpoint(payload["target"])
    return _done("保存済みの巡回差分です。cursor: " + str(point.get("cursor")) +
                 "。完了: " + ("はい" if point.get("complete") else "いいえ") + "。")


def _library_list(services, payload):
    listed = _need(services, "library").list(query=payload.get("query", ""))
    return _done("保存済みの閲覧一覧です。件数: " + str(_count(listed)) + "。外部へは取りに行っていません。",
                 total=_count(listed))


def _library_open(services, payload):
    library = _need(services, "library")
    selected = library.selected([payload["candidate_id"]])
    try:
        library.progress(payload["candidate_id"], 0)
    except ValueError:
        pass
    return _done("保存済みの候補を開きました。件数: " + str(_count(selected)) + "。")


def _library_flag(services, payload, field):
    result = _need(services, "library").update([payload["candidate_id"]], field, True)
    return _done("閲覧状態を保存しました。Tumblrへは送信していません。", **result)


def _library_clear(services, payload):
    library = _need(services, "library")
    library.update([payload["candidate_id"]], "favorite", False)
    library.update([payload["candidate_id"]], "watch_later", False)
    return _done("お気に入りとあとで見るを解除しました。Tumblrへは送信していません。")


def _library_resume(services, payload):
    item = _need(services, "library").resume_item()
    if not item:
        return _done("前回の続きはありません。")
    return _done("前回の続きです。候補ID: " + str(item.get("candidate_id", ""))[:80] + "。")


def _media_candidates(services, payload):
    rows = _need(services, "discovery").read_candidates(limit=20)
    return _done("保存候補として記録済みの投稿です。件数: " + str(len(rows)) + "。ダウンロードはしていません。")


def _media_save(services, payload):
    result = _need(services, "media").save(payload["candidate_id"], payload["media_index"], rights_confirmed=True)
    if not isinstance(result, dict):
        raise ValueError("media_save_unverified")
    return _done("メディアをこの端末へ保存しました。Tumblrへは公開していません。", **{
        key: result[key] for key in ("id", "state") if key in result
    })


def _media_list(services, payload):
    listed = _need(services, "media").list(candidate_id=payload.get("candidate_id"))
    return _done("保存状況です。件数: " + str(_count(listed)) + "。", total=_count(listed))


def _media_capacity(services, payload):
    capacity = _need(services, "media").capacity()
    return _done("保存容量を確認しました。空き容量は端末の実測です。新規ダウンロードはしていません。",
                 retained_bytes=capacity.get("retained_bytes"))


def _follow_candidates(services, payload):
    rows = _need(services, "discovery").read_blogs(limit=20)
    names = [row["name"] for row in rows if isinstance(row, dict) and isinstance(row.get("name"), str)]
    shown = "、".join(names[:10])
    return _done("保存済みのブログです。件数: " + str(len(rows)) +
                 ("。表示: " + shown if shown else "") + "。フォローはしていません。")


def _engagement_history(services, payload):
    history = _need(services, "engagement").history()
    return _done("保存済みの交流履歴です。件数: " + str(_count(history)) + "。")


def _preview(services, payload):
    item = _need(services, "drafts").preview(payload["draft_id"])
    text = item.get("text", "") if isinstance(item, dict) else ""
    return _done("下書きのプレビューです。Tumblrへは公開していません。\n" + str(text)[:400],
                 draft_id=payload["draft_id"], published=False, tumblr_upload=False)


def _schedule(services, payload):
    drafts = _need(services, "drafts")
    current = drafts.get(payload["draft_id"])
    digest = hashlib.sha256(current["text"].encode("utf-8")).hexdigest()
    result = _need(services, "scheduler").queue(payload["draft_id"], payload["publish_at"], digest)
    return _done("予約を台帳に記録しました。時刻になるまでTumblrへは投稿しません。予約ID: " +
                 str(result.get("schedule_id", "")) + "。", published=False)


def _reschedule(services, payload):
    scheduler = _need(services, "scheduler")
    match = [row for row in scheduler.list() if row["schedule_id"] == payload["schedule_id"]]
    if len(match) != 1:
        raise ValueError("schedule_not_editable")
    scheduler.reschedule(payload["schedule_id"], payload["publish_at"], match[0]["expected_digest"])
    return _done("予約時刻を変更しました。Tumblrへはまだ投稿していません。", published=False)


def _contest(services):
    from tumblrkit.campaign_runtime import CampaignContest
    return CampaignContest(_need(services, "campaign_db"))


def _campaign_create(services, payload):
    result = _contest(services).create(
        payload["campaign_id"], payload["prize"], deadline=payload["deadline"],
        winners=payload["winners"], claim_seconds=payload["claim_seconds"],
        gate_topic_id=payload.get("gate_topic_id"))
    return _done("企画を台帳に作成しました。公開と抽選は別の操作です。", **{
        key: result[key] for key in ("campaign_id", "deadline", "winners") if key in result
    })


def _campaign_terms(services, payload):
    _contest(services).configure_entry_terms(
        payload["campaign_id"], deadline=payload["deadline"], winners=payload["winners"],
        claim_seconds=payload["claim_seconds"], gate_topic_id=payload.get("gate_topic_id"))
    return _done("応募条件を保存しました。応募者へは通知していません。")


def _campaign_rows(services, payload, method, label):
    rows = getattr(_contest(services), method)(payload["campaign_id"])
    if isinstance(rows, dict):
        counted = rows.get("entries", rows.get("due_unclaimed"))
        count = len(counted) if isinstance(counted, list) else rows.get("total_entries", 0)
    else:
        count = len(rows)
    return _done(label + "件数: " + str(count) + "。")


def _campaign_deadline(services, payload):
    result = _contest(services).change_deadline(payload["campaign_id"], payload["deadline"])
    return _done("応募締切を変更しました。通知はしていません。", deadline=result["deadline"])


def _campaign_draw(services, payload):
    result = _contest(services).draw(payload["campaign_id"])
    winners = getattr(result, "winners", ())
    return _done("抽選を台帳で実行しました。当選者数: " + str(len(winners)) +
                 "。当選通知は別の操作です。")


def _campaign_promote(services, payload):
    promoted = _contest(services).promote_expired(payload["campaign_id"])
    count = len(promoted) if isinstance(promoted, list) else 0
    return _done("期限切れの繰り上げを台帳で実行しました。繰り上げ件数: " + str(count) +
                 "。通知は別の操作です。")


def _campaign_close(services, payload):
    state = _contest(services).close(payload["campaign_id"])
    return _done("企画を終了として記録しました。状態: " + str(state) + "。")


def _referrals(services):
    from tumblrkit.referral_runtime import ReferralManager
    return ReferralManager(_need(services, "campaign_db"))


def _referral_issue(services, payload):
    result = _referrals(services).issue(payload["campaign_id"], payload["referrer_id"])
    return _done("紹介コードを台帳に発行しました。公開URLは作っていません。コード: " +
                 str(result.get("referral_code", "")) + "。", public_url=None)


def _referral_member(services, payload):
    try:
        result = _referrals(services).confirm(payload["campaign_id"], payload["member_id"])
    except PermissionError as exc:
        raise ValueError(str(exc) or "referral_gate_not_connected") from None
    return _done("紹介成立の確認結果です。状態: " + str(result.get("state", "")) + "。")


def _referral_tier(services, payload):
    result = _referrals(services).set_tier(payload["campaign_id"], payload["threshold"], payload["reward"])
    return _done("段階特典を台帳に保存しました。特典はまだ付与していません。", state=result.get("state"))


def _referral_report(services, payload, label):
    result = _referrals(services).report(payload["campaign_id"])
    rows = result.get("referrers", []) if isinstance(result, dict) else result
    return _done(label + "件数: " + str(len(rows)) + "。")


def _referral_revoke(services, payload):
    state = _referrals(services).revoke(payload["campaign_id"], payload["referrer_id"])
    return _done("紹介リンクを台帳上で停止しました。状態: " + str(state) + "。")


def _analytics(services):
    return _need(services, "analytics")


def _compare(services, payload):
    result = _analytics(services).compare(payload["period"])
    return _done("保存済みイベントの期間比較です。期間: " + payload["period"] +
                 "。Tumblrの表示回数は測っていません。", period=payload["period"])


def _followers(services, payload):
    history = _need(services, "followers").history()
    return _done("保存済みのフォロワー実測です。件数: " + str(_count(history)) +
                 "。新しい取得はしていません。")


def _reactions(services, payload):
    result = _analytics(services).post_reactions(payload["candidate_id"])
    return _done("保存済みの投稿別反応です。候補ID: " + payload["candidate_id"] + "。")


def _campaign_outcome(services, payload):
    rows = _contest(services).outcomes(payload["campaign_id"])
    return _done("企画結果の台帳です。件数: " + str(len(rows)) + "。")


def _report_csv(services, payload):
    result = _analytics(services).report_csv(payload["period"])
    text = result if isinstance(result, str) else str(result.get("csv", result.get("text", "")))
    return _done("保存済み記録のレポートです。\n" + text[:400])


HANDLERS = {
    "account.01": lambda services, payload: _account_view(services, payload, "status"),
    "account.02": _account_start,
    "account.03": _account_start,
    "account.04": lambda services, payload: _account_view(services, payload, "permission"),
    "account.05": lambda services, payload: _account_view(services, payload, "settings"),
    "account.06": _account_disconnect,
    "discovery.01": _discovery_scan,
    "discovery.06": _discovery_membership,
    "discovery.07": _discovery_checkpoint,
    "library.01": _library_list,
    "library.02": _library_open,
    "library.03": lambda services, payload: _library_flag(services, payload, "seen"),
    "library.04": lambda services, payload: _library_flag(services, payload, "favorite"),
    "library.05": lambda services, payload: _library_flag(services, payload, "watch_later"),
    "library.06": _library_resume,
    "library.07": _library_list,
    "library.08": _library_clear,
    "media.01": _media_candidates,
    "media.02": _media_save,
    "media.03": _media_save,
    "media.04": _media_list,
    "media.05": _media_save,
    "media.06": _media_list,
    "media.07": _media_capacity,
    "engagement.04": _follow_candidates,
    "engagement.06": _engagement_history,
    "publisher.06": _preview,
    "publisher.08": _schedule,
    "publisher.09": _reschedule,
    "campaign.01": _campaign_create,
    "campaign.03": _campaign_terms,
    "campaign.04": lambda services, payload: _campaign_rows(services, payload, "entries", "応募状況です。"),
    "campaign.05": lambda services, payload: _campaign_rows(services, payload, "audit_entries", "重複・無効の確認です。"),
    "campaign.06": _campaign_deadline,
    "campaign.07": _campaign_draw,
    "campaign.09": lambda services, payload: _campaign_rows(services, payload, "expiry_preview", "期限切れの確認です。"),
    "campaign.10": _campaign_promote,
    "campaign.11": _campaign_close,
    "campaign.12": _campaign_outcome,
    "referral.01": _referral_issue,
    "referral.05": _referral_member,
    "referral.06": _referral_tier,
    "referral.07": lambda services, payload: _referral_report(services, payload, "特典達成の台帳です。"),
    "referral.08": lambda services, payload: _referral_report(services, payload, "付与履歴です。"),
    "referral.09": _referral_revoke,
    "analytics.04": _compare,
    "analytics.05": _followers,
    "analytics.06": _reactions,
    "analytics.07": _campaign_outcome,
    "analytics.08": lambda services, payload: _referral_report(services, payload, "紹介成果です。"),
    "analytics.11": _report_csv,
}
for _action in (
        "publisher.01", "publisher.02", "publisher.03", "publisher.04", "publisher.05",
        "engagement.02", "engagement.07", "campaign.02", "campaign.08",
        "referral.02", "referral.03", "referral.04", "analytics.09", "analytics.10"):
    HANDLERS[_action] = (lambda action: (lambda services, payload: _catalog(services, action, payload)))(_action)


def _registered_tags(services, payload):
    tags = _need(services, "discovery").registered_tags()
    shown = "、".join(tags) if tags else "なし"
    return _done("登録済みタグ: " + shown + "。", tags=tags)


def perform(action, payload, services):
    if action == "discovery.registered_tags":
        return _registered_tags(services, payload if isinstance(payload, dict) else {})
    handler = HANDLERS.get(action)
    if handler is None:
        raise ValueError("catalog_operation_invalid")
    prepared = normalize(action, payload if isinstance(payload, dict) else None)
    try:
        return handler(services, prepared)
    except PermissionError as exc:
        code = str(exc)
        if not re.fullmatch(r"[A-Za-z0-9_]{1,80}", code):
            code = "operation_not_permitted"
        raise ValueError(code) from None
