# MacBook and Mini physical test handoff

Preserve all existing profiles, configuration, memory, skills, credentials, services, saved Fleet gateways and pins. No Mini access was performed. This package does not update/reset either host's core checkout.

## MacBook

1. Verify bundle/archive checksums and the pinned source in the handoff. Inspect README, source and the prepared catalog disclosures. Stock v0.21.6 local installation/enablement and TLS/RPC tests were exercised in disposable profiles; this is not a live-profile activation receipt.
2. Inspect the selected live source and runtime. The observed MacBook checkout was clean but custom `ecd22716c35c6993797808cfd9c05feb8fa0193f`; it is not the supported stock target. Do not replace it automatically. A genuine stock gateway test needs a reviewed stock runtime/profile plan that preserves the current gateway and bot ownership. Do not start another Telegram poller with the same bot credentials while the current one is running. Local setup cannot count as Telegram acceptance.
3. Earlier host GUI/Aqua and qrencode/Quick Look checks do not validate the 0.1.2 Safari viewer. The dedicated GUI test is currently failing under diagnosis; no passing GUI test is claimed. Existing Tailscale Serve was configured privately with Funnel false. A trusted HTTPS GET to the existing origin returned HTTP 401 with successful certificate validation. That origin currently belongs to another service; the connector-origin and phone path remain unverified. Do not repoint the existing service. Review a distinct private origin/port forwarding plan first.
4. On the compatible selected host, follow supported README install/enable, gateway restart, `/fleetsetup`, local binding/setup approval, process activation, and private forwarding verification. `/fleetstatus` must show that the static prerequisites pass. If its only blocker is `current_owner_observation_required`, `/fleetqr` is the intended next step: the normal flow opens Safari preflight and refreshes the current phone/private-screen observations there. No Terminal observation refresh is required; the optional CLI `observe` command remains for advanced owner-run workflows. The current owner binding is preserved.
5. Send `/fleetqr` from your real private owner account. In Safari, verify the phone opens the configured private HTTPS health address without a certificate warning and confirm that the host screen is private. The two-minute QR is minted and shown only after the preflight page paints, those checks are confirmed, and readiness is rechecked. Scan in existing DEV 0.1.0 build 12 using its additional-server flow; keep saved gateways. Review host/scope/trust and explicitly Connect this phone. Verify authenticated connection, real read RPC, relaunch/reconnect and preserved existing data. This gate is owner-performed and has not passed.
6. Record test outcome without links/secrets. `/fleetdevices`, revoke only the test device, verify disconnect/new-login refusal, and use README rollback if required. No publication until you explicitly confirm this test passed.

## Mini (prepare first; switch only when authorized)

1. Transfer the same bundle/archive over an approved private path and verify hashes before execution. Never assume the MacBook's paths/settings are valid there.
2. Read-only inspect `git status --short --branch`, `git worktree list`, exact HEAD/version/runtime and effective isolation. Prior supplied Mini evidence was dirty `dce1e9b37581dd62e480a9064dc04a709c2940d3`, issuer absent, existing loopback 9119 and private Tailscale HTTPS forwarding unverified. Preserve every modification. This connector refuses that unsupported source; do not patch, reset or blindly update it.
3. Resolve the host runtime/GUI/private forwarding prerequisites in an isolated reviewed plan. The reported launcher install-lock problem is not authorization to chmod/bypass it. Check authenticated service readiness with the supported runtime, not merely `/api/health`. No QR can be issued in a headless/SSH-only GUI context. Quick Look is not required; Safari and a private screen are.
4. Only after explicit Mini access authorization, repeat the same install/setup/status/Telegram/DEV12 checklist there. Bind Mini's real bot/account/profile; no settings copy from MacBook. Keep phone data/pins. Revoke only Mini's test attempt and unload only its exact connector label for rollback.

### Generic Telegram setup request (truthful before publication)

> Help me set up Hermes Fleet with the reviewed Hermes Fleet connector. First inspect this host's real Hermes version/source, profile, effective plugin isolation, existing gateway/bot ownership, Safari GUI capability and private trusted HTTPS path. The connector is currently unpublished: use only the locally supplied repository/bundle after verifying its full commit and checksum from my handoff; do not invent a release URL or claim catalog approval. Install through the supported Hermes plugin CLI with legitimate dependency/code approvals. Preserve existing data/services/core changes. If this is compatible stock v0.21.6, guide me through enabling/restarting only the selected gateway, sending /fleetsetup so the bridge captures my genuine Telegram metadata, reviewing/approving local owner binding and service activation, and verifying private forwarding. Stop at a concrete static prerequisite blocker. If `/fleetstatus` reports only `current_owner_observation_required`, I will explicitly send `/fleetqr`; the private Safari preflight will guide the phone TLS and screen checks before any invitation is minted. The existing numeric owner binding must remain intact. I will scan and tap Connect this phone. Do not send the bearer link in Telegram, publish anything, update custom core, or clear Fleet data.

After publication but before catalog merge, substitute the **actually published** connector Git URL with the reviewed full SHA; this is still an unreviewed custom-source install, not catalog trust. After a reviewed catalog merge, `hermes plugins install hermes-fleet-connector` resolves the immutable catalog pin. Publication and discovery remain untested at this point.


## Combined reporting candidate

Run the existing full connector suite as well as `tests/test_reporting*.py`.
The reporting tests cover legacy schema-1 aggregation, duplicate PID preference,
expiry, idle/unavailable distinction, caps, profile scope, real Hermes dashboard
discovery, consent, router lifespan, GET-only proxy, retained credentials, safe
404 diagnostics, migration previews/rollback/concurrent edits, and deterministic
source packaging. Use a disposable HERMES_HOME and explicit read-only Hermes source
on PYTHONPATH. Never point tests at the installed profile or service.

The GitHub reporting workflow uses three immutable Hermes source checkpoints on
macOS and Linux. Its prepared archives are not uploaded or published. Physical
phone, live gateway, OS-specific and catalog acceptance remain separate gates.
