"""Clerk B2B Organization auth — only active when config.CLERK_ENABLED is True.

Clerk proves identity and ISKCON Shirpur organization membership; it is never
the source of truth for the app's own role/department assignment. That stays
in the local `users` table exactly as it always has (see app.py's
role_required/_manager_department_ids/_visible_departments), keyed by a new
`clerk_id` column instead of a password.

Session tokens are verified locally with PyJWT against Clerk's public key
(CLERK_JWT_KEY, or Clerk's JWKS fetched once per process), so page navigation
never waits on Clerk. The Clerk SDK (clerk-backend-api==7.0.0) is only used for
a person's first sign-in (users.get) and invitations.
"""
import logging
import threading
import time
from functools import wraps
from urllib.parse import urlsplit

import jwt
import requests
from cryptography.hazmat.primitives import serialization
from flask import g, request, redirect, url_for, flash, abort

import config
from database.db import get_db

logger = logging.getLogger(__name__)

_clerk_sdk = None


def get_clerk_sdk():
    global _clerk_sdk
    if _clerk_sdk is None:
        from clerk_backend_api import Clerk
        _clerk_sdk = Clerk(bearer_auth=config.CLERK_SECRET_KEY)
    return _clerk_sdk


def _extract_clerk_token(flask_request):
    """Bearer Authorization header first, then Clerk's `__session` cookie."""
    auth = flask_request.headers.get("Authorization") or flask_request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        if token:
            return token
    cookie_token = flask_request.cookies.get("__session")
    return cookie_token.strip() if cookie_token else None


# Clerk's public signing keys, by key id. Filled once per server process from
# Clerk's JWKS endpoint (unless CLERK_JWT_KEY is set, in which case it's never
# needed) and kept for the life of the process — the keys only change if the
# Clerk instance rotates them, which a lookup miss below handles.
_jwks_keys = {}
_jwks_fetched_at = 0.0
_jwks_lock = threading.Lock()
# A token with an unknown key id re-fetches at most this often, so a stream of
# forged tokens can't turn into a stream of calls to Clerk.
_JWKS_REFETCH_INTERVAL = 600
# Clock drift allowed between this server and Clerk's.
_CLOCK_SKEW_SECONDS = 5


def _refresh_jwks():
    global _jwks_fetched_at
    _jwks_fetched_at = time.time()
    response = requests.get(
        "https://api.clerk.com/v1/jwks",
        headers={"Authorization": f"Bearer {config.CLERK_SECRET_KEY}", "Accept": "application/json"},
        timeout=5,
    )
    response.raise_for_status()
    for jwk in response.json().get("keys", []):
        if jwk.get("kid"):
            _jwks_keys[jwk["kid"]] = jwt.PyJWK(jwk, algorithm="RS256").key


# Parsed CLERK_JWT_KEY values, or None for one that isn't a valid public key.
_configured_keys = {}


def _configured_jwt_key():
    """CLERK_JWT_KEY as a loaded public key, or None if it's unusable (e.g. a
    placeholder left in .env) — then Clerk's published keys are used instead,
    rather than every signed-in page failing."""
    pem = config.CLERK_JWT_KEY
    if pem not in _configured_keys:
        try:
            _configured_keys[pem] = serialization.load_pem_public_key(pem.encode())
        except ValueError:
            logger.error("CLERK_JWT_KEY in .env is not a valid public key — ignoring it and "
                         "using Clerk's published signing keys instead.")
            _configured_keys[pem] = None
    return _configured_keys[pem]


_mismatch_warned = False


def _warn_jwt_key_mismatch():
    global _mismatch_warned
    if not _mismatch_warned:
        _mismatch_warned = True
        logger.error("CLERK_JWT_KEY doesn't match the Clerk instance that signed this session "
                     "(e.g. development key with production Clerk keys) — update or remove it.")


def _published_key(kid):
    """Clerk's own published key for this key id (JWKS, fetched once and kept)."""
    key = _jwks_keys.get(kid)
    if key is not None:
        return key
    with _jwks_lock:
        if kid not in _jwks_keys and time.time() - _jwks_fetched_at > _JWKS_REFETCH_INTERVAL:
            try:
                _refresh_jwks()
            except (requests.RequestException, ValueError, jwt.PyJWKError) as e:
                logger.warning(f"Could not fetch Clerk signing keys: {e}")
    return _jwks_keys.get(kid)


def _azp_matches_host(azp, flask_request):
    """Clerk sets `azp` to the origin of the page that requested the token.
    Compared by host only: behind Render's proxy Flask sees http:// while the
    browser origin is https://, and that difference isn't a different site."""
    return bool(azp) and urlsplit(azp).netloc == flask_request.host


def verify_session_token(token, flask_request):
    """Verify a Clerk session JWT locally — signature (RS256), expiry and
    authorized party — with no call to Clerk. Returns (claims, expired):
    claims is the payload dict or None; expired is True only for a genuine,
    correctly signed token that has simply passed its 60-second lifetime."""
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.InvalidTokenError:
        return None, False

    configured = _configured_jwt_key() if config.CLERK_JWT_KEY else None
    candidates = [configured] if configured is not None else []
    candidates.append(None)  # placeholder: Clerk's published key, looked up only if needed

    claims = None
    for key in candidates:
        if key is None:
            key = _published_key(kid)
            if key is None:
                return None, False
        try:
            claims = jwt.decode(
                token, key, algorithms=["RS256"], leeway=_CLOCK_SKEW_SECONDS,
                options={"require": ["exp", "iat", "sub"], "verify_aud": False},
            )
            break
        except jwt.ExpiredSignatureError:
            return None, True
        except jwt.InvalidSignatureError:
            if key is configured:
                # CLERK_JWT_KEY is from a different Clerk instance — typically still the
                # development key after switching to production. Try Clerk's own key.
                _warn_jwt_key_mismatch()
                continue
            return None, False
        except jwt.PyJWTError:
            # Invalid token, or a key PyJWT can't use — treat as signed out, never a 500.
            return None, False
    if claims is None:
        return None, False

    if not _azp_matches_host(claims.get("azp"), flask_request):
        return None, False
    return claims, False


def authenticate_clerk_request(flask_request):
    """Verifies the incoming request's Clerk session (from the Authorization
    header or the __session cookie Clerk's JS SDK sets). Returns the JWT
    claims dict (with sub/sid) if signed in, else None.

    Verification is local (see verify_session_token) — ordinary page
    navigation makes no request to Clerk. When the token has only expired,
    g.clerk_token_expired is set so the caller can let Clerk's JS refresh it
    instead of treating the person as signed out.

    Cached on `g` for the lifetime of the request: load_logged_in_user()
    (before_request) and clerk_org_required() (per-route) both call this.
    """
    if hasattr(g, '_clerk_auth_claims'):
        return g._clerk_auth_claims

    claims, expired = None, False
    token = _extract_clerk_token(flask_request)
    if config.CLERK_SECRET_KEY and token:
        claims, expired = verify_session_token(token, flask_request)

    g._clerk_auth_claims = claims
    g.clerk_token_expired = expired
    return claims


def _fetch_clerk_identity(user_id):
    """Session token claims don't include email/name by default — fetched via
    the Backend API only on a user's first login (see load_clerk_user below),
    then cached locally via the clerk_id on their users row. Returns
    (email, display_name); email is None if Clerk has no primary email."""
    sdk = get_clerk_sdk()
    clerk_user = sdk.users.get(user_id=user_id)
    email = None
    for addr in clerk_user.email_addresses:
        if addr.id == clerk_user.primary_email_address_id:
            email = addr.email_address.lower()
            break
    name = " ".join(p for p in (clerk_user.first_name, clerk_user.last_name) if p).strip()
    return email, (name or (email.split("@")[0] if email else "New User"))


_USER_SELECT = """
    SELECT u.*, r.name as role FROM users u
    JOIN roles r ON u.role_id = r.id
"""


def load_clerk_user(db, cursor, claims):
    """Returns the local users row (same shape load_logged_in_user produces
    for the password-auth path) for a verified Clerk session, provisioning a
    `pending` one on first sign-in if this person has no local account yet.
    Returns None only for a deactivated/rejected account, or an email already
    linked to a different Clerk identity — those stay locked out (403).

    Resolution order, each step only on a miss of the previous one:
      1. active row already linked by clerk_id — the normal every-request path
      2. an Admin-created row (add_user) with this email and no clerk_id yet —
         link it once by backfilling clerk_id, keeping its Admin-assigned role
      3. no row with this email at all — INSERT a new `pending` user. A pending
         user can see nothing (app.py's enforce_pending_lockout) until an Admin
         approves them and assigns a role.
    Takes `db` (not just `cursor`) to commit these writes itself, matching every
    other caller in this codebase owning its own commit/close around a cursor.
    """
    clerk_user_id = claims.get("sub")
    if not clerk_user_id:
        return None

    cursor.execute(_USER_SELECT + " WHERE u.clerk_id = ?", (clerk_user_id,))
    user = cursor.fetchone()
    if user is not None:
        # A deactivated account is answered from the local row too — looking it
        # up at Clerk again would cost a Clerk API call on every request it makes.
        return user if user['is_active'] else None

    # Only reached on a person's very first sign-in: the one Clerk API call here.
    email, name = _fetch_clerk_identity(clerk_user_id)
    if not email:
        return None

    cursor.execute(_USER_SELECT + " WHERE u.email = ?", (email,))
    existing = cursor.fetchone()
    if existing is not None:
        if existing['clerk_id'] is not None or not existing['is_active']:
            # Rejected/deactivated, or this email belongs to another Clerk
            # identity — never silently re-provision either case.
            return None
        cursor.execute("UPDATE users SET clerk_id = ? WHERE id = ?", (clerk_user_id, existing['id']))
        db.commit()
    else:
        try:
            cursor.execute(
                "INSERT INTO users (name, email, password_hash, clerk_id, role_id, is_active) "
                "VALUES (?, ?, NULL, ?, (SELECT id FROM roles WHERE name = 'pending'), 1)",
                (name, email, clerk_user_id),
            )
            db.commit()
        except Exception:
            # A first sign-in fires several requests at once (page + CSS/JS), each
            # running this hook — if a parallel request inserted first, the UNIQUE
            # constraint rejects ours. Fall through and read the row it created.
            db.rollback()

    cursor.execute(_USER_SELECT + " WHERE u.clerk_id = ? AND u.is_active = 1", (clerk_user_id,))
    return cursor.fetchone()


def redirect_to_sign_in():
    """Send a request without a valid Clerk session to /login, remembering the
    page so login.html can return there. When the token only expired, the
    login page's Clerk JS refreshes it and comes straight back — no sign-in
    form, no "please sign in" message."""
    expired = g.get('clerk_token_expired', False)
    if request.method == "GET":
        back_to = request.full_path.rstrip("?")
    else:
        referrer = urlsplit(request.referrer or "")
        back_to = referrer.path if referrer.netloc == request.host else None
        if expired:
            flash("Your sign-in had expired, so that change wasn't saved. Please try again.", "warning")
    if not expired:
        flash("Please sign in to access this page.", "warning")
    return redirect(url_for('login', next=back_to))


def clerk_org_required(f):
    """Validate the Clerk session and require a local `users` row (matched by
    clerk_id / verified email, or auto-provisioned as `pending`).

    Clerk organization membership is intentionally NOT checked: any authenticated
    person gets a local account, and access is decided entirely by the role an
    Admin assigns locally — a `pending` account sees nothing (app.py's
    enforce_pending_lockout). The name is kept so existing call sites don't change.

    If CLERK_SECRET_KEY is missing, this decorator is a no-op so existing
    local session auth continues to work.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not config.CLERK_SECRET_KEY:
            return f(*args, **kwargs)

        claims = authenticate_clerk_request(request)
        if claims is None:
            return redirect_to_sign_in()

        if g.user is None:
            # load_logged_in_user() already tried and failed to match/provision
            # this Clerk identity to a local users row (role_id).
            abort(403)

        return f(*args, **kwargs)
    return decorated
