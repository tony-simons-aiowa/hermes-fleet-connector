# Hermes Fleet Connector

Version 0.1.4 is an independently maintained Hermes plugin and companion service for owner-authorized phone pairing with Hermes Fleet. It registers native Telegram commands and does not modify Hermes core.

## Compatibility

- Verified against unmodified stock Hermes v0.21.6 with Python 3.14. Other Hermes versions are refused until audited.
- Requires effective `plugins.isolation: in_process`; the connector does not change isolation settings.
- Supported desktop display paths are macOS and Linux with a logged-in local GUI session, a private browser window, a private screen, and the existing `qrencode` command. Headless/SSH-only display contexts are refused.
- The WSL-to-Windows Edge adapter is experimental and is not part of the supported platform matrix. End-to-end WSL browser paint has not been verified. Native Windows is not supported.

## Installation

The v0.1.4 release includes `hermes-fleet-connector-0.1.4-release-metadata.txt`. Its `Source commit` field is the immutable 40-character source pin. The metadata is a release asset, so the source commit does not need to contain its own hash.

```sh
SOURCE_URL="https://github.com/tony-simons-aiowa/hermes-fleet-connector.git"
RELEASE_METADATA_URL="https://github.com/tony-simons-aiowa/hermes-fleet-connector/releases/download/v0.1.4/hermes-fleet-connector-0.1.4-release-metadata.txt"
RELEASE_SHA="$(curl -fsSL "$RELEASE_METADATA_URL" | awk -F': ' '$1 == "Source commit" { print $2; exit }')"
printf '%s\n' "$RELEASE_SHA" | grep -Eq '^[0-9a-f]{40}$' || { echo "Invalid or missing v0.1.4 source SHA" >&2; exit 1; }
hermes plugins install "$SOURCE_URL" --ref "$RELEASE_SHA" --yes-deps --no-enable
hermes plugins enable hermes-fleet-connector
hermes plugins show hermes-fleet-connector
```

Review the plugin and dependency trust prompts. Do not enable it in a profile using host isolation. Installation does not start the service or issue an invitation.

## Owner flow

The selected profile must already have a working Telegram gateway, established access policy, a verified private HTTPS origin with trusted certificates, and the required local display prerequisites. Use `/fleetsetup` privately and approve the captured owner binding on the host. Review the generated service configuration before activating it. The connector never sends the invitation bearer through Telegram.

The phone invitation is single-use, short-lived (120 seconds), and scoped to `fleet:operator`, which grants broad dashboard operator access rather than read-only access. The owner reviews and approves the phone connection. Closing, expiry, or display failure cancels an unredeemed invitation.

## Development tests

With a clean official Hermes v0.21.6 checkout available, run:

```sh
PYTHONPATH=. HERMES_STOCK_ROOT=/path/to/clean/hermes-agent python -m pytest tests
```

The suite uses synthetic fixtures and disposable profiles. It does not replace owner acceptance, browser paint, or device pairing.
