# Tumblr ↔ Telegram integration contract

Scope: Tumblr discovery/publishing/referral attribution ↔ Telegram GATE membership verification and MPB announcement delivery. Neither Telegram source implementation nor infra/domain/DNS changes are within this workstream.

## Ownership
- Tumblr owns its post source IDs, campaign entry links, click aggregate and AI chat NEXT state.
- GATE alone owns actual Telegram membership validation; a click, referral code or UI label does not establish membership.
- MPB alone publishes to permitted Telegram destinations; its receipt must be read back and failed/unknown outcomes reconciled rather than blindly retried.
- GroupBot owns member/profile/recruitment publication policy; Tumblr never edits its DB directly.

## Integration flow
1. Tumblr post / campaign -> entry link with stable campaign and post IDs.
2. Visitor uses referral link -> record unique event ID as click, not member.
3. GATE authenticates Telegram identity independently -> verified membership event.
4. Campaign service attributes referral only after trusted verification, deduplicates member IDs and performs stage counting.
5. A cross-post request goes to MPB, must be explicitly confirmed, gets provider message_id and readback.
6. Chat shows verified outcome / failure and dynamically generated NEXT; preserve campaign/post/task context.

## Non-negotiable design boundaries
- GroupBot and Tumblr use trusted adapters with caller authentication, per-action authorization, least privilege and replay-safe event IDs. Production adapters not yet implemented.
- Transport URL, domain, Cloudflare settings, VPN, OAuth secrets are injected at deployment time and are not hardcoded in source. The separate domain workstream owns final values.
- No subscriber or Telegram profile data may be leaked into Tumblr public posts or third-party AI prompts.
- Unknown / timed-out write operations require readback and reconciliation rather than creating a duplicate post.

## Verification status
- Mock integration is in `tumblrkit/integration.py`; isolated Python tests `tests/test_integration.py`.
- Real GATE / MPB authentication, signatures, result persistence, provider retries and end-to-end tests remain outstanding.
