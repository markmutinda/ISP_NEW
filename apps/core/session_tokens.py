"""Helpers for binding JWT sessions to the user's current credentials."""

from django.utils.crypto import constant_time_compare
from rest_framework_simplejwt.tokens import RefreshToken


SESSION_HASH_CLAIM = "session_hash"
STAFF_ROLES = frozenset({"admin", "staff", "support", "technician", "accountant"})


def is_staff_account(user) -> bool:
    """Return whether a user represents a tenant/platform staff account."""
    return bool(
        getattr(user, "is_superuser", False)
        or getattr(user, "is_staff", False)
        or str(getattr(user, "role", "") or "").lower() in STAFF_ROLES
    )


def get_user_session_hash(user) -> str:
    """Derive a signed fingerprint that changes whenever the password changes."""
    return user.get_session_auth_hash()


def bind_token_to_user(token, user):
    """Attach the current credential fingerprint to a JWT token."""
    token[SESSION_HASH_CLAIM] = get_user_session_hash(user)
    return token


def issue_refresh_token(user) -> RefreshToken:
    """Issue a refresh token whose access tokens inherit the session binding."""
    return bind_token_to_user(RefreshToken.for_user(user), user)


def token_matches_user_session(token, user) -> bool:
    """Validate a token's credential fingerprint in constant time.

    Legacy customer tokens are accepted during rollout. Legacy staff tokens are
    intentionally rejected so all existing privileged sessions are re-authenticated
    once this protection is deployed.
    """
    token_hash = token.get(SESSION_HASH_CLAIM)
    if not token_hash:
        return not is_staff_account(user)
    return constant_time_compare(str(token_hash), get_user_session_hash(user))
