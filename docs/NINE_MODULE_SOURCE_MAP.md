# Tumblr 9-module source/contract map — 2026-11-19

Authority: TGtumbler current Notion decisions and original 79-action catalog, this repository README, existing GitHub PR #1; Telegram CM change unit remains issue #2. This map is a **source-index**, not a new design or acceptance record. Runtime and browser user acceptance are not implied.

| Module / original catalog | Owned source and runtime entry | Existing dependencies / persisted state | Verified source capability | Remaining design gaps / acceptance |
|---|---|---|---|---|
| account (6) | `account_runtime.py`, `oauth.py`; `appserve.py /oauth/*` | private OAuth token files, one recruitment blog | owner OAuth lifecycle and status tests | actual account authorization/refresh/readback and unlink UX |
| discovery (7) | `discovery.py`, `discovery_store.py`, `tumblr_read.py`; `/discovery/*` | official read-only Tumblr API, Discovery SQLite | bounded scan, candidate and diff store | real provider scope, following/tag coverage, browser acceptance |
| library (8) | `library.py`; `GET /library`, `POST /library/{update,selected,progress}`, `app/app.js showLibrary` | same Discovery DB and candidate IDs, no duplicate store | flags, filters, selected view and resume | **library.07 search missing**; server filtered search and client wiring required |
| media (7) | `media_save.py`, `media_extract.py`; `/media/*` | selected Discovery candidate, existing persistent app media root | bounded image/MP4 save, rights check, atomic result | real third-party provider rights, live codecs/download/readback |
| engagement (7) | `engagement_runtime.py`; Bridge `engagement.*` | owner OAuth, rate budget, Discovery candidate | verified like/reblog/follow source | undo/history and actual provider user acceptance |
| publisher (12) | `drafts.py`, `post_scheduler.py`, `publisher_runtime.py`, `post_lifecycle.py`; `/drafts`, `/schedules` | existing app drafts SQLite, official Tumblr writer | previews, direct publish, schedule, edit/delete/reconcile | media/tag authoring, provider operation E2E, browser acceptance |
| campaign (12) | `campaign_runtime.py`; `POST /campaigns` | existing core SQLite, GateEligibility through existing Telegram owner Bridge | contest, draw, one-time claim tickets, outbox/reconcile | real entrant user flow, authenticated member binding and DM provider |
| referral (9) | `referral_runtime.py`; `POST /referrals` | existing core SQLite and GateEligibility, no second GATE | codes, revoke, gate-confirmed report | real public GATE attribution URL and user-visible incentive workflow |
| analytics (11) | `analytics_runtime.py`, `follower_analytics.py`; `/analytics/*` | Discovery follower history, existing campaign/referral records | recorded follower time series and measured summaries | additional period comparisons, true source attribution, user report UX |

## Routes and change boundaries

- `tumblrkit/action_catalog.py`: original 79 IDs and stable Japanese names. `BRIDGE_ROUTES` dispatch only actual action adapters; `OWNER_UI_ROUTES` **opens an existing UI**, not proof that the named operation is completed.
- `assistant.py` → `bridge.py` → owned domain adapters → official Tumblr or `telegram_bridge_client.py` → existing GayBBS/GATE/MPB owner `artifacts/bot/src/services/tumblrBridgeServer.ts` (in `seigo-gace/GateApp-Bot`). Telegram approval, posting and receipt ledger remain owned by that repo.
- `appserve.py` authenticates owner access and routes HTTP; `app/app.js` shows six existing goals unchanged. Local Library state is in Discovery SQLite, Campaign/Referral in existing core SQLite; no new database, Bot, service or product UI.
- `tests/test_*{library,app_serve,action_catalog}*.py` (where named), GitHub Actions `.github/workflows/test.yml` guard Python tests, JS syntax and app release build. Offline tests do **not** prove external provider or userbot performance.

## Action-specific gap chosen for implementation

`library.07 — 検索する`: local Discovery-backed Library list currently supports only `filter`, `offset`, `limit`; neither UI nor HTTP API accepts a search term. Add bounded, parameterized search over the existing candidate blog/title/post ID and preserve all existing filters, selection, pagination, access controls, offline tests and status semantics. Do not scrape providers or create a separate search index. Keep the exact existing action ID and route it to Library.

No past source commit or CI success counts as Host deployment. Cross-repo provider changes, external claims and real Telegram posting must satisfy owner permissions and independent runtime admission.
