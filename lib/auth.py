"""HTTP Basic auth wrapper for kiln-controller routes.

Storage:
    auth.json (path from config.auth_file) holds the username plus a
    PBKDF2-HMAC-SHA256 hashed password. The hash is salted and uses
    200_000 iterations. The plaintext password is never stored.

First-run bootstrap:
    If config.auth_enabled is True but auth.json does not exist, the
    file is created using config.auth_username and
    config.auth_initial_password. The user is expected to change the
    initial password (via the web UI or by editing config.py and
    deleting auth.json) before exposing the controller to the network.

Why HTTP Basic and not session cookies / JWT:
    - The controller has one user. There is no signup flow, no
      password reset email, no role hierarchy.
    - HTTP Basic + HTTPS (even self-signed) is fine for a LAN device.
    - Stateless: every request is independently authenticated, so a
      power cycle never logs anyone out.
    - Browsers prompt for credentials automatically.

For Bottle, this exposes both:
    - ``check_basic_auth(handler, *args, **kwargs)`` for a wrapper
    - ``requires_auth`` decorator for routes
The websocket routes use ``check_websocket_auth`` because Bottle's
websocket transport does not pass through normal route auth.
"""
import base64
import functools
import hashlib
import hmac
import json
import logging
import os
import secrets

import bottle

import config

log = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 200_000
HASH_NAME = "sha256"


def _hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        HASH_NAME,
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return {
        "alg": "pbkdf2_%s" % HASH_NAME,
        "iter": PBKDF2_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(digest).decode("ascii"),
    }


def _verify_password(password, stored):
    try:
        salt = base64.b64decode(stored["salt"])
        expected = base64.b64decode(stored["hash"])
        iters = int(stored.get("iter", PBKDF2_ITERATIONS))
        digest = hashlib.pbkdf2_hmac(
            HASH_NAME, password.encode("utf-8"), salt, iters,
        )
        return hmac.compare_digest(digest, expected)
    except Exception:
        log.exception("auth: verify failed")
        return False


def _ensure_auth_file():
    """Create auth.json from config defaults if missing."""
    path = config.auth_file
    if os.path.exists(path):
        return
    log.warning(
        "auth: %s missing; creating with config.auth_username/"
        "auth_initial_password. CHANGE THE PASSWORD.", path
    )
    creds = {
        "username": config.auth_username,
        "password": _hash_password(config.auth_initial_password),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(creds, f, indent=2)
    os.chmod(path, 0o600)


def _load_credentials():
    with open(config.auth_file, "r", encoding="utf-8") as f:
        return json.load(f)


def is_enabled():
    return bool(getattr(config, "auth_enabled", False))


def init():
    """Initialize the auth file. Call once at startup."""
    if not is_enabled():
        log.info("auth: disabled")
        return
    _ensure_auth_file()
    log.info("auth: enabled (file=%s)", config.auth_file)


def change_password(old_password, new_password):
    """Rotate the password. Returns True on success."""
    if not is_enabled():
        return False
    creds = _load_credentials()
    if not _verify_password(old_password, creds["password"]):
        return False
    creds["password"] = _hash_password(new_password)
    with open(config.auth_file, "w", encoding="utf-8") as f:
        json.dump(creds, f, indent=2)
    return True


def _check_basic(header_value):
    if not header_value or not header_value.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header_value[6:]).decode("utf-8")
        username, _, password = decoded.partition(":")
    except Exception:
        return False
    creds = _load_credentials()
    if not hmac.compare_digest(username, creds["username"]):
        return False
    return _verify_password(password, creds["password"])


def check_request_auth():
    """Returns True if the current bottle.request is authenticated."""
    if not is_enabled():
        return True
    return _check_basic(bottle.request.get_header("Authorization", ""))


def check_websocket_auth(environ):
    """Returns True if the websocket request environ carries valid
    HTTP Basic credentials. Websockets in browsers cannot send
    Authorization headers directly, so we also accept a
    ``token`` querystring carrying base64(username:password).
    """
    if not is_enabled():
        return True
    auth = environ.get("HTTP_AUTHORIZATION", "")
    if _check_basic(auth):
        return True
    qs = environ.get("QUERY_STRING", "")
    for kv in qs.split("&"):
        k, _, v = kv.partition("=")
        if k == "token" and v:
            try:
                from urllib.parse import unquote
                v = unquote(v)
            except Exception:
                pass
            if _check_basic("Basic %s" % v):
                return True
    return False


def requires_auth(handler):
    """Decorator for bottle routes."""

    @functools.wraps(handler)
    def wrapper(*args, **kwargs):
        if not check_request_auth():
            bottle.response.headers["WWW-Authenticate"] = (
                'Basic realm="kiln-controller"'
            )
            bottle.response.status = 401
            return "Authentication required."
        return handler(*args, **kwargs)

    return wrapper
