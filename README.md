# Tumblr Integration System

Tumblr運営・Telegram集客連携のモジュールRepository。TGtumbler親Projectの一部であり、新しい親Projectを設けません。既存MPB / GATE / 告知・応募の責務を重複開発せず、API・イベント経由で接続します。

**Status:** Source開発中。Tumblr実OAuth、Telegram実連携、Gserver配備、操作アプリの実ブラウザ受入は未検証。

## Scope
- 9 modules: account, discovery, library, media, engagement, publishing, campaigns, referrals, analytics.
- Mobile chat: 6 visible objectives + free text; AI proposes purpose-grounded actions and NEXT; 79-action catalog is a tool index, not a rigid wizard.
- Gemini for writing/planning, Groq for simple commands, OpenRouter free-only fallback; no paid automatic fallback.
- One recruitment Tumblr account only; private account excluded.

## Operation center (priority: Master 2026-11-10)

Source: `app/index.html`, `app/styles.css`, `app/app.js`. The static entry screen supports mobile/desktop resizing, nine existing module navigation targets and an instruction field. This is **a non-sending UI shell**, not a connected chatbot or operational Tumblr client. Commands are visibly marked unconnected and have no provider side effects. No client-side credentials, local secret storage, requests, or direct provider write route.

Local UI inspection: build a stamped release, inject `TUMBLR_APP_ACCESS_TOKEN` (16+ characters, not stored in Git), then run `python -m tumblrkit.appserve serve`. The process binds loopback only. `/` requires that token; `/healthz` and `/version` return the release commit and do not return the screen. `python -m tumblrkit.appserve rollback` switches `dist/current` back to the previous release. This does not change DNS, Tunnel, Access, or Secrets. The live hentaigay.org application hostname remains `DOMAIN_OWNER_UNVERIFIED` until the private Gserver domain map names it. **Do not publish the UI unauthenticated**. The final adopted six visible objective labels/layout, AI planner, human confirmation and execution receipts need to be reconciled from the existing design before acceptance. Nine module categories do not silently replace the six objective shortcuts.

Tests: `PYTHONPATH=. python -m unittest discover -s tests -v`, `node --check app/app.js`. A passing static test does not establish browser/user acceptance.

OAuth Source: `tumblrkit/oauth.py` is an isolated adapter requiring injected transport and deployment Secret. Secure state lifecycle/refresh and live account readback are incomplete. Master reports the required Secret is already obtained; do not ask for reissue or write it to Git.

Development rules: [G-ACE GPT CHAT Rule Plane](https://github.com/G-ACE-inc/server-core/issues/17). Work only on owned branches; no secrets or personal data in this public repository.
