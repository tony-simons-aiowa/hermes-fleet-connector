#!/usr/bin/env python3
"""Build a deterministic source archive from an exact commit; never publish."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile


def release_files(paths):
    for name in sorted(paths):
        path = Path(name)
        if (name in {"plugin.yaml", "__init__.py", "LICENSE", "NOTICE.md", "README.md"}
                or path.parts[0] == "docs" and path.suffix == ".md"
                or path.parts[0] == "fleet_connector" and path.suffix in {".py", ".png"}
                or name in {"dashboard/manifest.json", "dashboard/plugin_api.py", "dashboard/index.js"}):
            yield name


def build(source_sha, paths, read):
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("Exact source commit required")
    files = {name: read(name) for name in release_files(paths)}
    manifest = json.loads(files["dashboard/manifest.json"])
    version = manifest["version"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or manifest["name"] != "fleet-liveops":
        raise ValueError("Invalid reporting manifest")
    if (f"version: {version}\n".encode() not in files["plugin.yaml"]
            or f'VERSION = "{version}"'.encode() not in files["fleet_connector/__init__.py"]):
        raise ValueError("Connector/reporting versions differ")
    if any(Path(name).name in {"push_sender.py", "push_store.py", "push_payload.py"} for name in files):
        raise ValueError("Push components do not belong in the observational package")
    files["release.json"] = (json.dumps({"schema": 1, "version": version, "source_sha": source_sha,
        "files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest()} for name, data in files.items()]},
        sort_keys=True, indent=2) + "\n").encode()
    prefix = f"hermes-fleet-connector-{version}"
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, data in sorted(files.items()):
                item = tarfile.TarInfo(prefix + "/" + name)
                item.size, item.mode, item.mtime = len(data), 0o644, 0
                archive.addfile(item, io.BytesIO(data))
    return prefix + ".tar.gz", output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    def git(*params):
        return subprocess.check_output(["git", "-C", str(repo), *params])
    sha = git("rev-parse", args.sha + "^{commit}").decode().strip()
    paths = git("ls-tree", "-r", "--name-only", sha).decode().splitlines()
    name, data = build(sha, paths, lambda path: git("show", sha + ":" + path))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / name).write_bytes(data)
    (args.output / "SHA256SUMS.txt").write_text(hashlib.sha256(data).hexdigest() + "  " + name + "\n", encoding="utf-8")
    print(f"Prepared {name} from {sha}; not installed or published")


if __name__ == "__main__":
    main()
