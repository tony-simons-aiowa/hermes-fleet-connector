import importlib.util
import io
import json
import hashlib
from pathlib import Path
import tarfile

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("release_builder", ROOT / "scripts/build_release.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_source_archive_is_deterministic_contains_one_plugin_and_no_private_or_push_state():
    paths = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file()]
    paths += [".env", "plugin-data/devices.sqlite3", "tests/sensitive.json"]
    first = builder.build("0" * 40, paths, lambda p: (ROOT / p).read_bytes())
    assert first == builder.build("0" * 40, paths, lambda p: (ROOT / p).read_bytes())
    with tarfile.open(fileobj=io.BytesIO(first[1])) as archive:
        files = {p.name: archive.extractfile(p).read() for p in archive.getmembers()}
    assert sum(name.endswith("/plugin.yaml") for name in files) == 1
    assert any(name.endswith("/dashboard/plugin_api.py") for name in files)
    assert any(name.endswith("/fleet_connector/reporting.py") for name in files)
    assert not any("push_sender" in name or ".env" in name or "devices.sqlite3" in name for name in files)
    provenance = json.loads(next(data for name, data in files.items() if name.endswith("/release.json")))
    assert provenance["source_sha"] == "0" * 40 and provenance["version"] == "0.3.0"
    assert provenance["schema"] == 1
    prefix = "hermes-fleet-connector-0.3.0/"
    for entry in provenance["files"]:
        assert hashlib.sha256(files[prefix + entry["path"]]).hexdigest() == entry["sha256"]


def test_mismatched_component_version_refuses_archive():
    paths = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file()]
    def read(path):
        raw = (ROOT / path).read_bytes()
        return raw.replace(b"version: 0.3.0", b"version: 0.2.0") if path == "plugin.yaml" else raw
    with pytest.raises(ValueError, match="versions differ"):
        builder.build("0" * 40, paths, read)
