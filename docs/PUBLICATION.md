# Publication gate and maintained distribution

Connector 0.3.0 is a combined reporting candidate. Preparing and testing its
source does not install it, restart services, publish a release, or update the
catalog. Prior 0.1.4/0.1.5 public releases and a catalog listing already exist;
their pins do not identify this candidate.

Before a later publication request: retain source/runtime-specific test evidence,
run normal plugin validation/security review without overriding a blocked scan,
confirm real phone/pairing continuity and selected-profile reporting acceptance,
and review the observational private-runtime API compatibility risk.

After separate approval, build `scripts/build_release.py --sha <full-commit>
--output <directory>`, verify the archive/checksums and fresh-host retrieval, then
update the existing `plugin-catalog/hermes-fleet-connector.yaml` entry at the exact
published SHA/version. Disclose shared local snapshot storage, selected-profile
session reads, background publishers, in-process requirements and unsupported
hosted/multiplexed cases. Preserve the existing connector's broader operator-access
disclosure; reporting adds no control power.

One listing and one installed package. No self-updater, core patch, wildcard proxy,
isolation downgrade, new credentials, or new approval/control path. Public catalog
acceptance remains an upstream review decision. Installation, service restarts,
upload and publication are separate owner gates.

## 0.3.0 publication scope

The owner authorized connector publication and a catalog update on October 17,
2026. This release integrates the locally installed and validated source
50990a79bf650d2c54f3f705576e304b8c0f336d into public main history; publication
documentation is the only additional change. Local acceptance at that source
included 390 passing tests, normal plugin validation, source/archive secret scans,
and an existing paired iPhone reconnecting with nine healthy reporting processes.
That observation covered the local MacBook and idle reporting, not Linux, all
remote gateways, or active agent/delegation acceptance. The public CI matrix
provides separate OS/runtime evidence.

The connector retains broad operator access for paired phones; observational
reporting does not reduce that existing authority. It reads selected profiles'
session registries and stores shared local snapshots. Private registry imports
are compatibility risks and are disclosed for catalog review. The owner-only
local socket additionally supports explicit idle/retirement checks; these are
not phone or remote maintenance endpoints. No pairing records, credentials,
selected-profile consent, or live services are changed by publishing source.
