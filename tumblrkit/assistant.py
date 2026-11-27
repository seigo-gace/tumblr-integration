"""Assistant loop for the operation screen.

Proposals stay separate from execution. Publish and announcement actions need
an explicit confirmation. Missing providers, unknown tools and failed calls are
not reported as completed work. Secret-like text is removed before any provider
prompt. Paid fallback is not attempted.
"""
import json
import hashlib
import os
import socket
import sqlite3
import re
import uuid
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tumblrkit.bridge import ALLOWED, CONFIRM, Bridge, ActionRequest
from tumblrkit.conversation import (
    BRIDGE_TARGET_FIELD, FIELDS as OPERATION_FIELDS, blank_dialog, choice_prompt,
    everyday, find_actions, ingest, operation_names, pick_waiting,
    registered_patrol, step,
)
from tumblrkit.action_catalog import BRIDGE_ROUTES, resolve_bridge_action

MODULES = (
    "account", "discovery", "library", "media", "engagement",
    "publishing", "campaigns", "referrals", "analytics",
)
INDEX = (
    ("tumblr.discover", ("検索", "見つけ", "発見")),
    ("tumblr.list_posts", ("一覧", "投稿を見")),
    ("tumblr.publish_draft", ("公開", "投稿して")),
    ("tumblr.edit_post", ("投稿を編集", "公開後編集")),
    ("tumblr.delete_post", ("投稿を削除", "公開投稿削除")),
    ("tumblr.reconcile_post", ("投稿の状態確認", "変更結果を照合")),
    ("telegram.check_membership", ("参加確認", "メンバー確認")),
    ("telegram.publish_announcement", ("告知",)),
    ("analytics.get_summary", ("分析", "集計")),
    ("engagement.like", ("いいね", "Like")),
    ("engagement.reblog", ("リブログ", "Reblog")),
    ("engagement.follow", ("フォローして", "Follow")),
)
SECRET = re.compile(r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]{8,}|api[_-]?key\s*[:=]\s*\S+|sk-[A-Za-z0-9]+|ghp_[A-Za-z0-9]+)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TELEGRAM = re.compile(r"(?i)\b(tg://\S+|@[A-Za-z0-9_]{5,})")
TARGET = re.compile(r"対象(?:[:：]\s*|\s+)([^\r\n]{1,64})")
GOALS = frozenset(("巡回", "投稿", "キャンペーン", "紹介", "集計", "メディア"))
# These are the *currently wired* dispatch actions only. Future catalog IDs
# must be imported from the exact original 79-ID artifact, not invented here.
GOAL_ACTIONS = {
    "巡回": ("tumblr.discover", "tumblr.list_posts",
             "engagement.like", "engagement.reblog", "engagement.follow"),
    "投稿": ("tumblr.list_posts", "tumblr.publish_draft",
             "tumblr.edit_post", "tumblr.delete_post",
             "tumblr.reconcile_post"),
    "キャンペーン": (),
    "紹介": (),
    "集計": ("analytics.get_summary",),
    "メディア": ("tumblr.discover",),
}
# Owner operations already implemented outside the Bridge. They become assistant
# actions only when the running app binds the same stores the owner screens use.
CATALOG_FIELDS = {
    "publisher.01": (frozenset({"text"}),),
    "publisher.02": (frozenset({"draft_id", "text", "expected_text"}),),
    "publisher.03": (frozenset({"draft_id"}),),
    "publisher.04": (frozenset({"draft_id", "attachment_ids", "media_job_ids"}),),
    "publisher.05": (frozenset({"draft_id"}), frozenset({"draft_id", "apply"})),
    "engagement.02": (frozenset({"candidate_id", "comment"}),),
    "engagement.07": (frozenset({"engagement_action", "target"}),),
    "campaign.02": (frozenset({"campaign_id"}),),
    "campaign.08": (frozenset({"campaign_id", "event_id"}),),
    "referral.02": (frozenset({"campaign_id", "referrer_id"}),),
    "referral.03": (
        frozenset({"campaign_id", "source_post_id"}),
        frozenset({"campaign_id", "source_post_id", "destination", "confirmed"}),
    ),
    "referral.04": (frozenset({"campaign_id", "name"}),),
    "analytics.09": (frozenset(),),
    "analytics.10": (frozenset(),),
}
CATALOG_GOALS = {
    "巡回": (("engagement.02", "discovery"), ("engagement.07", "engagement_history")),
    "投稿": tuple((action, "drafts") for action in (
        "publisher.01", "publisher.02", "publisher.03", "publisher.04", "publisher.05")),
    "キャンペーン": (("campaign.02", "campaign"), ("campaign.08", "campaign")),
    "紹介": (("referral.02", "referral"), ("referral.03", "referral"), ("referral.04", "referral")),
    "集計": (("analytics.09", "analytics"), ("analytics.10", "analytics")),
}
CATALOG_EXTERNAL = frozenset({"engagement.07", "campaign.08"})



class ProviderError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def redact(text):
    cleaned = SECRET.sub("[redacted]", text)
    cleaned = EMAIL.sub("[redacted]", cleaned)
    cleaned = TELEGRAM.sub("[redacted]", cleaned)
    return cleaned


def match_index(text):
    found = [action for action, words in INDEX if any(word in text for word in words)]
    if "削除" in text or "権限" in text:
        return ["unlisted"]
    return found



class DurableFreeQuota:
    """Fail-closed, cross-restart reservation before any live provider request.

    Counts attempted requests, including failures. One SQLite file must be shared
    by every running copy of this operation app on the same host.
    """

    DAILY_PER_PROVIDER = 5
    DAILY_TOTAL = 10
    COOLDOWN_SECONDS = 60

    def __init__(self, env):
        self.enabled = env.get("TUMBLR_AI_LIVE_ENABLED") == "1"
        self.path = env.get("TUMBLR_AI_QUOTA_DB", "").strip()

    def reserve(self, provider):
        if not self.enabled or not self.path:
            raise ProviderError("ai_disabled")
        import sqlite3
        import time

        now = int(time.time())
        day = now // 86400
        try:
            with sqlite3.connect(self.path, timeout=3, isolation_level=None) as db:
                db.execute("PRAGMA busy_timeout=3000")
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS ai_request_budget "
                    "(day INTEGER NOT NULL, provider TEXT NOT NULL, count INTEGER NOT NULL, "
                    "last_attempt INTEGER NOT NULL, PRIMARY KEY(day, provider))"
                )
                rows = dict(db.execute(
                    "SELECT provider, count FROM ai_request_budget WHERE day=?",
                    (day,)
                ).fetchall())
                recent = db.execute(
                    "SELECT MAX(last_attempt) FROM ai_request_budget WHERE day=?",
                    (day,)
                ).fetchone()[0]
                if sum(rows.values()) >= self.DAILY_TOTAL or rows.get(provider, 0) >= self.DAILY_PER_PROVIDER:
                    db.execute("ROLLBACK")
                    raise ProviderError("quota_exhausted")
                if recent is not None and now - recent < self.COOLDOWN_SECONDS:
                    db.execute("ROLLBACK")
                    raise ProviderError("quota_cooldown")
                db.execute(
                    "INSERT INTO ai_request_budget(day,provider,count,last_attempt) "
                    "VALUES(?,?,1,?) ON CONFLICT(day,provider) DO UPDATE SET "
                    "count=count+1,last_attempt=excluded.last_attempt",
                    (day, provider, now),
                )
                db.execute("COMMIT")
        except ProviderError:
            raise
        except (OSError, sqlite3.Error):
            raise ProviderError("quota_unavailable") from None

class ScriptedProvider:
    def __init__(self, name, payload=None, error=None, paid=True):
        self.name = name
        self.paid = paid
        self.payload = payload
        self.error = error
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise ProviderError(self.error)
        return dict(self.payload or {})


class HttpProvider:
    def __init__(self, name, url, headers, paid, transport, model=None, quota=None):
        self.name = name
        self.url = url
        self.headers = headers
        self.paid = paid
        self.transport = transport
        self.model = model
        self.quota = quota

    def complete(self, prompt):
        if self.quota is not None:
            self.quota.reserve(self.name)
        status, payload = self.transport.post(self.url, self.headers, json.dumps(self._body(prompt)).encode("utf-8"))
        failure = _http_failure(status, payload)
        if failure:
            raise ProviderError(failure)
        try:
            plan = json.loads(_provider_text(self.name, payload))
            if not isinstance(plan, dict):
                raise ProviderError('invalid_response')
            return plan
        except (json.JSONDecodeError, TypeError, KeyError, IndexError, AttributeError):
            raise ProviderError('invalid_response') from None

    def _body(self, prompt):
        if self.name == "gemini":
            return {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json"},
            }
        model = self.model or ("openai/gpt-oss-20b" if self.name == "groq" else "openrouter/free")
        body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        if self.name == "groq":
            body["response_format"] = {"type": "json_object"}
        return body


def providers_from_env(env, transport=None):
    quota = DurableFreeQuota(env) if transport is None else None
    transport = transport or UrllibTransport()
    chosen = []
    # Keep the existing provider keys, but use only zero-price models by default.
    # The former provider split is available only through explicit owner configuration.
    mode = env.get("TUMBLR_AI_ROUTING_MODE", "free_router")
    if mode not in ("free_router", "legacy_split"):
        raise ProviderError("invalid_routing_mode")
    if mode == "legacy_split" and env.get("TUMBLR_GEMINI_API_KEY"):
        chosen.append(HttpProvider(
            "gemini",
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent",
            {"x-goog-api-key": env["TUMBLR_GEMINI_API_KEY"]},
            True,
            transport,
            quota=quota,
        ))
    if mode == "legacy_split" and env.get("TUMBLR_GROQ_API_KEY"):
        chosen.append(HttpProvider(
            "groq",
            "https://api.groq.com/openai/v1/chat/completions",
            {"authorization": "Bearer " + env["TUMBLR_GROQ_API_KEY"]},
            True,
            transport,
            quota=quota,
        ))
    model = env.get("TUMBLR_OPENROUTER_MODEL", "openrouter/free")
    if env.get("TUMBLR_OPENROUTER_API_KEY"):
        if not str(model).endswith(":free") and model != "openrouter/free":
            raise ProviderError("paid_model_forbidden")
        chosen.append(HttpProvider(
            "openrouter",
            "https://openrouter.ai/api/v1/chat/completions",
            {"authorization": "Bearer " + env["TUMBLR_OPENROUTER_API_KEY"]},
            False,
            transport,
            model=model,
            quota=quota,
        ))
    return chosen


def _http_failure(status, payload):
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and error.get("type") == "permissions_error":
        return "model_forbidden"
    if status == 429 or (isinstance(error, dict) and error.get("status") == "RESOURCE_EXHAUSTED"):
        return "rate_limited"
    if status in (402, 403) or error == "quota":
        return "free_tier_exhausted"
    if status != 200 or not isinstance(payload, dict):
        return "failed"
    return None


def _provider_text(name, payload):
    if name == "gemini":
        parts = payload["candidates"][0]["content"]["parts"]
        visible = [part.get("text") for part in parts if part.get("text") and not part.get("thought")]
        if not visible:
            visible = [part.get("text") for part in parts if part.get("text")]
        return visible[-1]
    return payload["choices"][0]["message"]["content"]


class UrllibTransport:
    def post(self, url, headers, body):
        request = Request(url, data=body, headers={
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": "tgtumbler-assistant",
            **headers,
        }, method="POST")
        try:
            with urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            return error.code, payload
        except (URLError, TimeoutError, socket.timeout, OSError):
            raise ProviderError('network_unavailable') from None


def _order(text, providers):
    simple = len(text) <= 80 and not any(word in text for word in ("計画", "文章", "書いて", "構成"))
    names = ["groq", "gemini"] if simple else ["gemini", "groq"]
    ranked = []
    for name in names:
        ranked.extend(item for item in providers if item.name == name)
    ranked.extend(item for item in providers if item.name == "openrouter")
    return ranked


class Assistant:
    def __init__(self, bridge=None, providers=None, transport=None, draft_store=None,
                 catalog_perform=None):
        self.bridge = bridge or Bridge({})
        self.providers = providers
        self.transport = transport
        self.pending = {}
        self.dialog = blank_dialog()
        self._turn_lock = threading.RLock()
        self._scoped_turns = {}
        # One durable Owner draft store; no second in-memory draft ledger.
        self.draft_store = draft_store
        self.catalog_perform = catalog_perform

    def _available(self):
        if self.providers is not None:
            return list(self.providers)
        return providers_from_env(os.environ, self.transport)

    def turn(self, message="", confirm=False, request_id=None, draft=False, abort=False,
             goals=None, selected_action=None, selected_target=None, catalog_action=None,
             catalog_payload=None, existing_buttons=None, session_id=None):
        """Correlate authenticated HTTP turns without sharing conversation state.

        session_id is a trusted server-side scope, never prompt data.
        Calls without a scope retain the original direct-call interface.
        """
        args = dict(message=message, confirm=confirm, request_id=request_id,
                    draft=draft, abort=abort, goals=goals,
                    selected_action=selected_action, selected_target=selected_target,
                    catalog_action=catalog_action, catalog_payload=catalog_payload,
                    existing_buttons=existing_buttons)
        with self._turn_lock:
            if session_id is None:
                return self._turn(**args)
            if not isinstance(session_id, str) or not session_id:
                return self._pack("failed", "会話を識別できません。実行していません。",
                                  False, recovery=True)
            return self._scoped_turn(session_id, args)

    def _scoped_turn(self, session_id, args):
        now = time.monotonic()
        # In-memory continuation only; expire abandoned operations.
        for key, record in list(self._scoped_turns.items()):
            if record["expires"] <= now:
                self._scoped_turns.pop(key, None)
        request_id = args["request_id"]
        key = (session_id, request_id)
        record = self._scoped_turns.get(key) if request_id else None
        if request_id and record is None:
            return self._pack("failed", "この会話の操作を確認できません。実行していません。",
                              False, recovery=True)
        if args["abort"]:
            if record is not None:
                self._scoped_turns.pop(key, None)
            return self._pack("aborted", "中断しました。実行していません。", False)
        if args["confirm"] and (record is None or record["kind"] != "confirm"):
            return self._pack("failed", "確認できる提案がありません。実行していません。",
                              False, recovery=True, request_id=request_id if record else None)
        if record is not None and record["kind"] == "confirm" and not args["confirm"]:
            if not find_actions(redact((args["message"] or "").strip())):
                return self._pack("confirmation_required", "確認を押すまで実行しません。",
                                  False, request_id=request_id)
        # Serialize the existing stateful engine and restore direct-call state.
        # Each scoped operation has its own dialog and pending dictionary.
        previous_dialog, previous_pending = self.dialog, self.pending
        self.dialog = record["dialog"] if record else blank_dialog()
        self.pending = record["pending"] if record else {}
        if record and not args["goals"]:
            args["goals"] = record["goals"]
        if record is not None:
            self._scoped_turns.pop(key, None)
        try:
            response = self._turn(**args)
            confirmation = response.get("state") == "confirmation_required"
            waiting = (not confirmation and response.get("state") == "guidance"
                       and bool(self.dialog.get("current") or self.dialog.get("queue")))
            if confirmation or waiting:
                next_id = response.get("request_id") if confirmation else None
                next_id = next_id or uuid.uuid4().hex
                response["request_id"] = next_id
                if waiting:
                    response["state"] = "input_required"
                self._scoped_turns[(session_id, next_id)] = {
                    "kind": "confirm" if confirmation else "input",
                    "dialog": self.dialog, "pending": self.pending,
                    "goals": list(args["goals"] or []), "expires": now + 12 * 60 * 60,
                }
            return response
        finally:
            self.dialog, self.pending = previous_dialog, previous_pending

    def _turn(self, message="", confirm=False, request_id=None, draft=False, abort=False,
              goals=None, selected_action=None, selected_target=None, catalog_action=None,
              catalog_payload=None, existing_buttons=None):
        if abort:
            self.pending.clear()
            self.dialog = blank_dialog()
            return self._pack("aborted", "中断しました。実行していません。", False)
        if draft:
            if self.draft_store is None:
                return self._pack("not_connected",
                    "永続下書きDBが未接続のため保存していません。",False,recovery=True)
            try:
                stored=self.draft_store.save(message or "")
            except (ValueError,OSError):
                return self._pack("failed","下書きを保存できませんでした。再実行前に保存状態を確認してください。",
                                  False,recovery=True)
            result=self._pack("draft_saved",
                "下書きを既存の保存先に記録しました。Tumblrへは投稿していません。",
                False)
            result["draft_id"]=stored["id"]
            return result
        if confirm:
            response = self._confirm(request_id)
            return self._after_dialog(response, goals if isinstance(goals, list) else [])
        if goals is None:
            goals=[]
        if (not isinstance(goals,list) or len(goals)>len(GOALS)
                or any(not isinstance(goal,str) or goal not in GOALS for goal in goals)
                or len(set(goals))!=len(goals)):
            return self._pack("invalid_goals", "目的の選択が不正です。実行していません。", False, recovery=True)
        if catalog_action in CATALOG_FIELDS:
            payload = catalog_payload if isinstance(catalog_payload, dict) else {}
            return self._catalog(catalog_action, payload, "owner_selected", owner_selected=True)
        if catalog_action is not None:
            from tumblrkit.action_catalog import resolve_bridge_action
            try:
                action,target=resolve_bridge_action(catalog_action,selected_target or "")
            except ValueError as exc:
                if str(exc)=="catalog_action_not_connected":
                    return self._pack("not_connected",
                        "この79操作は元設計にありますが、実行先が未接続です。"
                        "実行済みとは表示しません。",False,recovery=True)
                return self._pack("invalid_tool",
                    "元の操作IDか対象が不正です。実行していません。",False,recovery=True)
            # Reuse exact selected action gate (same target constraints).
            return self._turn(message="",goals=goals,
                             selected_action=action,selected_target=target)
        uttered = redact((message or "").strip())
        if uttered or self.dialog.get("current") or self.dialog.get("queue") or self.dialog.get("paused"):
            handled = self._conversation(uttered, goals)
            if handled is not None:
                return handled
        if existing_buttons is not None and selected_action!="telegram.publish_announcement":
            return self._pack("invalid_tool",
                "既存ボタンはTelegram告知の投稿確認だけに指定できます。",False,recovery=True)
        if selected_action is not None:
            saved_discovery=(selected_action=="tumblr.discover" and
                             isinstance(selected_target,str) and
                             selected_target.startswith("saved:"))
            edit_target=(selected_action=="tumblr.edit_post" and
                         isinstance(selected_target,str) and
                         re.fullmatch(r"[a-f0-9]{32}:[a-f0-9]{32}",selected_target))
            maximum=150 if saved_discovery else (65 if edit_target else 64)
            if (not isinstance(selected_action,str) or selected_action not in ALLOWED
                    or not isinstance(selected_target,str) or not 0<len(selected_target)<=maximum):
                return self._pack("invalid_tool","未登録の操作または不正な対象を拒否しました。実行していません。",False,recovery=True)
            return self._propose(selected_action,selected_target,
                                 message if selected_action=="telegram.publish_announcement" else "",
                                 "", "", "owner_selected",ai=False,
                                 existing_buttons=existing_buttons)
        text = (message or "").strip()
        if not text and not goals:
            return self._pack("unclear", "やりたいことを一文で書いてください。実行していません。", False, recovery=True)
        if not text and goals:
            return self._goal_guidance(goals)
        cleaned = redact(text)
        omitted = "秘密らしき文字列はAIへ送っていません。" if cleaned != text else ""
        local = match_index(cleaned)
        if local == ["unlisted"]:
            return self._pack("refused_unlisted", "その操作は登録された道具に無いため実行していません。" + omitted, False, recovery=True)
        try:
            plan, provider_name = self._ask(cleaned,goals)
        except ProviderError as exc:
            return self._pack(exc.code, self._failure_text(exc.code) + omitted, False, recovery=True)
        if plan is None:
            if goals:
                return self._goal_guidance(goals)
            return self._local(local, cleaned, omitted)
        if not isinstance(plan, dict):
            return self._pack("invalid_response", "AI応答形式が不正なため実行していません。" + omitted, False, recovery=True)
        action, target, question, reply = (plan.get(key, "") for key in ("action", "target", "question", "reply"))
        if any(not isinstance(value, str) for value in (action, target, question, reply)):
            return self._pack("invalid_response", "AI応答形式が不正なため実行していません。" + omitted, False, recovery=True)
        if len(action) > 128 or len(target) > 256 or len(question) > 2000 or len(reply) > 4000:
            return self._pack("invalid_response", "AI応答が長すぎるため実行していません。" + omitted, False, recovery=True)
        if action in OPERATION_FIELDS:
            self.dialog = blank_dialog()
            self.dialog["current"] = action
            supplied = plan.get("fields") if isinstance(plan.get("fields"), dict) else {}
            self.dialog["fields"] = {
                key: value for key, value in supplied.items()
                if key in OPERATION_FIELDS[action] and isinstance(value, (str, int))
                and not isinstance(value, bool)
            }
            response = self._drain(goals)
            if omitted:
                response["text"] += omitted
            return response
        named = find_actions(reply)
        if not action and len(named) == 1:
            self.dialog = blank_dialog()
            self.dialog["current"] = named[0]
            response = self._drain(goals)
            if omitted:
                response["text"] += omitted
            return response
        if action and action not in ALLOWED:
            return self._pack("invalid_tool", "AIの応答に未登録の操作があったため捨てました。実行していません。" + omitted, False, recovery=True)
        if not action:
            if reply or question:
                answer = reply + ("\n" if reply and question else "") + question + omitted
                response=self._pack("guidance", answer, False,
                                  recovery=bool(question), provider=provider_name)
                if goals:
                    response["plan"]=self._goal_plan(goals)
                return response
            return self._pack("unclear", "目的をもう少し詳しく教えてください。実行していません。" + omitted,
                              False, recovery=True, provider=provider_name)
        response=self._propose(action, target, cleaned, omitted, question, provider_name, ai=True)
        if goals:
            response["plan"]=self._goal_plan(goals)
        return response

    def _ask(self, cleaned, goals=None):
        providers = _order(cleaned, self._available())
        if not providers:
            return None, "none"
        prompt = (
            "Tumblr運用のAIアシスタントとして日本語で要望を理解し誘導する。"
            "JSONオブジェクトのみ返す。キーはaction,target,reply,question,fields。"
            "操作しない相談・企画・説明はactionを空にしてreplyで案内する。"
            "不足情報はquestionで一つずつ確認する。接続・公開済みと推測で主張しない。"
            "実操作の提案は登録済アクションのみ。許可された操作: "
            + ",".join(sorted(ALLOWED))
            + "。運営操作は次のIDから一つ選び、actionにそのID、分かった入力はfieldsへ入れる: "
            + operation_names()
            + "。選択目的: " + "・".join(goals or [])
            + "。複数の目的は必ず考慮するが、未接続操作は実行済みと主張しない。"
            + "。利用者の文: " + cleaned
        )
        primary = providers[0]
        fallback = next((item for item in providers[1:] if item.name == "openrouter" and not item.paid), None)
        try:
            plan = primary.complete(prompt)
            return plan, primary.name
        except ProviderError as exc:
            if fallback is None or exc.code in ("rate_limited", "free_tier_exhausted", "model_forbidden", "ai_disabled", "quota_exhausted", "quota_cooldown", "quota_unavailable"):
                raise
            try:
                plan = fallback.complete(prompt)
            except ProviderError:
                raise exc
            return plan, fallback.name

    def _goal_plan(self, goals):
        """Display only real dispatch contracts; never silently omit a goal."""
        actions=[]
        unavailable=[]
        seen=set()
        for goal in goals:
            candidates=GOAL_ACTIONS[goal]
            catalog=CATALOG_GOALS.get(goal, ()) if self.catalog_perform is not None else ()
            if not candidates and not catalog:
                unavailable.append(goal)
            for action in candidates:
                if action in ALLOWED and action not in seen:
                    seen.add(action)
                    domain=action.split(".",1)[0]
                    actions.append({
                        "action":action,"goal":goal,
                        "basis":"既存Bridgeに登録された操作契約",
                        "connected":domain in self.bridge.adapters,
                        "requires_target":True,
                        "confirmation_required":action in CONFIRM,
                    })
            for action, screen in catalog:
                if action in seen:
                    continue
                seen.add(action)
                actions.append({
                    "action":action,"goal":goal,
                    "basis":"既存の運営操作",
                    "connected":True,
                    "catalog":True,
                    "owner_ui":screen,
                    "requires_target":action not in ("analytics.09","analytics.10"),
                    "confirmation_required":action in CATALOG_EXTERNAL,
                })
        return {"goals":list(goals),"recommendations":actions,
                "not_connected_goals":unavailable,
                "all_actions_count":len(actions),"evidence":"existing_bridge_registry",
                "executed":False}

    def _goal_guidance(self, goals):
        self.dialog = blank_dialog()
        everyday(self.dialog, "", goals)
        return self._drain(goals)

    def _conversation(self, text, goals):
        if self.dialog.get("paused"):
            if not find_actions(text):
                request_id = next(iter(self.pending), None)
                return self._pack(
                    "confirmation_required",
                    "確認を押すまで実行しません。別の操作名を書くと、その操作に移ります。",
                    False, recovery=True, request_id=request_id)
            self.pending.clear()
            self.dialog = blank_dialog()
        patrol = registered_patrol(text)
        if patrol and not self.dialog.get("paused"):
            return self._start_registered_patrol(patrol["follow"], goals)
        if pick_waiting(self.dialog, text):
            return self._drain(goals)
        if ingest(self.dialog, text) == "passthrough" and not everyday(self.dialog, text, goals):
            return None
        return self._drain(goals)

    def _drain(self, goals):
        prompt = choice_prompt(self.dialog)
        if prompt and not self.dialog.get("current") and not self.dialog.get("queue"):
            response = self._pack("guidance", prompt, False, recovery=True, provider="conversation")
            if goals:
                response["plan"] = {"goals": list(goals), "recommendations": [],
                                    "not_connected_goals": [], "all_actions_count": 0,
                                    "evidence": "existing_bridge_registry", "executed": False}
            return response
        texts = []
        executed = False
        response = None
        for _ in range(8):
            if not self.dialog.get("current"):
                if not self.dialog.get("queue"):
                    break
                self.dialog["current"] = self.dialog["queue"].pop(0)
                self.dialog["fields"] = {}
            decision = step(self.dialog)
            if decision["kind"] == "done":
                break
            if decision["kind"] == "ask":
                response = self._pack("guidance", "\n".join(texts + [decision["text"]]), False,
                                      recovery=True, action=decision["action"], provider="conversation")
                break
            if decision["kind"] == "confirm":
                response = self._hold(decision)
                if texts:
                    response["text"] = "\n".join(texts) + "\n" + response["text"]
                break
            response = self._run_decision(decision)
            if response["state"] == "confirmation_required":
                if texts:
                    response["text"] = "\n".join(texts) + "\n" + response["text"]
                break
            if response["state"] != "completed":
                if texts:
                    response["text"] = "\n".join(texts) + "\n" + response["text"]
                self.dialog["queue"] = []
                self.dialog["current"] = None
                self.dialog["fields"] = {}
                break
            executed = executed or response["executed"] is True
            texts.append(response["text"])
            self._note_blogs(response)
            self.dialog["current"] = None
            self.dialog["fields"] = {}
            if self.dialog.get("tag_queue"):
                self.dialog["current"] = "discovery.05"
                self.dialog["fields"] = {"target": self.dialog["tag_queue"].pop(0)}
                continue
            if self.dialog.get("follow_after"):
                self.dialog["follow_after"] = False
                blogs = self.dialog.get("follow_blogs") or []
                if blogs:
                    self.dialog["follow_queue"] = blogs[1:]
                    self.dialog["current"] = "engagement.05"
                    self.dialog["fields"] = {"target": blogs[0]}
                    continue
                texts.append("フォローできる新しいブログはありません。フォローはしていません。")
            if not self.dialog.get("queue"):
                response["text"] = "\n".join(texts)
                response["executed"] = executed
                break
        if response is None:
            response = self._pack("guidance", "やりたい操作名を書いてください。実行はまだしていません。",
                                  False, recovery=True, provider="conversation")
        if goals:
            response["plan"] = {"goals": list(goals), "recommendations": [],
                                "not_connected_goals": [], "all_actions_count": 0,
                                "evidence": "existing_bridge_registry", "executed": False}
        return response

    def _hold(self, decision):
        request_id = uuid.uuid4().hex
        self.pending[request_id] = {"kind": "catalog", "action": decision["action"],
                                    "payload": decision["fields"]}
        self.dialog["paused"] = True
        return self._pack("confirmation_required", decision["text"], False,
                          request_id=request_id, action=decision["action"], provider="conversation")

    def _run_decision(self, decision):
        action = decision["action"]
        if action in BRIDGE_ROUTES:
            field = BRIDGE_TARGET_FIELD.get(action, "")
            supplied = decision["fields"].get(field, "") if field else ""
            bridge_action, target = resolve_bridge_action(action, supplied)
            response = self._propose(bridge_action, target, "", "", "", "conversation", ai=False)
            if response["state"] == "confirmation_required":
                self.dialog["paused"] = True
            return response
        return self._execute_owner(action, decision["fields"])

    def _execute_owner(self, action, fields):
        if self.catalog_perform is None:
            return self._pack("not_connected", "この操作の保存先が未接続です。実行していません。",
                              False, recovery=True, action=action, provider="conversation")
        try:
            result = self.catalog_perform(action, fields)
        except ValueError as exc:
            code = str(exc)
            if not re.fullmatch(r"[A-Za-z0-9_]{1,80}", code):
                code = "operation_failed"
            return self._pack("failed", "操作を完了できませんでした（" + code + "）。完了扱いしません。",
                              False, recovery=True, action=action, provider="conversation")
        if not isinstance(result, dict) or result.get("verified") is not True:
            return self._pack("failed", "操作の結果を確認できません。完了扱いしません。",
                              False, recovery=True, action=action, provider="conversation")
        text = result.get("summary") if isinstance(result.get("summary"), str) else "操作を実行しました。"
        external = action in CATALOG_EXTERNAL or result.get("external") is True
        return self._pack("completed", text[:2000], external, action=action, provider="conversation")

    def _note_blogs(self, response):
        if not self.dialog.get("follow_after"):
            return
        view = response.get("view") if isinstance(response.get("view"), dict) else {}
        rows = view.get("rows") if isinstance(view.get("rows"), list) else []
        found = self.dialog.setdefault("follow_blogs", [])
        for row in rows:
            blog = row.get("blog") if isinstance(row, dict) else None
            if isinstance(blog, str) and blog and blog not in found and len(found) < 5:
                found.append(blog)

    def _start_registered_patrol(self, follow, goals):
        self.dialog = blank_dialog()
        self.dialog["follow_after"] = bool(follow)
        self.dialog["follow_blogs"] = []
        self.dialog["tag_queue"] = []
        self.dialog["follow_queue"] = []
        if self.catalog_perform is None:
            return self._pack("not_connected",
                              "登録タグの保存先が未接続です。巡回もフォローもしていません。",
                              False, recovery=True, provider="conversation")
        try:
            listed = self.catalog_perform("discovery.registered_tags", {})
        except ValueError as exc:
            code = str(exc)
            if not re.fullmatch(r"[A-Za-z0-9_]{1,80}", code):
                code = "operation_failed"
            return self._pack("failed", "登録タグを確認できませんでした（" + code + "）。巡回もフォローもしていません。",
                              False, recovery=True, provider="conversation")
        tags = listed.get("tags") if isinstance(listed, dict) else None
        tags = [tag for tag in tags if isinstance(tag, str) and tag][:5] if isinstance(tags, list) else []
        if not tags:
            self.dialog["current"] = "discovery.05"
            response = self._drain(goals)
            response["text"] = ("登録済みのタグは0件です。巡回もフォローもしていません。\n"
                                + response["text"])
            return response
        self.dialog["tag_queue"] = tags[1:]
        self.dialog["current"] = "discovery.05"
        self.dialog["fields"] = {"target": tags[0]}
        response = self._drain(goals)
        lead = "登録済みタグ " + "、".join(tags) + " を巡回します。"
        if follow:
            lead += "フォローは確認してから1件ずつ実行します。"
        response["text"] = lead + "\n" + response["text"]
        return response

    def _after_dialog(self, response, goals):
        if not self.dialog.get("paused"):
            return response
        self.dialog["paused"] = False
        if response.get("state") != "completed":
            self.dialog["current"] = None
            self.dialog["fields"] = {}
            self.dialog["queue"] = []
            self.dialog["tag_queue"] = []
            self.dialog["follow_queue"] = []
            self.dialog["follow_after"] = False
            return response
        self.dialog["current"] = None
        self.dialog["fields"] = {}
        if self.dialog.get("follow_queue"):
            self.dialog["current"] = "engagement.05"
            self.dialog["fields"] = {"target": self.dialog["follow_queue"].pop(0)}
        elif self.dialog.get("queue"):
            self.dialog["current"] = self.dialog["queue"].pop(0)
        else:
            return response
        extra = self._drain(goals)
        if extra and extra.get("text"):
            response["text"] += "\n" + extra["text"]
            if extra.get("state") != "completed":
                response["state"] = extra["state"]
                response["request_id"] = extra.get("request_id")
                response["recovery"] = extra.get("recovery", True)
                response["executed"] = False
                response["action"] = extra.get("action")
        return response

    def _local(self, local, cleaned, omitted, question=""):
        if len(local) != 1:
            notice = "AI Providerは未接続です。意図を一つに決められませんでした。実行していません。"
            return self._pack("unclear", notice + omitted + question, False, recovery=True, provider="none")
        target = TARGET.search(cleaned)
        return self._propose(local[0], target.group(1) if target else "", cleaned, omitted, question, "none", ai=False)

    def _catalog(self, action, payload, provider, owner_selected=False):
        if self.catalog_perform is None:
            return self._pack("not_connected",
                "この運営操作の保存先が未接続です。実行していません。",
                False, recovery=True, action=action, provider=provider)
        if not self._catalog_payload_ok(payload) or set(payload) not in CATALOG_FIELDS[action]:
            return self._pack("unclear",
                "この運営操作に必要な入力が不足しています。画面の項目を指定してください。実行していません。",
                False, recovery=True, action=action, provider=provider)
        external = action in CATALOG_EXTERNAL or (action == "referral.03" and "destination" in payload)
        if (external or not owner_selected) and provider != "confirmed":
            request_id = uuid.uuid4().hex
            self.pending[request_id] = {"kind": "catalog", "action": action, "payload": payload}
            return self._pack("confirmation_required",
                "運営操作の確認: " + action + "。確認を押すまで実行しません。",
                False, request_id=request_id, action=action, provider=provider)
        try:
            result = self.catalog_perform(action, payload)
        except ValueError as exc:
            code = str(exc)
            if not code or " " in code or len(code) > 80:
                code = "catalog_operation_invalid"
            return self._pack("failed",
                "運営操作を完了できませんでした（" + code + "）。完了扱いしません。",
                False, recovery=True, action=action, provider=provider)
        if not isinstance(result, dict) or result.get("verified") is not True:
            return self._pack("failed", "運営操作の結果を確認できません。完了扱いしません。",
                              False, recovery=True, action=action, provider=provider)
        external_done = external and result.get("verified") is True
        text = "運営操作を実行しました。"
        if result.get("published") is False or result.get("tumblr_upload") is False:
            text = "運営操作を既存の保存先で実行しました。Tumblrへは公開していません。"
        if action in ("analytics.09", "analytics.10"):
            text = "記録済みの流入を確認しました。新しい計測はしていません。件数: " + str(result.get("total", 0))
        if isinstance(result.get("draft_id"), str):
            text += " 下書きID: " + result["draft_id"]
        response = self._pack("completed", text, external_done, action=action, provider=provider)
        response["catalog_result"] = {
            key: result[key] for key in ("verified", "draft_id", "published", "tumblr_upload", "total", "kind")
            if key in result
        }
        return response

    @staticmethod
    def _catalog_payload_ok(payload):
        if not isinstance(payload, dict) or len(payload) > 8:
            return False
        for key, value in payload.items():
            if not isinstance(key, str) or len(key) > 40:
                return False
            if isinstance(value, str) and len(value) <= 2000:
                continue
            if isinstance(value, bool):
                continue
            if (isinstance(value, list) and len(value) <= 8
                    and all(isinstance(item, str) and 0 < len(item) <= 128 for item in value)):
                continue
            return False
        return True

    def _propose(self, action, target, cleaned, omitted, question, provider, ai, existing_buttons=None):
        if not target:
            text = "対象が不足しています。対象: 名前 の形で教えてください。実行していません。"
            return self._pack("unclear", text + omitted, False, recovery=True, provider=provider, action=action)
        engagement_preview=None
        if action.startswith("engagement."):
            adapter=self.bridge.adapters.get("engagement")
            if adapter is None:
                return self._pack("not_connected",
                    "Engagement操作の実接続がありません。実行していません。",
                    False,recovery=True,action=action,provider=provider)
            try:
                engagement_preview=adapter.preview(action,target)
            except (ValueError,OSError):
                return self._pack("failed",
                    "指定された保存済みの投稿・ブログを確認できません。実行していません。",
                    False,recovery=True,action=action,provider=provider)
        if action in ("tumblr.publish_draft","tumblr.edit_post",
                      "tumblr.delete_post"):
            tumblr=self.bridge.adapters.get("tumblr")
            publisher=getattr(tumblr,"publisher",None)
            if publisher is None:
                return self._pack(
                    "not_connected","Tumblr投稿は未連携です。本人のTumblr連携と投稿権限の承諾が必要です。投稿していません。",
                    False,recovery=True,action=action,provider=provider)
        if action=="telegram.publish_announcement":
            if "telegram" not in self.bridge.adapters:
                return self._pack("not_connected",
                    "Telegram投稿先との接続がありません。まだ送信していません。",
                    False,recovery=True,action=action,provider=provider)
            # CMなし/CM非表示 are owner control lines, never published body.
            body_source=re.sub(r"(?m)^\s*CM(?:なし|非表示)\s*$", "", cleaned or "")
            body=re.search(r"(?:^|\n)\s*本文\s*[:：]\s*(.+)\Z",
                           body_source,flags=re.DOTALL)
            if not body or not body.group(1).strip():
                return self._pack("unclear",
                    "告知する本文を入力してください。例：告知 対象: 投稿先\n本文: 告知内容",
                    False,recovery=True,action=action,provider=provider)
        if action in CONFIRM:
            request_id = uuid.uuid4().hex
            label = "AIの提案" if ai else "既存ツール索引の候補であり、AIの判断ではありません"
            text = label + "。プレビュー: " + action + " を " + target + " に対して行う操作です。まだ実行していません。"
            expected_digest=None
            preserved_buttons = []
            if action=="telegram.publish_announcement" and existing_buttons is not None:
                from tumblrkit.cm_links import validate_existing_url_buttons
                try:
                    preserved_buttons = validate_existing_url_buttons(existing_buttons)
                except ValueError:
                    return self._pack("invalid_tool",
                        "既存ボタンのURLまたは配置が不正です。投稿していません。",
                        False,recovery=True)
            cm_disabled = (action=="telegram.publish_announcement" and
                           re.search(r"(?m)^\s*CM(?:なし|非表示)\s*$", cleaned or "") is not None)
            if action=="telegram.publish_announcement":
                expected_digest=body.group(1).strip()
                text=("Telegram告知の投稿前確認。\n投稿先："+target+
                      "\n本文：\n"+expected_digest+"\n確認後、既存MPBで投稿します。")
                if preserved_buttons:
                    text += "\n既存のURLボタン（順序保持）：\n" + "\n".join(
                        " / ".join(button["text"] for button in row)
                        for row in preserved_buttons)
                cm_preview = getattr(self.bridge.adapters.get("telegram"), "preview_cm", None)
                if cm_disabled:
                    text += "\n掲載予定CM：なし（指定により非表示）"
                elif callable(cm_preview):
                    try:
                        cm_items = cm_preview(target, request_id)
                    except (ValueError, OSError, sqlite3.Error):
                        return self._pack("failed",
                            "CM台帳を確認できません。投稿前の確認を行っていません。",
                            False, recovery=True, action=action, provider=provider)
                    if cm_items:
                        text += "\n掲載予定CM（本文ではなくURLボタン）：\n" + "\n".join(
                            "PR / " + item["label"] for item in cm_items)
                    else:
                        text += "\n掲載予定CM：なし"

            if engagement_preview is not None:
                operation={"engagement.like":"いいね",
                           "engagement.reblog":"リブログ",
                           "engagement.follow":"フォロー"}[action]
                text=("Tumblr本人確認："+operation+"を1件だけ行います。\n"
                      "対象ブログ："+engagement_preview["blog"]+"\n"
                      "対象："+str(engagement_preview["title"])[:150]+"\n"
                      "確認を押すまでは実行しません。再実行を自動化しません。")
            if action=="tumblr.publish_draft":
                tumblr=self.bridge.adapters.get("tumblr")
                publisher=getattr(tumblr,"publisher",None)
                if publisher is not None:
                    try:
                        saved=publisher.drafts.get(target)
                    except (ValueError,OSError):
                        return self._pack("failed","保存済み下書きを確認できません。投稿していません。",
                                          False,recovery=True,action=action,provider=provider)
                    expected_digest=hashlib.sha256(saved["text"].encode("utf-8")).hexdigest()
                    text="Tumblr公開前確認。対象：本人のブログ\n投稿本文：\n"+saved["text"]+"\n確認ボタンを押すまで公開しません。"
            if action in ("tumblr.edit_post","tumblr.delete_post"):
                try:
                    intent=publisher.lifecycle.preview(action,target)
                except (ValueError,OSError):
                    return self._pack("failed",
                        "既存投稿の状態または修正案を確認できません。実行していません。",
                        False,recovery=True,action=action,provider=provider)
                expected_digest=intent["approval"]
                if action=="tumblr.edit_post":
                    text=("本人のTumblr公開投稿の編集前確認。投稿ID："+intent["post_id"]+
                          "\n現在の本文：\n"+intent["current_text"]+
                          "\n変更後の全文：\n"+intent["new_text"]+
                          "\n確認を押すまでTumblrには反映しません。")
                else:
                    text=("【取り消せない削除】本人のTumblr公開投稿を削除します。"+
                          "\n投稿ID："+intent["post_id"]+
                          "\n現在の本文：\n"+intent["current_text"]+
                          "\n削除後は元へ戻せません。確認まで実行しません。")
            self.pending[request_id] = (action, target, expected_digest, cm_disabled, preserved_buttons)
            return self._pack("confirmation_required", text + omitted + question, False, request_id=request_id, action=action, provider=provider)
        result = self.bridge.dispatch(ActionRequest(uuid.uuid4().hex, action, target))
        return self._from_bridge(result, provider)

    def _confirm(self, request_id):
        item = self.pending.pop(request_id or "", None)
        if isinstance(item, dict) and item.get("kind") == "catalog":
            return self._execute_owner(item["action"], item["payload"])
        if not item:
            return self._pack("failed", "確認できる提案がありません。実行していません。", False, recovery=True)
        action, target, expected_digest, cm_disabled, preserved_buttons = item
        if action=="tumblr.publish_draft":
            tumblr=self.bridge.adapters.get("tumblr")
            publisher=getattr(tumblr,"publisher",None)
            try:
                current=publisher.drafts.get(target) if publisher is not None else None
            except (ValueError,OSError):
                current=None
            if (current is None or expected_digest is None or
                    hashlib.sha256(current["text"].encode("utf-8")).hexdigest()!=expected_digest):
                self.pending.pop(request_id,None)
                return self._pack("failed",
                    "確認した本文が変更または削除されました。公開していません。"
                    "現在の下書きを表示して再確認してください。",
                    False,recovery=True,action=action,provider="confirmed")
        payload={"expected_digest":expected_digest} if action in (
            "tumblr.publish_draft","tumblr.edit_post","tumblr.delete_post") else {}
        if action=="telegram.publish_announcement":
            payload={"text":expected_digest}
            if preserved_buttons:
                payload["buttons"] = preserved_buttons
            if cm_disabled:
                payload["cm_disabled"] = True
        if action in ("tumblr.edit_post","tumblr.delete_post"):
            tumblr=self.bridge.adapters.get("tumblr")
            publisher=getattr(tumblr,"publisher",None)
            try:
                current=publisher.lifecycle.preview(action,target) if publisher else None
            except (ValueError,OSError):
                current=None
            if current is None or current.get("approval")!=expected_digest:
                self.pending.pop(request_id,None)
                return self._pack("failed",
                    "確認時から公開投稿または編集案が変わりました。"
                    "再度内容を表示して確認してください。Tumblrへは送信していません。",
                    False,recovery=True,action=action,provider="confirmed")
        result = self.bridge.dispatch(ActionRequest(request_id, action, target,
                                                    payload=payload,confirmed=True))
        self.pending.pop(request_id, None)
        return self._from_bridge(result, "confirmed", confirmed=True)

    def _from_bridge(self, result, provider, confirmed=False):
        if result.state == "completed":
            response = self._pack("completed", "取得結果を表示します。", True, action=result.action, provider=provider)
            response["view"] = self._result_view(result.action, result.detail)
            return response
        if result.state == "confirmation_required":
            return self._pack("confirmation_required", "まだ実行していません。", False, action=result.action, provider=provider)
        if result.state == "not_connected":
            lead = "確認は受け取りました。" if confirmed else "候補は見つかりました。"
            return self._pack("not_connected", lead + "接続先が無いため実行していません。", False, action=result.action, provider=provider, recovery=True)
        # Provider-error descriptions are fixed Japanese messages. Never
        # include arbitrary remote bodies, tokens or exception strings.
        notices={
            "tumblr_auth_expired":"Tumblrの認証が失効した可能性があります。再連携してください。",
            "tumblr_permission_denied":"Tumblrから権限不足またはアクセス拒否が返されました。操作を停止しました。",
            "tumblr_rate_limited":"Tumblr側の制限を検知したため停止しました。自動再試行はしません。",
            "oauth_not_connected":"Tumblrアカウントの認証情報がありません。連携してください。",
            "oauth_refresh_unavailable":"認証の更新に必要な情報がありません。再連携してください。",
            "oauth_token_permissions":"Tumblrの認証情報の保存権限が不適切です。安全のため停止しました。",
            "tumblr_unavailable":"Tumblrへの接続に失敗しました。取得済みデータは変更しません。",
            "tumblr_publish_draft_changed":"確認後に下書き本文が変わりました。公開せず、再確認を要求します。",
            "tumblr_publish_reconcile_required":"前回の投稿結果が不確定です。二重投稿防止のため再送せず、投稿状況の照合が必要です。",
            "tumblr_publish_uncertain":"投稿結果を確認できません。二重投稿防止のため自動再送しません。",
            "tumblr_publish_readback_unverified":"Tumblrの投稿読戻しが一致しません。実行済みと扱いません。",
            "tumblr_publish_rejected":"Tumblrが投稿を拒否しました。公開完了していません。",
            "tumblr_write_consent_required":"Tumblr投稿の権限が未承諾です。Tumblr連携から投稿権限を承諾してください。投稿していません。",
            "tumblr_post_approval_stale":"投稿内容または承認対象が変更されています。再確認が必要です。",
            "tumblr_post_external_changed":"Tumblr側の現在の本文と記録が一致しません。上書き・削除は行いません。",
            "tumblr_post_reconcile_required":"前回操作の結果が不確定です。二重操作を防ぐため再送しません。",
            "tumblr_post_reconcile_missing":"照合する未完了の操作はありません。新たな投稿は実行していません。",
            "tumblr_post_readback_unverified":"Tumblr側で編集・削除結果を確認できず、完了扱いにしません。",
            "tumblr_mutation_uncertain":"変更結果を確認できません。二重操作を防ぐため自動再送しません。",
            "tumblr_mutation_rejected":"Tumblrが変更操作を拒否しました。完了していません。",

            "engagement_target_missing":"保存済みの対象を確認できないため、Tumblrには送信していません。",
            "engagement_target_invalid":"選択された対象の形式が正しくありません。操作していません。",
            "engagement_provider_refused":"Tumblr公式APIが操作を拒否しました。反映済みとは扱いません。",
            "engagement_post_uncertain":"Tumblrへの操作結果が不明です。二重操作防止のため自動再送しません。",
            "engagement_readback_unverified":"Tumblr側の反映を読み戻せませんでした。再送せず、再照合が必要です。",
            "engagement_reconcile_required":"以前の操作結果が不明です。二重操作防止のため再実行しません。",
        }
        code=result.detail.get("error_code") if isinstance(result.detail,dict) else None
        message=notices.get(code,"操作は完了していません。実行していません。")
        return self._pack(result.state,message,False,action=result.action,provider=provider,recovery=True)

    @staticmethod
    def _result_view(action, detail):
        """Bounded, fixed-order display contract; verified adapter output only."""
        spec = {
            "tumblr.discover": ("検索結果", ("title", "type", "date", "thumbnail_url", "url")),
            "tumblr.list_posts": ("投稿一覧", ("title", "type", "date", "thumbnail_url", "url")),
            "analytics.get_summary": ("集計結果", ("label", "value", "period", "url")),
            "telegram.check_membership": ("参加状況", ("name", "status", "date", "url")),
            "tumblr.publish_draft": ("投稿結果", ("title", "status", "date", "url")),
            "tumblr.edit_post": ("編集結果", ("title", "status", "date", "url")),
            "tumblr.delete_post": ("削除結果", ("title", "status", "date", "url")),
            "tumblr.reconcile_post": ("投稿変更の照合結果", ("title", "status", "date", "url")),
            "engagement.like": ("いいね確認結果", ("name", "status", "date", "url")),
            "engagement.reblog": ("リブログ確認結果", ("name", "status", "date", "url")),
            "engagement.follow": ("フォロー確認結果", ("name", "status", "date", "url")),
            "telegram.publish_announcement": ("告知結果", ("title", "status", "date", "url")),
        }
        title, columns = spec.get(action, ("実行結果", ("name", "status", "date", "url")))
        rows = detail.get("rows", detail.get("items", []))
        if not isinstance(rows, list):
            rows = []
        safe_rows = []
        for row in rows[:100]:
            if not isinstance(row, dict):
                continue
            clean = {}
            for key in columns:
                value = row.get(key)
                if value is None or isinstance(value, bool):
                    clean[key] = ""
                elif isinstance(value, (str, int, float)):
                    clean[key] = str(value)[:500]
                else:
                    clean[key] = ""
            if action == "tumblr.discover":
                blog = row.get("blog")
                candidate = row.get("candidate_id")
                if isinstance(blog, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", blog):
                    clean["blog"] = blog
                if isinstance(candidate, str) and len(candidate) <= 100:
                    clean["candidate_id"] = candidate
            safe_rows.append(clean)
        result = {"kind": "records", "title": title, "action": action,
                  "status": "connected", "columns": list(columns), "rows": safe_rows,
                  "total": len(rows), "truncated": len(rows) > 100}
        if action=="tumblr.list_posts":
            offset=detail.get("page_offset")
            page_size=detail.get("page_size")
            verified_total=detail.get("total")
            if (type(offset) is int and offset>=0 and offset%20==0
                    and type(page_size) is int and 0<=page_size<=20):
                result["page_offset"]=offset
                result["page_size"]=page_size
                if type(verified_total) is int and verified_total>=0:
                    result["total"]=verified_total
                    result["total_verified"]=True
                else:
                    result["total"]=None
                    result["total_verified"]=False
                result["truncated"]=False
                for key in ("next_target","prev_target"):
                    value=detail.get(key)
                    if isinstance(value,str) and re.fullmatch(r"posts:(0|[1-9][0-9]{0,13})",value):
                        result[key]=value
                owner=detail.get("owner_blog")
                if isinstance(owner,str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}",owner):
                    result["owner_blog"]=owner
        if action == "tumblr.discover":
            stored_total=detail.get("stored_total")
            stored_offset=detail.get("stored_offset")
            if (type(stored_total) is int and stored_total>=0
                    and type(stored_offset) is int and stored_offset>=0):
                result["stored_total"]=stored_total
                result["stored_offset"]=stored_offset
                # Existing bounded view is a page, not the whole data set.
                result["total"]=stored_total
                result["truncated"]=False
            for key in ("stored_next_target","stored_prev_target"):
                link=detail.get(key)
                if isinstance(link,str) and len(link)<=150 and link.startswith("saved:"):
                    result[key]=link
            target = detail.get("next_target")
            if isinstance(target,str) and 0 < len(target) <= 100:
                result["next_target"] = target
                result["complete"] = detail.get("complete") is True
                result["from_cache"] = detail.get("from_cache") is True
            candidates=detail.get("related_tags")
            if (detail.get("related_basis")=="observed_cooccurring_tags"
                    and isinstance(candidates,list)):
                related=[]
                for candidate in candidates:
                    if not isinstance(candidate,dict):
                        continue
                    tag=candidate.get("tag")
                    count=candidate.get("count")
                    if (isinstance(tag,str) and 0<len(tag)<=64
                            and not any(char in tag for char in "/?#&=<>")
                            and isinstance(count,int) and not isinstance(count,bool)
                            and count>0):
                        related.append({"tag":tag,"count":count,"target":"tag:"+tag})
                result["related_tags"]=related
                result["related_basis"]="observed_cooccurring_tags"
        return result

    def _failure_text(self, code):
        if code in ("ai_disabled", "quota_unavailable"):
            return "AIの本番利用は無効です。無料枠の保護設定を確認してください。外部へ送信していません。"
        if code in ("quota_exhausted", "quota_cooldown"):
            return "無料枠保護のため、追加のAI呼出を停止しました。外部へ送信していません。"
        if code == "invalid_response":
            return "AIの応答形式を安全に解釈できませんでした。実行していません。"
        if code == "network_unavailable":
            return "AI Providerへの通信に失敗しました。実行していません。"
        if code == "rate_limited":
            return "AI Providerの回数制限です。実行していません。"
        if code == "model_forbidden":
            return "指定したモデルは停止済みか、このキーでは許可されていません。実行していません。"
        if code == "free_tier_exhausted":
            return "無料枠を超えたため停止しました。有料への自動切替はしていません。実行していません。"
        if code == "paid_model_forbidden":
            return "有料モデルは使っていません。実行していません。"
        return "AI Providerに届きませんでした。実行していません。"

    def _pack(self, state, text, executed, recovery=False, request_id=None, action=None, provider="none"):
        return {
            "state": state,
            "text": text,
            "executed": executed,
            "recovery": recovery or state in ("confirmation_required",),
            "request_id": request_id,
            "action": action,
            "provider": provider,
        }

