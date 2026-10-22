"""API endpoints for tenant Payment Methods PIN setup and verification."""

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.models import AuditLog, GlobalSystemSettings
from apps.core.payment_method_pin import (
    LOCK_SECONDS,
    MAX_FAILED_ATTEMPTS,
    UNLOCK_SECONDS,
    lock_seconds_remaining,
    payment_method_pin_is_unlocked,
    record_failed_attempt,
    unlock_payment_methods,
    validate_new_pin,
)


def _can_manage_pin(user) -> bool:
    return bool(
        getattr(user, "is_superuser", False)
        or (
            str(getattr(user, "role", "") or "").lower() in {"admin", "super_admin", "superadmin"}
            and getattr(user, "is_staff", False)
        )
    )


def _status_payload(request):
    settings = GlobalSystemSettings.get_solo()
    bypass = bool(getattr(request.user, "is_superuser", False))
    return {
        "configured": bool(settings.payment_methods_pin_hash),
        "unlocked": bypass or payment_method_pin_is_unlocked(request),
        "bypass": bypass,
        "can_setup": _can_manage_pin(request.user),
        "locked_seconds": lock_seconds_remaining(request),
        "unlock_seconds": UNLOCK_SECONDS,
        "updated_at": settings.payment_methods_pin_updated_at,
    }


def _audit(request, action):
    AuditLog.log_action(
        user=request.user,
        action=action,
        model_name="Payment Methods PIN",
        object_id="tenant-security",
        object_repr="Payment Methods security PIN",
        changes={"security_control": "payment_methods_pin"},
        ip_address=request.META.get("REMOTE_ADDR"),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
    )


class PaymentMethodPinStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(_status_payload(request))


class PaymentMethodPinSetupView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not _can_manage_pin(request.user):
            return Response(
                {"detail": "Only the tenant administrator can create the Payment Methods PIN."},
                status=status.HTTP_403_FORBIDDEN,
            )
        pin = str(request.data.get("pin") or "")
        confirmation = str(request.data.get("pin_confirm") or "")
        if pin != confirmation:
            return Response({"pin_confirm": ["PINs do not match."]}, status=status.HTTP_400_BAD_REQUEST)
        try:
            pin = validate_new_pin(pin)
        except ValueError as exc:
            return Response({"pin": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            settings = GlobalSystemSettings.objects.select_for_update().get(pk=GlobalSystemSettings.get_solo().pk)
            if settings.payment_methods_pin_hash:
                return Response(
                    {"detail": "A Payment Methods PIN already exists. Use Change PIN instead."},
                    status=status.HTTP_409_CONFLICT,
                )
            settings.payment_methods_pin_hash = make_password(pin)
            settings.payment_methods_pin_updated_at = timezone.now()
            settings.save(update_fields=["payment_methods_pin_hash", "payment_methods_pin_updated_at"])

        unlock_payment_methods(request)
        _audit(request, "create")
        return Response(_status_payload(request), status=status.HTTP_201_CREATED)


class PaymentMethodPinVerifyView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if getattr(request.user, "is_superuser", False):
            return Response(_status_payload(request))
        remaining = lock_seconds_remaining(request)
        if remaining:
            return Response(
                {"detail": f"Too many incorrect attempts. Try again in {remaining} seconds.", "locked_seconds": remaining},
                status=status.HTTP_423_LOCKED,
            )
        settings = GlobalSystemSettings.get_solo()
        if not settings.payment_methods_pin_hash:
            return Response(
                {"detail": "Set a Payment Methods PIN before opening this page.", "setup_required": True},
                status=status.HTTP_409_CONFLICT,
            )
        pin = str(request.data.get("pin") or "").strip()
        if not check_password(pin, settings.payment_methods_pin_hash):
            attempts = record_failed_attempt(request)
            attempts_left = max(0, MAX_FAILED_ATTEMPTS - attempts)
            detail = (
                f"Incorrect PIN. {attempts_left} attempt{'s' if attempts_left != 1 else ''} remaining."
                if attempts_left
                else f"Too many incorrect attempts. Try again in {LOCK_SECONDS // 60} minutes."
            )
            return Response(
                {"detail": detail, "attempts_left": attempts_left, "locked_seconds": LOCK_SECONDS if not attempts_left else 0},
                status=status.HTTP_423_LOCKED if not attempts_left else status.HTTP_400_BAD_REQUEST,
            )
        unlock_payment_methods(request)
        return Response(_status_payload(request))


class PaymentMethodPinChangeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not _can_manage_pin(request.user):
            return Response(
                {"detail": "Only the tenant administrator can change the Payment Methods PIN."},
                status=status.HTTP_403_FORBIDDEN,
            )
        settings = GlobalSystemSettings.get_solo()
        if not settings.payment_methods_pin_hash:
            return Response({"detail": "Set the PIN first."}, status=status.HTTP_409_CONFLICT)
        if not getattr(request.user, "is_superuser", False):
            current_pin = str(request.data.get("current_pin") or "").strip()
            if not check_password(current_pin, settings.payment_methods_pin_hash):
                return Response({"current_pin": ["Current PIN is incorrect."]}, status=status.HTTP_400_BAD_REQUEST)
        pin = str(request.data.get("pin") or "")
        confirmation = str(request.data.get("pin_confirm") or "")
        if pin != confirmation:
            return Response({"pin_confirm": ["PINs do not match."]}, status=status.HTTP_400_BAD_REQUEST)
        try:
            pin = validate_new_pin(pin)
        except ValueError as exc:
            return Response({"pin": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        settings.payment_methods_pin_hash = make_password(pin)
        settings.payment_methods_pin_updated_at = timezone.now()
        settings.save(update_fields=["payment_methods_pin_hash", "payment_methods_pin_updated_at"])
        unlock_payment_methods(request)
        _audit(request, "update")
        return Response(_status_payload(request))
