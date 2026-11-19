# Tumblr 79-action existing-implementation coverage

Generated from the **actual** `tumblrkit/action_catalog.py` on `feat/initial-modular-core`, 2026-11-19. Names/IDs match the original `tumblr-chat-ux-catalog.zip`; no changes to six user-visible goals or 9 domain boundaries. This is a **routing inventory, not a completion score**. `UI navigation` only opens a related owner screen and may not complete the action; `Bridge action` only forwards an allowed action and may be unconfigured. All external Runtime and user acceptance remain **NOT VERIFIED**.

| ID | Original operation | Current source route | Evidence class |
|---|---|---|---|
| `account.01` | 接続状態を見る | UI `oauth_status` | Navigation only; individual action unverified |
| `account.02` | 接続を開始 | UI `oauth_connect` | Navigation only; individual action unverified |
| `account.03` | 再認証する | UI `oauth_connect` | Navigation only; individual action unverified |
| `account.04` | 操作権限を見る | UI `oauth_status` | Navigation only; individual action unverified |
| `account.05` | 設定を確認する | UI `oauth_settings`, read-only existing `/oauth/status` | Source only; runtime verification pending |
| `account.06` | 接続を解除する | No owner route | Not connected |
| `discovery.01` | 新着を巡回 | UI `discovery` | Navigation only; individual action unverified |
| `discovery.02` | フォロワーを確認 | Bridge `tumblr.discover` | Bridge adapter contract only |
| `discovery.03` | フォロー先を巡回 | Bridge `tumblr.discover` | Bridge adapter contract only |
| `discovery.04` | 新しいブログを探す | No owner route | Not connected |
| `discovery.05` | タグから探す | Bridge `tumblr.discover` | Bridge adapter contract only |
| `discovery.06` | 対象を登録・解除 | No owner route | Not connected |
| `discovery.07` | 差分を見る | UI `discovery` | Navigation only; individual action unverified |
| `library.01` | 投稿一覧を見る | UI `library` | Navigation only; individual action unverified |
| `library.02` | 画像・動画を開く | UI `library` | Navigation only; individual action unverified |
| `library.03` | 既読にする | UI `library` | Navigation only; individual action unverified |
| `library.04` | お気に入りに登録 | UI `library` | Navigation only; individual action unverified |
| `library.05` | あとで見るに登録 | UI `library` | Navigation only; individual action unverified |
| `library.06` | 前回の続きから見る | UI `library` | Navigation only; individual action unverified |
| `library.07` | 検索する | UI `library` | UI + owned operation (Source; CI pending/readback) |
| `library.08` | 登録を解除 | UI `library` | Navigation only; individual action unverified |
| `media.01` | 保存候補を見る | UI `library` | Navigation only; individual action unverified |
| `media.02` | 動画をMP4で保存 | UI `library` | Navigation only; individual action unverified |
| `media.03` | 画像を保存 | UI `library` | Navigation only; individual action unverified |
| `media.04` | 保存状況を見る | UI `media_history` | Navigation only; individual action unverified |
| `media.05` | 保存失敗を再試行 | UI `media_history` | UI + owned operation (Source; CI pending/readback) |
| `media.06` | 保存済みを探す | UI `media_history` | UI + owned operation (Source; CI pending/readback) |
| `media.07` | 容量を確認 | UI `media_history` | UI + owned operation (Source; CI pending/readback) |
| `engagement.01` | スキを付ける | Bridge `engagement.like` | Bridge adapter contract only |
| `engagement.02` | リブログ下書きを作る | No owner route | Not connected |
| `engagement.03` | リブログする | Bridge `engagement.reblog` | Bridge adapter contract only |
| `engagement.04` | フォロー候補を見る | No owner route | Not connected |
| `engagement.05` | フォローする | Bridge `engagement.follow` | Bridge adapter contract only |
| `engagement.06` | 交流履歴を見る | UI `engagement_history` | UI + owned operation (Source; CI pending/readback) |
| `engagement.07` | 操作を取り消す | No owner route | Not connected |
| `publisher.01` | 投稿文をゼロから作る | No owner route | Not connected |
| `publisher.02` | 元文を手直しする | No owner route | Not connected |
| `publisher.03` | 過去文から作り直す | No owner route | Not connected |
| `publisher.04` | 画像・動画を添付 | No owner route | Not connected |
| `publisher.05` | タグを提案 | No owner route | Not connected |
| `publisher.06` | プレビューする | UI `drafts` | UI + owned operation (Source; Runtime unverified) |
| `publisher.07` | 今すぐ投稿 | Bridge `tumblr.publish_draft` | Bridge adapter contract only |
| `publisher.08` | 予約投稿 | UI `drafts` | Navigation only; individual action unverified |
| `publisher.09` | 予約を変更 | UI `drafts` | Navigation only; individual action unverified |
| `publisher.10` | 投稿を編集 | Bridge `tumblr.edit_post` | Bridge adapter contract only |
| `publisher.11` | 投稿を削除 | Bridge `tumblr.delete_post` | Bridge adapter contract only |
| `publisher.12` | 投稿結果を見る | Bridge `tumblr.reconcile_post` | Bridge adapter contract only |
| `campaign.01` | 新規企画を作る | UI `campaign` | Navigation only; individual action unverified |
| `campaign.02` | 企画文を作る | No owner route | Not connected |
| `campaign.03` | 応募条件を設定 | UI `campaign`, `POST /campaigns` configure (pre-entry only) | Source only; runtime verification pending |
| `campaign.04` | 応募状況を見る | UI `campaign` | Navigation only; individual action unverified |
| `campaign.05` | 重複・無効候補を見る | UI `campaign`, `POST /campaigns` audit (bounded GATE recheck) | Source only; runtime verification pending |
| `campaign.06` | 応募締切を変更 | UI `campaign` | Navigation only; individual action unverified |
| `campaign.07` | 抽選を実行 | UI `campaign` | Navigation only; individual action unverified |
| `campaign.08` | 当選者へ通知 | No owner route | Not connected |
| `campaign.09` | 期限切れを確認 | UI `campaign`, `POST /campaigns` expiries (read-only preview) | Source only; runtime verification pending |
| `campaign.10` | 繰り上げを行う | UI `campaign` | Navigation only; individual action unverified |
| `campaign.11` | 企画を終了 | UI `campaign` | Navigation only; individual action unverified |
| `campaign.12` | 企画結果を見る | UI `campaign` | Navigation only; individual action unverified |
| `referral.01` | 紹介リンクを発行 | UI `referral` | Navigation only; individual action unverified |
| `referral.02` | ワンタッチ共有文を作る | No owner route | Not connected |
| `referral.03` | リンクを共有 | No owner route | Not connected |
| `referral.04` | 紹介キャンペーンを設定 | No owner route | Not connected |
| `referral.05` | 紹介成立を確認 | UI `referral` | Navigation only; individual action unverified |
| `referral.06` | 段階特典を設定 | UI `referral`, `POST /referrals` set_tier (before first code) | Source only; runtime verification pending |
| `referral.07` | 特典達成を確認 | UI `referral` | UI + owned operation (Source; CI pending/readback) |
| `referral.08` | 付与履歴を見る | UI `referral` | Navigation only; individual action unverified |
| `referral.09` | 紹介リンクを停止 | UI `referral` | Navigation only; individual action unverified |
| `analytics.01` | 今日の集計 | Bridge `analytics.get_summary` | Bridge adapter contract only |
| `analytics.02` | 今週の集計 | Bridge `analytics.get_summary` | Bridge adapter contract only |
| `analytics.03` | 今月の集計 | Bridge `analytics.get_summary` | Bridge adapter contract only |
| `analytics.04` | 期間比較 | UI `analytics` | UI + owned operation (Source; CI pending/readback) |
| `analytics.05` | フォロワー推移 | UI `analytics` | Navigation only; individual action unverified |
| `analytics.06` | 投稿別反応 | UI `analytics` | UI + owned operation (Source; Runtime unverified) |
| `analytics.07` | キャンペーン成果 | UI `analytics` | Navigation only; individual action unverified |
| `analytics.08` | 紹介成果 | UI `analytics` | Navigation only; individual action unverified |
| `analytics.09` | Telegram流入経路 | No owner route | Not connected |
| `analytics.10` | 未分類流入を見る | No owner route | Not connected |
| `analytics.11` | レポートを出す | UI `analytics` | UI + owned operation (Source; CI pending/readback) |

## Counts and verification contract

- Exactly 79 catalog IDs. 44 link to an owner UI, 13 link to a Bridge contract, 22 have no route. These counts **do not equal implemented actions**.
- Verified source deltas in this change unit: `library.07` searched via existing Discovery DB; `analytics.04` measured matched period comparison; `media.05` explicit failed-save retry via existing MediaSave; `media.06` candidate-ID scoped save history; `media.07` actual storage capacity readback; `referral.07` existing GATE tier-progress ledger report; `analytics.11` owner-only measured CSV export; `engagement.06` owner-private receipt history. Each still requires actual runtime/browser acceptance.
- Existing `tests/test_action_catalog.py` protects 79 IDs and prohibits treating disconnected actions as executed. New tests independently cover implemented owned operations; no faked provider, GATE, or Telegram response is accepted as live.
- Exact owners, stores, dependency mapping and deployment boundaries are in [NINE_MODULE_SOURCE_MAP.md](NINE_MODULE_SOURCE_MAP.md); authoritative UX examples remain the original supplied catalog. Never replace implementation with route documentation.

- `publisher.06`: original draft cards now show read-only source-text preview with status and digest via existing private draft store and authenticated endpoint; separate confirmed publishing remains required. Media/tag-complete preview is still NOT IMPLEMENTED.

- `analytics.06`: verifies a selected saved Discovery candidate, reports only the most recent Tumblr note-count snapshot and separately this app's verified personal engagement receipts. Notes do NOT mean likes-only count, and no real-time API or invented impressions.
