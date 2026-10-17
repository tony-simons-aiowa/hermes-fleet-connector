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


CLIENT = "c" * 43
OTHER_CLIENT = "d" * 43


class ApprovalCase(StoreCase):
    def v2(self, **kwargs):
        kwargs.setdefault("requires_approval", True)
        return self.issue(**kwargs)

    def request(self, issued=None, client=CLIENT, **kwargs):
        issued = issued or self.v2()
        kwargs.setdefault("device_name", "Test Phone")
        kwargs.setdefault("device_model", "Model1,1")
        return issued, self.store.create_request(
            invitation_id=issued.invitation.id, secret=issued.secret, client_secret=client,
            origin=ORIGIN, **kwargs)

    def status(self, req, client=CLIENT):
        self.clock.advance(1.5)  # stay clear of the poll rate limit
        return self.store.request_status(request_id=req.id, client_secret=client)

    def approve(self, code, user="1001", platform="telegram"):
        return self.store.approve_by_code(
            code=code, approver_platform=platform, approver_user_id=user)

    def wrong_code(self, req):
        return "999999" if req.match_code != "999999" else "888888"


class RequestCreation(ApprovalCase):
    def test_request_shape_and_code(self):
        _, req = self.request()
        self.assertEqual(len(req.match_code), 6)
        self.assertTrue(req.match_code.isdigit())
        self.assertEqual(req.expires_at - req.created_at, 600)
        self.assertEqual(req.device_name, "Test Phone")

    def test_preview_reports_requires_approval(self):
        v1 = self.issue()
        v2 = self.v2()
        self.assertFalse(self.store.preview(
            invitation_id=v1.invitation.id, secret=v1.secret).requires_approval)
        self.assertTrue(self.store.preview(
            invitation_id=v2.invitation.id, secret=v2.secret).requires_approval)

    def test_idempotent_for_same_client_secret(self):
        issued, first = self.request()
        _, again = self.request(issued)
        self.assertEqual(first, again)
        self.assertEqual(len(self.store.list_pending()), 1)

    def test_different_client_secret_loses(self):
        issued, _ = self.request()
        with self.assertRaises(fd.AlreadyRedeemed):
            self.request(issued, client=OTHER_CLIENT)

    def test_v1_invitation_is_not_supported_for_requests(self):
        issued = self.issue()
        with self.assertRaises(fd.ApprovalNotSupported):
            self.request(issued)
        self.store.redeem(invitation_id=issued.invitation.id, secret=issued.secret)

    def test_v1_redeem_refuses_approval_invitations_without_consuming(self):
        issued = self.v2()
        with self.assertRaises(fd.ApprovalRequired):
            self.store.redeem(invitation_id=issued.invitation.id, secret=issued.secret)
        self.assertEqual(self.store.list_devices(), [])
        self.request(issued)

    def test_wrong_invitation_secret_and_bad_client_secret(self):
        issued = self.v2()
        with self.assertRaises(fd.InvalidInvitation):
            self.store.create_request(invitation_id=issued.invitation.id, secret="x" * 43,
                                      client_secret=CLIENT)
        with self.assertRaises(fd.InvalidRequest):
            self.store.create_request(invitation_id=issued.invitation.id, secret=issued.secret,
                                      client_secret="short")
        self.assertEqual(self.store.list_pending(), [])

    def test_expired_invitation_cannot_request(self):
        issued = self.v2(ttl_seconds=60)
        self.clock.advance(61)
        with self.assertRaises(fd.InvitationExpired):
            self.request(issued)

    def test_codes_are_unique_among_pending(self):
        codes = {self.request()[1].match_code for _ in range(fd.MAX_PENDING_INVITATIONS)}
        self.assertEqual(len(codes), fd.MAX_PENDING_INVITATIONS)

    def test_code_collision_is_avoided(self):
        calls = iter([5, 5, 5, 7])
        real = fd.secrets.randbelow
        fd.secrets.randbelow = lambda n: next(calls)
        try:
            _, a = self.request()
            _, b = self.request()
        finally:
            fd.secrets.randbelow = real
        self.assertEqual((a.match_code, b.match_code), ("000005", "000007"))

    def test_client_secret_is_stored_only_as_digest(self):
        self.request()
        with self.store._read() as conn:
            blob = bytes(conn.execute("SELECT client_secret_hash FROM pairing_requests").fetchone()[0])
        self.assertEqual(blob, fd._digest(CLIENT))
        self.assertNotIn(CLIENT.encode(), self.path.read_bytes())


class RequestStatusAndApproval(ApprovalCase):
    def test_pending_then_approved_returns_credential_each_poll(self):
        _, req = self.request()
        self.assertEqual(self.status(req).status, "pending")
        device = self.approve(req.match_code)
        self.assertEqual((device.approved_by_platform, device.approved_by_user), ("telegram", "1001"))
        first = self.status(req)
        second = self.status(req)
        self.assertEqual(first.status, "approved")
        self.assertEqual(first.credential, second.credential)
        self.assertTrue(first.credential.startswith("hfd1." + device.id + "."))
        self.assertEqual(first.device_id, device.id)
        self.assertEqual(first.scopes, (fd.SCOPE_OPERATOR,))
        self.assertIsNotNone(self.store.verify_credential(first.credential))

    def test_codes_with_spaces_are_accepted(self):
        _, req = self.request()
        self.approve(" ".join([req.match_code[:3], req.match_code[3:]]))

    def test_first_login_wipes_the_plaintext(self):
        _, req = self.request()
        self.approve(req.match_code)
        credential = self.status(req).credential
        self.store.open_session(credential)
        done = self.status(req)
        self.assertEqual((done.status, done.credential), ("delivered", None))
        with self.store._read() as conn:
            self.assertIsNone(conn.execute("SELECT credential FROM pairing_requests").fetchone()[0])

    def test_window_closes_after_ten_minutes(self):
        _, req = self.request()
        self.approve(req.match_code)
        self.assertEqual(self.status(req).status, "approved")
        self.clock.advance(fd.CREDENTIAL_WINDOW_SECONDS)
        late = self.store.request_status(request_id=req.id, client_secret=CLIENT)
        self.assertEqual((late.status, late.credential), ("delivered", None))

    def test_unpolled_plaintext_is_purged_by_later_writes(self):
        _, req = self.request()
        self.approve(req.match_code)
        self.clock.advance(fd.CREDENTIAL_WINDOW_SECONDS + 5)
        self.v2()  # any write purges
        with self.store._read() as conn:
            self.assertIsNone(conn.execute("SELECT credential FROM pairing_requests").fetchone()[0])

    def test_wrong_secret_and_unknown_id_are_indistinguishable(self):
        _, req = self.request()
        self.clock.advance(2)
        for rid, secret in ((req.id, OTHER_CLIENT), ("A" * 22, CLIENT), ("bad", CLIENT),
                            (req.id, "bad"), (None, CLIENT)):
            with self.assertRaises(fd.InvalidRequest, msg=repr((rid, secret))):
                self.store.request_status(request_id=rid, client_secret=secret)

    def test_wrong_secret_cannot_collect_after_approval(self):
        _, req = self.request()
        self.approve(req.match_code)
        self.clock.advance(2)
        with self.assertRaises(fd.InvalidRequest):
            self.store.request_status(request_id=req.id, client_secret=OTHER_CLIENT)

    def test_deny(self):
        _, req = self.request()
        denied = self.store.deny_by_code(code=req.match_code, approver_platform="local",
                                         approver_user_id="host")
        self.assertEqual(denied.id, req.id)
        self.assertEqual(self.status(req).status, "denied")
        self.assertEqual(self.store.list_devices(), [])
        with self.assertRaises(fd.NoMatch):
            self.approve(req.match_code)

    def test_expiry(self):
        _, req = self.request()
        self.clock.advance(fd.REQUEST_TTL_SECONDS + 1)
        self.assertEqual(self.store.request_status(request_id=req.id, client_secret=CLIENT).status,
                         "expired")
        with self.assertRaises(fd.NoMatch):
            self.approve(req.match_code)
        self.assertEqual(self.store.list_pending(), [])

    def test_code_is_reusable_after_expiry(self):
        _, req = self.request()
        self.clock.advance(fd.REQUEST_TTL_SECONDS + 1)
        calls = iter([int(req.match_code)])
        real = fd.secrets.randbelow
        fd.secrets.randbelow = lambda n: next(calls)
        try:
            _, again = self.request()
        finally:
            fd.secrets.randbelow = real
        self.assertEqual(again.match_code, req.match_code)

    def test_list_pending(self):
        _, a = self.request(device_name="A")
        self.clock.advance(1)
        _, b = self.request(device_name="B")
        self.assertEqual([r.device_name for r in self.store.list_pending()], ["A", "B"])
        self.approve(a.match_code)
        self.assertEqual([r.device_name for r in self.store.list_pending()], ["B"])

    def test_no_match_for_unknown_or_malformed_code(self):
        self.request()
        for code in ("000000x", "12345", "", None, 12.5, "١٢٣٤٥٦"):
            with self.assertRaises(fd.NoMatch, msg=repr(code)):
                self.approve(code)

    def test_invalid_approver_is_rejected(self):
        _, req = self.request()
        for platform, user in (("", "1"), ("Tele gram", "1"), ("telegram", ""), ("telegram", None)):
            with self.assertRaises(fd.InvalidRequest):
                self.approve(req.match_code, user=user, platform=platform)

    def test_exactly_one_approver_wins(self):
        _, req = self.request()
        stores = [fd.FleetDeviceStore(self.path, clock=self.clock) for _ in range(4)]
        barrier = threading.Barrier(16)
        results, lock = [], threading.Lock()

        def worker(i):
            barrier.wait()
            try:
                out = stores[i % 4].approve_by_code(
                    code=req.match_code, approver_platform="telegram", approver_user_id=str(i))
            except fd.PairingError as error:
                out = error
            with lock:
                results.append(out)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        winners = [r for r in results if isinstance(r, fd.Device)]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(self.store.list_devices()), 1)

    def test_approval_creates_device_like_v1(self):
        _, req = self.request(device_name="  Named\u0007 Phone ")
        device = self.approve(req.match_code)
        self.assertEqual(device.label, "Named Phone")
        self.assertEqual(device.device_model, "Model1,1")
        self.assertEqual(device.origin, ORIGIN)
        self.assertEqual(self.store.list_devices()[0].id, device.id)


class RateLimitAndLockout(ApprovalCase):
    def test_second_poll_within_a_second_is_limited(self):
        _, req = self.request()
        self.store.request_status(request_id=req.id, client_secret=CLIENT)
        with self.assertRaises(fd.RateLimited):
            self.store.request_status(request_id=req.id, client_secret=CLIENT)
        self.clock.advance(0.5)
        with self.assertRaises(fd.RateLimited):
            self.store.request_status(request_id=req.id, client_secret=CLIENT)
        self.clock.advance(0.6)
        self.store.request_status(request_id=req.id, client_secret=CLIENT)

    def test_rate_limit_is_per_request_and_not_an_oracle(self):
        _, a = self.request()
        _, b = self.request()
        self.store.request_status(request_id=a.id, client_secret=CLIENT)
        self.store.request_status(request_id=b.id, client_secret=CLIENT)
        with self.assertRaises(fd.InvalidRequest):
            self.store.request_status(request_id=a.id, client_secret=OTHER_CLIENT)

    def test_ten_wrong_codes_lock_the_approver_for_ten_minutes(self):
        _, req = self.request()
        for _ in range(fd.MAX_APPROVER_FAILURES):
            with self.assertRaises(fd.NoMatch):
                self.approve(self.wrong_code(req))
        with self.assertRaises(fd.Locked):
            self.approve(req.match_code)  # even the right code is refused while locked
        self.clock.advance(fd.APPROVER_LOCK_SECONDS - 1)
        with self.assertRaises(fd.Locked):
            self.approve(req.match_code)
        self.clock.advance(2)
        _, fresh = self.request()  # the first request itself expired after ten minutes
        self.assertEqual(self.approve(fresh.match_code).label, "Test Phone")

    def test_lockout_is_per_approver(self):
        _, req = self.request()
        for _ in range(fd.MAX_APPROVER_FAILURES):
            with self.assertRaises(fd.NoMatch):
                self.approve(self.wrong_code(req), user="bad")
        with self.assertRaises(fd.Locked):
            self.approve(req.match_code, user="bad")
        self.approve(req.match_code, user="good")

    def test_failures_age_out_of_the_window(self):
        for _ in range(fd.MAX_APPROVER_FAILURES - 1):
            with self.assertRaises(fd.NoMatch):
                self.approve("999999")
        self.clock.advance(fd.APPROVER_WINDOW_SECONDS + 1)
        _, req = self.request()
        with self.assertRaises(fd.NoMatch):
            self.approve(self.wrong_code(req))
        self.approve(req.match_code)  # one fresh failure only: not locked

    def test_deny_shares_the_lockout(self):
        _, req = self.request()
        for _ in range(fd.MAX_APPROVER_FAILURES):
            with self.assertRaises(fd.NoMatch):
                self.store.deny_by_code(code=self.wrong_code(req), approver_platform="telegram",
                                        approver_user_id="1001")
        with self.assertRaises(fd.Locked):
            self.approve(req.match_code)


class Migration(unittest.TestCase):
    OLD_SCHEMA = """
    CREATE TABLE invitations (id TEXT PRIMARY KEY, secret_hash BLOB NOT NULL, origin TEXT NOT NULL,
        label TEXT NOT NULL, scopes TEXT NOT NULL, created_by TEXT NOT NULL, created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL, state TEXT NOT NULL, failed_attempts INTEGER NOT NULL DEFAULT 0,
        redeemed_at INTEGER, device_id TEXT);
    CREATE TABLE devices (id TEXT PRIMARY KEY, credential_hash BLOB NOT NULL, label TEXT NOT NULL,
        device_model TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL, scopes TEXT NOT NULL,
        created_at INTEGER NOT NULL, last_used_at INTEGER, revoked_at INTEGER, invitation_id TEXT);
    CREATE TABLE device_sessions (token_hash BLOB PRIMARY KEY, device_id TEXT NOT NULL, expires_at INTEGER NOT NULL);
    """

    def test_existing_database_gains_columns_and_stays_v1(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "devices.sqlite3"
            clock = FakeClock()
            conn = sqlite3.connect(path)
            conn.executescript(self.OLD_SCHEMA)
            conn.execute("INSERT INTO invitations VALUES (?,?,?,?,?,?,?,?,'pending',0,NULL,NULL)",
                         ("I" * 22, fd._digest("s" * 43), ORIGIN, "Old", '["fleet:operator"]',
                          "owner", int(clock.now), int(clock.now) + 600))
            conn.commit()
            conn.close()
            store = fd.FleetDeviceStore(path, clock=clock)
            pending = store.list_pending_invitations()
            self.assertEqual(len(pending), 1)
            self.assertFalse(pending[0].requires_approval)
            fd.FleetDeviceStore(path, clock=clock).list_devices()  # re-running is a no-op
            issued = store.create_invitation(origin=ORIGIN, created_by="o", requires_approval=True)
            self.assertTrue(issued.invitation.requires_approval)
            redeemed = store.redeem(invitation_id="I" * 22, secret="s" * 43)  # old v1 row still redeems
            self.assertTrue(redeemed.credential.startswith("hfd1."))


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
