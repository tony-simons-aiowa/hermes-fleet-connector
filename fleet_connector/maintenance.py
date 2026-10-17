"""Owner-socket maintenance using the connector's existing host-only lease."""
import json
import re

import httpx

_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_UNAVAILABLE = {"ok": False, "message": "Backend maintenance unavailable; backend left running."}


def backend_maintenance(connector, proof):
    """Inspect or fence the configured backend; never expose its credential or signal it."""
    operation = proof.get("operation", "inspect")
    if proof.get("local_operator") is not True or (not isinstance(operation, str) or operation not in {"inspect", "prepare", "commit", "cancel"}):
        return {"ok": False, "message": "Local backend maintenance request refused."}
    expected = proof.get("expected_pid")
    token = proof.get("token")
    if operation != "inspect" and (type(expected) is not int or expected <= 0):
        return {"ok": False, "message": "Backend process identity required."}
    if operation in {"commit", "cancel"} and (not isinstance(token, str) or not _TOKEN.fullmatch(token)):
        return {"ok": False, "message": "Backend retirement permit required."}

    def exchange(client, method, path, body=None):
        with client.stream(method, connector.backend + path, json=body) as response:
            if response.status_code != 200:
                raise ValueError("Backend request refused")
            data = bytearray()
            for chunk in response.iter_bytes(chunk_size=4096):
                data.extend(chunk)
                if len(data) > 8192:
                    raise ValueError("Oversized backend response")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError("Invalid backend response")
            return result

    try:
        with httpx.Client(headers=connector.backend_headers(), timeout=5, trust_env=False,
                          follow_redirects=False) as client:
            identity = exchange(client, "GET", "/api/host/identity")
            pid, role = identity.get("pid"), identity.get("role")
            if (identity.get("ok") is not True or type(pid) is not int or pid <= 0
                    or role not in {"serve", "desktop-serve"} or (expected is not None and expected != pid)):
                return {"ok": False, "message": "Backend process identity changed or unavailable."}
            if operation == "inspect":
                result = exchange(client, "GET", "/api/health/idle")
            else:
                result = exchange(client, "POST", "/api/health/retirement",
                                  {"action": operation, **({"token": token} if operation != "prepare" else {})})
            output = {"ok": result.get("ok") is True, "pid": pid, "role": role}
            if operation in {"inspect", "prepare"}:
                idle = result.get("idle")
                output["idle"] = idle if type(idle) is bool else None
            if operation == "prepare" and output["ok"] and output["idle"] is True:
                permit = result.get("token")
                if not isinstance(permit, str) or not _TOKEN.fullmatch(permit):
                    return dict(_UNAVAILABLE)
                output["token"] = permit  # Only the private owner socket receives this retirement permit.
            return output
    except (httpx.HTTPError, ValueError, TypeError):
        return dict(_UNAVAILABLE)
