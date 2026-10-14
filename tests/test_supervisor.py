import stat
from pathlib import Path

import pytest

from fleet_connector import __main__ as cli
from fleet_connector.settings import private_json, root


def test_systemd_user_unit_is_review_only_and_profile_scoped(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    home = tmp_path / "profile with spaces"
    home.mkdir()

    result = cli.prepare_service(home, {"owners": [123]})
    unit = Path(result["unit"])
    content = unit.read_text()

    assert result["ok"] is True
    assert result["label"].startswith("hermes-fleet-connector-")
    assert result["message"].endswith("not linked, loaded or started.")
    assert unit.name == result["label"] + ".service"
    assert stat.S_IMODE(unit.stat().st_mode) == 0o600
    assert "Type=simple" in content
    assert "UMask=0077" in content
    assert "--approve-activation" in content
    assert "--pending-setup" not in content
    assert "ExecStart=\"" + cli.sys.executable + "\"" in content
    assert "Install]" not in content
    assert cli.prepare_service(home, {"owners": [123]}) == result


def test_systemd_pending_setup_is_only_a_supervisor_argument(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    home = tmp_path / "profile"
    home.mkdir()

    result = cli.prepare_systemd_user(home, {"owners": []})
    content = Path(result["unit"]).read_text()
    assert "--pending-setup" in content
    assert "--approve-activation" in content
    assert "systemctl" not in content


def test_systemd_refuses_missing_manager_and_preserves_different_unit(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    with pytest.raises(ValueError, match="requires systemctl"):
        cli.prepare_systemd_user(tmp_path, {"owners": [123]})

    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/bin/systemctl")
    initial = cli.prepare_systemd_user(tmp_path, {"owners": [123]})
    path = Path(initial["unit"])
    path.write_text("operator-owned content\n")
    with pytest.raises(ValueError, match="differs; preserved"):
        cli.prepare_systemd_user(tmp_path, {"owners": [123]})
    assert path.read_text() == "operator-owned content\n"


def test_systemd_quoting_escapes_unit_expansion_and_rejects_controls():
    assert cli._systemd_quote('/tmp/a b/$profile%"x') == '"/tmp/a b/$$profile%%\\"x"'
    assert cli._systemd_quote('PYTHONPATH=/tmp/$profile%dir', exec_word=False) == '"PYTHONPATH=/tmp/$profile%%dir"'
    with pytest.raises(ValueError, match="Control characters"):
        cli._systemd_quote("bad\npath")


def test_supervisor_dispatch_is_platform_specific(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli, "prepare_launchd", lambda home, cfg: {"manager": "launchd"})
    assert cli.prepare_service(tmp_path, {"owners": [123]}) == {"manager": "launchd"}

    monkeypatch.setattr(cli.sys, "platform", "freebsd")
    with pytest.raises(ValueError, match="only on macOS and Linux"):
        cli.prepare_service(tmp_path, {"owners": [123]})


def test_rollback_removes_only_unmodified_linux_unit(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    cfg = {"home": str(tmp_path), "origin": "https://fixture.example.test", "owners": [123],
           "bot_id": 456, "port": 9129, "backend_port": 9130, "hermes_root": str(tmp_path),
           "display_name": "Fixture", "version": "0.1.4"}
    private_json(root(tmp_path) / "settings.json", cfg)
    result = cli.prepare_systemd_user(tmp_path, cfg)
    unit = Path(result["unit"])
    monkeypatch.setattr(cli, "control", lambda *a: (_ for _ in ()).throw(ConnectionRefusedError()))

    cli.main(["--home", str(tmp_path), "rollback"])
    assert not unit.exists()
    assert (root(tmp_path) / "settings.json").exists()
    assert '"ok": true' in capsys.readouterr().out


def test_rollback_preserves_operator_edited_linux_unit(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    cfg = {"home": str(tmp_path), "origin": "https://fixture.example.test", "owners": [123],
           "bot_id": 456, "port": 9129, "backend_port": 9130, "hermes_root": str(tmp_path),
           "display_name": "Fixture", "version": "0.1.4"}
    private_json(root(tmp_path) / "settings.json", cfg)
    unit = Path(cli.prepare_systemd_user(tmp_path, cfg)["unit"])
    unit.write_text("operator-edited unit\n")
    (root(tmp_path) / "service.plist").write_text("preserve until rollback can proceed")
    monkeypatch.setattr(cli, "control", lambda *a: (_ for _ in ()).throw(ConnectionRefusedError()))

    with pytest.raises(ValueError, match="Edited systemd unit differs"):
        cli.main(["--home", str(tmp_path), "rollback"])
    assert unit.read_text() == "operator-edited unit\n"
    assert (root(tmp_path) / "service.plist").exists()
