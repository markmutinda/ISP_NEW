"""Authentication policies shared by tenant and platform API routes."""

from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication

from .session_tokens import token_matches_user_session


class SessionBoundJWTAuthentication(JWTAuthentication):
    """Reject staff JWTs issued before their latest password change.

    The base implementation already rejects deleted and inactive users. The
    additional session fingerprint closes the gap where a valid access or refresh
    token otherwise survives a password reset.
    """

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        if not token_matches_user_session(validated_token, user):
            raise AuthenticationFailed(
                "Your session has expired. Please sign in again.",
                code="session_revoked",
            )
        return user
