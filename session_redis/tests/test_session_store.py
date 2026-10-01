# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html)

import fnmatch

from odoo.tests import BaseCase

from ..session import RedisSessionStore

# A partial sid has to satisfy odoo.http._session_identifier_re, which
# delete_from_identifiers enforces; these are hex of the right shape.
SID_A = "a" * 42
SID_B = "b" * 42


class FakeRedis:
    """Enough of the redis API for the key-naming contract, with real globbing.

    The point of this fake is that `keys` and `scan_iter` do actual fnmatch
    against the keys `save` wrote. A fake that returned a canned list would
    assert nothing: the bugs this file exists for are pattern bugs, where the
    glob is well-formed but cannot match the keys the same class writes.
    """

    def __init__(self):
        self.store = {}

    def set(self, key, value, **_kwargs):
        self.store[key] = value
        return True

    def expire(self, _key, _expiration):
        return True

    def get(self, key):
        return self.store.get(key)

    def keys(self, pattern="*"):
        return [k for k in self.store if fnmatch.fnmatchcase(k, pattern)]

    def scan_iter(self, match="*"):
        return iter(self.keys(match))

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)


class TestSessionStoreKeyNaming(BaseCase):
    """The prefix contract between the methods that write keys and read them.

    session.py builds a redis key or glob in five places. They have to agree,
    and twice now they have not: a pattern was spelled `session::{self.prefix}:`
    while `self.prefix` was already `"session:"`, producing the glob
    `session::session::<sid>*` against keys named `session:<sid>`. Nothing
    raises -- `keys()` simply returns nothing, so every session looks absent.

    These tests therefore assert agreement between methods rather than any
    literal key format, so they keep holding if the prefix scheme changes.

    BaseCase, not a plain TestCase, even though nothing here needs a cursor:
    TagsSelector.check() returns False for any test lacking a `test_tags`
    attribute (odoo/tests/tag_selector.py), so a bare TestCase is SILENTLY
    SKIPPED whenever --test-tags is in play -- which is always, in OCA CI.
    BaseCase.__init_subclass__ assigns the default tags, and it defines no
    setUp, so it costs nothing here.
    """

    def _store(self, prefix=""):
        # Only `prefix` is passed: the session-class kwarg is named
        # session_class on 19.0 and session_cls on 20.0, and its default is
        # fine here because no Session is ever instantiated.
        return RedisSessionStore(FakeRedis(), prefix=prefix)

    def _save_raw(self, store, sid):
        """Write a session key the way save() names it, without a Session."""
        store.redis.set(store.build_key(sid), b"{}")

    def test_get_missing_finds_a_saved_session(self):
        """A session that was saved must not be reported missing."""
        for prefix in ("", "mydb"):
            with self.subTest(prefix=prefix):
                store = self._store(prefix)
                self._save_raw(store, SID_A)
                self.assertEqual(store.get_missing_session_identifiers([SID_A]), set())

    def test_get_missing_reports_an_absent_session(self):
        """A session that was never saved must be reported missing."""
        for prefix in ("", "mydb"):
            with self.subTest(prefix=prefix):
                store = self._store(prefix)
                self._save_raw(store, SID_A)
                self.assertEqual(
                    store.get_missing_session_identifiers([SID_A, SID_B]), {SID_B}
                )

    def test_delete_from_identifiers_removes_the_saved_session(self):
        """delete_from_identifiers must reach the keys build_key wrote."""
        for prefix in ("", "mydb"):
            with self.subTest(prefix=prefix):
                store = self._store(prefix)
                self._save_raw(store, SID_A)
                self._save_raw(store, SID_B)
                store.delete_from_identifiers([SID_A])
                self.assertIsNone(store.redis.get(store.build_key(SID_A)))
                self.assertIsNotNone(store.redis.get(store.build_key(SID_B)))

    def test_list_round_trips_the_sid(self):
        """list() strips exactly the prefix build_key added."""
        for prefix in ("", "mydb"):
            with self.subTest(prefix=prefix):
                store = self._store(prefix)
                self._save_raw(store, SID_A)
                self.assertEqual(store.list(), [SID_A])

    def test_every_pattern_matches_the_key_build_key_produces(self):
        """The contract itself: each glob must match build_key's output.

        This is the assertion that would have caught both prefix bugs at the
        point they were written, independently of any one caller.
        """
        for prefix in ("", "mydb"):
            with self.subTest(prefix=prefix):
                store = self._store(prefix)
                key = store.build_key(SID_A)
                for pattern in (
                    f"{store.prefix}*",  # list() / vacuum
                    f"{store.prefix}{SID_A}*",  # get_missing / delete_from_identifiers
                ):
                    self.assertTrue(
                        fnmatch.fnmatchcase(key, pattern),
                        f"glob {pattern!r} does not match key {key!r}",
                    )
