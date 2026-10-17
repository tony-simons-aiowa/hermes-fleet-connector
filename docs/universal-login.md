# Universal login (Build 17): ask Hermes, approve the code

Status: contract for Build 17. Shared by the iOS app (`hermes-fleet`) and the
Hermes Fleet connector plugin (`hermes-fleet-connector` 0.2.0). Both sides
implement exactly this; anything not written here is out of scope.

## Goal

Connecting a phone must work from wherever the owner already talks to Hermes:
any messaging platform Hermes supports, the CLI/TUI, or desktop. No Telegram
requirement, no copy-paste prompt, no need to be at the host's screen, no
requirement that iOS already trusts the server certificate.

## User flow

1. The owner asks Hermes for a link: `/fleet pair` on any platform (private
   chat), or `hermes fleet pair` in a terminal on the host.
2. Hermes replies with a link (and, in a terminal, a QR code).
3. On the iPhone, the owner opens the link (tap it, scan the QR, or paste it in
   the app). Fleet shows the server and a 6-digit **match code**.
4. The owner approves that code where Hermes trusts them: `/fleet approve
   482913` in their private chat, or answers `y` in the waiting `hermes fleet
   pair` terminal after comparing the code.
5. The phone receives its device credential and is connected.

The link is **not** a credential. On its own it can only create a pending
request that the owner must approve by its code. That is what makes it safe to
send through any chat.

## Security invariants (both sides)

- **S1 Approval is mandatory for v2 invitations.** An invitation created by
  `/fleet pair` or `hermes fleet pair` is marked `requires_approval`. The v1
  `POST /api/fleet/pairing/redeem` endpoint MUST refuse it with
  `approval_required`. Only the request/approve/poll path below can turn it
  into a device.
- **S2 Owner-only approval.** Only an owner (see Owners) can approve, deny,
  pair, list or revoke. Identity comes only from Hermes' trusted session
  context (platform, user id, chat type) or from local host access, never from
  message text or model output.
- **S3 Private chats only.** On messaging platforms the owner commands work only
  in a direct/private chat with the bot (`chat_type == "dm"`).
- **S4 The credential is bound to the requesting phone.** The phone sends a
  random `client_secret` with its request. Polling for the result requires it.
  Someone else holding the link cannot collect the credential, even after
  approval.
- **S5 The code is the anti-phishing check.** Approval always names the code the
  phone shows. There is no "approve the only pending request" shortcut. A code
  that matches nothing produces a warning that the phone may be talking to a
  different server.
- **S6 Pinned first contact.** The link carries the SHA-256 of the server's TLS
  public key (SPKI). The phone accepts the server if iOS trusts the certificate
  or if the presented key matches the pin. If both a pin and system trust are
  present, the key must still match the pin (mismatch = `identityMismatch`).
  The confirmed key is saved as the gateway's TLS pin, as today.
- **S7 No secrets in logs.** Invitation secrets, client secrets and credentials
  never appear in logs, chat replies (except the invitation link itself, which
  is safe by S1), command arguments, exceptions or test fixtures.
- **S8 No personal data in the repositories.** Fixtures use synthetic hosts such
  as `fleet.example.test`; never a real hostname, tailnet name, account id or
  device id.

## Link format v2

```
https://<host>[:port]/pair#v=2&i=<invitation id>&s=<invitation secret>&k=<spki pin>
```

- `v=2` required. `i`, `s`: same encoding and lengths as v1 (URL-safe base64).
- `k`: SHA-256 of the DER `SubjectPublicKeyInfo` of the certificate the origin
  presents to clients, base64url without padding (exactly 43 chars). The
  connector computes it by connecting to its own configured HTTPS origin (so a
  TLS-terminating proxy such as Tailscale Serve is pinned correctly). Required
  in v2.
- Host rules are unchanged from v1 (https, real multi-label DNS name, no IP
  literal, no user-info/query, path `/pair`).
- v1 links keep working exactly as before (no approval step).
- App-scheme form, for opening the app directly from the `/pair` web page:
  `<app-url-scheme>://pair#v=2&i=…&s=…&k=…&o=<base64url of https origin>`.
  The app accepts its own scheme (`hermes-fleet-dev` for the Dev app) with host
  `pair`; `o` gives the origin because the scheme URL has no https host.

## Connector HTTP API (additions)

All bodies JSON; errors are `{"error": "<code>"}` with the existing status codes
and the existing error vocabulary plus the new codes below. Every response is
`Cache-Control: no-store`.

### `GET /pair`
Serves a tiny static HTML page (no secrets server-side: the fragment never
reaches the server). Its script reads `location.hash` and shows an **Open in
Hermes Fleet** button linking to the app-scheme form above (Dev scheme
`hermes-fleet-dev`, production `hermes-fleet`), plus the text: "Don't have the
app? It's in TestFlight." No external resources. Strict CSP.

### `POST /api/fleet/pairing/preview` (changed)
Unchanged request. Response adds `"approval_required": true|false` inside
`invitation`, and top-level `"protocols": [1, 2]`.

### `POST /api/fleet/pairing/request` (new)
Request:
```json
{"invitation_id": "…", "secret": "…", "device_name": "Tony's iPhone",
 "device_model": "iPhone17,1", "client_secret": "<43-char base64url>"}
```
- Valid only for an invitation with `requires_approval`. A v1 invitation answers
  `approval_not_supported` (the app then uses v1 redeem).
- Consumes the invitation into exactly one pending request. Repeating the call
  with the same invitation and the same `client_secret` returns the same
  request (idempotent, for lost responses). A different `client_secret` answers
  `already_redeemed`.
- Response `201`:
```json
{"request": {"id": "<request id>", "match_code": "482913",
             "expires_at": 1800000600, "poll_interval": 2, "server_time": 1800000000},
 "gateway": {…same object as preview…}}
```
- `match_code`: 6 decimal digits, uniformly random, unique among unexpired
  pending requests. Request lifetime: 10 minutes.

### `POST /api/fleet/pairing/request/status` (new)
Request: `{"request_id": "…", "client_secret": "…"}`
Responses (`200`):
- `{"status": "pending", "expires_at": …, "server_time": …}`
- `{"status": "approved", "device": {"id": "…", "label": "…"}, "credential": "hfd1.…",
   "gateway": {…}, "scopes": ["fleet:operator"]}`. Returned on every poll
   until that credential's first successful `/auth/device-login` or until 10
   minutes after approval, whichever comes first. The connector keeps the
   plaintext credential only in the request row for that window, then wipes it.
- `{"status": "delivered"}`: approved and already collected (or the window
  closed). No credential.
- `{"status": "denied"}`, `{"status": "expired"}`
Errors: `invalid_request` (unknown request or wrong `client_secret`; never
distinguish the two), `rate_limited` (more than one poll per second per
request, status 429).

### Unchanged
`/api/fleet/pairing/redeem` (v1, refuses `requires_approval` invitations with
`approval_required`), `/auth/device-login`, `/auth/device-revoke`, device
listing/revocation semantics, `hfd1` credential format, scopes.

## Connector control actions (plugin ⇄ service, local socket)

The in-gateway plugin talks to the connector service over the existing owned
local control socket. New actions (request → response, all JSON):

| action | input | output |
|---|---|---|
| `pair` | `approver` | `{"ok", "link", "expires_at"}` (creates a v2 invitation, 10 min) |
| `pending` | `approver` | `{"ok", "requests": [{"match_code","device_name","device_model","created_at","expires_at"}]}` |
| `approve` | `approver`, `code` | `{"ok", "device": {...}}` or `{"ok": false, "error": "no_match"\|"locked"}` |
| `deny` | `approver`, `code` | `{"ok"}` or `no_match` |
| `devices` | `approver` | existing device listing, numbered |
| `revoke` | `approver`, `device` (number or id) | `{"ok"}` |
| `status` | `approver` | readiness summary (origin, pin computed?, backend ok, version) |

`approver` is `{"platform": "telegram"|"discord"|…|"local", "user_id": "…"}`
and is recorded on the device for audit. The service re-checks that the
approver is an owner (defense in depth). After 10 wrong codes from one approver
within 10 minutes, `approve` answers `locked` for 10 minutes.

## Owners

- Stored by the connector as a list of `{"platform", "user_id", "added_at",
  "added_via"}` in its settings directory (`added_via`: `migrated`, `claim`).
- Migration: existing Telegram numeric owner ids become `telegram` owners
  (`migrated`). Existing bindings are never dropped.
- **Local access is ownership.** `hermes fleet …` on the host and local CLI/TUI
  sessions count as the owner (platform `local`).
- **Claiming another platform:** `hermes fleet claim` (local) prints a 6-digit
  claim code valid for 10 minutes. Sending `/fleet claim <code>` in a private
  chat on any platform adds that `(platform, user_id)` as an owner. Single use,
  5 wrong attempts invalidate the code.
- `hermes fleet owners` lists owners, `hermes fleet owners remove <n>` removes.

## Commands

### Chat: `/fleet` (registered once with Hermes `register_command`, all platforms)

| command | who | reply |
|---|---|---|
| `/fleet pair` | owner, private chat | link + "Open it on your iPhone, then send `/fleet approve <code>` with the code Fleet shows. Works once, expires in 10 minutes." |
| `/fleet approve <code>` | owner | "Approved {device}. It's connecting now." / no-match warning (S5) |
| `/fleet deny <code>` | owner | confirmation |
| `/fleet devices` | owner | numbered list (label, model, added, last used) |
| `/fleet revoke <n>` | owner | confirmation |
| `/fleet claim <code>` | anyone, private chat | owner added / invalid code |
| `/fleet status` | owner | readiness summary in plain words |
| `/fleet` or `/fleet help` | anyone | short usage |

Non-owners get: "Only this Hermes owner can do that. On the computer running
Hermes, run `hermes fleet claim` and send the code here as `/fleet claim
<code>`." Spaces in codes are ignored (`482 913` == `482913`).

### Terminal: `hermes fleet …` (registered with `register_cli_command`)

`pair` (prints link and an ANSI QR via `qrencode -t ANSIUTF8` reading stdin when
available, else the link only; then waits up to 10 minutes and, when a request
arrives, prints `iPhone request: {name} ({model}) · code 482 913. Approve? [y/N]`),
`approve <code>`, `deny <code>`, `devices`, `revoke <n>`, `claim`, `owners
[remove <n>]`, `status`.

The legacy Telegram commands (`/fleetsetup`, `/fleetstatus`, `/fleetqr`,
`/fleetdevices`) keep working unchanged for compatibility but are no longer
documented as the primary path.

## Compatibility

- Connector 0.2.0 requires Hermes `>=0.21.6` and the public plugin APIs (the host currently runs
  0.22.x; 0.1.3 was excluded by the 0.22 update).
- Fleet app with a v1-only connector: v1 links work as today.
- Fleet app receiving a v2 link from a connector it cannot reach: reachability
  diagnosis (below).

## iOS app behaviour

### Add-server screen (first server and "Connect another server")
- Title "Connect to Hermes". Three short steps:
  1. "Ask Hermes for a link: send `/fleet pair` in any chat with Hermes, or run
     `hermes fleet pair` on your computer."
  2. "Open the link on this iPhone, or scan its QR code."
  3. "Approve the code Fleet shows."
- Buttons: **Scan QR code**, **Paste link**. Incoming universal/app-scheme links
  route to the same flow.
- "Other ways to connect" (secondary) holds the existing owner sign-in, manual
  setup and the v1 QR flow. The Telegram setup prompt and every Telegram
  mention are removed from the default path.

### Pairing states
`previewing` → `confirming` (server, host, access, "Connect this phone") →
`requesting` → **`awaitingApproval`** (large code `482 913`, "Send this to
Hermes: `/fleet approve 482913`" with Copy, "or answer y in the terminal",
expiry countdown, Cancel) → `completed`. Also `denied`, `expired`.
- Polling every `poll_interval` seconds (min 1 s, back off to 5 s on errors),
  only while the screen is visible; resumes when the app returns to foreground.
- The credential and TLS pin are saved exactly as v1 grants are today (Keychain,
  rollback/revocation on local save failure).
- Cancel while awaiting: stop polling; the request expires server-side.

### Reachability diagnosis (pairing and connection failures)
Map the failure to one plain sentence plus one action:
- DNS failure on `*.ts.net`: "Your iPhone can't find {host}. Turn on Tailscale
  on this iPhone and sign in to the same tailnet as your computer."
- DNS failure elsewhere: "Your iPhone can't find {host}. Check the link, or that
  this iPhone is on the same network as your computer."
- Timeout / connection refused: "{host} didn't answer. Make sure your computer
  is awake and Hermes is running (`hermes fleet status`)."
- Untrusted certificate without a matching pin: "{host}'s certificate isn't
  trusted and doesn't match the link. Ask Hermes for a new link."
- Pin mismatch: "This server isn't the one that made the link. Don't continue;
  ask Hermes for a new link."
- Offline device: "This iPhone is offline."
Each diagnosis offers **Try again**.

## Tests (required)

- Connector: store tests for request/approve/deny/expire/idempotency/
  client-secret binding/credential wipe/rate limit/lockout; v1 redeem refuses
  `requires_approval`; owners migration and claim; command handlers with fake
  session context for owner/non-owner/group chat; CLI pair flow with a fake
  service; pin computation against a local TLS test server. Synthetic data only.
- iOS: v2 link parsing (valid/invalid pins, app-scheme form), pinned trust
  evaluation, coordinator state machine through approval/denial/expiry/cancel/
  lost-response idempotency, reachability classification, a UI test of the
  add-server screen and waiting state against the scripted simulator fleet.
