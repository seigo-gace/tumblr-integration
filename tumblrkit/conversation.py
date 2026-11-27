"""Choose a catalog operation from the owner's words and ask for what is missing.

The 79 names are internal operations. This module does not open a screen and
does not execute anything. The assistant asks, confirms, then calls the
existing store or Bridge.
"""
import re

from tumblrkit.action_catalog import BRIDGE_ROUTES, BY_ID, CATALOG

# One question at a time, in this order. Bridge confirmation stays in the
# existing preview; local confirmation uses the catalog flag.
FIELDS = {
    "account.01": (), "account.02": (), "account.03": (),
    "account.04": (), "account.05": (), "account.06": (),
    "discovery.01": ("target",), "discovery.02": (), "discovery.03": (),
    "discovery.04": ("target",), "discovery.05": ("target",),
    "discovery.06": ("target", "mode"), "discovery.07": ("target",),
    "library.01": (), "library.02": ("candidate_id",),
    "library.03": ("candidate_id",), "library.04": ("candidate_id",),
    "library.05": ("candidate_id",), "library.06": (),
    "library.07": ("query",), "library.08": ("candidate_id",),
    "media.01": (), "media.02": ("candidate_id", "media_index", "rights"),
    "media.03": ("candidate_id", "media_index", "rights"), "media.04": (),
    "media.05": ("candidate_id", "media_index", "rights"),
    "media.06": ("candidate_id",), "media.07": (),
    "engagement.01": ("target",), "engagement.02": ("candidate_id", "comment"),
    "engagement.03": ("target",), "engagement.04": (), "engagement.05": ("target",),
    "engagement.06": (), "engagement.07": ("engagement_action", "target"),
    "publisher.01": ("text",),
    "publisher.02": ("draft_id", "text", "expected_text"),
    "publisher.03": ("draft_id",),
    "publisher.04": ("draft_id", "attachment_ids", "media_job_ids"),
    "publisher.05": ("draft_id", "apply"), "publisher.06": ("draft_id",),
    "publisher.07": ("draft_id",), "publisher.08": ("draft_id", "publish_at"),
    "publisher.09": ("schedule_id", "publish_at"),
    "publisher.10": ("target",), "publisher.11": ("target",),
    "publisher.12": ("target",),
    "campaign.01": ("campaign_id", "prize", "deadline", "winners", "claim_seconds", "gate_topic_id"),
    "campaign.02": ("campaign_id",),
    "campaign.03": ("campaign_id", "deadline", "winners", "claim_seconds", "gate_topic_id"),
    "campaign.04": ("campaign_id",), "campaign.05": ("campaign_id",),
    "campaign.06": ("campaign_id", "deadline"), "campaign.07": ("campaign_id",),
    "campaign.08": ("campaign_id", "event_id"), "campaign.09": ("campaign_id",),
    "campaign.10": ("campaign_id",), "campaign.11": ("campaign_id",),
    "campaign.12": ("campaign_id",),
    "referral.01": ("campaign_id", "referrer_id"),
    "referral.02": ("campaign_id", "referrer_id"),
    "referral.03": ("campaign_id", "source_post_id", "destination"),
    "referral.04": ("campaign_id", "name"),
    "referral.05": ("campaign_id", "member_id"),
    "referral.06": ("campaign_id", "threshold", "reward"),
    "referral.07": ("campaign_id",), "referral.08": ("campaign_id",),
    "referral.09": ("campaign_id", "referrer_id"),
    "analytics.01": (), "analytics.02": (), "analytics.03": (),
    "analytics.04": ("period",), "analytics.05": (),
    "analytics.06": ("candidate_id",), "analytics.07": ("campaign_id",),
    "analytics.08": ("campaign_id",), "analytics.09": (), "analytics.10": (),
    "analytics.11": ("period",),
}
BRIDGE_TARGET_FIELD = {
    "discovery.04": "target", "discovery.05": "target",
    "publisher.07": "draft_id", "publisher.10": "target",
    "publisher.11": "target", "publisher.12": "target",
    "engagement.01": "target", "engagement.03": "target",
    "engagement.05": "target",
}
LOCAL_CONFIRM = frozenset(
    action.action_id for action in CATALOG
    if action.confirmation and action.action_id not in BRIDGE_ROUTES
) | frozenset({"engagement.07"})
QUESTIONS = {
    "text": "本文を書いてください。",
    "expected_text": "現在の本文を書いてください。",
    "draft_id": "下書きIDを書いてください。",
    "target": "対象を書いてください。",
    "query": "検索語を書いてください。",
    "candidate_id": "候補IDを書いてください。",
    "comment": "リブログ文を書いてください。",
    "engagement_action": "取り消す操作（like、reblog、follow）を書いてください。",
    "campaign_id": "企画IDを書いてください。",
    "prize": "景品の内容を書いてください。",
    "deadline": "締切のUNIX時刻を数字で書いてください。",
    "winners": "当選人数を数字で書いてください。",
    "claim_seconds": "受取期限の秒数を数字で書いてください。",
    "gate_topic_id": "GATE話題IDを書くか、なければ「なし」と書いてください。",
    "event_id": "通知IDを書いてください。",
    "referrer_id": "紹介者IDを書いてください。",
    "source_post_id": "紹介元の投稿IDを書いてください。",
    "destination": "共有先を書くか、台帳だけなら「なし」と書いてください。",
    "name": "名前を書いてください。",
    "member_id": "メンバーIDを書いてください。",
    "threshold": "段階の人数を数字で書いてください。",
    "reward": "特典の内容を書いてください。",
    "period": "期間（today、week、month）を書いてください。",
    "publish_at": "予約時刻のUNIX時刻を数字で書いてください。",
    "schedule_id": "予約IDを書いてください。",
    "media_index": "メディア番号を数字で書いてください。",
    "rights": "保存する権利を確認できた場合は「承諾」と書いてください。",
    "mode": "登録するか解除するかを書いてください。",
    "attachment_ids": "添付IDをカンマ区切りで書くか、なければ「なし」と書いてください。",
    "media_job_ids": "動画ジョブIDをカンマ区切りで書くか、なければ「なし」と書いてください。",
    "apply": "付けるタグをカンマ区切りで書くか、提案だけなら「なし」と書いてください。",
}
LABELS = (
    ("現在の本文", "expected_text"), ("下書きID", "draft_id"),
    ("候補ID", "candidate_id"), ("企画ID", "campaign_id"),
    ("取消操作", "engagement_action"), ("通知ID", "event_id"),
    ("紹介者", "referrer_id"), ("投稿ID", "source_post_id"),
    ("共有先", "destination"), ("メンバー", "member_id"),
    ("段階人数", "threshold"), ("当選人数", "winners"),
    ("受取秒数", "claim_seconds"), ("GATE話題", "gate_topic_id"),
    ("予約時刻", "publish_at"), ("予約ID", "schedule_id"),
    ("メディア番号", "media_index"), ("検索語", "query"),
    ("コメント", "comment"), ("対象", "target"), ("本文", "text"),
    ("景品", "prize"), ("締切", "deadline"), ("名前", "name"),
    ("特典", "reward"), ("期間", "period"), ("権利", "rights"),
    ("登録解除", "mode"), ("添付ID", "attachment_ids"),
    ("動画ジョブ", "media_job_ids"), ("適用タグ", "apply"),
)
_NAMES = tuple(sorted(((item.name, item.action_id) for item in CATALOG), key=lambda item: len(item[0]), reverse=True))


def blank_dialog():
    return {"current": None, "fields": {}, "queue": [], "paused": False,
            "choices": None, "goal_set": ()}


def find_actions(text):
    """Official names, longest first, in the order the owner wrote them."""
    if not isinstance(text, str) or not text:
        return []
    occupied = [False] * len(text)
    spans = []
    for name, action_id in _NAMES:
        start = 0
        while True:
            index = text.find(name, start)
            if index < 0:
                break
            end = index + len(name)
            if not any(occupied[index:end]):
                spans.append((index, action_id))
                for cursor in range(index, end):
                    occupied[cursor] = True
            start = index + len(name)
    spans.sort()
    return [action_id for _, action_id in spans]


def extract(text):
    if not isinstance(text, str):
        return {}
    masked = text
    found = {}
    for label, key in LABELS:
        match = re.search(re.escape(label) + r"\s*[:：]\s*([^\n\r]{1,2000})", masked)
        if not match:
            continue
        found[key] = match.group(1).strip()
        masked = masked[:match.start()] + (" " * (match.end() - match.start())) + masked[match.end():]
    return found


def _wanted(action):
    return FIELDS[action]


def ingest(dialog, text):
    """Return active when the utterance belongs to the catalog conversation."""
    found = find_actions(text)
    extracted = extract(text)
    current = dialog.get("current")
    if current and not found:
        missing = [field for field in _wanted(current) if field not in dialog["fields"]]
        if missing and text.strip():
            value = extracted.get(missing[0], text.strip())
            if value:
                dialog["fields"][missing[0]] = value[:2000]
        return "active"
    if not found:
        return "passthrough"
    useful = {key: value for key, value in extracted.items() if current and key in _wanted(current)}
    if current and current not in found and not dialog.get("fields"):
        dialog["choices"] = None
        dialog["current"] = found[0]
        dialog["queue"] = found[1:]
        dialog["fields"] = {key: value for key, value in extracted.items() if key in _wanted(found[0])}
        return "active"
    if current and current not in found:
        dialog["queue"].extend(found)
        dialog["fields"].update(useful)
        return "active"
    if current:
        dialog["fields"].update(useful)
        dialog["queue"].extend(item for item in found if item != current)
        return "active"
    dialog["current"] = found[0]
    dialog["queue"] = found[1:]
    dialog["fields"] = {key: value for key, value in extracted.items() if key in _wanted(found[0])}
    return "active"


def step(dialog):
    action = dialog.get("current")
    if not action:
        return {"kind": "done"}
    missing = [field for field in _wanted(action) if field not in dialog["fields"]]
    name = BY_ID[action].name
    if missing:
        question = _ASK.get((action, missing[0]), "「" + name + "」の" + QUESTIONS[missing[0]])
        return {"kind": "ask", "action": action,
                "text": question + "実行はまだしていません。"}
    if action in LOCAL_CONFIRM:
        return {"kind": "confirm", "action": action, "fields": dict(dialog["fields"]),
                "text": "「" + name + "」を実行します。確認を押すまで実行しません。"}
    return {"kind": "run", "action": action, "fields": dict(dialog["fields"])}


def operation_names():
    return ",".join(item.action_id + ":" + item.name for item in CATALOG)


_GOAL_ORDER = ("巡回", "投稿", "キャンペーン", "紹介", "集計", "メディア")
_GOAL_START = {
    "投稿": "publisher.01",
    "キャンペーン": "campaign.04",
    "紹介": "referral.08",
    "集計": "analytics.01",
    "メディア": "media.01",
}
_GOAL_OPTIONS = {
    "巡回": (
        ("フォロワー", "discovery.02"),
        ("フォロー先", "discovery.03"),
        ("フォロー中", "discovery.03"),
        ("新着", "discovery.01"),
        ("タグ", "discovery.05"),
        ("差分", "discovery.07"),
    ),
}
_PLAIN = (
    ("フォロワー", "discovery.02"),
    ("フォロー先", "discovery.03"),
    ("保存候補", "media.01"),
    ("容量", "media.07"),
    ("応募状況", "campaign.04"),
    ("付与履歴", "referral.08"),
    ("未分類", "analytics.10"),
    ("流入", "analytics.09"),
)
_ASK = {
    ("discovery.01", "target"): "どのタグ、またはどのブログを巡回しますか。例: cats。",
    ("discovery.04", "target"): "探すブログ名を書いてください。",
    ("discovery.05", "target"): "探すタグを書いてください。例: cats。",
    ("publisher.01", "text"): "投稿する本文を書いてください。",
    ("campaign.04", "campaign_id"): "状況を見る企画IDを書いてください。",
    ("referral.08", "campaign_id"): "履歴を見る企画IDを書いてください。",
}
_DO = re.compile(r"(して|しろ|してくれ|やれ|お願い|頼む|見せて|見たい|見て|確認して|確認しろ)")
_PARTICLES = frozenset({"を", "は", "が", "に", "で", "と", "も", "から", "して", "しろ", "見て", "新着", "巡回", "タグ"})


def choice_prompt(dialog):
    choices = dialog.get("choices")
    if choices == "several":
        names = "、".join(dialog.get("goal_set") or ())
        return "どれを実行しますか。" + names + " のどれか一言で書いてください。"
    if choices == "巡回":
        return "巡回を実行します。フォロワー、フォロー先、新着のどれを見ますか。新着ならタグかブログ名も書いてください。"
    return None


def _goals_in(text, selected):
    if not text and selected:
        return [goal for goal in _GOAL_ORDER if goal in selected]
    if not text:
        return []
    compact = re.sub(r"[\sをはがにでも、。！!？?]+", "", text)
    commands = {
        goal + suffix
        for goal in _GOAL_ORDER
        for suffix in ("", "して", "しろ", "してくれ", "やれ", "お願い", "見せて", "見て", "確認して", "確認しろ", "開始")
    }
    if compact not in commands:
        return []
    return [goal for goal in _GOAL_ORDER if goal in text]


def _option(choices, text):
    if not text:
        return None
    for phrase, action in _GOAL_OPTIONS.get(choices, ()):
        if phrase in text:
            return action
    return None


def _plain(text):
    if not text:
        return None
    for phrase, action in _PLAIN:
        if phrase in text:
            return action
    return None


def _fields_for(action, text):
    fields = {key: value for key, value in extract(text).items() if key in FIELDS[action]}
    if action in ("discovery.01", "discovery.05") and "target" not in fields:
        tagged = re.search(r"タグ\s*[:：]?\s*([^\s、。]{1,40})", text or "")
        if tagged and tagged.group(1) not in _PARTICLES:
            fields["target"] = tagged.group(1)
    if action == "discovery.01" and "target" not in fields:
        tokens = [part for part in re.split(r"[\s、。]+", text or "") if part and part not in _PARTICLES]
        if len(tokens) == 1 and len(tokens[0]) <= 64:
            fields["target"] = tokens[0]
    return fields


def pick_waiting(dialog, text):
    """Continue a goal question. Return True when the dialog changed."""
    choices = dialog.get("choices")
    if not choices or not text or find_actions(text):
        if find_actions(text):
            dialog["choices"] = None
        return False
    if choices == "several":
        hits = [goal for goal in (dialog.get("goal_set") or ()) if goal in text]
        if len(hits) != 1:
            return False
        dialog["choices"] = None
        dialog["goal_set"] = ()
        return everyday(dialog, hits[0], [hits[0]])
    action = _option(choices, text) or _plain(text)
    if not action:
        return False
    dialog["choices"] = None
    dialog["current"] = action
    dialog["fields"] = _fields_for(action, text)
    return True


def registered_patrol(text):
    """「登録されている#を巡回してフォローして」is one concrete request."""
    if not isinstance(text, str) or not text:
        return None
    registered = "登録" in text and ("#" in text or "タグ" in text or "ハッシュ" in text)
    if not (registered and "巡回" in text):
        return None
    return {"follow": "フォロ" in text}


def everyday(dialog, text, goals):
    """Turn 「巡回をしろ」 into a concrete operation or one plain question."""
    if find_actions(text or ""):
        return False
    action = _plain(text or "")
    if action and not dialog.get("current"):
        dialog["choices"] = None
        dialog["current"] = action
        dialog["fields"] = _fields_for(action, text or "")
        return True
    hits = _goals_in(text or "", goals or [])
    if not hits or dialog.get("current"):
        return False
    if len(hits) > 1:
        dialog["choices"] = "several"
        dialog["goal_set"] = tuple(hits)
        return True
    goal = hits[0]
    if goal == "巡回":
        dialog["choices"] = "巡回"
        return True
    dialog["choices"] = None
    dialog["current"] = _GOAL_START[goal]
    dialog["fields"] = {}
    return True
