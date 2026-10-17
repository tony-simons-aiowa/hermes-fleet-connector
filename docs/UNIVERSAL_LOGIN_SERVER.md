# Universal login: connector service side (0.2.0)

Implements the server half of [universal-login.md](universal-login.md).

## HTTP
- `GET /pair`: static page, no server-side secrets. CSP `default-src 'none'` with SHA-256 hashes of the single inline script and style. The script reads `location.hash` and links to `hermes-fleet-dev://pair#...&o=<base64url origin>` (primary) and `hermes-fleet://pair#...`.
- `POST /api/fleet/pairing/request` and `.../request/status`: see the contract. Error statuses: `approval_required` 403, `approval_not_supported` 409, `already_used` 409, `rate_limited` 429, `invalid_request` 400 (shape errors 422).
- `preview` adds `invitation.approval_required` and top-level `protocols: [1, 2]`.
- The shared 60/min request budget is unchanged; status polling uses its own 240/min bucket so a waiting phone cannot starve other traffic.

## Storage (`devices.sqlite3`, migrated in place)
- `invitations.requires_approval` (default 0, existing rows stay v1); `devices.approved_by_platform/user`.
- `pairing_requests`: only a SHA-256 of `client_secret`; the plaintext `hfd1` credential is kept until the first successful `/auth/device-login` with it or 10 minutes after approval (also wiped by any later write), then status is `delivered`.
- `approver_failures` / `approver_locks`: 10 wrong codes (approve or deny) in 10 minutes lock that approver for 10 minutes.

## Owners (`owners.json` in the connector directory)
Entries `{platform, user_id, added_at, added_via}`. Legacy Telegram ids in `settings.json` are migrated once (`migrated`); `settings.json` is never rewritten. `local` is always an owner. Claim codes: 6 digits, 10 minutes, single use, void after 5 wrong attempts; only a digest is stored.

## Control socket
Actions carrying `approver: {platform, user_id}`: `pair`, `pending`, `approve`, `deny`, `devices`, `revoke`, `status`, `claim_start` (local only), `claim` (`code`, `platform`, `user_id`; open to anyone), `owners`, `owners_remove` (`index`, 1-based). Failures: `{"ok": false, "error": not_owner|no_match|locked|invalid_code|pin_unavailable|no_such_device|no_such_owner|too_many_pending, "message": ...}`. Legacy actions (no `approver`) are unchanged.

## Pin
`k` = base64url(SHA-256(DER SPKI)) of the certificate the configured origin presents, read with verification disabled (nothing is sent), cached 10 minutes; `pair` fails with `pin_unavailable` rather than emit a link without it. Requires the `cryptography` package.
