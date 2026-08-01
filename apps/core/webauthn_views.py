"""
Passkey (WebAuthn) registration and login.
Uses the `webauthn` PyPI package. Relying party ID must match the
browser's document origin host (no scheme/port).
"""
import base64
import json
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from .session_tokens import issue_refresh_token

from webauthn import (
    generate_registration_options,
    verify_registration_response,
    generate_authentication_options,
    verify_authentication_response,
    options_to_json,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    UserVerificationRequirement,
    ResidentKeyRequirement,
    PublicKeyCredentialDescriptor,
)
from webauthn.helpers import bytes_to_base64url, base64url_to_bytes

from .models import WebAuthnCredential

logger = logging.getLogger(__name__)
User = get_user_model()

CHALLENGE_TTL = 120  # seconds


def _rp_id(request) -> str:
    return getattr(settings, "WEBAUTHN_RP_ID", None) or request.get_host().split(":")[0]


def _rp_name() -> str:
    return getattr(settings, "WEBAUTHN_RP_NAME", "Netily")


def _origin(request) -> str:
    scheme = "https" if request.is_secure() else "http"
    return f"{scheme}://{request.get_host()}"


class PasskeyRegisterOptionsView(APIView):
    """Step 1: authenticated user requests registration options for a new passkey."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        existing = list(
            WebAuthnCredential.objects.filter(user=user).values_list("credential_id", flat=True)
        )
        options = generate_registration_options(
            rp_id=_rp_id(request),
            rp_name=_rp_name(),
            user_id=str(user.id).encode(),
            user_name=user.email or user.phone_number,
            user_display_name=user.get_full_name() or user.email or user.phone_number,
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(cid)) for cid in existing
            ],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.PREFERRED,
            ),
        )
        cache.set(f"webauthn_reg_challenge:{user.id}", bytes_to_base64url(options.challenge), timeout=CHALLENGE_TTL)
        return Response(json.loads(options_to_json(options)))


class PasskeyRegisterVerifyView(APIView):
    """Step 2: browser posts back the attestation response."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        expected_challenge_b64 = cache.get(f"webauthn_reg_challenge:{user.id}")
        if not expected_challenge_b64:
            return Response({"detail": "Registration challenge expired. Please try again."}, status=400)

        try:
            verification = verify_registration_response(
                credential=request.data.get("credential"),
                expected_challenge=base64url_to_bytes(expected_challenge_b64),
                expected_origin=_origin(request),
                expected_rp_id=_rp_id(request),
            )
        except Exception as exc:
            logger.warning("Passkey registration verify failed for user_id=%s: %s", user.id, exc)
            return Response({"detail": "Could not verify passkey. Please try again."}, status=400)

        cache.delete(f"webauthn_reg_challenge:{user.id}")

        WebAuthnCredential.objects.update_or_create(
            credential_id=bytes_to_base64url(verification.credential_id),
            defaults={
                "user": user,
                "public_key": bytes_to_base64url(verification.credential_public_key),
                "sign_count": verification.sign_count,
                "device_label": request.data.get("device_label", "")[:120],
            },
        )
        return Response({"status": "ok", "message": "Passkey registered."})


class PasskeyLoginOptionsView(APIView):
    """Step 1: anonymous — request auth options. If email given, scope to that user's credentials."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        allow_credentials = []
        if email:
            user = User.objects.filter(email__iexact=email).first()
            if user:
                allow_credentials = [
                    PublicKeyCredentialDescriptor(id=base64url_to_bytes(cid))
                    for cid in WebAuthnCredential.objects.filter(user=user).values_list("credential_id", flat=True)
                ]

        options = generate_authentication_options(
            rp_id=_rp_id(request),
            allow_credentials=allow_credentials or None,
            user_verification=UserVerificationRequirement.PREFERRED,
        )
        session_key = request.data.get("session_key") or bytes_to_base64url(options.challenge)
        cache.set(f"webauthn_auth_challenge:{session_key}", bytes_to_base64url(options.challenge), timeout=CHALLENGE_TTL)
        payload = json.loads(options_to_json(options))
        payload["session_key"] = session_key
        return Response(payload)


class PasskeyLoginVerifyView(APIView):
    """Step 2: verify assertion, issue JWT — mirrors CustomTokenObtainPairView's payload shape."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        session_key = request.data.get("session_key")
        expected_challenge_b64 = cache.get(f"webauthn_auth_challenge:{session_key}")
        if not expected_challenge_b64:
            return Response({"detail": "Passkey session expired. Please try again."}, status=400)

        credential = request.data.get("credential") or {}
        credential_id_b64 = credential.get("id")
        stored = WebAuthnCredential.objects.select_related("user").filter(credential_id=credential_id_b64).first()
        if not stored:
            return Response({"detail": "Passkey not recognized."}, status=401)

        try:
            verification = verify_authentication_response(
                credential=credential,
                expected_challenge=base64url_to_bytes(expected_challenge_b64),
                expected_origin=_origin(request),
                expected_rp_id=_rp_id(request),
                credential_public_key=base64url_to_bytes(stored.public_key),
                credential_current_sign_count=stored.sign_count,
            )
        except Exception as exc:
            logger.warning("Passkey login verify failed: %s", exc)
            return Response({"detail": "Passkey verification failed."}, status=401)

        cache.delete(f"webauthn_auth_challenge:{session_key}")

        stored.sign_count = verification.new_sign_count
        stored.last_used_at = timezone.now()
        stored.save(update_fields=["sign_count", "last_used_at"])

        user = stored.user
        if not user.is_active:
            return Response({"detail": "Account is disabled."}, status=403)

        refresh = issue_refresh_token(user)
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])

        return Response({
            "refresh": str(refresh),
            "access": str(refresh.access_token),
            "user": {
                "id": user.id,
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "role": user.role,
                "is_verified": user.is_verified,
                "is_staff": user.is_staff,
                "is_superuser": user.is_superuser,
            },
        })


# ────────────────────────────────────────────────────────────────
#  PASSKEY MANAGEMENT (List & Delete)
# ────────────────────────────────────────────────────────────────

class PasskeyListView(APIView):
    """List the authenticated user's registered passkeys."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        creds = WebAuthnCredential.objects.filter(user=request.user).order_by("-created_at")
        return Response([
            {
                "id": c.id,
                "device_label": c.device_label or "Unnamed device",
                "created_at": c.created_at,
                "last_used_at": c.last_used_at,
            }
            for c in creds
        ])


class PasskeyDeleteView(APIView):
    """Delete one of the authenticated user's passkeys."""
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        deleted, _ = WebAuthnCredential.objects.filter(user=request.user, pk=pk).delete()
        if not deleted:
            return Response({"detail": "Passkey not found."}, status=404)
        return Response(status=204)
