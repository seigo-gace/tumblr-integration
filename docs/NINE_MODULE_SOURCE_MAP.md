# Tumblr 9-module source/contract map — 2026-11-19

Authority: TGtumbler current Notion decisions and original 79-action catalog, this repository README, existing GitHub PR #1; Telegram CM change unit remains issue #2. This map is a **source-index**, not a new design or acceptance record. Runtime and browser user acceptance are not implied.

| Module / original catalog | Owned source and runtime entry | Existing dependencies / persisted state | Verified source capability | Remaining design gaps / acceptance |
|---|---|---|---|---|
| account (6) | `account_runtime.py`, `oauth.py`; `appserve.py /oauth/*` | private OAuth token files, one recruitment blog | owner OAuth lifecycle and status tests | actual account authorization/refresh/readback and unlink UX |
| discovery (7) | `discovery.py`, `discovery_store.py`, `tumblr_read.py`; `/discovery/*` | official read-only Tumblr API, Discovery SQLite | bounded scan, candidate and diff store | real provider scope, following/tag coverage, browser acceptance |
| library (8) | `library.py`; `GET /library`, `POST /library/{update,selected,progress}`, `app/app.js showLibrary` | same Discovery DB and candidate IDs, no duplicate store | flags, filters, selected view and resume | library.07 search wired to existing Library query, panel, pagination and tests (Source; Runtime unverified) |
| media (7) | `media_save.py`, `media_extract.py`; `/media/*` | selected Discovery candidate, existing persistent app media root | bounded image/MP4 save, rights check, atomic result | media.05 retry reuses existing one-item rights-confirmed save; media.06 candidate-ID history search; media.07 capacity uses existing storage (Source; Runtime unverified); real third-party provider rights, live codecs/download/readback |
| engagement (7) | `engagement_runtime.py`; Bridge `engagement.*` | owner OAuth, rate budget, Discovery candidate | verified like/reblog/follow source | engagement.06 now reads owner-private saved receipt history (Source; Runtime unverified); undo and actual provider user acceptance remain |
| publisher (12) | `drafts.py`, `post_scheduler.py`, `publisher_runtime.py`, `post_lifecycle.py`; `/drafts`, `/schedules` | existing app drafts SQLite, official Tumblr writer | previews, direct publish, schedule, edit/delete/reconcile | media/tag authoring, provider operation E2E, browser acceptance |
| campaign (12) | `campaign_runtime.py`; `POST /campaigns` | existing core SQLite, GateEligibility through existing Telegram owner Bridge | contest, draw, one-time claim tickets, outbox/reconcile | real entrant user flow, authenticated member binding and DM provider |
| referral (9) | `referral_runtime.py`; `POST /referrals` | existing core SQLite and GateEligibility, no second GATE | codes, revoke, gate-confirmed report | referral.07 verified-tier progress now provided from existing grants (Source; Runtime unverified); real public GATE attribution URL and user-visible incentive workflow |
| analytics (11) | `analytics_runtime.py`, `follower_analytics.py`; `/analytics/*` | Discovery follower history, existing campaign/referral records | recorded follower time series and measured summaries | analytics.04 compares disjoint observed periods and analytics.11 exports measured CSV (Source; Runtime unverified); true source attribution and full report UX still missing |

## Routes and change boundaries

- `tumblrkit/action_catalog.py`: original 79 IDs and stable Japanese names. `BRIDGE_ROUTES` dispatch only actual action adapters; `OWNER_UI_ROUTES` **opens an existing UI**, not proof that the named operation is completed.
- `assistant.py` → `bridge.py` → owned domain adapters → official Tumblr or `telegram_bridge_client.py` → existing GayBBS/GATE/MPB owner `artifacts/bot/src/services/tumblrBridgeServer.ts` (in `seigo-gace/GateApp-Bot`). Telegram approval, posting and receipt ledger remain owned by that repo.
- `appserve.py` authenticates owner access and routes HTTP; `app/app.js` shows six existing goals unchanged. Local Library state is in Discovery SQLite, Campaign/Referral in existing core SQLite; no new database, Bot, service or product UI.
- `tests/test_*{library,app_serve,action_catalog}*.py` (where named), GitHub Actions `.github/workflows/test.yml` guard Python tests, JS syntax and app release build. Offline tests do **not** prove external provider or userbot performance.

## Action-specific gap chosen for implementation

`library.07 — 検索する`: initial gap was the absence of a query parameter and search control; the current branch now contains a bounded local query and existing panel control (CI/Runtime evidence tracked separately). Add bounded, parameterized search over the existing candidate blog/title/post ID and preserve all existing filters, selection, pagination, access controls, offline tests and status semantics. Do not scrape providers or create a separate search index. Keep the exact existing action ID and route it to Library.

No past source commit or CI success counts as Host deployment. Cross-repo provider changes, external claims and real Telegram posting must satisfy owner permissions and independent runtime admission.

## Source-delta verification and unresolved boundaries

- `library.07`: `Library.list(query)`, owner-authenticated `GET /library?query=`, original Library panel search. No duplicate search store.
- `analytics.04`: `AnalyticsAdapter.compare()`, owner-authenticated `GET /analytics/compare?period=today|week|month`, existing 集計 panel, matched and disjoint locally observed candidate/verified engagement windows. External impressions or conversion are not inferred.
- `media.05`: failed single-item save can be retried only after a fresh rights/Terms confirmation; existing `MediaSave.save()` contains the prior-job deduplication and cleanup.
- `media.06`: retained MediaSave `list(candidate_id=...)` capability exposed in original history screen and owner-authenticated endpoint; no second index.
- `media.07`: `MediaSave.capacity()` reads current filesystem free bytes and completed-job ledger; `GET /media/capacity` reuses owner authentication.

All these entries are Source modifications. Actual Host release, external Tumblr/Telegram results, rights and mobile browser acceptance still require separate evidence.

- `referral.07`: existing Referral report now reads stored `tiers`, `memberships` and `grants` and returns each threshold's verified-member remainder and actual grant receipt; referrer codes and public GATE link attribution remain separate pending work.

- `analytics.11`: `AnalyticsAdapter.report_csv()` exports owner-private period summaries as UTF-8 CSV with measured rows and an explicit unmeasured line; `GET /analytics/report` requires the existing owner session, and the 集計 panel provides the download link. It does not query new providers.

- `engagement.06`: `EngagementAdapter.history()` reads bounded, timestamp-ordered stored states `reserved/sent/verified`, with verified provider reference only when confirmed; `GET /engagement/history` and an existing 巡回 panel button provide owner-only access and pagination. No provider mutations during reading.
