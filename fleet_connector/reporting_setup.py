"""Explicit local reporting configuration/migration. Never install or restart.

Dry-run is the default. Existing profile selections and connector authority are
independent. Recovery copies live outside plugin discovery and never contain
device stores, credentials, .env files, or session snapshots.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import uuid

CONSENT = "fleet-liveops"
PACKAGE = "hermes-fleet-connector"
SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


class SetupError(ValueError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise SetupError("Expected an owner-controlled regular file; no changes made")
    return path.read_bytes(), stat.S_IMODE(info.st_mode)


def directory(path, *, create=False):
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise SetupError("Expected an owner-controlled directory; no changes made")


def yaml_document(raw):
    from ruamel.yaml import YAML
    yaml = YAML()
    yaml.preserve_quotes = True
    doc = yaml.load(raw.decode("utf-8"))
    if not isinstance(doc, dict):
        raise SetupError("Invalid profile configuration; no changes made")
    plugins = doc.get("plugins", {})
    if not isinstance(plugins, dict):
        raise SetupError("Invalid plugin configuration; no changes made")
    for key in ("enabled", "disabled"):
        values = plugins.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise SetupError("Invalid plugin selection; no changes made")
    return yaml, doc


def enabled(raw):
    _, doc = yaml_document(raw)
    plugins = doc.get("plugins", {})
    return CONSENT in plugins.get("enabled", []) and CONSENT not in plugins.get("disabled", [])


def root_home(home):
    home = Path(home).absolute()
    directory(home)
    if home.parent.name == "profiles" and (home.parent.parent / "config.yaml").is_file():
        return home.parent.parent
    return home


def profiles(root):
    directory(root)
    regular(root / "config.yaml")
    result = {"default": root}
    parent = root / "profiles"
    if parent.exists() or parent.is_symlink():
        directory(parent)
        for path in sorted(parent.iterdir()):
            if path.name == ".deleted":
                continue
            if not SLUG.fullmatch(path.name) or path.name == "default":
                raise SetupError("Unsupported profile name; no changes made")
            directory(path)
            if (path / "config.yaml").exists():
                regular(path / "config.yaml")
                result[path.name] = path
    return result


def _enable(raw, allow_disabled):
    yaml, doc = yaml_document(raw)
    plug = doc.setdefault("plugins", {})
    if CONSENT in plug.get("disabled", []) and not allow_disabled:
        raise SetupError("Reporting is explicitly disabled; select --enable-disabled to change that choice")
    if enabled(raw):
        return raw
    values = plug.setdefault("enabled", [])
    if CONSENT not in values:
        values.append(CONSENT)
    if CONSENT in plug.get("disabled", []):
        plug["disabled"].remove(CONSENT)
    out = io.StringIO()
    yaml.dump(doc, out)
    return out.getvalue().encode()


def _legacy_directories(homes):
    found = []
    for name, home in homes.items():
        parent = home / "plugins"
        if not parent.exists():
            continue
        directory(parent)
        for path in sorted(parent.iterdir()):
            if not path.is_dir() and not path.is_symlink():
                continue
            directory(path)
            manifest = path / "dashboard/manifest.json"
            if not manifest.exists():
                continue
            directory(path / "dashboard")
            data = json.loads(regular(manifest)[0])
            if data.get("name") != CONSENT or path.name == PACKAGE:
                continue
            _, owner = yaml_document(regular(path / "plugin.yaml")[0])
            if owner.get("name") != CONSENT:
                raise SetupError("A different plugin owns the reporting namespace; review it before migration")
            version = tuple(int(v) for v in str(owner.get("version", "")).split("."))
            if len(version) != 3 or version > (0, 3, 0):
                raise SetupError("Unknown reporter version; review it before migration")
            # No opt-in push enrollment/control capability is migrated or removed.
            if version >= (0, 3, 0) and (root_home(home) / CONSENT / "push.json").exists():
                raise SetupError("Existing push configuration needs separate review before migration")
            found.append((name, path))
    return found


def plan(home, selected=None, gateway=None, *, allow_disabled=False, migrate=False):
    root = root_home(home)
    if Path("/etc/hermes/config.yaml").exists():
        raise SetupError("Managed Hermes policy requires administrator review; no changes made")
    homes = profiles(root)
    gateway = gateway or next((n for n, p in homes.items() if p == Path(home).absolute()), None)
    if gateway not in homes:
        raise SetupError("Select the actual gateway profile")
    raw = {n: regular(p / "config.yaml") for n, p in homes.items()}
    selected = list(dict.fromkeys(selected if selected is not None else
                                  [n for n, (data, _) in raw.items() if enabled(data)]))
    if any(n not in homes for n in selected):
        raise SetupError("Select existing profiles; no changes made")
    # An aggregator must be enabled in the gateway profile too; never assume default.
    targets = list(dict.fromkeys([*selected, gateway]))
    changes = {}
    for name in targets:
        before, mode = raw[name]
        after = _enable(before, allow_disabled)
        if after != before:
            changes[name] = (before, after, mode)
    legacy = _legacy_directories(homes) if migrate else []
    return {"root": root, "homes": homes, "gateway": gateway, "selected": selected,
            "changes": changes, "legacy": legacy, "observed": raw}


def atomic_write(path, raw, mode):
    temp = path.with_name(".fleet-reporting-" + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def apply(preview):
    root, homes = preview["root"], preview["homes"]
    # Do not accidentally turn a source checkout into an installation command.
    installed = root / "plugins" / PACKAGE
    directory(installed)
    package = json.loads(regular(installed / "dashboard/manifest.json")[0])
    if package.get("name") != CONSENT or package.get("version") != "0.3.0":
        raise SetupError("Install the combined connector through Hermes before applying this plan")
    if not preview["changes"] and not preview["legacy"]:
        return None
    for name, (before, _) in preview["observed"].items():
        if regular(homes[name] / "config.yaml")[0] != before:
            raise SetupError("Settings changed during review; make a new plan")
    backups = root / "backups"
    directory(backups, create=True)
    backup = backups / ("fleet-reporting-" + uuid.uuid4().hex)
    directory(backup, create=True)
    journal = {"version": 1, "changes": {}, "legacy": [], "complete": False}
    for name, (before, after, mode) in preview["changes"].items():
        atomic_write(backup / (name + ".yaml"), before, 0o600)
        journal["changes"][name] = {"before": digest(before), "after": digest(after), "mode": mode}
    for index, (name, path) in enumerate(preview["legacy"]):
        journal["legacy"].append({"profile": name, "directory": path.name, "backup": f"plugin-{index}"})
    atomic_write(backup / "migration.json", (json.dumps(journal, indent=2) + "\n").encode(), 0o600)
    moved, written = [], []
    try:
        for entry in journal["legacy"]:
            source = homes[entry["profile"]] / "plugins" / entry["directory"]
            os.rename(source, backup / entry["backup"])
            moved.append(entry)
        for name, (before, after, mode) in preview["changes"].items():
            target = homes[name] / "config.yaml"
            if regular(target)[0] != before:
                raise SetupError("Settings changed during migration")
            atomic_write(target, after, mode)
            written.append(name)
        journal["complete"] = True
        atomic_write(backup / "migration.json", (json.dumps(journal, indent=2) + "\n").encode(), 0o600)
    except Exception:
        for name in reversed(written):
            before, after, mode = preview["changes"][name]
            target = homes[name] / "config.yaml"
            if regular(target)[0] == after:
                atomic_write(target, before, mode)
        for entry in reversed(moved):
            source = homes[entry["profile"]] / "plugins" / entry["directory"]
            if not source.exists():
                os.rename(backup / entry["backup"], source)
        raise SetupError("Migration did not finish; recovery copies retained, concurrent settings preserved") from None
    return backup


def rollback(home, backup, *, apply_changes=False):
    root = root_home(home)
    backup = Path(backup).absolute()
    if backup.parent != root / "backups" or not backup.name.startswith("fleet-reporting-"):
        raise SetupError("Choose a reporting backup in this Hermes root")
    directory(root / "backups")
    directory(backup)
    journal = json.loads(regular(backup / "migration.json")[0])
    if journal.get("version") != 1 or not journal.get("complete"):
        raise SetupError("Incomplete backup needs manual recovery review")
    homes = profiles(root)
    changes, moves = [], []
    for name, entry in journal["changes"].items():
        if name not in homes:
            raise SetupError("A profile changed since migration")
        target = homes[name] / "config.yaml"
        before = regular(backup / (name + ".yaml"))[0]
        if digest(before) != entry["before"] or digest(regular(target)[0]) != entry["after"]:
            raise SetupError("Settings changed since migration; preserve them and reconcile recovery manually")
        changes.append((target, before, entry["mode"], regular(target)[0]))
    for entry in journal["legacy"]:
        if (entry["profile"] not in homes or not SLUG.fullmatch(entry["directory"])
                or not re.fullmatch(r"plugin-[0-9]+", entry["backup"])):
            raise SetupError("Invalid recovery manifest")
        target = homes[entry["profile"]] / "plugins" / entry["directory"]
        directory(target.parent)
        directory(backup / entry["backup"])
        if target.exists() or target.is_symlink():
            raise SetupError("A reporting installation already occupies the recovery path")
        moves.append((backup / entry["backup"], target))
    if apply_changes:
        # Caller must first downgrade the combined package through Hermes. Otherwise
        # restoring the old namespace would leave two discoverable owners.
        installed = root / "plugins" / PACKAGE / "dashboard/manifest.json"
        if installed.exists():
            raise SetupError("Restore the previous connector package through Hermes before rollback")
        written, moved = [], []
        try:
            for target, before, mode, after in changes:
                if regular(target)[0] != after:
                    raise SetupError("Settings changed during recovery")
                atomic_write(target, before, mode)
                written.append((target, before, mode, after))
            for source, target in moves:
                os.rename(source, target)
                moved.append((source, target))
        except Exception:
            for source, target in reversed(moved):
                if not source.exists():
                    os.rename(target, source)
            for target, before, mode, after in reversed(written):
                if regular(target)[0] == before:
                    atomic_write(target, after, mode)
            raise SetupError("Recovery did not finish; migration state and concurrent settings retained") from None
    return len(changes), len(moves)


def missing_route_diagnostic(home):
    """Only after an authenticated upstream 404; no backend call or policy bypass.

    A raw local config cannot establish effective managed enablement. Unknown
    layouts/policy return no diagnosis. Never expose paths, settings or secrets.
    """
    try:
        if Path("/etc/hermes/config.yaml").exists():
            return None
        home = Path(home)
        raw = regular(home / "config.yaml")[0]
        _, doc = yaml_document(raw)
        plugins = doc.get("plugins", {})
        root = root_home(home)
        installed = root / "plugins" / PACKAGE
        manifest = json.loads(regular(installed / "dashboard/manifest.json")[0])
        if manifest.get("name") != CONSENT:
            return None
        if not enabled(raw):
            return "disabled", "profile_disabled"
        if plugins.get("isolation") == "host":
            return "unsupported", "host_isolation"
        return "restart_required", "route_not_loaded"
    except Exception:
        return None


def add_arguments(parser):
    sub = parser.add_subparsers(dest="reporting_command")
    sub.add_parser("status", help="inspect reporting profile selections; no changes")
    for name in ("configure", "migrate"):
        p = sub.add_parser(name, help="preview local reporting changes (dry-run by default)")
        p.add_argument("--profiles", nargs="+", help="enable these profiles; preserve other selections")
        p.add_argument("--gateway-profile", help="actual gateway profile, default is the current profile")
        p.add_argument("--enable-disabled", action="store_true", help="explicitly re-enable selected reporting")
        p.add_argument("--apply", action="store_true", help="apply the reviewed local changes; never restart")
    p = sub.add_parser("rollback", help="preview recovery after restoring the previous connector")
    p.add_argument("backup")
    p.add_argument("--apply", action="store_true")


def run(args, home, out):
    try:
        command = args.reporting_command or "status"
        if command == "status":
            homes = profiles(root_home(home))
            selected = [name for name, path in homes.items() if enabled(regular(path / "config.yaml")[0])]
            print("Reporting profiles: " + (", ".join(selected) or "none") +
                  ". Configuration only; verify live reporting in Fleet. No changes made.", file=out)
            return 0
        if command == "rollback":
            counts = rollback(home, args.backup, apply_changes=args.apply)
            print(f"Recovery: {counts[0]} profile settings, {counts[1]} reporter copies. "
                  + ("Applied; restart affected backends when idle." if args.apply else "Preview only."), file=out)
            return 0
        preview = plan(home, getattr(args, "profiles", None), getattr(args, "gateway_profile", None),
                       allow_disabled=getattr(args, "enable_disabled", False), migrate=command == "migrate")
        print("Reporting profiles: " + (", ".join(preview["selected"]) or "none") +
              "; gateway: " + preview["gateway"], file=out)
        print(f"Changes: {len(preview['changes'])} profile settings, {len(preview['legacy'])} reporter copies. "
              "Connector authority and pairing stores are preserved.", file=out)
        if command != "status" and args.apply:
            backup = apply(preview)
            print("No changes needed." if backup is None else f"Recovery copy: {backup}", file=out)
            print("Restart affected Hermes backends when idle, then check reporting in Fleet. No service was restarted.", file=out)
        else:
            print("Preview only; no settings, plugins or services changed.", file=out)
        return 0
    except Exception as exc:
        # Known classifications only; YAML parser exceptions may contain config values.
        print(str(exc) if isinstance(exc, SetupError) else "Reporting setup unavailable; no service was restarted.", file=out)
        return 1


if __name__ == "__main__":
    import argparse
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    add_arguments(parser)
    args = parser.parse_args()
    raise SystemExit(run(args, args.home, sys.stdout))
