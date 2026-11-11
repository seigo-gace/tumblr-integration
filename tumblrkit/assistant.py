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
import re
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tumblrkit.bridge import ALLOWED, CONFIRM, Bridge, ActionRequest

MODULES = (
    "account", "discovery", "library", "media", "engagement",
    "publishing", "campaigns", "referrals", "analytics",
)
INDEX = (
    ("tumblr.discover", ("検索", "見つけ", "発見")),
    ("tumblr.list_posts", ("一覧", "投稿を見")),
    ("tumblr.publish_draft", ("公開", "投稿して")),
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
    "投稿": ("tumblr.list_posts", "tumblr.publish_draft"),
    "キャンペーン": (),
    "紹介": (),
    "集計": ("analytics.get_summary",),
    "メディア": ("tumblr.discover",),
}



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
        if status == 429:
            raise ProviderError("rate_limited")
        if status in (402, 403) or (isinstance(payload, dict) and payload.get("error") == "quota"):
            raise ProviderError("free_tier_exhausted")
        if status != 200 or not isinstance(payload, dict):
            raise ProviderError("failed")
        try:
            plan = json.loads(_provider_text(self.name, payload))
            if not isinstance(plan, dict):
                raise ProviderError('invalid_response')
            return plan
        except (json.JSONDecodeError, TypeError, KeyError, IndexError, AttributeError):
            raise ProviderError('invalid_response') from None

    def _body(self, prompt):
        if self.name == "gemini":
            return {"contents": [{"parts": [{"text": prompt}]}]}
        model = self.model or ("llama-3.1-8b-instant" if self.name == "groq" else "openrouter/free")
        return {"model": model, "messages": [{"role": "user", "content": prompt}]}


def providers_from_env(env, transport=None):
    quota = DurableFreeQuota(env) if transport is None else None
    transport = transport or UrllibTransport()
    chosen = []
    if env.get("TUMBLR_GEMINI_API_KEY"):
        chosen.append(HttpProvider(
            "gemini",
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
            {"x-goog-api-key": env["TUMBLR_GEMINI_API_KEY"]},
            True,
            transport,
            quota=quota,
        ))
    if env.get("TUMBLR_GROQ_API_KEY"):
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


def _provider_text(name, payload):
    if name == "gemini":
        return payload["candidates"][0]["content"]["parts"][0]["text"]
    return payload["choices"][0]["message"]["content"]


class UrllibTransport:
    def post(self, url, headers, body):
        request = Request(url, data=body, headers={"content-type": "application/json", **headers}, method="POST")
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
    def __init__(self, bridge=None, providers=None, transport=None, draft_store=None):
        self.bridge = bridge or Bridge({})
        self.providers = providers
        self.transport = transport
        self.pending = {}
        # One durable Owner draft store; no second in-memory draft ledger.
        self.draft_store = draft_store

    def _available(self):
        if self.providers is not None:
            return list(self.providers)
        return providers_from_env(os.environ, self.transport)

    def turn(self, message="", confirm=False, request_id=None, draft=False, abort=False,
             goals=None, selected_action=None, selected_target=None):
        if abort:
            self.pending.clear()
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
            return self._confirm(request_id)
        if goals is None:
            goals=[]
        if (not isinstance(goals,list) or len(goals)>len(GOALS)
                or any(not isinstance(goal,str) or goal not in GOALS for goal in goals)
                or len(set(goals))!=len(goals)):
            return self._pack("invalid_goals", "目的の選択が不正です。実行していません。", False, recovery=True)
        if selected_action is not None:
            saved_discovery=(selected_action=="tumblr.discover" and
                             isinstance(selected_target,str) and
                             selected_target.startswith("saved:"))
            maximum=150 if saved_discovery else 64
            if (not isinstance(selected_action,str) or selected_action not in ALLOWED
                    or not isinstance(selected_target,str) or not 0<len(selected_target)<=maximum):
                return self._pack("invalid_tool","未登録の操作または不正な対象を拒否しました。実行していません。",False,recovery=True)
            return self._propose(selected_action,selected_target,"","", "", "owner_selected",ai=False)
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
            "JSONオブジェクトのみ返す。キーはaction,target,reply,question。"
            "操作しない相談・企画・説明はactionを空にしてreplyで案内する。"
            "不足情報はquestionで一つずつ確認する。接続・公開済みと推測で主張しない。"
            "実操作の提案は登録済アクションのみ。許可された操作: "
            + ",".join(sorted(ALLOWED))
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
            if fallback is None or exc.code in ("rate_limited", "free_tier_exhausted", "ai_disabled", "quota_exhausted", "quota_cooldown", "quota_unavailable"):
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
            if not candidates:
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
        return {"goals":list(goals),"recommendations":actions,
                "not_connected_goals":unavailable,
                "all_actions_count":len(actions),"evidence":"existing_bridge_registry",
                "executed":False}

    def _goal_guidance(self, goals):
        response=self._pack(
            "guidance",
            "目的を確認しました。実行する操作と対象を選んでください。"
            "候補は現在の接続契約に基づきます。未接続の目的を完了扱いにしません。",
            False,recovery=True)
        response["plan"]=self._goal_plan(goals)
        return response

    def _local(self, local, cleaned, omitted, question=""):
        if len(local) != 1:
            notice = "AI Providerは未接続です。意図を一つに決められませんでした。実行していません。"
            return self._pack("unclear", notice + omitted + question, False, recovery=True, provider="none")
        target = TARGET.search(cleaned)
        return self._propose(local[0], target.group(1) if target else "", cleaned, omitted, question, "none", ai=False)

    def _propose(self, action, target, cleaned, omitted, question, provider, ai):
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
        if action=="tumblr.publish_draft":
            tumblr=self.bridge.adapters.get("tumblr")
            publisher=getattr(tumblr,"publisher",None)
            if publisher is None:
                return self._pack(
                    "not_connected","Tumblr投稿は未連携です。本人のTumblr連携と投稿権限の承諾が必要です。投稿していません。",
                    False,recovery=True,action=action,provider=provider)
        if action in CONFIRM:
            request_id = uuid.uuid4().hex
            label = "AIの提案" if ai else "既存ツール索引の候補であり、AIの判断ではありません"
            text = label + "。プレビュー: " + action + " を " + target + " に対して行う操作です。まだ実行していません。"
            expected_digest=None
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
            self.pending[request_id] = (action, target, expected_digest)
            return self._pack("confirmation_required", text + omitted + question, False, request_id=request_id, action=action, provider=provider)
        result = self.bridge.dispatch(ActionRequest(uuid.uuid4().hex, action, target))
        return self._from_bridge(result, provider)

    def _confirm(self, request_id):
        item = self.pending.get(request_id or "")
        if not item:
            return self._pack("failed", "確認できる提案がありません。実行していません。", False, recovery=True)
        action, target, expected_digest = item
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
        payload={"expected_digest":expected_digest} if action=="tumblr.publish_draft" else {}
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
                for candidate in candidates[:8]:
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

