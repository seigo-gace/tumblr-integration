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

## Owner login and local drafts (Source implemented, 2026-11-10)

The application now serves an unauthenticated **login form** at `/login` and returns a 401 login page for unauthenticated `/`. The already configured private `TUMBLR_APP_ACCESS_TOKEN` is used for sign-in. Successful browser sign-in creates a random **HttpOnly, Secure, SameSite=Strict** server-side session, expires after 12 hours; the owner token itself is never placed in a browser cookie or script. Authenticated browser writes require same-origin checks. An external/public Cloudflare Access policy is **not** enabled; the existing app-owned authentication remains the protection, not a verified identity/SSO integration.

The operation screen has **実物の下書き管理**: input text → `入力を保存` → `保存一覧を表示` → `編集する` / `削除`. Endpoints `GET/POST /drafts`, `DELETE /drafts/<id>` require the owner session or bearer credential. Drafts live in the app's own persistent SQLite at `<resolved-release-root>/data/drafts.sqlite3`; no AI, Tumblr or Telegram calls occur during draft operations. The app must have filesystem permissions on this location and the release root must persist across revisions. All draft input is length-checked and secret-like patterns are redacted, but **do not enter credentials or sensitive personal data in drafts**. Existing release rollback swaps static assets and does not delete drafts.

Important: GitHub PR #1 Source/CI changes require an explicit **existing Gserver owner app lifecycle release** to update the live `tumblr.hentaigay.org` process. Passing CI, `/healthz` for an older revision, or a GitHub commit is not runtime acceptance. Do not claim the new login/draft controls are live until the public HTTPS owner login, CRUD and revision readback are observed. All AI providers remain disabled by default and the free-token live calls have **not been tested**.

## Existing Gserver app live update (owner key required once)

A live process without a persistent owner token file cannot be safely restarted by copying secrets from process memory. A source commit is not a live deployment. The original `TUMBLR_APP_ACCESS_TOKEN` stored only in the old process is not extracted or logged. The **owner** performs a one-time WSL password choice with `scripts/tumblr_owner_setup.py`; the helper prompts twice using hidden input, creates a mode-0600 private file `~/.config/tgtumbler/tumblr-app-access-token` (private parent directory), then invokes `scripts/tumblr_release.py`. This is an owner-authorized replacement of the inaccessible old application login key; it must not be performed automatically in CI or by GPT CHAT. It does not change the three AI provider API keys.

`tumblr_release.py` requires a clean existing `feat/initial-modular-core` checkout and a fresh exact Source SHA, builds a separate `dist-staging` release, tests a temporary loopback service on 8082 for version, unauthenticated login, authenticated UI and draft list **without any inference calls**, then updates the existing versioned `dist` release and replaces only the one existing Tumblr operation app on 8081, with a rollback attempt if new live readback fails. It never manages unrelated Bots, DNS/Tunnel or Gserver core. Do not mark accepted until the HTTPS login and authenticated draft readback are independently observed.

The operation app starts with `TUMBLR_AI_LIVE_ENABLED=0` even if GitHub Actions Secrets exist. It remains a **partial product**, since external Tumblr OAuth and the three AI keys' safe Gserver injection still require separate completion. No CI test or bootstrap claims those APIs have run.
