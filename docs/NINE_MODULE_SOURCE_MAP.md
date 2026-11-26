# Tumblr 9-module source/contract map — 2026-11-19

Authority: TGtumbler current Notion decisions and original 79-action catalog, this repository README, existing GitHub PR #1; Telegram CM change unit remains issue #2. This map is a **source-index**, not a new design or acceptance record. Runtime and browser user acceptance are not implied.

| Module / original catalog | Owned source and runtime entry | Existing dependencies / persisted state | Verified source capability | Remaining design gaps / acceptance |
|---|---|---|---|---|
| account (6) | `account_runtime.py`, `oauth.py`; `appserve.py /oauth/*` | private OAuth token files, one recruitment blog | owner OAuth lifecycle and status tests | actual account authorization/refresh/readback and unlink UX |
| discovery (7) | `discovery.py`, `discovery_store.py`, `tumblr_read.py`; `/discovery/*` | official read-only Tumblr API, Discovery SQLite | bounded scan, candidate and diff store | real provider scope, following/tag coverage, browser acceptance |
| library (8) | `library.py`; `GET /library`, `POST /library/{update,selected,progress}`, `app/app.js showLibrary` | same Discovery DB and candidate IDs, no duplicate store | flags, filters, selected view and resume | library.07 search wired to existing Library query, panel, pagination and tests (Source; Runtime unverified) |
| media (7) | `media_save.py`, `media_extract.py`; `/media/*` | selected Discovery candidate, existing persistent app media root | bounded image/MP4 save, rights check, atomic result | media.05 retry reuses existing one-item rights-confirmed save; media.06 candidate-ID history search; media.07 capacity uses existing storage (Source; Runtime unverified); real third-party provider rights, live codecs/download/readback |
| engagement (7) | `engagement_runtime.py`; Bridge `engagement.*` | owner OAuth, rate budget, Discovery candidate | verified like/reblog/follow source | engagement.06 now reads owner-private saved receipt history (Source; Runtime unverified); undo and actual provider user acceptance remain |
| publisher (12) | `drafts.py`, `post_scheduler.py`, `publisher_runtime.py`, `post_lifecycle.py`; `/drafts`, `/schedules` | existing app drafts SQLite, official Tumblr writer | previews, direct publish, schedule, edit/delete/reconcile | publisher.06 exact saved-text preview is wired (Source; Runtime unverified); media/tag authoring, complete media/tag preview, provider operation E2E and browser acceptance remain |
| campaign (12) | `campaign_runtime.py`; `POST /campaigns` | existing core SQLite, GateEligibility through existing Telegram owner Bridge | contest, draw, one-time claim tickets, outbox/reconcile | real entrant user flow, authenticated member binding and DM provider |
| referral (9) | `referral_runtime.py`; `POST /referrals` | existing core SQLite and GateEligibility, no second GATE | codes, revoke, gate-confirmed report | referral.07 verified-tier progress now provided from existing grants (Source; Runtime unverified); real public GATE attribution URL and user-visible incentive workflow |
| analytics (11) | `analytics_runtime.py`, `follower_analytics.py`; `/analytics/*` | Discovery follower history, existing campaign/referral records | recorded follower time series and measured summaries | analytics.04 compares disjoint observed periods and analytics.11 exports measured CSV (Source; Runtime unverified); analytics.06 now reports saved post note snapshots separately from own engagement receipts (Source; Runtime unverified); true source attribution and full report UX still missing |

## Module resume records

<!-- module-resume-catalog-sha256: 1aaac59e7260a9f620b4ce82f30c8e83ec9dbcd5a6a743939231d760018f0994 -->

Current source-routing baseline: PR #1 branch `feat/initial-modular-core`, catalog reconciled from `0e1217a0` on 2026-11-27. This section is the existing module map's **resume entry**, not a second issue tracker or proof of live acceptance. Read the selected module row and its actual code/tests, use the original 79-operation scope, then implement/verify a demonstrated gap. Do not stop after repeating this table. History below records already-landed Source work and is not the next task.

| Module | No catalog route IDs | Source files | Existing tests | Next implementation/verification unit | Acceptance still open |
|---|---|---|---|---|---|
| account | — | `tumblrkit/account_runtime.py`; `tumblrkit/oauth.py`; `tumblrkit/appserve.py` | `tests/test_account_runtime.py`; `tests/test_oauth.py`; `tests/test_app_serve.py` | Trace account.01–04 from catalog through existing OAuth UI/API tests; fix a demonstrated mismatch rather than rebuilding OAuth or account.05/06. | Actual owner connection, refresh and browser unlink; source routes alone are not acceptance. |
| discovery | — | `tumblrkit/discovery.py`; `tumblrkit/discovery_store.py`; `tumblrkit/tumblr_read.py` | `tests/test_discovery.py`; `tests/test_discovery_engine.py` | Trace discovery.07 diff delivery in the existing store/API/UI; preserve current source checkpoint and exclusion semantics, add missing integration coverage where demonstrated. | Provider scope and end-to-end handoff to Library; do not repeat live scans just to inspect the source. |
| library | — | `tumblrkit/library.py`; `app/app.js` | `tests/test_library.py`; `tests/test_app_serve.py` | Verify library.01–08 against existing state/filter/selection/progress paths; library.07 search is already Source-wired and is not a new implementation task. | Actual browser viewing/state/resume acceptance and cross-module handoff; provider media rights are not inferred. |
| media | — | `tumblrkit/media_save.py`; `tumblrkit/media_extract.py` | `tests/test_media_save.py`; `tests/test_media_fallback.py`; `tests/test_media_images.py` | Check media.01–04 source-to-screen integration against existing save/history tests; reuse media.05–07 already-wired history/capacity/retry operations. | Local codec/readback, provider availability/rights and actual browser workflow separately remain open. |
| engagement | `engagement.02`; `engagement.07` | `tumblrkit/engagement_runtime.py`; `tumblrkit/drafts.py`; `tumblrkit/action_catalog.py` | `tests/test_engagement_runtime.py`; `tests/test_drafts.py`; `tests/test_action_catalog.py` | First inspect engagement.02 reblog-draft support versus the existing draft and reblog paths, then wire/implement the missing adopted operation and its tests; do not rebuild verified like/reblog/follow. | Both missing catalog routes, actual provider undo semantics and browser acceptance; no publish is authorized by drafting. |
| publisher | `publisher.01`; `publisher.02`; `publisher.03`; `publisher.04`; `publisher.05` | `tumblrkit/drafts.py`; `tumblrkit/assistant.py`; `tumblrkit/publisher_runtime.py`; `tumblrkit/post_scheduler.py` | `tests/test_drafts.py`; `tests/test_assistant.py`; `tests/test_publisher_runtime.py`; `tests/test_post_scheduler.py` | Start publisher.01 by matching the existing Assistant text-generation and draft APIs to the original catalog, then implement the missing connection/tests; keep draft locking and publisher.06 text preview. | Five missing authoring routes, complete media/tag preview, live publication/scheduling/edit/reconcile and browser acceptance. |
| campaign | `campaign.02`; `campaign.08` | `tumblrkit/campaign_runtime.py`; `tumblrkit/assistant.py` | `tests/test_campaign_runtime.py`; `tests/test_gate_campaign_referral.py` | Start campaign.02 text authoring with existing Assistant/draft reuse; then handle campaign.08 actual DM transport through its owner. campaign.03/05/09 are already Source-wired. | Real entrants/GATE and DM delivery, lifecycle and browser acceptance; a source/mock PASS is insufficient. |
| referral | `referral.02`; `referral.03`; `referral.04` | `tumblrkit/referral_runtime.py`; `tumblrkit/assistant.py` | `tests/test_referral_runtime.py`; `tests/test_gate_campaign_referral.py` | Start referral.02 share-text authoring through existing referral/Assistant flows, then connect sharing/campaign settings to original routes; preserve referral.06 locked terms and .07 tier reports. | Public GATE attribution and actual user workflow, three missing catalog routes and browser acceptance. |
| analytics | `analytics.09`; `analytics.10` | `tumblrkit/analytics_runtime.py`; `tumblrkit/follower_analytics.py` | `tests/test_analytics_runtime.py`; `tests/test_follower_analytics.py` | Trace recorded traffic provenance for analytics.09/10 and implement missing classification/report wiring with measured events only; do not recreate .04 comparison, .06 saved-note report or .11 CSV export. | Real traffic-source binding and unclassified traffic handling; do not invent conversions/impressions or treat reports as live acceptance. |

No module is marked complete. Modules with zero missing catalog routes still require operation-level Source review and Runtime/browser evidence. If the caller has not selected a module, `engagement.02` is the first catalog-unconnected candidate in original order, **not a proven absence of reusable code**; inspect the listed files before editing. A blocked external operation does not stop independent original source work.

Optional read-only helper: `python3 -B scripts/tumblr_development_resume.py --module engagement`. Without `--module`, it returns all nine records and current catalog-derived counts. It statically parses the catalog without importing/executing the application, validates every coverage row, totals, catalog fingerprint and owned source/test path, and never calls providers or grants execution permission. Normal GPT with only GitHub tools can read this section directly; CLI availability is not a new prerequisite.

## Implemented Source history: campaign.05 audited candidate verification

- `campaign.05 — 重複・無効候補を見る` routes to the **existing** Campaign Contest + GateEligibility in the owner-only `/campaigns` API and six-goal campaign panel. `audit_entries(campaign_id,limit=20)` checks only up to 20 already persisted entries, read-only, and never deletes a candidate or re-rolls the draw.
- Current duplicate rows are impossible under `PRIMARY KEY(campaign_id,applicant_id)`; historical rejected duplicate attempts were **not recorded**, so the returned `duplicate_attempts` is `not_recorded`, not a guessed count.
- A current GATE recheck returns `verified`, `not_verified` or `check_unavailable`; a failed check is not automatically a new exclusion and previous entry acceptance remains historical. All external rechecks require the same already-authorized existing Telegram owner bridge. Unit and authenticated HTTP contract tests are offline; no live GATE verification is claimed.
- This fills the original `campaign.05` UI lookup and audit action, **not** `campaign.08` real DM delivery or a public entrant/claim flow.

## Campaign.09 read-only expiry verification

- Original `campaign.09 — 期限切れを確認` now uses `CampaignContest.expiry_preview` from the existing contest/entry tables, served through owner-only `POST /campaigns` action `expiries`. It returns due unclaimed winners, waiting entries and previously expired entries, without changing records or granting a prize.
- The separate existing `promote` action remains a mutation and still requires its normal owner interface; no automatic promotion or DM delivery occurs from preview.
- `campaign.05` and `campaign.09` are source/test integrated; provider GATE/Telegram runtime and actual browser acceptance remain NOT_VERIFIED.

## 2026-11-19 continued source mapping

- `campaign.03`: `CampaignContest.configure_entry_terms` edits only existing deadline/winner count/claim seconds/GATE topic in the existing owner DB, only while contest is open and before the first applicant; preserves committed random seed. Owner API `POST /campaigns configure`; current campaign action screen; separate tests for immutable terms after first entry.
- `referral.06`: `ReferralManager.set_tier` uses existing `tiers` table, validates member threshold and reward, locks the terms after the first referral code, verified member, or grant; owner API `POST /referrals set_tier` and existing Referral panel. No new GATE or reward dispatcher.
- `account.05`: existing private `/oauth/status` displays sanitized saved settings only. No actual provider refresh or OAuth claims.
- `discovery.06`: existing `DiscoveryStore.exclude_blog` now preserves state `unavailable`. New `include_blog` reverses blog block but does not set old excluded posts to visible; a fresh official scan must do so. Existing authorized API + chat catalog provide blocked-list and explicit re-inclusion; no unsolicited Tumblr call.
- All listed changes require CI confirmation and separate Host and user acceptance. UI routing is not proof of outside-provider completion.

## Account.06 explicit local disconnect

- `OwnerOAuth.disconnect("disconnect_local_tumblr")` requires exact owner confirmation, unlinks only app-owned private `account-tokens.json` and `access-token`, clears pending OAuth states and increments an in-memory generation. A callback that was already exchanging credentials must check that generation before any token persistence and is rejected if disconnect happened meanwhile.
- `POST /oauth/disconnect` reuses the current owner session/Bearer and same-origin guard; six-goal screen and original account catalog surface a confirmation prompt. A password reset, Tumblr remote token revoke, new OAuth app, and independent provider restart are **not** performed.
- Owner source/test verification is not proof that a real Tumblr authorization was revoked at the provider or that any live browser was exercised.

## Routes and change boundaries

- `tumblrkit/action_catalog.py`: original 79 IDs and stable Japanese names. `BRIDGE_ROUTES` dispatch only actual action adapters; `OWNER_UI_ROUTES` **opens an existing UI**, not proof that the named operation is completed.
- `assistant.py` → `bridge.py` → owned domain adapters → official Tumblr or `telegram_bridge_client.py` → existing GayBBS/GATE/MPB owner `artifacts/bot/src/services/tumblrBridgeServer.ts` (in `seigo-gace/GateApp-Bot`). Telegram approval, posting and receipt ledger remain owned by that repo.
- `appserve.py` authenticates owner access and routes HTTP; `app/app.js` shows six existing goals unchanged. Local Library state is in Discovery SQLite, Campaign/Referral in existing core SQLite; no new database, Bot, service or product UI.
- `tests/test_*{library,app_serve,action_catalog}*.py` (where named), GitHub Actions `.github/workflows/test.yml` guard Python tests, JS syntax and app release build. Offline tests do **not** prove external provider or userbot performance.

## Implemented Source history: library.07 search

`library.07 — 検索する`: the current branch already contains bounded parameterized local search over candidate blog/title/post ID, wired to the Library query and panel with tests. Preserve filters, selection, pagination, access controls and offline status semantics. This is completed Source work to reuse, not an instruction to implement it again; CI/Runtime acceptance remains separately evidenced. No provider scraping or second search index is implied.

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

- `publisher.06`: `DraftStore.preview(id)` reuses private saved draft row and sha256 digest; authenticated `GET /drafts/{id}/preview` and an original draft-card control show exact saved text without Tumblr requests. Distinct provider-confirmed publish still requires separate approval and readback; attachment/tag-complete authoring preview remains a gap.

- `analytics.06`: `AnalyticsAdapter.post_reactions(candidate_id)` joins one verified Discovery candidate with latest locally observed mixed Tumblr notes and the app's own verified engagements. Authenticated `GET /analytics/post` and the existing 集計 panel provide the input. No provider-side likes-only total, reach or freshness beyond the saved observation is asserted.
