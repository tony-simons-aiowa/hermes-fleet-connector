> Current 0.2.0 policy: public API capabilities, not Git metadata or an upper version cutoff, determine compatibility. Historical 0.1.x audit limits below describe those old artifacts.

# DEV 12 / stock Hermes compatibility audit

Audit performed October 11, 2026 America/Chicago. App source was inspected read-only at `f021b0b3a0822f675305eca9919a0c90f99905af`. No phone build or app source change was needed. The prior offline release checksum matched `3021e0bcfd750eaea6fb208a1088c70f75f999e55525293ebe3c44e46049d73c`; it is audit evidence, not this connector's install path.

The old pairing protocol was inspected at backend candidate `1bffff90b482dba75aeb9c55a5fb704c74c75841`, including `fleet_pairing_routes.py`, `fleet_devices.py`, device provider, and the owner-control bridge. The connector's `devices.py` is a byte-for-byte copy of that pairing store; the store/security tests retain attribution in the source history/notice. Official stock v0.21.6 was retrieved from NousResearch and verified at `818c13be1dc4fd28987e1e881a9408224afd4535`. Its fixed Fleet issuer routes/module are absent. No core patch or copied file inside stock is used.

| Contract | Exact behavior |
|---|---|
| QR | `https://DNS_ORIGIN[:port]/pair#v=1&i=ID&s=SECRET`; ID 22 base64url characters, secret 43, no userinfo/query; secret stays in URI fragment and request body. DEV 12 parser performs its existing URI, scheme, origin and field checks. |
| Preview | `POST /api/fleet/pairing/preview`, JSON `{invitation_id,secret}`. Response gateway `{instance_id,display_name,origin}`, invitation `{id,label,scopes,expires_at,server_time}`, access `[{scope,summary}]`. Preview never redeems. |
| Redemption | `POST /api/fleet/pairing/redeem`, proof plus optional device_name/device_model. 201 returns `{device:{id,label},credential,gateway,scopes}`. Concurrent redemption has exactly one winner. Wrong host/secret is refused, used 409, expired/cancelled 410. |
| Credential | `hfd1.DEVICE_ID.RANDOM_SECRET`, 256-bit secret, stored only hashed at host. Device-specific, long-lived until revoked. It is NOT itself short-lived. Only the invitation/session/ticket are short-lived. Allowed scope is exactly `fleet:operator`, broad dashboard operator access; no phone invitation or other-device management. |
| Lifetime | Store policy 60–900 seconds, default 600; connector issues 120 seconds. Device session 3600 seconds. WS ticket 30 seconds, single use, bound to the live device session. |
| Device login | `POST /auth/device-login` with `{device_credential}`. Secure, HttpOnly, SameSite=Lax `__Host-hermes_session_at`; `{ok:true,device_id,expires_at}`. DEV 12 decodes expires_at as String, so this connector emits ISO-8601 rather than the old route's integer. Cookie Max-Age remains one hour. |
| Ticket / WebSocket | Authenticated cookie `POST /api/auth/ws-ticket` → `{ticket,ttl_seconds:30}`. `wss://ORIGIN/api/ws?ticket=...`; connector consumes once and gates before proxying to real stock `/api/ws` using a random host-only backend token. Backend authority is never sent to the phone. Stock RPC/approval behavior is retained. |
| Removal | `POST /auth/device-revoke` body credential, or exact local-owner `revoke DEVICE_ID --approve-revocation`. Invalidates device and every session; new tickets refuse, live sockets close. Phone deletion alone is not assumed to prove server revocation. |
| Owner proof | Genuine PTB Update.message/from_user/private chat plus exact configured numeric owner, numeric bot ID and update ID, no forward/via-bot/injected event; ordinary gateway access checked through stock adapter policy. Initial binding requires fresh captured `/fleetsetup` event and separate local approval. |
| Trust | Existing Fleet HTTPS/system-root/SPKI-pinning checks and explicit connection approval remain untouched. Host readiness uses OS truststore and refuses redirects. Phone trust/reachability is a short-lived owner observation, still pending physical acceptance. No broad API_SERVER_KEY provisioned. |

Stock extension evidence: `hermes_cli/plugins.py` exposes `register_platform_handler`; `plugins/platforms/telegram/adapter.py` supplies the current native application and established authorization check; `plugin_isolation.py` defaults to in_process and explicitly rejects native handlers in host isolation. The connector respects that refusal. Dashboard custom plugin namespaces cannot provide DEV 12's fixed routes; the standalone service supplies them without changing the stock route table. A custom route never inherits auth by assumption.

Stock catalog evidence: [versioned catalog rules](https://github.com/NousResearch/hermes-agent/blob/v0.21.6/plugin-catalog/README.md) require a reviewed full 40-character Git pin, truthful capabilities/version/dependencies, no self-updating code, no core runtime overrides, preserved approvals and disclosed risky behavior. [Current catalog rules](https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/README.md) were checked online too. The actual CLI validates this candidate with dependencies, safe scanner, no core overrides and an explicit in-process-only verdict. Catalog acceptance is a separate human decision.

Limitations (as audited for 0.1.x; connector 0.2.0 widens the supported range to Hermes `>=0.21.6`, see the 0.2.0 supplement below): official v0.21.6 alone was supported; no claim for current main, release candidates, packaged installs without source provenance, host-isolated pools, Windows/Linux, Mini runtime, phone scan, or future catalog acceptance. Stock process tests use a temporary CA; production trust was not weakened. The entire Fleet matrix was not rerun because app code was unchanged. Prior app evidence does not prove new connector transport.

## Connector 0.1.2 private browser viewer supplement

The 0.1.2 local candidate presents its pairing flow in Safari. A logged-in macOS Aqua session and a private screen are still required; Quick Look/`qlmanage` is no longer a prerequisite. `qrencode` remains an explicit system prerequisite for creating the PNG, and the viewer separately requires authenticated browser paint confirmation. The static capability check does not establish that Safari opened or that a person saw the page.

The browser uses a separate ephemeral HTTP server bound only to `127.0.0.1` on an OS-assigned port. Safari opens a private temporary bootstrap file that contains a random one-use grant. The bootstrap form sends that grant in a POST body; after acceptance the file is deleted. The returned self-contained page receives a session token in memory and sends it in an authorization header. The token and invitation bearer do not appear in URLs, logs, persistent cookies, browser storage, or cacheable responses. The page has no external assets or network origins; the preflight and invitation QR images are held by the connector in memory.

Native owner flow: `/fleetqr` first opens the private preflight page, which shows a display check that cannot pair a phone. The owner verifies the phone reaches the configured private HTTPS health address without a certificate warning, confirms the host screen is private, and checks both confirmations in Safari. The connector's observation callback rechecks protected backend and trusted HTTPS readiness, then verifies that this same viewer is still loaded in preflight before recording the observation. The current numeric Telegram owner binding and process/configuration checks remain in force. Only after page paint, current observations, and readiness pass does the service mint the 120-second single-use invitation and render its QR. Closing, expiry, disconnect, renderer failure, or shutdown cancels any unredeemed invitation; a redeemed invitation remains redeemed.

No Terminal observation refresh is required for the normal `/fleetqr` flow. The explicit local `observe --phone-tls-verified --private-display-verified` command remains available for advanced owner-run use and does not bypass the browser page's current preflight and paint gate. A task-owned launchd fixture in the same Aqua domain, managed Python and GUI command PATH as the retained connector verified real Safari decode/paint acknowledgements with fixed synthetic QR data, followed by eight headed Chromium lifecycle cases. It used only a temporary home and unforwarded loopback ports, with no issuer or phone actions. This proves synthetic service-context presentation; it does not claim live owner screen/phone acceptance or publication. The source-bound receipt and preceding failed fixture runs are included in the local repair handoff.

## Connector 0.1.3 Kanban routes supplement

0.1.3 lets an authenticated `fleet:operator` device session reach the stock Kanban dashboard plugin for the Fleet app's Kanban board. The allowlist is an exact path/method table under `/api/plugins/kanban`; there is no wildcard, and any other path or method returns 403 `out_of_scope` (401 without a session, as before). Paths containing `..` or `%` are refused. Caller cookies and Authorization never reach the backend; only the connector's backend credential is sent.

| Path (relative to `/api/plugins/kanban`) | Methods |
|---|---|
| `/board`, `/boards`, `/assignees` | GET |
| `/orchestration` | GET, PUT |
| `/dispatch` | POST |
| `/links` | POST, DELETE |
| `/tasks`, `/tasks/bulk` | POST |
| `/tasks/{id}` | GET, PATCH, DELETE |
| `/tasks/{id}/comments`, `/reclaim`, `/specify`, `/decompose`, `/reassign` | POST |

`WSS ORIGIN/api/plugins/kanban/events?since=N&board=SLUG&ticket=T` uses the same single-use, 30-second, session-bound ticket from `POST /api/auth/ws-ticket` and the same transport/origin/revocation checks as `/api/ws`. Only `since` (digits) and `board` (lowercase slug) are forwarded, validated; the ticket is never forwarded. Invalid values close with 4400, a missing, reused or expired ticket with 4401. Board creation/edit/delete/switch/import/export, attachments, runs, profiles, config and other Kanban routes are intentionally not exposed. Like the existing grant, these mutations are operator-level access, not read-only.

## Connector 0.2.0 universal login supplement

0.2.0 supports stock Hermes `>=0.21.6` (the manifest minimum and capability checks agree: command/CLI APIs are required; native platform/dashboard APIs are additionally required for in-process operation). 0.1.3 and older are limited to v0.21.6 as described above. The tests import Hermes 0.22.1; under `host` isolation `/fleet` registers but commands refuse without a trusted identity, and the legacy Telegram handlers are skipped.

The pairing contract is [universal-login.md](universal-login.md) and the server notes are [UNIVERSAL_LOGIN_SERVER.md](UNIVERSAL_LOGIN_SERVER.md). v1 links and the `/fleetqr` Telegram flow are unchanged.

Telegram is optional. `settings.json` may contain only the origin, ports and Hermes root (`setup --origin ...`, no `--owner/--bot-id`). With no Telegram owners or bot ID: the legacy Telegram handlers answer nothing, legacy control proofs are refused ("Owner authorization refused"), and the legacy readiness used by `/fleetqr` keeps reporting `owner_binding_pending`. The new `status` action does not, because local access is ownership; messaging owners are added with `hermes fleet claim` and `/fleet claim <code>`. Existing settings with Telegram owners load unchanged and are migrated into the owner list on first use.

Runtime dependencies added to the manifest: `cryptography>=44,<51` (SPKI pin computation; Hermes 0.22.1 pins 50.0.1) and `truststore>=0.10,<0.11` (host HTTPS readiness check; Hermes 0.22.1 pins 0.10.4). Both ranges include the versions Hermes ships, so installing the plugin never forces a change to Hermes' own environment.

Verification: the connector test suite runs against a read-only Hermes 0.22.1 checkout (including `test_native_setup.py` and `test_backend_lease.py`, which import Hermes and `python-telegram-bot`), and `tests/test_e2e_universal_login.py` drives claim, pair, request, approve, device login, revoke over a real control socket and the real HTTP app with no Telegram configuration. Physical phone acceptance and a live-gateway run are not claimed here.


## Connector 0.3.0 reporting candidate

Pairing keeps its existing capability checks. Reporting independently requires
in-process live session/subagent registries and explicit `fleet-liveops` component
consent. Schema 1, the old endpoint and existing device-session/TLS gates remain.
Only GET is allowed for reporting. The package excludes push and approval responders.

Multiple separately launched profiles are supported. A backend hosting secondary
profile sessions or a hosted plugin reports unsupported until a safe observation
surface is available; isolation is never changed. Reporting-only profiles do not
enable the connector's general registrations. See [REPORTING.md](REPORTING.md).

Local synthetic tests must establish each runtime checkpoint separately. Passing
an import/capability test does not establish physical phone acceptance or public
catalog acceptance. The collector's private read-only core imports are disclosed
and can break on future Hermes releases; no core runtime objects are replaced.
