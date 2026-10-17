# Preserve an existing gateway with live plugin activation

Version 0.1.1 adds an explicit `existing_gated` backend mode. It uses the supported dashboard session-provider API to authorize a host-only connector lease against an already-running, authenticated Hermes dashboard backend. It does not start another Telegram poller, start another backend, migrate the existing profile database, or modify core/LaunchAgents. Install/enable can hot-load native handlers through the supported gateway control socket on compatible versions; verify the live activation receipt and original gateway PID instead of assuming a restart is necessary.

The live integration seams were audited against stock v0.21.6 and the retained local gateway source `ecd22716c35c6993797808cfd9c05feb8fa0193f` (reported runtime base version 0.21.6). The retained local source is NOT represented as a stock installation; the separate stock compatibility test remains necessary for the fresh-host product claim. The connector's phone contract never calls the old custom issuer.

The provider `fleet-connector-backend` accepts only a random connector-owned `hfcproxy.` session token whose SHA256 digest is stored under its own owner-only data directory. Its lease expires after 35 seconds, renews every 5 seconds while the connector runs, is profile-bound, and requires the connector process alive. It has no password/OAuth/refresh login and never accepts HFD1/device/session credentials from the phone. Stock Hermes verifies it through `register_dashboard_auth_provider`; the connector makes local HTTP requests with it and stock gated WebSocket requests using its supported session-token path. The frontend still independently authenticates each phone request and enforces its own HTTP scope. This is an additional host-to-host proxy session using the supported auth seam, not a replacement phone authentication protocol. No API_SERVER_KEY or existing administrative credential is read.

Setup after a genuine `/fleetsetup` event and explicit host authorization:

```sh
"$FLEET_PYTHON" -m fleet_connector --home "$HERMES_HOME" setup \
  --hermes-root "$EXISTING_AUDITED_HERMES_ROOT" \
  --backend-mode existing_gated --backend-port 9119 --port 9129 \
  --origin "$SEPARATE_PRIVATE_HTTPS_ORIGIN" \
  --from-telegram-request --approve-owner-binding
```

The existing backend must be gated and must actually load this plugin/provider. A valid protected-route response using the connector lease is required for readiness; `/api/health`, version labels and a plugin directory alone do not prove that. Authorization failure is not a reason to disable the gate or fall back to a broad key. The service's process receipt says `backend_owned: false`; stopping/rolling back this connector never stops the existing backend.

For a separate Tailscale Serve route on a previously unused HTTPS port:

```sh
tailscale serve --bg --https=8443 --yes http://127.0.0.1:9129
# Verify the exact existing 443 mapping remains byte-equivalent in serve status --json.
# Scope rollback to this new listener only:
tailscale serve --https=8443 off
```

Use only a port confirmed free in the current Serve configuration. No Serve reset, Funnel, firewall/router change, public exposure or TLS weakening. Verify trusted TLS and the exact connector instance through the new route, followed by actual phone/private-screen observations. The same hostname on port 8443 is a separate HTTPS origin for pairing and pinning. Do not include private hostnames or bearer QR links in public evidence.
