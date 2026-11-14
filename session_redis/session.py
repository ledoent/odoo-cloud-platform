# Copyright 2016-2024 Camptocamp SA
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html)

import builtins
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import TypeAlias

from odoo.http.session import (
    SESSION_LIFETIME,
    STORED_SESSION_BYTES,
    Session,
    SessionStore,
    _session_identifier_re,
    get_default_session,
)

from . import json_encoding

# this was equal to the duration of the session garbage collector in
# odoo.http.session_gc()
DEFAULT_SESSION_TIMEOUT_ANONYMOUS = 60 * 60 * 3  # 3 hours in seconds

# A hard-deleted SID must not be recreated by a request already in flight.
# Keep the marker for at least a full session lifetime. It contains no user data.
_REVOKED = b"revoked-session"
_SAVE_UNLESS_REVOKED = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""

_logger = logging.getLogger(__name__)
_HELD_LOCKS = ContextVar("redis_session_locks", default=frozenset())


def _guarded(method):
    @wraps(method)
    def guarded(self, session, *args, **kwargs):
        with self._session_lock(session.sid[:STORED_SESSION_BYTES]):
            return method(self, session, *args, **kwargs)

    return guarded


# Many parts of the session store API operate not on full session keys, but only
# the first n characters of them (see odoo.http.session.STORED_SESSION_BYTES).
# In particular used by Devices, but Odoo in general seems to promise that this
# partial sid will be safe to store in the database, and can be used to later
# find sessions, even if those sessions are actually longer.
PartialSid: TypeAlias = str


class RedisSessionStore(SessionStore):
    """SessionStore that saves session to redis"""

    def __init__(
        self,
        path=None,
        session_cls=Session,
        *,
        redis,
        prefix="",
        expiration=None,
        anon_expiration=None,
    ):
        super().__init__(path=path, session_cls=session_cls)
        self.redis = redis
        if expiration is None:
            self.expiration = SESSION_LIFETIME
        else:
            self.expiration = expiration
        if anon_expiration is None:
            self.anon_expiration = DEFAULT_SESSION_TIMEOUT_ANONYMOUS
        else:
            self.anon_expiration = anon_expiration
        self.prefix = "session:"
        if prefix:
            self.prefix = f"{self.prefix}:{prefix}:"

    def build_key(self, sid):
        return f"{self.prefix}{sid}"

    @contextmanager
    def _session_lock(self, identifier):
        key = f"{self.prefix}lock:{identifier}"
        held = _HELD_LOCKS.get()
        if key in held:
            yield
            return
        # Bound the wait and fail closed if Redis cannot serialize a rotation.
        with self.redis.lock(key, timeout=30, blocking_timeout=5):
            token = _HELD_LOCKS.set(held | {key})
            try:
                yield
            finally:
                _HELD_LOCKS.reset(token)

    def _is_revoked(self, sid):
        return self.redis.exists(f"{self.prefix}revoked:{sid[:STORED_SESSION_BYTES]}")

    @_guarded
    def save(self, session):
        if self._is_revoked(session.sid):
            return 0
        key = self.build_key(session.sid)

        # If the session has a deletion_time, it is slated for rotation, and
        # should be removed once the rotation window is over. See
        # odoo.http.session.SESSION_DELETION_TIMER.
        # Otherwise, allow to set a custom expiration for a session
        # such as a very short one for monitoring requests
        if session.uid:
            expiration = (
                session.get("deletion_time")
                or session.get("expiration")
                or self.expiration
            )
        else:
            expiration = (
                session.get("deletion_time")
                or session.get("expiration")
                or self.anon_expiration
            )
        if _logger.isEnabledFor(logging.DEBUG):
            if session.uid:
                user_msg = f"user '{session.login}' (id: {session.uid})"
            else:
                user_msg = "anonymous user"
            _logger.debug(
                f"saving session with key '{key}' and "
                f"expiration of {expiration} seconds for {user_msg}"
            )

        data = json.dumps(dict(session), cls=json_encoding.SessionEncoder).encode(
            "utf-8"
        )
        if not (expiration and isinstance(expiration, int)):
            expiration = DEFAULT_SESSION_TIMEOUT_ANONYMOUS
        # One key and one atomic operation: also works with Redis Cluster.
        return self.redis.eval(_SAVE_UNLESS_REVOKED, 1, key, _REVOKED, data, expiration)

    def _revoke_keys(self, keys):
        lifetime = max(
            SESSION_LIFETIME,
            int(self.expiration or SESSION_LIFETIME),
            int(self.anon_expiration or DEFAULT_SESSION_TIMEOUT_ANONYMOUS),
        )
        # Each SET is atomic; an older save can never overwrite its marker.
        for key in keys:
            self.redis.set(key, _REVOKED, ex=lifetime)

    def _revoke_family(self, identifier):
        with self._session_lock(identifier):
            lifetime = max(SESSION_LIFETIME, int(self.expiration or SESSION_LIFETIME))
            self.redis.set(f"{self.prefix}revoked:{identifier}", _REVOKED, ex=lifetime)
            self._revoke_keys(self.redis.scan_iter(match=f"{self.prefix}{identifier}*"))

    @_guarded
    def delete(self, session):
        self._revoke_family(session.sid[:STORED_SESSION_BYTES])

    @_guarded
    def rotate(self, session, env, *, soft=False):
        if self._is_revoked(session.sid):
            db = session.db
            session.clear()
            session.update(get_default_session(), db=db)
            soft = False
        return super().rotate(session, env, soft=soft)

    def get(self, sid, *, keep_sid=False):
        if not self.is_valid_session_id(sid):
            _logger.debug(
                f"session with invalid sid '{sid}' has been asked, returning a new one"
            )
            return self.new()

        if self._is_revoked(sid):
            return self.new()
        key = self.build_key(sid)
        saved = self.redis.get(key)
        if not saved or saved == _REVOKED:
            if keep_sid and saved != _REVOKED:
                _logger.debug(
                    f"session with non-existent key '{key}' has been asked, "
                    "returning an empty one"
                )
                return self.session_cls({}, sid, new=False)
            _logger.debug(
                f"session with non-existent key '{key}' has been asked, "
                "returning a new one"
            )
            return self.new()
        try:
            data = json.loads(saved.decode("utf-8"), cls=json_encoding.SessionDecoder)
        except ValueError:
            _logger.debug(
                f"session for key '{key}' has been asked but its json "
                "content could not be read, it has been reset"
            )
            data = {}
        return self.session_cls(data, sid, new=False)

    def list(self):
        keys = self.redis.keys(f"{self.prefix}*")
        _logger.debug("a listing redis keys has been called")
        return [
            key[len(self.prefix) :]
            for key in keys
            if self.is_valid_session_id(key[len(self.prefix) :].decode())
            and not self._is_revoked(key[len(self.prefix) :].decode())
            and self.redis.get(key) not in (None, _REVOKED)
        ]

    def vacuum(self, *args, **kwargs):
        """Do not garbage collect the sessions

        Redis keys are automatically cleaned at the end of their
        expiration.
        """
        return

    def delete_old_sessions(self, session):
        """
        # Deletion of rotated sessions is handled by updating the sessions'
        # expiry based on deletion_time in save(), so this method is redundant
        # when using a redis store.

        # While this method is not part of the generic SessionStore API, it is
        # defined on the file session store, and is used by the Session itself
        # as part of the session rotation (see odoo.http.session.check).
        """
        return

    def get_missing_session_identifiers(
        self, identifiers: builtins.list[PartialSid]
    ) -> set[PartialSid]:
        """
        Given a list of partial session ids, return a set of those session ids
        which no longer exist in the keystore.

        While this method is not part of the generic SessionStore API, it is
        defined on the file session store, and is used by Odoo's devices to
        figure out what needs to be revoked
        (see odoo.addons.base.models.res_device.ResDeviceLog.__update_revoked).
        """
        identifiers = set(identifiers)
        not_found = set()
        for partial_sid in identifiers:
            key = f"{self.prefix}{partial_sid}*"
            match = self.redis.keys(pattern=key)
            if self._is_revoked(partial_sid) or not any(
                self.redis.get(key) not in (None, _REVOKED) for key in match
            ):
                not_found.add(partial_sid)
        return not_found

    def delete_from_identifiers(self, identifiers: builtins.list[PartialSid]):
        """
        Given a list of partial session ids, remove any that are in the session store.

        While this method is not part of the generic SessionStore API, it is
        defined on the file session store, and is used by devices when revoking
        device sessions (see odoo.addons.base.models.res_device.ResDevice._revoke).
        """
        for identifier in identifiers:
            if not _session_identifier_re.fullmatch(identifier):
                continue
            self._revoke_family(identifier)
