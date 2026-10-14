#!/usr/bin/env python3
"""Owner-visible, no-invitation Linux display smoke using the candidate viewer."""
from __future__ import annotations

import json
import sys
import time

from fleet_connector.viewer import PrivateViewer


def main() -> int:
    viewer = PrivateViewer(
        origin="https://fixture.invalid",
        observe=lambda: False,
        setup_seconds=45,
    )
    try:
        viewer.start()
        deadline = time.monotonic() + 30
        while not viewer.loaded and time.monotonic() < deadline:
            if viewer.phase in {"expired", "display_failed", "disconnected", "interrupted"}:
                break
            time.sleep(.1)
        if not viewer.loaded:
            print("Private preflight page did not acknowledge visible paint.")
            print("Sanitized viewer handshake diagnostics:")
            print(json.dumps(viewer.diagnostic_snapshot(), sort_keys=True))
            input("Inspect the dedicated test window if useful, close only that window normally, then press Enter to clean up: ")
            return 1
        print("Candidate viewer painted its non-pairing preflight page and test QR in a private browser window.")
        print("The QR encodes only the fixed PRIVATE DISPLAY CHECK label; it is not an invitation.")
        print("No phone check, observation, invitation bearer, Telegram action, or external origin was used.")
        input("Inspect the page, then press Enter to wait for its visible setup expiry: ")
        while viewer.phase not in {"expired", "display_failed", "disconnected", "interrupted"}:
            time.sleep(.1)
        print(f"Viewer terminal phase: {viewer.phase}. The preflight test QR should be cleared; terminal status should be visible.")
        input("Close only the candidate test window normally, then press Enter to stop the local viewer: ")
        return 0 if viewer.phase == "expired" else 1
    finally:
        # Only the loopback viewer and its owner-only bootstrap file are closed.
        # Browser processes/windows belong to the user and are never terminated.
        viewer.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted; no browser process was terminated.", file=sys.stderr)
        raise SystemExit(130)
