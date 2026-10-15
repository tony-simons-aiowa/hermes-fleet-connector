# Hermes Fleet Connector

Version 0.1.5 is an independently maintained Hermes plugin and companion service for owner-authorized phone pairing with Hermes Fleet. It registers native Telegram commands and does not modify Hermes core.

## Compatibility

- Verified against clean official Hermes v0.21.6 (`818c13be1dc4fd28987e1e881a9408224afd4535`) and v0.22.1 (`297552a9d646119871391d202509822cd86330df`) with Python 3.14. Plugin registration, setup and service startup require one of these exact source commits with no tracked changes; other versions, commits, and dirty tracked trees are refused.
- Hermes's manifest syntax supports comma-separated AND clauses, not a disjunction, so the range admits versions from `>=0.21.6,<0.22.2`. Registration checks Hermes's reported version, commit and dirty state before adding a handler or auth provider; the exact source-commit allowlist also gates setup and service startup. Versions in between are not supported.
- Requires effective `plugins.isolation: in_process`; the connector does not change isolation settings.
- Supported desktop display paths are macOS and Linux with a logged-in local GUI session, a private browser window, a private screen, and the existing `qrencode` command. Headless/SSH-only display contexts are refused.
- The WSL-to-Windows Edge adapter is experimental and is not part of the supported platform matrix. End-to-end WSL browser paint has not been verified. Native Windows is not supported.

## Installation

The v0.1.5 release includes `hermes-fleet-connector-0.1.5-release-metadata.txt`. Its `Source commit` field is the immutable 40-character source pin. The metadata is a release asset, so the source commit does not need to contain its own hash.

```sh
SOURCE_URL="https://github.com/tony-simons-aiowa/hermes-fleet-connector.git"
RELEASE_METADATA_URL="https://github.com/tony-simons-aiowa/hermes-fleet-connector/releases/download/v0.1.5/hermes-fleet-connector-0.1.5-release-metadata.txt"
RELEASE_SHA="$(curl -fsSL "$RELEASE_METADATA_URL" | awk -F': ' '$1 == "Source commit" { print $2; exit }')"
printf '%s\n' "$RELEASE_SHA" | grep -Eq '^[0-9a-f]{40}$' || { echo "Invalid or missing v0.1.5 source SHA" >&2; exit 1; }
hermes plugins install "$SOURCE_URL" --ref "$RELEASE_SHA" --yes-deps --no-enable
hermes plugins enable hermes-fleet-connector
hermes plugins show hermes-fleet-connector
```

Review the plugin and dependency trust prompts. Do not enable it in a profile using host isolation. Installation does not start the service or issue an invitation.

## Updating an existing install

A release installed with `--ref` is recorded as an exact source pin. For that install, `hermes plugins update hermes-fleet-connector` intentionally refuses to move the pin. Update it by resolving the new release SHA from its metadata and force-reinstalling that exact commit:

```sh
SOURCE_URL="https://github.com/tony-simons-aiowa/hermes-fleet-connector.git"
RELEASE_METADATA_URL="https://github.com/tony-simons-aiowa/hermes-fleet-connector/releases/download/v0.1.5/hermes-fleet-connector-0.1.5-release-metadata.txt"
RELEASE_SHA="$(curl -fsSL "$RELEASE_METADATA_URL" | awk -F': ' '$1 == "Source commit" { print $2; exit }')"
printf '%s\n' "$RELEASE_SHA" | grep -Eq '^[0-9a-f]{40}$' || { echo "Invalid or missing v0.1.5 source SHA" >&2; exit 1; }
hermes plugins install "$SOURCE_URL" --force --ref "$RELEASE_SHA" --yes-deps --no-enable
hermes plugins show hermes-fleet-connector
```

Hermes stages and validates the replacement manifest, substitutes that staged tree for the installed connector while preparing the enabled dependency set, then publishes the replacement. This is the supported recovery path when the old connector's manifest rejects the current Hermes version. It does not start the connector or change its enabled selection. Use `hermes plugins list` to inspect the Source column before choosing the path: `git pinned@<sha>` is an exact custom pin and requires the command above; an unpinned Git install can use `hermes plugins update`; `catalog:<tier>@<sha>` follows its reviewed catalog pin and can move to v0.1.5 after the catalog entry is updated.

## Owner flow

The selected profile must already have a working Telegram gateway, established access policy, a verified private HTTPS origin with trusted certificates, and the required local display prerequisites. Use `/fleetsetup` privately and approve the captured owner binding on the host. Review the generated service configuration before activating it. The connector never sends the invitation bearer through Telegram.

The phone invitation is single-use, short-lived (120 seconds), and scoped to `fleet:operator`, which grants broad dashboard operator access rather than read-only access. The owner reviews and approves the phone connection. Closing, expiry, or display failure cancels an unredeemed invitation.

## Development tests

With a clean official Hermes v0.21.6 or v0.22.1 checkout available, run:

```sh
PYTHONPATH=/path/to/clean/hermes-agent:. HERMES_STOCK_ROOT=/path/to/clean/hermes-agent python -m pytest tests
```

The suite uses synthetic fixtures and disposable profiles. It does not replace owner acceptance, browser paint, or device pairing.
