"""Run against a disposable Redis with REDIS_TEST_HOST set; no database needed."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import redis

from odoo.addons.session_redis.session import RedisSessionStore


@unittest.skipUnless(os.getenv("REDIS_TEST_HOST"), "requires disposable Redis")
class SessionRevocation(unittest.TestCase):
    def setUp(self):
        self.redis = redis.Redis(host=os.environ["REDIS_TEST_HOST"])
        self.store = RedisSessionStore(redis=self.redis, prefix="test-" + uuid4().hex)
        self.session = self.store.new()
        self.session.update(uid=42, login="fictional-test")
        self.store.save(self.session)

    def tearDown(self):
        keys = list(self.redis.scan_iter(match=self.store.prefix + "*"))
        if keys:
            self.redis.delete(*keys)

    def test_late_save_cannot_restore_logged_out_user(self):
        stale = self.store.get(self.session.sid)
        self.store.delete(self.session)
        self.assertFalse(self.store.save(stale))
        self.assertIsNone(self.store.get(stale.sid).uid)

    def test_save_before_logout_is_revoked(self):
        self.store.save(self.store.get(self.session.sid))
        self.store.delete(self.session)
        self.assertIsNone(self.store.get(self.session.sid).uid)

    def test_repeated_late_saves_remain_revoked(self):
        stale = self.store.get(self.session.sid)
        self.store.delete(self.session)
        for _ in range(3):
            self.store.save(stale)
        self.assertIsNone(self.store.get(stale.sid).uid)

    def test_revoked_id_is_never_kept(self):
        self.store.delete(self.session)
        fresh = self.store.get(self.session.sid, keep_sid=True)
        self.assertNotEqual(fresh.sid, self.session.sid)
        self.assertIsNone(fresh.uid)

    def test_unrelated_session_remains_authenticated(self):
        other = self.store.new()
        other.update(uid=43)
        self.store.save(other)
        self.store.delete(self.session)
        self.assertEqual(self.store.get(other.sid).uid, 43)

    def test_marker_expires_and_contains_no_identity(self):
        self.store.delete(self.session)
        key = self.store.build_key(self.session.sid)
        self.assertGreater(self.redis.ttl(key), 0)
        self.assertNotIn(b"fictional-test", self.redis.get(key))

    def test_device_revocation_blocks_late_saves_and_marks_device_missing(self):
        stale = self.store.get(self.session.sid)
        identifier = self.session.sid[:42]
        self.store.delete_from_identifiers([identifier])
        self.store.save(stale)
        self.assertIsNone(self.store.get(stale.sid).uid)
        self.assertEqual(
            self.store.get_missing_session_identifiers([identifier]), {identifier}
        )
        self.assertNotIn(stale.sid.encode(), self.store.list())

    def test_missing_id_keeps_existing_contract(self):
        sid = self.store.generate_key()
        self.assertEqual(self.store.get(sid, keep_sid=True).sid, sid)
        self.assertNotEqual(self.store.get(sid).sid, sid)

    def test_old_soft_rotation_ids_are_revoked_together(self):
        old_sid = self.session.sid
        with patch("odoo.http.session.update_session_token"):
            self.store.rotate(self.session, object(), soft=True)
        self.assertNotEqual(old_sid, self.session.sid)
        self.store.delete(self.session)
        self.assertIsNone(self.store.get(old_sid).uid)
        self.assertIsNone(self.store.get(self.session.sid).uid)

    def test_late_soft_rotation_cannot_authenticate_after_logout(self):
        stale = self.store.get(self.session.sid)
        self.store.delete(self.session)
        with patch("odoo.http.session.update_session_token"):
            self.store.rotate(stale, object(), soft=True)
        self.assertIsNone(stale.uid)
        self.assertIsNone(self.store.get(stale.sid).uid)

    def test_concurrent_rotation_and_logout_cannot_restore_authentication(self):
        for _ in range(10):
            current = self.store.new()
            current.update(uid=42)
            self.store.save(current)
            stale = self.store.get(current.sid)
            barrier = Barrier(2)

            def rotate(barrier=barrier, stale=stale):
                barrier.wait()
                self.store.rotate(stale, object(), soft=True)

            def logout(barrier=barrier, current=current):
                barrier.wait()
                self.store.delete(current)

            with patch("odoo.http.session.update_session_token"):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(rotate), executor.submit(logout)]
                    for future in futures:
                        future.result()
            self.assertIsNone(self.store.get(current.sid).uid)
            self.assertIsNone(self.store.get(stale.sid).uid)
