# Private browser display repair — local candidate 0.1.2

This repair replaces Quick Look with an authenticated private Safari page. It is a
local test candidate; installation into the live gateway/service, a real invitation,
phone acceptance and publication remain separate actions.

The original 0.1.1 display path checked only Aqua/tool presence and replied before
rendering. The observed native `qlmanage` bootstrap permission error was accompanied
by `renderer_failed`; the single test invitation was cancelled and no device existed.
The old 64-test suite mocked a renderer failure instead of exercising its real GUI.

The new owner command first opens a non-pairing display check. Safari must decode
and paint it, and the owner must explicitly confirm the real phone health/TLS and
private-screen observations. Current backend/HTTPS readiness is rechecked before
creating the existing 120-second invitation. The saved owner binding is retained;
normal observation refresh happens on the page without Terminal commands.

The viewer has its own short-lived `127.0.0.1` listener, never mounted on the
connector port or forwarded to the tailnet. A 0700 temporary directory/0600 HTML
file contains only a one-use bootstrap grant, delivered in a form POST. The grant
is consumed atomically and the file removed. A separate token stays in page memory
and authenticates frame/events with headers (unload cancellation uses a POST body).
No token or QR payload enters a URL, process argument, access log, browser cookie,
local/session storage or cacheable response. QR PNGs remain in process memory.
The returned page uses bundled Fleet branding, fixed script/style CSP hashes,
no external resources/CORS, exact Host/Origin checks, request/time/thread bounds,
and no general device/owner-management endpoints.

Close, first Back, hiding/leaving the tab, missed heartbeat, expiry, renderer failure
and shutdown clear the page/QR and stop only the pending attempt. The first Back
uses a same-document history entry so a POST-resubmission dialog cannot postpone
cancellation. Late fetch/paint/observation responses cannot restore a stopped QR.
Redemption blanks the page with a truthful instruction to finish on the phone;
it is not reported as an authenticated connection. False/unconfirmed cancellation
is reported distinctly. Already redeemed devices are retained, never silently revoked.

## Verification and scope

The task-owned GUI harness uses the same Aqua launchd domain, managed Python and
GUI command PATH as the retained connector, with byte-matched candidate modules
copied to `/tmp`. It has a temporary home, OS-assigned unforwarded loopback ports,
no native Telegram registration, no issuer/backend and no phone actions. The real
Safari opener must produce an authenticated visible decode/paint acknowledgement;
process launch alone fails the gate. A separate isolated headed Chromium profile
checks Close, Back, expiry, redeemed status, disconnect, retry, slow-check double
click and Close during pending checks. Its screenshot contains only fixed
`fixture.invalid` QR data. The final handoff binds receipts to source hashes.

Protocol/security regressions use disposable homes and clean stock v0.21.6. The
HFD1 store, login/ticket/API forwarding and backend-provider contracts remain
unchanged. Actual owner visibility, camera, phone connection/read/relaunch and
saved-data continuity remain owner acceptance gates. No camera/physical acceptance
is inferred from synthetic GUI tests. All publication/catalog gates remain closed.

## Update and rollback after explicit operational approval

Verify exact source/bundle pins and clean installed 0.1.1 source before update.
Preserve user files, binding/settings, replay/device stores, the existing gateway,
backend, LaunchAgents and private routes. A supported pinned plugin reinstall and
restart of only this connector are sufficient: no changed auth provider, gateway
configuration, backend restart, network route or OS permission is required by this
display repair. Check current operations/source again at action time; do not execute
these actions merely because the artifact was prepared.

For rollback, stop only this connector, reinstall the included known 0.1.1 pin
`fdc9a3c38af437315b0974bb2f1d04725dfcbd9a` through the supported plugin CLI, preserve
the same settings and stores, and restart only this connector when approved. The
old renderer has the documented Quick Look failure; do not retry its issuance as
part of rollback. Do not reset routes, clear owner/replay/device data, revoke unrelated
devices, or roll back Hermes core. Stopping this connector removes its host lease;
the existing gateway/backend remain running. Mini access/compatibility is still pending.
