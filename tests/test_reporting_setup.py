import argparse
import io
import json
from pathlib import Path

import pytest

from fleet_connector import reporting_setup as S
from fleet_connector.cli import add_arguments, run


@pytest.fixture
def installation(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "# preserve comment\nplugins:\n  enabled: [hermes-fleet-connector, fleet-liveops]\n  disabled: []\nmodel: fixture\n")
    for name, selection in (("selected", "enabled: [fleet-liveops]"),
                            ("off", "enabled: []\n  disabled: [fleet-liveops]")):
        home = tmp_path / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("# profile\nplugins:\n  " + selection + "\nother: fixture\n")
    package = tmp_path / "plugins/hermes-fleet-connector/dashboard"
    package.mkdir(parents=True)
    (package / "manifest.json").write_text(json.dumps({"name": S.CONSENT, "version": "0.3.0"}))
    old = tmp_path / "plugins/fleet-liveops"
    (old / "dashboard").mkdir(parents=True)
    (old / "plugin.yaml").write_text("name: fleet-liveops\nversion: 0.1.0\n")
    (old / "dashboard/manifest.json").write_text(json.dumps({"name": S.CONSENT}))
    # These stand in for retained pairing state and must never be copied/edited.
    protected = tmp_path / "plugin-data/hermes-fleet-connector"
    protected.mkdir(parents=True)
    for name in ("settings.json", "devices.sqlite3", "fleet_instance_id"):
        (protected / name).write_bytes(b"fixture retained identity")
    return tmp_path


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_default_migration_preserves_selections_pairing_and_backs_up_outside_discovery(installation):
    home = installation
    before = tree(home)
    preview = S.plan(home, migrate=True)
    assert preview["selected"] == ["default", "selected"] and not preview["changes"]
    assert tree(home) == before
    backup = S.apply(preview)
    assert backup.parent == home / "backups"
    assert not (home / "plugins/fleet-liveops").exists()
    assert (backup / "plugin-0/dashboard/manifest.json").exists()
    for name in ("config.yaml", "profiles/off/config.yaml", "profiles/selected/config.yaml",
                 "plugin-data/hermes-fleet-connector/settings.json", "plugin-data/hermes-fleet-connector/devices.sqlite3",
                 "plugin-data/hermes-fleet-connector/fleet_instance_id"):
        assert (home / name).read_bytes() == before[name]
    assert S.apply(S.plan(home, migrate=True)) is None
    with pytest.raises(S.SetupError, match="previous connector"):
        S.rollback(home, backup, apply_changes=True)
    (home / "plugins/hermes-fleet-connector/dashboard/manifest.json").unlink()
    S.rollback(home, backup, apply_changes=True)
    assert (home / "plugins/fleet-liveops/plugin.yaml").read_bytes() == before["plugins/fleet-liveops/plugin.yaml"]


def test_named_gateway_and_explicit_disabled_choice_do_not_enable_connector_in_other_profiles(installation):
    home = installation
    with pytest.raises(S.SetupError, match="explicitly disabled"):
        S.plan(home, selected=["off"], gateway="selected")
    preview = S.plan(home, selected=["off"], gateway="selected", allow_disabled=True)
    S.apply(preview)
    _, doc = S.yaml_document((home / "profiles/off/config.yaml").read_bytes())
    assert doc["plugins"]["enabled"] == ["fleet-liveops"]
    assert doc["plugins"]["disabled"] == []
    assert "# profile" in (home / "profiles/off/config.yaml").read_text()
    assert S.enabled((home / "profiles/selected/config.yaml").read_bytes())


def test_concurrent_edit_refuses_before_any_mutation(installation):
    preview = S.plan(installation, migrate=True)
    config = installation / "config.yaml"
    config.write_text(config.read_text() + "new_setting: keep\n")
    before = tree(installation)
    with pytest.raises(S.SetupError, match="Settings changed"):
        S.apply(preview)
    assert tree(installation) == before


def test_mid_transaction_failure_restores_legacy_and_config(installation, monkeypatch):
    preview = S.plan(installation, selected=["off"], allow_disabled=True, migrate=True)
    original = S.atomic_write
    def write(path, raw, mode):
        if path == installation / "profiles/off/config.yaml":
            raise OSError("synthetic disk failure")
        original(path, raw, mode)
    monkeypatch.setattr(S, "atomic_write", write)
    before = (installation / "profiles/off/config.yaml").read_bytes()
    with pytest.raises(S.SetupError, match="did not finish"):
        S.apply(preview)
    assert (installation / "plugins/fleet-liveops/plugin.yaml").exists()
    assert (installation / "profiles/off/config.yaml").read_bytes() == before


def test_rollback_refuses_later_settings_changes(installation):
    backup = S.apply(S.plan(installation, selected=["off"], allow_disabled=True))
    config = installation / "profiles/off/config.yaml"
    config.write_text(config.read_text() + "later: keep\n")
    before = tree(installation)
    with pytest.raises(S.SetupError, match="Settings changed"):
        S.rollback(installation, backup, apply_changes=True)
    assert tree(installation) == before


def test_symlink_and_unknown_namespace_owner_are_rejected(installation):
    path = installation / "profiles/selected/config.yaml"
    raw = path.read_text()
    path.unlink()
    target = installation / "original.yaml"
    target.write_text(raw)
    path.symlink_to(target)
    with pytest.raises(S.SetupError, match="regular file"):
        S.plan(installation, migrate=True)


def test_migration_refuses_push_configuration(installation):
    (installation / "plugins/fleet-liveops/plugin.yaml").write_text("name: fleet-liveops\nversion: 0.3.0\n")
    (installation / "fleet-liveops").mkdir()
    (installation / "fleet-liveops/push.json").write_text("{}")
    before = tree(installation)
    with pytest.raises(S.SetupError, match="push configuration"):
        S.plan(installation, migrate=True)
    assert tree(installation) == before


def test_cli_defaults_to_preview_without_contacting_control_service(installation):
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    args = parser.parse_args(["reporting", "migrate"])
    before = tree(installation)
    out = io.StringIO()
    def no_control(*args):
        pytest.fail("reporting setup must not send any control command")
    assert run(args, home=installation, out=out, control_fn=no_control) == 0
    assert "Preview only" in out.getvalue() and tree(installation) == before


def test_rollback_io_failure_retains_post_migration_configuration(installation, monkeypatch):
    backup = S.apply(S.plan(installation, selected=["off"], allow_disabled=True, migrate=True))
    (installation / "plugins/hermes-fleet-connector/dashboard/manifest.json").unlink()
    config = installation / "profiles/off/config.yaml"
    after = config.read_bytes()
    original = S.os.rename
    def fail_move(source, target):
        if str(source).endswith("plugin-0"):
            raise OSError("synthetic recovery failure")
        return original(source, target)
    monkeypatch.setattr(S.os, "rename", fail_move)
    with pytest.raises(S.SetupError, match="Recovery did not finish"):
        S.rollback(installation, backup, apply_changes=True)
    assert config.read_bytes() == after
    assert (backup / "plugin-0/plugin.yaml").exists()


def test_status_reads_explicitly_disabled_gateway_without_changing_it(installation):
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    args = parser.parse_args(["reporting", "status"])
    before = tree(installation)
    assert run(args, home=installation / "profiles/off", out=io.StringIO()) == 0
    assert tree(installation) == before
