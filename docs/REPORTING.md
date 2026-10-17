# Read-only Live Operations reporting

Connector 0.3.0 includes the observational Fleet reporter in the same installation.
It is a local candidate until separately validated and published. Existing public
connector releases and the catalog pin do not acquire this capability automatically.

The package ID stays `hermes-fleet-connector`. Its hidden dashboard extension is
named `fleet-liveops`, preserving `GET /api/plugins/fleet-liveops/snapshot`.
Hermes uses the dashboard name for consent as well as routing. The internal
`fleet-liveops` enabled-list entry is a component selection, not a second install.

## Configure or migrate

After a separately approved installation of this combined package through Hermes,
run these commands locally from the profile where the connector is enabled.
Reporting-only profiles intentionally do not register these commands. The commands
never restart a service.

- `hermes fleet reporting status` reads profile reporting selections.
- `hermes fleet reporting configure --gateway-profile <name> --profiles <names>`
  previews enabling reporting for those existing profiles plus the actual gateway.
- `hermes fleet reporting migrate --gateway-profile <name>` previews preserving
  every currently enabled reporting profile and retiring separate reporter copies.
- Add `--apply` only after reviewing the preview. An explicitly disabled selected
  profile additionally requires `--enable-disabled`; nothing clears that choice silently.

Use `default` for the root profile only when it actually hosts the gateway.
Omitting the gateway argument uses the profile invoking the command. Selecting
additional reporting profiles never enables the connector's general command,
Telegram-handler, or dashboard-auth registrations there. Other profile selections
are retained. Configuration-only status does not prove a backend is publishing.

Migration moves old reporter directories into private recovery directories under
the Hermes root's `backups/`, outside all plugin discovery roots. A renamed folder
inside `plugins/` remains discoverable by its dashboard manifest and is not safe.
Malformed configs, symlinks, conflicting namespace owners, unsupported reporter
versions, managed host policy, and concurrent edits fail closed. A legacy 0.3.0
push configuration requires separate review; no push state or responder is migrated.

Wait for running work to finish, then restart affected Desktop/profile backends and
the separate gateway backend if necessary. Mounted code remains in memory until
restart. Open each selected profile and check reporting in Fleet. The helper does
not install code, stop work, restart a backend, issue invitations, change owners,
or edit device stores, instance identity, TLS pins, scopes, or connector settings.

## Local maintenance

`hermes fleet backend-status` checks the configured backend's identity and idle
ledger through the connector's existing owner-only local socket. It uses the
connector's host-only lease; no browser session or dashboard login is required.
The local socket also supports the backend's prepare/commit/cancel retirement
handshake, bound to an expected PID. This operation never signals or restarts a
process. A maintainer must verify process ownership and its successful idle
retirement before requesting a scoped supervisor restart. Busy, unknown, auth
failure, missing endpoint, and changed process identity leave the backend running.
These maintenance operations are unavailable through the phone/reporting routes.

## Compatibility and coverage

Schema 1 and the mobile path remain stable. Frozen DEV19 decoder tests cover the
combined response; older reporters remain readable by DEV20. Optional `reporting`
metadata has its own version and fixed state/reason codes. No credentials, paths,
or raw exceptions appear there. A fresh publisher with zero sessions is healthy
idle; zero publishers, stalled publishers, corrupt data, or exceeded caps are not.

The collector reads live session and active-subagent registries once per second.
Files are owner-only and atomically replaced. Reports expire after eight seconds;
stalled live processes mark incomplete coverage. A per-process OS lock prevents
duplicate combined publishers; aggregation prefers combined over legacy reports
from the same PID without collapsing sessions across different processes/profiles.
Fresh legacy publishers keep DEV20 in restart-pending state until they transition;
DEV19 can continue reading the compatible legacy fields during that interval.

Reporting supports launch-profile Desktop/dashboard backends with the required
in-process registry APIs. A mixed-profile process, a secondary hosted profile, or
host-isolated plugin cannot establish that scope safely and reports unsupported.
No policy is weakened to gain access. Messaging bots and standalone CLI activity
are outside this reporting coverage. One healthy publisher does not establish
that all selected profiles have opened/restarted. Registry API compatibility is
checked independently of the connector's existing pairing capability.

The connector keeps the existing exact proxy path and device-session/TLS gates.
Reporting mutations and push routes are refused. Auth failures are never converted
to installation advice. Only a credential-accepted upstream 404, with no profile
query selector and a verified combined installation, may receive configuration-only
disabled/restart diagnostics. Missing evidence remains unknown.

This component contains no approval responder, push sender, tool hook, execution,
steering, interruption, or session attachment. `fleet:` IDs remain observation-only.
Existing app controls retain their original RPC and presence/attachment gates.

## Rollback

Retain the reported private recovery directory. When idle, restore the previous
connector package through Hermes first, then preview
`hermes fleet reporting rollback <backup>` and apply with `--apply`. Older connectors do not have this helper. After downgrading, invoke it from a
retained candidate checkout with the Hermes Python runtime and that checkout on
PYTHONPATH: `python -m fleet_connector.reporting_setup --home <profile-home>
rollback <backup> --apply`. Alternatively reconcile the recovery files locally. Changes after migration are never overwritten silently:
a conflict requires manual reconciliation. Restart affected backends when idle.
Restoring the reporter does not require resetting or re-pairing devices.

Do not delete the shared snapshot directory or push state. Never restart or stop
an existing backend merely because the connector records `backend_owned=false`.

## Release gates

Run synthetic reporting, migration, proxy/pairing, real Hermes discovery and frozen
DEV19 compatibility tests. Validate supported Hermes runtimes and OSes independently.
Private runtime imports remain an upstream compatibility/catalog-review risk.
No core functions or registries are rebound. Update the existing catalog listing's
exact SHA/version/disclosures only after separate publication approval.
