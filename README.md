# Tumblr Integration System

Tumblr運営・Telegram集客連携のモジュールRepository。TGtumbler親Projectの一部であり、新しい親Projectを設けません。既存MPB / GATE / 告知・応募の責務を重複開発せず、API・イベント経由で接続します。

**Status:** Source開発中。Tumblr実OAuth、Telegram実連携、Gserver配備、操作アプリの実ブラウザ受入は未検証。

## Scope
- 9 modules: account, discovery, library, media, engagement, publishing, campaigns, referrals, analytics.
- Mobile chat: 6 visible objectives + free text; AI proposes purpose-grounded actions and NEXT; 79-action catalog is a tool index, not a rigid wizard.
- Gemini for writing/planning, Groq for simple commands, OpenRouter free-only fallback; no paid automatic fallback.
- One recruitment Tumblr account only; private account excluded.

## Operation center (priority: Master 2026-11-10)

Source: `app/index.html`, `app/styles.css`, `app/app.js`, `tumblrkit/assistant.py`. The home screen is a conversation. Sending does not require a module button. The nine modules stay an internal toolbox, and the bridge's allowed actions are the tool index. The separate 79-action catalog and the six shortcut labels are not rendered here because their canonical display names are not in this repository. Recovery controls (retry, draft, abort, confirm) appear only after a proposal or a failure. Provider calls use injected `TUMBLR_GEMINI_API_KEY`, `TUMBLR_GROQ_API_KEY`, and free-only `TUMBLR_OPENROUTER_API_KEY`. With no key, the reply says the provider is absent and does not claim the action ran. Publish and announcement actions stay on preview until confirmation. No client-side credential storage.

Local UI inspection: build a stamped release, inject `TUMBLR_APP_ACCESS_TOKEN` (16+ characters, not stored in Git), then run `python -m tumblrkit.appserve serve`. The process binds loopback only. `/` requires that token; `/healthz` and `/version` return the release commit and do not return the screen. `python -m tumblrkit.appserve rollback` switches `dist/current` back to the previous release. This command does not change DNS, Tunnel, Access, or Secrets. Master approved the candidate `tumblr.hentaigay.org`; a public lookup found no record, and the record is not created until the existing Tunnel and Access target are read back. **Do not publish the UI unauthenticated**. Six shortcut labels stay off the home screen until their canonical names are in this repository. Nine module names remain internal and do not gate the chat.

Tests: `PYTHONPATH=. python -m unittest discover -s tests -v`, `node --check app/app.js`. A passing static test does not establish browser/user acceptance.

OAuth Source: `tumblrkit/oauth.py` is an isolated adapter requiring injected transport and deployment Secret. Secure state lifecycle/refresh and live account readback are incomplete. Master reports the required Secret is already obtained; do not ask for reissue or write it to Git.

Development rules: [G-ACE GPT CHAT Rule Plane](https://github.com/G-ACE-inc/server-core/issues/17). Work only on owned branches; no secrets or personal data in this public repository.

## Free AI quota guard (offline-source implementation)

GitHub Actions Secrets (`TUMBLR_GROQ_API_KEY`, `TUMBLR_GEMINI_API_KEY`, `TUMBLR_OPENROUTER_API_KEY`) are **not** automatically available to the Gserver process. Never echo, copy or inject them from CI without the existing Gserver private-Secret owner path and separate runtime authorization.

Live provider calls are **OFF by default**, even if keys are present. Before any real request, operations must explicitly set **both** `TUMBLR_AI_LIVE_ENABLED=1` and `TUMBLR_AI_QUOTA_DB` to an existing writable persistent SQLite-file location owned by the app (not in Git, not in a temporary/ephemeral release directory). Do not enable these during CI or quota tests. Every real request is reserved before HTTP and counted even on failure; a missing/unwritable database blocks calls.

Conservative fixed source-side limits: **5 attempts per provider per UTC day, 10 attempts total across three providers per UTC day, and minimum 60 seconds between requests across all providers**. Counters survive process restart and are shared by processes using the same SQLite file. Provider HTTP 429 / exhausted quota does not trigger fallback. Fallback to OpenRouter remains only when a primary provider fails for another reason and the quota guard permits it. OpenRouter remains free-model-only. These are additional local safety caps, **not proof of provider-side free-tier terms or current model availability**.

All `tests/test_free_ai_quota.py` checks are local mocks/SQLite and never call the three AI service APIs. Successful tests do not authorize Gserver live activation. No client-side API keys, paid escalation, or automatic load testing.
