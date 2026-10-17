import json
from pathlib import Path
import pytest
from fleet_connector.owners import OwnerRegistry, MAX_CLAIM_ATTEMPTS, CLAIM_TTL_SECONDS
from fleet_connector.settings import private_json

class Clock:
    now = 1_800_000_000.0
    def __call__(self): return self.now

@pytest.fixture
def env(tmp_path):
    clock = Clock()
    return tmp_path, clock, OwnerRegistry(tmp_path, clock=clock)

def test_local_is_always_owner_and_unknown_is_not(env):
    _, _, reg = env
    assert reg.is_owner("local", "anything")
    assert not reg.is_owner("telegram", "1")
    assert not reg.is_owner("", "1") and not reg.is_owner(None, "1")
    assert not reg.is_owner("telegram", None)
    assert reg.list_owners() == []

def test_migration_from_settings_keeps_settings_untouched(env):
    d, _, reg = env
    private_json(d / "settings.json", {"owners": [123, 456], "bot_id": 9, "x": 1})
    before = (d / "settings.json").read_bytes()
    assert reg.is_owner("telegram", 123) and reg.is_owner("telegram", "456")
    assert not reg.is_owner("discord", "123")
    owners = reg.list_owners()
    assert [o["user_id"] for o in owners] == ["123", "456"]
    assert {o["added_via"] for o in owners} == {"migrated"}
    assert set(owners[0]) == {"platform", "user_id", "added_at", "added_via"}
    assert (d / "settings.json").read_bytes() == before
    assert (d / "owners.json").stat().st_mode & 0o077 == 0

def test_migration_waits_for_settings_and_runs_once(env):
    d, _, reg = env
    assert reg.list_owners() == []           # no settings yet: nothing flagged as migrated
    private_json(d / "settings.json", {"owners": [7]})
    assert [o["user_id"] for o in reg.list_owners()] == ["7"]
    assert reg.remove_owner(1)["user_id"] == "7"
    private_json(d / "settings.json", {"owners": [7]})
    assert reg.list_owners() == []           # a removed owner is not resurrected

def test_migration_ignores_bad_legacy_values(env):
    d, _, reg = env
    private_json(d / "settings.json", {"owners": [5, "6", True, -1, 0]})
    assert [o["user_id"] for o in reg.list_owners()] == ["5"]

def test_claim_flow(env):
    d, clock, reg = env
    code, expires = reg.start_claim()
    assert len(code) == 6 and code.isdigit() and expires == int(clock.now) + CLAIM_TTL_SECONDS
    assert str(code) not in (d / "owners.json").read_text()
    entry = reg.claim(" ".join([code[:3], code[3:]]), "Discord", 42)
    assert entry["platform"] == "discord" and entry["added_via"] == "claim"
    assert reg.is_owner("discord", "42")
    assert reg.claim(code, "discord", "43") is None     # single use
    assert not reg.is_owner("discord", "43")

def test_claim_refuses_local_and_bad_identity(env):
    _, _, reg = env
    code, _ = reg.start_claim()
    assert reg.claim(code, "local", "x") is None
    assert reg.claim(code, "", "x") is None and reg.claim(code, "slack", "") is None
    assert reg.claim(code, "slack", "u1")["user_id"] == "u1"   # refusals did not burn the code

def test_claim_attempt_limit_invalidates_code(env):
    _, _, reg = env
    code, _ = reg.start_claim()
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(MAX_CLAIM_ATTEMPTS):
        assert reg.claim(wrong, "slack", "u") is None
    assert reg.claim(code, "slack", "u") is None
    assert reg.list_owners() == []

def test_claim_expiry(env):
    _, clock, reg = env
    code, _ = reg.start_claim()
    clock.now += CLAIM_TTL_SECONDS
    assert reg.claim(code, "slack", "u") is None

def test_new_claim_replaces_old_and_no_claim_means_none(env):
    _, _, reg = env
    assert reg.claim("123456", "slack", "u") is None
    first, _ = reg.start_claim()
    second, _ = reg.start_claim()
    if first != second:
        assert reg.claim(first, "slack", "u") is None
    assert reg.claim(second, "slack", "u") is not None

def test_remove_owner_allows_last_and_local_remains(env):
    _, _, reg = env
    code, _ = reg.start_claim()
    reg.claim(code, "slack", "u")
    assert reg.remove_owner(0) is None and reg.remove_owner(2) is None and reg.remove_owner("1") is None
    assert reg.remove_owner(1)["user_id"] == "u"
    assert not reg.is_owner("slack", "u") and reg.is_owner("local", "")
    assert reg.list_owners() == []

def test_claim_twice_for_same_identity_does_not_duplicate(env):
    _, _, reg = env
    for _ in range(2):
        code, _ = reg.start_claim()
        reg.claim(code, "slack", "u")
    assert len(reg.list_owners()) == 1
