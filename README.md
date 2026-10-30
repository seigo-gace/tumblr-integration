# Tumblr Integration System

Tumblr運営・Telegram集客連携の独立リポジトリ。既存MPB / GATE / 告知・応募の責務を重複開発せず、API・イベント経由で接続します。

**Status:** initial development; no live Tumblr OAuth, Telegram integration, or deployment verified.

## Scope
- 9 modules: account, discovery, library, media, engagement, publishing, campaigns, referrals, analytics.
- Mobile chat: 6 visible objectives + free text; AI proposes purpose-grounded actions and NEXT; 79-action catalog is a tool index, not a rigid wizard.
- Gemini for writing/planning, Groq for simple commands, OpenRouter free-only fallback; no paid automatic fallback.
- One recruitment Tumblr account only; private account excluded.

Development rules: [G-ACE GPT CHAT Rule Plane](https://github.com/G-ACE-inc/server-core/issues/17). Work only on owned branches; no secrets or personal data in this public repository.
