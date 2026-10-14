"""Hermes Fleet device pairing: store/service security properties.

Written with ``unittest`` so it runs under pytest and with plain ``python -m unittest``.
"""
from __future__ import annotations

import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from fleet_connector import devices as fd

ORIGIN = "https://gateway.example.test"


class FakeClock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "state" / "fleet_devices.sqlite3"
        self.clock = FakeClock()
        self.store = fd.FleetDeviceStore(self.path, clock=self.clock)

    def issue(self, **kwargs):
        kwargs.setdefault("origin", ORIGIN)
        kwargs.setdefault("created_by", "owner")
        return self.store.create_invitation(**kwargs)


class InvitationLifecycle(StoreCase):
    def test_preview_never_consumes(self):
        issued = self.issue()
        for _ in range(5):
            seen = self.store.preview(
                invitation_id=issued.invitation.id, secret=issued.secret, origin=ORIGIN)
            self.assertEqual(seen.state, "pending")
        redeemed = self.store.redeem(
            invitation_id=issued.invitation.id, secret=issued.secret, device_name="Phone",
            origin=ORIGIN)
        self.assertTrue(redeemed.credential.startswith("hfd1."))

    def test_redeem_is_single_use_and_replay_is_rejected(self):
        issued = self.issue()
        first = self.store.redeem(
            invitation_id=issued.invitation.id, secret=issued.secret, origin=ORIGIN)
        with self.assertRaises(fd.AlreadyRedeemed):
            self.store.redeem(
                invitation_id=issued.invitation.id, secret=issued.secret, origin=ORIGIN)
        with self.assertRaises(fd.AlreadyRedeemed):
            self.store.preview(
                invitation_id=issued.invitation.id, secret=issued.secret, origin=ORIGIN)
        # The first credential still works; replay did not mint a second one.
        self.assertEqual(len(self.store.list_devices()), 1)
        self.assertIsNotNone(self.store.verify_credential(first.credential))

    def test_expiry(self):
        issued = self.issue(ttl_seconds=120)
        self.clock.advance(119)
        self.store.preview(invitation_id=issued.invitation.id, secret=issued.secret)
        self.clock.advance(2)
        with self.assertRaises(fd.InvitationExpired):
            self.store.preview(invitation_id=issued.invitation.id, secret=issued.secret)
        with self.assertRaises(fd.InvitationExpired):
            self.store.redeem(invitation_id=issued.invitation.id, secret=issued.secret)
        self.assertEqual(self.store.list_devices(), [])

    def test_ttl_is_clamped_to_policy(self):
        self.assertEqual(
            self.issue(ttl_seconds=1).invitation.expires_at - self.clock.now, fd.MIN_TTL_SECONDS)
        self.assertEqual(
            self.issue(ttl_seconds=10 ** 6).invitation.expires_at - self.clock.now,
            fd.MAX_TTL_SECONDS)
        self.assertEqual(
            self.issue().invitation.expires_at - self.clock.now, fd.DEFAULT_TTL_SECONDS)
        with self.assertRaises(fd.InvalidRequest):
            self.issue(ttl_seconds="600")
        with self.assertRaises(fd.InvalidRequest):
            self.issue(ttl_seconds=True)

    def test_only_https_origins_and_known_scopes(self):
        with self.assertRaises(fd.InvalidRequest):
            self.issue(origin="http://gateway.example.test")
        with self.assertRaises(fd.InvalidRequest):
            self.issue(scopes=("root",))

    def test_cancel_blocks_redemption(self):
        issued = self.issue()
        self.assertTrue(self.store.cancel_invitation(issued.invitation.id))
        with self.assertRaises(fd.InvitationCancelled):
            self.store.redeem(invitation_id=issued.invitation.id, secret=issued.secret)
        self.assertFalse(self.store.cancel_invitation(issued.invitation.id))
        self.assertEqual(self.store.list_pending_invitations(), [])

    def test_cannot_cancel_a_redeemed_invitation(self):
        issued = self.issue()
        self.store.redeem(invitation_id=issued.invitation.id, secret=issued.secret)
        self.assertFalse(self.store.cancel_invitation(issued.invitation.id))

    def test_pending_invitations_are_capped(self):
        for _ in range(fd.MAX_PENDING_INVITATIONS):
            self.issue()
        with self.assertRaises(fd.TooManyInvitations):
            self.issue()
        self.clock.advance(fd.MAX_TTL_SECONDS + 1)
        self.issue()  # expired ones no longer count


class GuessingAndScoping(StoreCase):
    def test_wrong_unknown_and_malformed_are_indistinguishable(self):
        issued = self.issue()
        cases = [
            (issued.invitation.id, "x" * 43),                       # wrong secret
            ("A" * 22, issued.secret),                              # unknown id
            ("short", issued.secret), (issued.invitation.id, "short"),  # malformed
            (None, issued.secret), (issued.invitation.id, None),
            (issued.invitation.id, issued.secret + "!"),            # bad charset
        ]
        for invitation_id, secret in cases:
            with self.assertRaises(fd.InvalidInvitation, msg=repr((invitation_id, secret))):
                self.store.preview(invitation_id=invitation_id, secret=secret)

    def test_state_is_not_revealed_without_the_secret(self):
        issued = self.issue(ttl_seconds=60)
        self.store.redeem(invitation_id=issued.invitation.id, secret=issued.secret)
        self.clock.advance(3600)
        # Redeemed AND expired, but a guesser only ever sees "invalid".
        with self.assertRaises(fd.InvalidInvitation):
            self.store.preview(invitation_id=issued.invitation.id, secret="y" * 43)

    def test_repeated_wrong_secrets_lock_the_invitation(self):
        issued = self.issue()
        for _ in range(fd.MAX_FAILED_ATTEMPTS):
            with self.assertRaises(fd.InvalidInvitation):
                self.store.redeem(invitation_id=issued.invitation.id, secret="z" * 43)
        with self.assertRaises(fd.InvitationCancelled):
            self.store.redeem(invitation_id=issued.invitation.id, secret=issued.secret)
        self.assertEqual(self.store.list_devices(), [])

    def test_valid_secret_on_the_wrong_origin_is_invalid_and_not_consumed(self):
        issued = self.issue()
        with self.assertRaises(fd.InvalidInvitation):
            self.store.redeem(
                invitation_id=issued.invitation.id, secret=issued.secret,
                origin="https://evil.example.test")
        with self.assertRaises(fd.InvalidInvitation):
            self.store.preview(
                invitation_id=issued.invitation.id, secret=issued.secret,
                origin="https://evil.example.test")
        # Still redeemable at the intended origin.
        self.store.redeem(invitation_id=issued.invitation.id, secret=issued.secret, origin=ORIGIN)


class ConcurrentRedemption(StoreCase):
    def _race(self, stores, issued, attempts):
        barrier = threading.Barrier(attempts)
        results = []
        lock = threading.Lock()

        def worker(index):
            store = stores[index % len(stores)]
            barrier.wait()
            try:
                outcome = store.redeem(
                    invitation_id=issued.invitation.id, secret=issued.secret,
                    device_name=f"d{index}", origin=ORIGIN)
            except fd.PairingError as error:
                outcome = error
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(attempts)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        return results

    def test_exactly_one_of_many_simultaneous_redemptions_wins(self):
        issued = self.issue()
        results = self._race([self.store], issued, 24)
        winners = [r for r in results if isinstance(r, fd.RedeemedDevice)]
        losers = [r for r in results if isinstance(r, fd.AlreadyRedeemed)]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), 23, results)
        self.assertEqual(len(self.store.list_devices()), 1)

    def test_independent_store_instances_on_one_file_also_serialise(self):
        # Separate instances model separate processes sharing the database file.
        issued = self.issue()
        stores = [fd.FleetDeviceStore(self.path, clock=self.clock) for _ in range(4)]
        results = self._race(stores, issued, 16)
        winners = [r for r in results if isinstance(r, fd.RedeemedDevice)]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(self.store.list_devices()), 1)


class DeviceCredentials(StoreCase):
    def redeem(self):
        issued = self.issue()
        return self.store.redeem(
            invitation_id=issued.invitation.id, secret=issued.secret, device_name="Tony's iPhone")

    def test_session_roundtrip_and_expiry(self):
        redeemed = self.redeem()
        session = self.store.open_session(redeemed.credential)
        live = self.store.verify_session(session.access_token)
        self.assertEqual(live.device_id, redeemed.device.id)
        self.clock.advance(fd.DEVICE_SESSION_TTL_SECONDS + 1)
        self.assertIsNone(self.store.verify_session(session.access_token))

    def test_revocation_is_immediate_and_kills_sessions(self):
        redeemed = self.redeem()
        session = self.store.open_session(redeemed.credential)
        self.assertTrue(self.store.revoke_device(redeemed.device.id))
        self.assertIsNone(self.store.verify_session(session.access_token))
        self.assertIsNone(self.store.verify_credential(redeemed.credential))
        with self.assertRaises(fd.InvalidCredential):
            self.store.open_session(redeemed.credential)
        self.assertTrue(self.store.revoke_device(redeemed.device.id))  # idempotent
        self.assertFalse(self.store.revoke_device("0" * 32))

    def test_self_revocation_needs_the_credential(self):
        redeemed = self.redeem()
        self.assertFalse(self.store.revoke_with_credential("hfd1." + "a" * 32 + "." + "b" * 43))
        self.assertTrue(self.store.revoke_with_credential(redeemed.credential))
        self.assertFalse(self.store.revoke_with_credential(redeemed.credential))

    def test_tampered_or_foreign_credentials_are_rejected(self):
        redeemed = self.redeem()
        prefix, device_id, secret = redeemed.credential.split(".")
        for bad in (
            f"{prefix}.{device_id}.{'x' * len(secret)}", f"hfd2.{device_id}.{secret}",
            f"{prefix}.{'0' * 32}.{secret}", redeemed.credential + "x", "", None, 7,
            "hfa1." + device_id + "." + secret,
        ):
            self.assertIsNone(self.store.verify_credential(bad), repr(bad))
        # An access token is not a credential and vice versa.
        session = self.store.open_session(redeemed.credential)
        self.assertIsNone(self.store.verify_credential(session.access_token))
        self.assertIsNone(self.store.verify_session(redeemed.credential))

    def test_close_session(self):
        redeemed = self.redeem()
        session = self.store.open_session(redeemed.credential)
        self.store.close_session(session.access_token)
        self.assertIsNone(self.store.verify_session(session.access_token))
        self.assertIsNotNone(self.store.verify_credential(redeemed.credential))


class AtRest(StoreCase):
    def test_no_secret_or_credential_is_stored_in_the_clear(self):
        issued = self.issue(label="private label")
        redeemed = self.store.redeem(invitation_id=issued.invitation.id, secret=issued.secret)
        session = self.store.open_session(redeemed.credential)
        blobs = b""
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                blobs += candidate.read_bytes()
        credential_secret = redeemed.credential.split(".")[2]
        session_secret = session.access_token.split(".")[2]
        for needle in (issued.secret, credential_secret, session_secret):
            self.assertNotIn(needle.encode(), blobs)

    def test_files_are_owner_only(self):
        self.issue()
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.path.parent).st_mode), 0o700)

    def test_error_messages_carry_no_secret(self):
        issued = self.issue()
        self.store.redeem(invitation_id=issued.invitation.id, secret=issued.secret)
        with self.assertRaises(fd.AlreadyRedeemed) as caught:
            self.store.redeem(invitation_id=issued.invitation.id, secret=issued.secret)
        self.assertNotIn(issued.secret, str(caught.exception))
        self.assertNotIn(issued.invitation.id, str(caught.exception))


class InstanceIdentity(unittest.TestCase):
    def test_stable_and_race_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "auth"
            seen = []
            lock = threading.Lock()

            def worker():
                value = fd.get_instance_id(directory)
                with lock:
                    seen.append(value)

            threads = [threading.Thread(target=worker) for _ in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            self.assertEqual(len(set(seen)), 1, seen)
            self.assertEqual(len(seen[0]), 32)
            self.assertEqual(fd.get_instance_id(directory), seen[0])


if __name__ == "__main__":
    unittest.main()
