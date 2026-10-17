# Hermes Fleet connector 0.3.0

Connect Hermes Fleet phones from any platform: ask Hermes for a link, approve the code. The connector is an external Hermes plugin plus a separately activated service; it never edits Hermes core. The wire contract is in [docs/universal-login.md](docs/universal-login.md).

## Read-only Live Operations reporting

Version 0.3.0 bundles Live Operations reporting in the connector installation.
No separate Fleet Live Reporting download is needed after the combined package
has been installed. Install an exact reviewed commit. Catalog availability depends on the
separate Nous-maintainer review of its pinned commit.

Reporting keeps the schema-1 `GET /api/plugins/fleet-liveops/snapshot` endpoint,
existing authentication/TLS, and observation-only operation IDs. Enable its
`fleet-liveops` dashboard component only for the profiles you want to observe.
That consent entry is not another plugin installation and does not activate
connector commands or auth providers in reporting-only profiles.

Start with `hermes fleet reporting status`. Configuration and migration default
to previews; they never install code, restart services, issue pairing invitations,
or touch device/owner stores. See [profile selection, migration, coverage and
rollback](docs/REPORTING.md). Hosted and mixed-profile backends fail closed when
their live registry scope cannot be established. Push and approval-response
components are excluded.

## How connecting works

1. Ask Hermes for a link: send `/fleet pair` in a **private chat** with Hermes on any platform, or run `hermes fleet pair` on the computer running Hermes.
2. Open the link on your iPhone (tap it, scan the QR code the terminal prints, or paste it into the Fleet app). Fleet shows the server and a 6-digit match code.
3. Approve that code: send `/fleet approve 482913` in your private chat, or answer `y` in the waiting `hermes fleet pair` terminal after comparing the code.

The link is not a credential. On its own it can only create a pending request that an owner must approve by code. Approval needs the exact code the phone shows; a code that matches nothing gets a warning that the phone may be talking to a different server.

## Install or upgrade

```sh
hermes plugins validate "$FLEET_SOURCE" --install-deps
hermes plugins install "file://$FLEET_SOURCE" --ref "$FLEET_PIN" --no-enable --yes-deps   # add --force to upgrade
hermes plugins enable hermes-fleet-connector
```

`FLEET_SOURCE` is the reviewed connector repository and `FLEET_PIN` its full commit hash. Installing is an explicit code and dependency approval. Restart only the Hermes gateway that should serve `/fleet`. The connector service must also be running for the commands to work; see the legacy sections below for preparing and activating it. Connector settings and device data live outside the plugin source and survive upgrades.

Requires Hermes `>=0.21.6`, Python 3.14 (the Hermes runtime) and an HTTPS origin for this computer that the computer itself can reach (for example a Tailscale Serve address); the connector reads that origin's certificate key for the link pin. Plugin dependencies (`fastapi`, `uvicorn`, `httpx`, `websockets`, `cryptography`, `truststore`, `ruamel.yaml`) are declared in `plugin.yaml` and installed by `--install-deps` / `--yes-deps`.

## Set up without Telegram

Telegram is optional. A host needs only its HTTPS origin:

```sh
FLEET_PYTHON="/absolute/path/to/hermes/runtime/bin/python"      # the Hermes Python 3.14 runtime
STOCK_ROOT="/absolute/path/to/hermes-root"      # Hermes >=0.21.6 source or installed package root with required APIs
export PYTHONPATH="$HERMES_HOME/plugins/hermes-fleet-connector"
"$FLEET_PYTHON" -m fleet_connector --home "$HERMES_HOME" setup \
  --hermes-root "$STOCK_ROOT" --origin "https://YOUR_PRIVATE_ORIGIN"
"$FLEET_PYTHON" -m fleet_connector --home "$HERMES_HOME" prepare-service
```

No `--owner`, `--bot-id`, `--from-telegram-request` or `--approve-owner-binding` is needed (those remain for the Telegram path). Activate the prepared service as described in the legacy section below, then:

1. `hermes fleet status` on the host confirms the service, certificate pin and Hermes backend. Local access is ownership; nothing waits on a Telegram binding.
2. `hermes fleet pair` pairs a phone straight from the terminal.
3. To pair from a messaging platform, run `hermes fleet claim` on the host and send `/fleet claim <code>` to Hermes in a private chat on that platform. That account is then an owner and can use `/fleet pair`, `/fleet approve <code>` and the rest.

## Upgrade from 0.1.x

For a Mac whose Hermes is 0.22.x with connector 0.1.3 already installed (0.1.3's manifest range excluded 0.22, so Hermes may have stopped loading it). Existing settings, Telegram owners, devices and replay records live in `plugin-data/hermes-fleet-connector/` under the Hermes home and are kept: do not rerun `setup`, and do not delete that directory. Telegram owners from `settings.json` are copied into the owner list automatically (`settings.json` is never rewritten); paired phones keep working.

```sh
export HERMES_HOME="/absolute/path/to/selected/profile"
FLEET_SOURCE="/absolute/path/to/reviewed/connector/repository"
FLEET_PIN="FULL_40_CHARACTER_HASH_FOR_0.2.0"
hermes plugins validate "$FLEET_SOURCE" --install-deps
hermes plugins install "file://$FLEET_SOURCE" --ref "$FLEET_PIN" --force --no-enable --yes-deps
hermes plugins enable hermes-fleet-connector
hermes plugins show hermes-fleet-connector        # expect version 0.2.0
```

Then restart exactly two things, nothing else:

1. The connector service, so it runs the new code. Unload and load its own launchd job (its label is in `plugin-data/hermes-fleet-connector/service.plist`, `Label`), or `launchctl kickstart -k "gui/$(id -u)/<label>"`. If you run it in a foreground terminal, stop it with Ctrl-C and start it again with the same `serve --approve-activation` command as before. The existing plist needs no change.
2. The Hermes gateway for this profile, so `/fleet` is registered (the `hermes fleet` terminal command works without a restart). The legacy Telegram commands keep working.

Verify: `hermes fleet status` reports connector 0.2.0, certificate pin ready and backend reachable. Then `hermes fleet pair` (or `/fleet pair`) and approve the code. If the connector's own HTTPS origin is unreachable from this computer, `status` says so and `pair` refuses rather than make a link without a pin. Rollback: reinstall the 0.1.3 pin with `--force` on a Hermes release it supports and restart the same two things; settings and devices are untouched. The `owners.json` file written by 0.2.0 is ignored by 0.1.3.

## Who is an owner

- Anyone with local access: `hermes fleet ...` on the host, and the local CLI/TUI, count as the owner.
- Messaging accounts become owners by claiming. On the host run `hermes fleet claim`; it prints a 6-digit code valid for 10 minutes. In a private chat with Hermes on any platform send `/fleet claim <code>`. A code is single use and five wrong attempts invalidate it.
- `hermes fleet owners` lists owners; `hermes fleet owners remove <n>` removes one.

Identity comes only from Hermes' trusted session context (platform, user id, chat type) or local host access, never from message text. On messaging platforms every command except `/fleet help` works only in a private chat.

## Commands

In chat (`/fleet`, one command on every platform):

| command | who | what it does |
|---|---|---|
| `/fleet pair` | owner, private chat | sends a one-time link (10 minutes) |
| `/fleet approve <code>` | owner | approves the phone showing that code (spaces and dashes are ignored) |
| `/fleet deny <code>` | owner | rejects a request |
| `/fleet devices` | owner | numbered list of connected phones |
| `/fleet revoke <n>` | owner | disconnects phone number n |
| `/fleet claim <code>` | anyone, private chat | becomes an owner using a code from `hermes fleet claim` |
| `/fleet status` | owner | readiness in plain words |
| `/fleet`, `/fleet help` | anyone | short usage |

In a terminal on the host: `hermes fleet pair`, `approve <code>`, `deny <code>`, `devices`, `revoke <n>`, `claim`, `owners [remove <n>]`, `status`.

`hermes fleet pair` prints the link and, if `qrencode` is installed, a QR code (the link is passed to `qrencode` on stdin, never as an argument). It then waits up to 10 minutes. When a phone requests access it prints `iPhone request: <name> (<model>) · code 482 913. Approve? [y/N]`; `y` approves, anything else denies. When stdin is not a terminal it prints the link and the approve instructions and exits.

## Troubleshooting

- "Fleet connector isn't running on this computer": on the computer running Hermes, run `hermes fleet status`. It explains what is missing (service not started, origin not configured, backend not ready).
- The phone cannot reach the server: turn on Tailscale on the iPhone and sign in to the same tailnet as your computer (when the server uses a tailnet name), make sure the computer is awake and Hermes is running, then ask for a new link.
- "No phone is waiting with that code": check the digits. If the phone shows that code, it may be connected to a different server; cancel on the phone and ask for a new link.
- A link works once and expires after 10 minutes. After 10 wrong codes approvals pause for 10 minutes.
- `/fleet` says it cannot tell who is asking: use a private chat with Hermes, or run `hermes fleet` on the host.
- If a plugin is loaded with `plugins.isolation: host`, `/fleet` registers but the plugin runs in a separate process where Hermes may not carry the chat identity; commands then refuse rather than guess. Use `hermes fleet` in a terminal, or `in_process` isolation. The legacy Telegram handlers and dashboard provider are skipped under `host`, and Hermes skips plugin CLI commands there too.

## Legacy Telegram QR flow (0.1.x, kept for compatibility)

The material below documents the original Telegram-only DEV 12 flow, which still works unchanged. It is no longer the primary path; use `/fleet pair` or `hermes fleet pair` above. Its version, compatibility and pin statements describe the 0.1.3 test candidate; 0.2.0 supports Hermes `>=0.21.6`.


An external Hermes plugin and supervised connector service implementing the Fleet DEV 12 pairing contract. The connector never edits Hermes core. It registers four native Telegram commands through `register_platform_handler`: `/fleetsetup`, `/fleetstatus`, `/fleetqr`, `/fleetdevices`. It registers no model tools, hooks or middleware. Versions 0.1.1 and later also register a supported host-only dashboard session provider for explicitly selected existing-backend mode; see [existing gateway integration](docs/EXISTING_GATEWAY.md).

The original 0.1.x preparation notes below are historical. Public 0.1.5 and its
catalog pin do not identify 0.3.0. See [publication and validation](docs/PUBLICATION.md)
for the current release scope; catalog acceptance is a separate maintainer decision.

### Compatibility and prerequisites

- Verified official stock Hermes **v0.21.6**, commit `818c13be1dc4fd28987e1e881a9408224afd4535`, Python 3.14, web and Telegram runtime dependencies. This describes the 0.1.x audit; 0.2.0 requires Hermes `>=0.21.6` and the public plugin APIs. Gitless installed packages and future releases exposing those APIs are accepted; missing APIs fail closed. A version label or Git cleanliness is not the acceptance policy.
- A working Telegram gateway in the selected profile, with an established access policy. Native handlers require effective `plugins.isolation: in_process`. `host` isolation is incompatible and is never changed automatically.
- macOS user GUI/Aqua session, Safari, `qrencode`, and a private screen. SSH/headless rendering is refused. Quick Look/`qlmanage` is not required. `qrencode` is a disclosed system prerequisite; it is never silently installed. Browser launch and authenticated page paint are checked separately.
- Distinct free loopback ports (defaults 9129 connector, 9130 stock backend). Existing services are preserved. Do not use 9119 when another service owns it.
- An **existing or explicitly approved private HTTPS forwarding origin to connector port 9129**, using trusted certificates. Tailscale Serve is suitable if both hosts can reach the private tailnet. No Funnel, public exposure, insecure TLS, firewall or router changes. A forwarding origin currently serving another backend is not connector readiness.
- The Fleet DEV app on the phone, reachable HTTPS, normal iOS trust/pinning approvals and an explicit `Connect this phone` action. Never clear the app, Keychain, saved gateways or TLS pins.

### Supported installation

Use the verified source hash in the handoff. Start with the selected stock `hermes` executable and exact profile. The source must be an inspected Git repository, including a repository cloned from the supplied bundle.

```sh
export HERMES_HOME="/absolute/path/to/selected/profile"
FLEET_SOURCE="/absolute/path/to/reviewed/connector/repository"
FLEET_PIN="FULL_40_CHARACTER_HASH_FROM_HANDOFF"
hermes plugins validate "$FLEET_SOURCE" --install-deps
hermes plugins install "file://$FLEET_SOURCE" --ref "$FLEET_PIN" --no-enable --yes-deps
hermes plugins enable hermes-fleet-connector
hermes plugins show hermes-fleet-connector
```

Installing is an explicit code/dependency trust approval. The stock CLI manages dependency consent and enablement. A read-only discovery list is not installation proof. Installing/enabling does not start the connector or mint an invitation. On compatible gateways, use the supported live plugin activation and verify its receipt/PID continuity. Restart **only the selected gateway** if live activation is unavailable, after the operational approval; do not blindly restart unrelated gateways.

Reinstall the **same inspected source and exact pin** with `--force --no-enable --yes-deps`. Hermes carries user files forward. Do not replace a modified install without reviewing the changes. Stock Hermes refuses a different pin without explicit selection. Connector settings/data live outside the plugin source and survive install/reinstall/remove. Changing settings through `setup` is refused when they differ; preserve and review the existing file before an intentional migration.

### Guided owner setup

For an already configured connector, keep the saved numeric owner/bot binding, settings, device store and replay records. Do not rerun `setup` or the old pending-setup helper during a viewer update. Existing 0.1.1 settings remain readable by 0.1.2 and 0.1.3; their stored version label does not trigger a migration. After an approved update and connector-only restart, use `/fleetqr` and confirm observations on the private page. The following binding steps are for a new host.

After the approved gateway restart, send `/fleetsetup` privately to that gateway. The bridge captures the real PTB sender, bot and update metadata, checks ordinary gateway policy, and records a ten-minute local setup request. A group, forwarded message, bot, injected/edited event or unauthorized user cannot bind an owner. The sender does not become owner until the **local host owner reviews and explicitly approves** the request. Existing owner bindings are never overwritten by Telegram.

Select `FLEET_PYTHON` from the active stock Hermes **PM-managed runtime**, containing its declared plugin dependencies. Do not use a historical Python 3.11 venv or an unrelated interpreter. `STOCK_ROOT` is the selected Hermes source or installed package root exposing the required public plugin APIs.

```sh
FLEET_PYTHON="/absolute/path/to/selected/stock/runtime/bin/python"
STOCK_ROOT="/absolute/path/to/hermes-root"
export PYTHONPATH="$HERMES_HOME/plugins/hermes-fleet-connector"
# Review setup-request.json locally; it contains account IDs, no bearer secrets.
"$FLEET_PYTHON" -m fleet_connector --home "$HERMES_HOME" setup \
  --hermes-root "$STOCK_ROOT" --origin "https://YOUR_VERIFIED_PRIVATE_ORIGIN" \
  --from-telegram-request --approve-owner-binding
"$FLEET_PYTHON" -m fleet_connector --home "$HERMES_HOME" prepare-service
```

For a host-local test without Telegram setup, `setup --owner NUMERIC_ACCOUNT_ID --bot-id NUMERIC_BOT_ID --approve-owner-binding` is supported only with independently verified account IDs. This is an explicit local-owner fallback, not proof of the one-message Telegram product flow.

The prepared plist has an exact profile-specific label and commands. Review it before activation. It is stored under the connector's own data; it does not overwrite any existing LaunchAgent. The service supervises a dedicated, unchanged stock `hermes serve --isolated --skip-build` process. Its random backend session token exists only in process memory/the child environment; the phone never receives it or `API_SERVER_KEY`. Normal stock server startup may reconcile its profile database and start its standard background components. Activation on an existing profile therefore requires the ordinary backup/review gate.

```sh
# Foreground test: stop with Ctrl-C; it stops only its owned stock child.
"$FLEET_PYTHON" -m fleet_connector --home "$HERMES_HOME" serve --approve-activation
# OR load the reviewed supervisor from the user GUI session:
launchctl bootstrap "gui/$(id -u)" "$HERMES_HOME/plugin-data/hermes-fleet-connector/service.plist"
```

Before issuing, verify the static service prerequisites: the actual private HTTPS `/fleet/health` returns this service's instance identity, the protected stock backend route works, the owner binding is present, and a supported GUI session is available. `/fleet/health` is a non-secret process probe; it proves no phone or backend capability itself.

The normal `/fleetqr` flow refreshes the current phone and screen observations in the private Safari viewer, so it does not require a Terminal observation refresh. If `/fleetstatus` reports only `current_owner_observation_required`, send `/fleetqr` from the real private owner account. It opens a preflight page containing a non-pairing display check. Open the displayed private HTTPS health address on the phone without a certificate warning, verify the host screen is private, and explicitly confirm both checks on the Safari page. The connector reruns its protected backend and trusted HTTPS checks, then records the observations for ten minutes against the current process and connector configuration. Existing owner bindings are preserved.

The local operator `observe --phone-tls-verified --private-display-verified` command remains available for advanced owner-run workflows. It records explicit local assertions and does not skip the `/fleetqr` viewer's current preflight, paint, and observation checks.

For a separate advanced local observation flow, record only observations actually performed:

```sh
"$FLEET_PYTHON" -m fleet_connector --home "$HERMES_HOME" observe \
  --phone-tls-verified --private-display-verified
"$FLEET_PYTHON" -m fleet_connector --home "$HERMES_HOME" status
```

Any observation expires after ten minutes and is bound to the exact process and connector configuration. Status is read-only and does not issue. Readiness also checks a protected real stock route with the connector-owned backend token, trusted host HTTPS with redirects refused, instance identity, and GUI capability. Phone observations are owner attestations, not automated measurements. No attestation is recorded during preparation.

When the Safari page is painted and both owner observations pass, the connector rechecks readiness and only then creates a real two-minute, single-use invitation. The QR appears in that private Safari page. The current numeric owner, exact bot and ordinary gateway policy are checked again; the Telegram update is permanently claimed before the viewer starts. Telegram receives only a safe status message. Scan, review the host and broad `fleet:operator` access, approve normal TLS trust, and tap **Connect this phone**. Rendering or launch return is not a claim that the owner saw or scanned the QR. Query status after a failure. Closing the viewer or display failure cancels any remaining pending invitation; if cancellation is unconfirmed the status says so. Expiry also enforces the boundary.

### Scoped recovery and rollback

`/fleetdevices` shows active non-secret device IDs to the authorized owner. Revoke exactly the test attempt:

```sh
"$FLEET_PYTHON" -m fleet_connector --home "$HERMES_HOME" revoke TEST_DEVICE_ID --approve-revocation
# For launchd activation, unload the exact connector plist; no broad stop command:
launchctl bootout "gui/$(id -u)" "$HERMES_HOME/plugin-data/hermes-fleet-connector/service.plist"
"$FLEET_PYTHON" -m fleet_connector --home "$HERMES_HOME" rollback
hermes plugins disable hermes-fleet-connector
hermes plugins remove hermes-fleet-connector
```

Restart only the gateway that loaded the bridge if removing it from a live process. Rollback refuses while the connector socket is active. It removes its own prepared plist/observations/process receipt and retains settings, devices, instance identity and issuance replay records. Plugin removal retains connector-owned data. Never delete pairing or replay stores to make a retry work. A lost redemption response/process death can orphan a device; inspect/revoke only that attempt. An abrupt SIGKILL can leave a private QR temporary file until normal OS cleanup; immediate deletion is not guaranteed.

### Security and disclosure

The bridge is explicitly native, in-process trusted code. It reads the selected profile's configuration/access policy and its own settings; it does not read another tool's login, browser profile, OAuth credentials or broad API key. Local Unix-socket permissions are the general operator boundary (same OS user); this is not a defense against a compromised user account. The separate authenticated viewer can confirm observations or cancel only its already owner-requested display attempt; it exposes no general issuance, owner binding, device management or revocation API. Those operations remain on the owner-only Unix socket. The service's loopback-only HTTPS-terminator trust is explicit; forwarded headers from remote peers do not authorize transport. Phone HTTP calls have body-size/rate/origin checks and their own session gate.

Only Fleet media, cron, optional live-ops and (0.1.3) a fixed set of Kanban routes are proxied; see the [Kanban supplement](docs/COMPATIBILITY.md#connector-013-kanban-routes-supplement). Generic config/admin/auth/pairing-owner routes refuse. Stock WebSocket JSON-RPC gives the disclosed `fleet:operator` grant, broadly the normal dashboard operator capability; it is not a read-only grant. Approvals inside Hermes remain stock behavior. Device revocation kills sessions/tickets and closes live sockets on the next frame or a 0.5-second check. Already dispatched Hermes work is not retroactively cancelled by revocation.

The browser viewer is a separate ephemeral HTTP server bound to loopback on an OS-assigned port; it is not the connector/tailnet API. Safari opens a private temporary bootstrap file containing a one-use grant. The browser submits that grant to the loopback server in a POST body; the grant is not placed in a URL or logs and the file is removed after use. The returned page keeps its session token in page memory and sends it in an authorization header. The viewer uses no persistent cookies, browser storage, external assets, or cacheable responses. QR images remain in process memory and are returned only to the authenticated page. Closing, expiry, disconnect, or display failure clears the image and cancels any unredeemed invitation.

Network calls: the selected private HTTPS origin, local owned stock backend, and the ordinary Hermes runtime/dependency installers. No outbound telemetry. Background process: the explicitly activated connector plus its supervised stock child. External commands: supported Hermes CLI, Git provenance inspection, `qrencode`, Safari via `/usr/bin/open`, optional owner-run `launchctl`. QR bearer goes over encoder stdin, never argv or Telegram. Private state is owner-only. Reports and logs must not contain bearer links/credentials.

See [compatibility audit](docs/COMPATIBILITY.md), [test plan](docs/TEST_PLAN.md), and [publication gate](docs/PUBLICATION.md). Run tests with `PYTHONPATH=. HERMES_STOCK_ROOT=/path/to/clean/stock python -m pytest tests`; process tests skip explicitly without that checkout. Unit fixtures do not prove live host readiness.

## Local compatibility consolidation

Host acceptance checks the public command/CLI registration APIs, and additionally native platform/dashboard APIs for in-process operation. Git metadata and an upper release-version cutoff are not required. Missing capabilities fail before partial registration; host isolation retains the universal command flow. The locally available public display and supervisor fixes are included. Version remains 0.2.0; no installed service is changed by this source update.
