"""Assistant loop for the operation screen.

Proposals stay separate from execution. Publish and announcement actions need
an explicit confirmation. Missing providers, unknown tools and failed calls are
not reported as completed work. Secret-like text is removed before any provider
prompt. Paid fallback is not attempted.
"""
import json
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
)
SECRET = re.compile(r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]{8,}|api[_-]?key\s*[:=]\s*\S+|sk-[A-Za-z0-9]+|ghp_[A-Za-z0-9]+)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TELEGRAM = re.compile(r"(?i)\b(tg://\S+|@[A-Za-z0-9_]{5,})")
TARGET = re.compile(r"対象(?:[:：]\s*|\s+)([^\r\n]{1,64})")


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
    def __init__(self, bridge=None, providers=None, transport=None):
        self.bridge = bridge or Bridge({})
        self.providers = providers
        self.transport = transport
        self.pending = {}
        self.drafts = {}

    def _available(self):
        if self.providers is not None:
            return list(self.providers)
        return providers_from_env(os.environ, self.transport)

    def turn(self, message="", confirm=False, request_id=None, draft=False, abort=False):
        if abort:
            self.pending.clear()
            return self._pack("aborted", "中断しました。実行していません。", False)
        if draft:
            draft_id = uuid.uuid4().hex
            self.drafts[draft_id] = redact(message or "")
            return self._pack("draft_saved", "下書きとして残しました。外部へは送っていません。", False)
        if confirm:
            return self._confirm(request_id)
        text = (message or "").strip()
        if not text:
            return self._pack("unclear", "やりたいことを一文で書いてください。実行していません。", False, recovery=True)
        cleaned = redact(text)
        omitted = "秘密らしき文字列はAIへ送っていません。" if cleaned != text else ""
        local = match_index(cleaned)
        if local == ["unlisted"]:
            return self._pack("refused_unlisted", "その操作は登録された道具に無いため実行していません。" + omitted, False, recovery=True)
        try:
            plan, provider_name = self._ask(cleaned)
        except ProviderError as exc:
            return self._pack(exc.code, self._failure_text(exc.code) + omitted, False, recovery=True)
        if plan is None:
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
                return self._pack("guidance", answer, False,
                                  recovery=bool(question), provider=provider_name)
            return self._pack("unclear", "目的をもう少し詳しく教えてください。実行していません。" + omitted,
                              False, recovery=True, provider=provider_name)
        return self._propose(action, target, cleaned, omitted, question, provider_name, ai=True)

    def _ask(self, cleaned):
        providers = _order(cleaned, self._available())
        if not providers:
            return None, "none"
        prompt = (
            "Tumblr運用のAIアシスタントとして日本語で要望を理解し誘導する。"
            "JSONオブジェクトのみ返す。キーはaction,target,reply,question。"
            "操作しない相談・企画・説明はactionを空にしてreplyで案内する。"
            "不足情報はquestionで一つずつ確認する。接続・公開済みと推測で主張しない。"
            "実操作の提案は登録済アクションのみ。許可された操作: "
            + ",".join(sorted(ALLOWED)) + "。利用者の文: " + cleaned
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
        if action in CONFIRM:
            request_id = uuid.uuid4().hex
            self.pending[request_id] = (action, target)
            label = "AIの提案" if ai else "既存ツール索引の候補であり、AIの判断ではありません"
            text = label + "。プレビュー: " + action + " を " + target + " に対して行う操作です。まだ実行していません。"
            return self._pack("confirmation_required", text + omitted + question, False, request_id=request_id, action=action, provider=provider)
        result = self.bridge.dispatch(ActionRequest(uuid.uuid4().hex, action, target))
        return self._from_bridge(result, provider)

    def _confirm(self, request_id):
        item = self.pending.get(request_id or "")
        if not item:
            return self._pack("failed", "確認できる提案がありません。実行していません。", False, recovery=True)
        action, target = item
        result = self.bridge.dispatch(ActionRequest(request_id, action, target, confirmed=True))
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
        return self._pack(result.state, "操作は完了していません。実行していません。", False, action=result.action, provider=provider, recovery=True)

    @staticmethod
    def _result_view(action, detail):
        """Bounded, fixed-order display contract; verified adapter output only."""
        spec = {
            "tumblr.discover": ("検索結果", ("title", "type", "date", "thumbnail_url", "url")),
            "tumblr.list_posts": ("投稿一覧", ("title", "type", "date", "thumbnail_url", "url")),
            "analytics.get_summary": ("集計結果", ("label", "value", "period", "url")),
            "telegram.check_membership": ("参加状況", ("name", "status", "date", "url")),
            "tumblr.publish_draft": ("投稿結果", ("title", "status", "date", "url")),
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
        if action == "tumblr.discover":
            target = detail.get("next_target")
            if isinstance(target,str) and 0 < len(target) <= 100:
                result["next_target"] = target
                result["complete"] = detail.get("complete") is True
                result["from_cache"] = detail.get("from_cache") is True
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

