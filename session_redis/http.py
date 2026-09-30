# Copyright 2016-2024 Camptocamp SA
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html)
import logging
import os
import shutil

from odoo.http import session as http_session
from odoo.tools import config

from .session import RedisSessionStore
from .strtobool import strtobool

_logger = logging.getLogger(__name__)

try:
    import redis
    from redis.sentinel import Sentinel
except ImportError:
    redis = None  # noqa
    _logger.debug("Cannot 'import redis'.")


def is_true(strval):
    return bool(strtobool(strval or "0".lower()))


sentinel_host = os.getenv("ODOO_SESSION_REDIS_SENTINEL_HOST")
sentinel_master_name = os.getenv("ODOO_SESSION_REDIS_SENTINEL_MASTER_NAME")
if sentinel_host and not sentinel_master_name:
    raise Exception(
        "ODOO_SESSION_REDIS_SENTINEL_MASTER_NAME must be defined "
        "when using session_redis"
    )
sentinel_port = int(os.getenv("ODOO_SESSION_REDIS_SENTINEL_PORT", 26379))
host = os.getenv("ODOO_SESSION_REDIS_HOST", "localhost")
port = int(os.getenv("ODOO_SESSION_REDIS_PORT", 6379))
prefix = os.getenv("ODOO_SESSION_REDIS_PREFIX")
url = os.getenv("ODOO_SESSION_REDIS_URL")
password = os.getenv("ODOO_SESSION_REDIS_PASSWORD")
expiration = os.getenv("ODOO_SESSION_REDIS_EXPIRATION")
anon_expiration = os.getenv("ODOO_SESSION_REDIS_EXPIRATION_ANONYMOUS")
# For non url connections
ssl = os.getenv("ODOO_SESSION_REDIS_SSL", "1")
ssl_cert_reqs = os.getenv("ODOO_SESSION_REDIS_SSL_CERT_REQS", "1")
redis_cluster = os.getenv("ODOO_SESSION_REDIS_CLUSTER", "0")


def build_redis_client():
    if sentinel_host:
        sentinel = Sentinel([(sentinel_host, sentinel_port)], password=password)
        return sentinel.master_for(sentinel_master_name)
    if url:
        return redis.from_url(url)
    if is_true(redis_cluster):
        return redis.RedisCluster(
            host=host,
            port=port,
            password=password,
            ssl=is_true(ssl),
            ssl_cert_reqs=is_true(ssl_cert_reqs),
        )
    return redis.Redis(
        host=host,
        port=port,
        password=password,
        ssl=is_true(ssl),
        ssl_cert_reqs=is_true(ssl_cert_reqs),
    )


class ConfiguredRedisSessionStore(RedisSessionStore):
    """RedisSessionStore pre-bound to the environment's Redis settings.

    20.0 builds the store itself, as ``SessionStore(path=config.session_dir)``,
    so the connection settings cannot be passed in at the call site any more.
    """

    def __init__(self, path=None, session_cls=http_session.Session):
        super().__init__(
            build_redis_client(),
            path=path,
            session_cls=session_cls,
            prefix=prefix,
            expiration=expiration,
            anon_expiration=anon_expiration,
        )


def purge_fs_sessions(path):
    """Remove the filesystem sessions left behind before Redis took over.

    Odoo's filesystem session store keeps sessions in two-character
    subdirectories, so the entries are directories as well as files.
    """
    if not os.path.isdir(path):
        _logger.warning("Session directory '%s' does not exist.", path)
        return

    entries = os.listdir(path)
    failed = 0
    for fname in entries:
        entry = os.path.join(path, fname)
        try:
            if os.path.isdir(entry) and not os.path.islink(entry):
                shutil.rmtree(entry)
            else:
                os.unlink(entry)
        except OSError:
            failed += 1
            _logger.debug("Could not remove '%s'", entry, exc_info=True)
    if failed:
        _logger.warning(
            "Could not purge %d of %d entries in the filesystem session "
            "directory '%s'.",
            failed,
            len(entries),
            path,
        )


if is_true(os.getenv("ODOO_SESSION_REDIS")):
    if sentinel_host:
        _logger.debug(
            "HTTP sessions stored in Redis with prefix '%s'. Using Sentinel on %s:%s",
            prefix or "",
            sentinel_host,
            sentinel_port,
        )
    else:
        _logger.debug(
            "HTTP sessions stored in Redis with prefix '%s' on %s:%s",
            prefix or "",
            host,
            port,
        )
    # 20.0 moved the store off the WSGI application: it is now built by the
    # module-level odoo.http.session.session_store(), whose body is
    # `return SessionStore(path=config.session_dir)`. That name is resolved as
    # a global at call time, so rebinding the class here reaches every caller
    # -- including odoo.http.dispatcher, odoo.http.router and
    # odoo.tests.common, which all import the *function* by value and so
    # cannot be reached by patching session_store itself.
    http_session.SessionStore = ConfiguredRedisSessionStore
    # session_store() is functools.cache'd, so drop anything a caller built
    # from the filesystem store before we got loaded.
    http_session.session_store.cache_clear()
    # clean the existing sessions on the file system
    purge_fs_sessions(config.session_dir)
