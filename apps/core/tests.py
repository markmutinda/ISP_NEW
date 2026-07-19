from django.core import mail
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from datetime import timedelta
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.serializers import ValidationError

from apps.core.models import User
from apps.core.email_delivery import send_transactional_email
from apps.core.otp_service import OTPService, OTPRateLimitedError, OTPError
from apps.core.permissions import HasRoleAccessPolicy, IsAdminOrStaff
from apps.core.rbac_defaults import DEFAULT_ROLE_ACCESS_POLICIES
from apps.core.serializers import UserCreateSerializer
from apps.core.views import RoleAccessPolicyViewSet


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class OTPServiceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.filter(email__isnull=False).first()
        if not self.user:
            suffix = uuid.uuid4().hex[:8]
            self.user = User.objects.create_user(
                email=f"tenant.admin.{suffix}@example.com",
                password="StrongPass123!",
                first_name="Tenant",
                last_name="Admin",
                phone_number=f"+2547000{suffix[:5]}",
                role="admin",
            )

    def test_issue_and_verify_otp(self):
        otp = OTPService.issue_otp(self.user, OTPService.LOGIN_PURPOSE)
        self.assertEqual(len(mail.outbox), 1)
        verified = OTPService.verify_and_consume(
            user=self.user,
            otp_id=otp.id,
            code=otp.code,
            purpose=OTPService.LOGIN_PURPOSE,
        )
        self.assertTrue(verified.is_used)

    def test_rate_limit_is_enforced(self):
        OTPService.issue_otp(self.user, OTPService.PAYMENT_PURPOSE)
        with self.assertRaises(OTPRateLimitedError):
            OTPService.issue_otp(self.user, OTPService.PAYMENT_PURPOSE)

    def test_max_attempts_invalidate_otp(self):
        otp = OTPService.issue_otp(self.user, OTPService.PAYMENT_PURPOSE)
        for _ in range(5):
            with self.assertRaises(OTPError):
                OTPService.verify_and_consume(
                    user=self.user,
                    otp_id=otp.id,
                    code="000000",
                    purpose=OTPService.PAYMENT_PURPOSE,
                )
        otp.refresh_from_db()
        self.assertTrue(otp.is_used)

    def test_expired_otp_rejected(self):
        otp = OTPService.issue_otp(self.user, OTPService.LOGIN_PURPOSE)
        otp.expires_at = timezone.now() - timedelta(seconds=1)
        otp.save(update_fields=["expires_at"])

        with self.assertRaises(OTPError):
            OTPService.verify_and_consume(
                user=self.user,
                otp_id=otp.id,
                code=otp.code,
                purpose=OTPService.LOGIN_PURPOSE,
            )

    def test_login_challenge_resend_and_verify(self):
        challenge = OTPService.start_login_challenge(
            user=self.user,
            tenant_scope="tenant_demo",
            session_scope="browser-a",
            ip_address="127.0.0.1",
        )
        self.assertIsNotNone(challenge.id)

        with self.assertRaises(OTPRateLimitedError):
            OTPService.resend_login_challenge(
                user=self.user,
                challenge_id=str(challenge.id),
                tenant_scope="tenant_demo",
                session_scope="browser-a",
                ip_address="127.0.0.1",
            )

        # move cooldown back
        challenge.last_sent_at = timezone.now() - timedelta(seconds=61)
        challenge.save(update_fields=["last_sent_at"])
        challenge = OTPService.resend_login_challenge(
            user=self.user,
            challenge_id=str(challenge.id),
            tenant_scope="tenant_demo",
            session_scope="browser-a",
            ip_address="127.0.0.1",
        )
        latest_otp = challenge.otps.order_by("-created_at").first()
        OTPService.verify_login_challenge(
            user=self.user,
            challenge_id=str(challenge.id),
            code=latest_otp.code,
            tenant_scope="tenant_demo",
            session_scope="browser-a",
        )


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RESEND_API_KEY="",
)
class TransactionalEmailTests(TestCase):
    def test_smtp_fallback_returns_delivery_feedback(self):
        result = send_transactional_email(
            subject="Welcome",
            recipient="tenant@example.com",
            plain_message="Plain body",
            html_message="<p>HTML body</p>",
            from_email="Netily <noreply@example.com>",
        )

        self.assertTrue(result["sent"])
        self.assertEqual(result["provider"], "smtp")
        self.assertEqual(len(mail.outbox), 1)


class RoleAccessPermissionTests(SimpleTestCase):
    def setUp(self):
        self.permission = HasRoleAccessPolicy()
        self.user = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
            role="technician",
            access_level="",
        )
        self.request = SimpleNamespace(user=self.user, method="GET")

    def _view(self, path, action="list"):
        return SimpleNamespace(required_rbac_path=path, action=action)

    @patch("apps.core.models.RoleAccessPolicy.objects.filter")
    def test_missing_policy_uses_safe_role_defaults(self, policy_filter):
        policy_filter.return_value.first.return_value = None

        self.assertTrue(
            self.permission.has_permission(self.request, self._view("/admin/routers"))
        )
        self.assertFalse(
            self.permission.has_permission(self.request, self._view("/admin/settings"))
        )

    @patch("apps.core.models.RoleAccessPolicy.objects.filter")
    def test_safe_defaults_cover_every_configurable_staff_role(self, policy_filter):
        policy_filter.return_value.first.return_value = None

        for role, tokens in DEFAULT_ROLE_ACCESS_POLICIES.items():
            with self.subTest(role=role):
                self.user.role = role
                self.user.custom_allowed_paths = None
                view_path = next(token for token in tokens if token.endswith("::view")).split("::", 1)[0]
                self.assertTrue(
                    self.permission.has_permission(self.request, self._view(view_path))
                )

    @patch("apps.core.models.RoleAccessPolicy.objects.filter")
    def test_saved_policy_is_applied_immediately(self, policy_filter):
        policy_filter.return_value.first.return_value = SimpleNamespace(
            allowed_paths=["/admin/settings::view", "/admin/network-map::view"]
        )

        self.assertTrue(
            self.permission.has_permission(self.request, self._view("/admin/settings"))
        )
        self.assertFalse(
            self.permission.has_permission(self.request, self._view("/admin/routers"))
        )

    @patch("apps.core.models.RoleAccessPolicy.objects.filter")
    def test_view_grant_does_not_grant_edit_or_create(self, policy_filter):
        policy_filter.return_value.first.return_value = SimpleNamespace(
            allowed_paths=["/admin/routers::view"]
        )
        self.request.method = "POST"

        self.assertFalse(
            self.permission.has_permission(
                self.request,
                self._view("/admin/routers", action=None),
            )
        )

        policy_filter.return_value.first.return_value.allowed_paths.append(
            "/admin/routers::add"
        )
        self.assertTrue(
            self.permission.has_permission(
                self.request,
                self._view("/admin/routers", action=None),
            )
        )

    @patch("apps.core.models.RoleAccessPolicy.objects.filter")
    def test_per_user_policy_overrides_shared_role_policy(self, policy_filter):
        policy_filter.return_value.first.return_value = SimpleNamespace(
            allowed_paths=["/admin/routers::view"]
        )
        self.user.custom_allowed_paths = ["/admin/settings::view"]

        self.assertTrue(
            self.permission.has_permission(self.request, self._view("/admin/settings"))
        )
        self.assertFalse(
            self.permission.has_permission(self.request, self._view("/admin/routers"))
        )
        policy_filter.assert_not_called()

    def test_staff_management_cannot_be_delegated(self):
        serializer = UserCreateSerializer()

        with self.assertRaisesMessage(ValidationError, "cannot be delegated"):
            serializer.validate_custom_allowed_paths(["/admin/staff::view"])

    @patch("apps.core.models.RoleAccessPolicy.objects.filter", side_effect=RuntimeError("db unavailable"))
    def test_policy_lookup_error_does_not_grant_unrestricted_access(self, _policy_filter):
        self.assertFalse(
            self.permission.has_permission(self.request, self._view("/admin/settings"))
        )


class RoleAccessBootstrapTests(SimpleTestCase):
    def test_me_read_does_not_require_staff_page_permission(self):
        view = RoleAccessPolicyViewSet()
        view.action = "me"

        permission_types = {type(permission) for permission in view.get_permissions()}

        self.assertIn(IsAdminOrStaff, permission_types)
        self.assertNotIn(HasRoleAccessPolicy, permission_types)

    @patch.object(RoleAccessPolicyViewSet, "_ensure_defaults")
    @patch.object(RoleAccessPolicyViewSet, "get_queryset")
    def test_me_returns_only_the_signed_in_roles_policy(self, get_queryset, _ensure_defaults):
        allowed_paths = ["/admin/settings::view", "/admin/network-map::view"]
        get_queryset.return_value.filter.return_value.first.return_value = SimpleNamespace(
            allowed_paths=allowed_paths
        )
        user = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
            role="technician",
            tenant_subdomain="tenant-test",
        )
        request = APIRequestFactory().get("/api/v1/core/role-access/me/")
        force_authenticate(request, user=user)

        response = RoleAccessPolicyViewSet.as_view({"get": "me"})(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], "technician")
        self.assertEqual(response.data["allowed_paths"], allowed_paths)
        self.assertFalse(response.data["is_unrestricted"])

    @patch.object(RoleAccessPolicyViewSet, "_ensure_defaults")
    @patch.object(RoleAccessPolicyViewSet, "get_queryset")
    def test_me_prefers_per_user_override(self, get_queryset, ensure_defaults):
        allowed_paths = ["/admin/invoices::view"]
        user = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
            role="support",
            tenant_subdomain="tenant-test",
            custom_allowed_paths=allowed_paths,
        )
        request = APIRequestFactory().get("/api/v1/core/role-access/me/")
        force_authenticate(request, user=user)

        response = RoleAccessPolicyViewSet.as_view({"get": "me"})(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["allowed_paths"], allowed_paths)
        self.assertEqual(response.data["source"], "user")
        ensure_defaults.assert_not_called()
        get_queryset.assert_not_called()
