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
from apps.core.permissions import HasCompanyAccess, HasRoleAccessPolicy, IsAdminOrStaff
from apps.core.rbac_defaults import DEFAULT_ROLE_ACCESS_POLICIES
from apps.core.serializers import UserCreateSerializer
from apps.core.session_tokens import (
    SESSION_HASH_CLAIM,
    bind_token_to_user,
    token_matches_user_session,
)
from apps.core.views import RoleAccessPolicyViewSet
from apps.notifications.services.lead_alert_service import build_lead_alert_message


class SessionTokenTests(SimpleTestCase):
    def setUp(self):
        self.user = User(
            email="staff@example.com",
            phone_number="+254700000001",
            role="technician",
            is_staff=True,
        )
        self.user.set_password("FirstStrongPassword123!")

    def test_staff_token_is_invalid_after_password_change(self):
        token = {}
        bind_token_to_user(token, self.user)
        self.assertTrue(token_matches_user_session(token, self.user))

        self.user.set_password("SecondStrongPassword456!")

        self.assertFalse(token_matches_user_session(token, self.user))

    def test_legacy_staff_token_requires_fresh_login(self):
        self.assertFalse(token_matches_user_session({}, self.user))

    def test_bound_token_contains_only_a_signed_session_fingerprint(self):
        token = {}
        bind_token_to_user(token, self.user)

        self.assertIn(SESSION_HASH_CLAIM, token)
        self.assertNotIn("FirstStrongPassword123!", token[SESSION_HASH_CLAIM])


class LeadAlertMessageTests(SimpleTestCase):
    def test_message_escapes_user_input_and_includes_affiliate_context(self):
        lead = SimpleNamespace(
            pk=42,
            name="A <b>Network</b>",
            company_name="ISP & Sons",
            phone="+254700000000",
            email="lead@example.com",
            lead_source="Affiliate Referral",
            referral_name="",
            message="Need <fast> setup",
        )

        message = build_lead_alert_message(
            lead,
            {"affiliate_name": "Jane & Co", "referral_code": "REF123"},
        )

        self.assertIn("Lead ID:</b> 42", message)
        self.assertIn("A &lt;b&gt;Network&lt;/b&gt;", message)
        self.assertIn("Jane &amp; Co", message)
        self.assertIn("REF123", message)
        self.assertNotIn("Need <fast>", message)


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

    def _request(self, path, method="GET"):
        return SimpleNamespace(user=self.user, method=method, path=path)

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

    def test_supporting_endpoint_accepts_any_authorized_parent_page(self):
        self.user.custom_allowed_paths = ["/admin/users::view"]
        view = SimpleNamespace(
            required_rbac_paths=("/admin/users", "/admin/radius"),
            action=None,
        )

        self.assertTrue(self.permission.has_permission(self.request, view))

        self.user.custom_allowed_paths = ["/admin/radius::view"]
        self.assertTrue(self.permission.has_permission(self.request, view))

        self.user.custom_allowed_paths = ["/admin/invoices::view"]
        self.assertFalse(self.permission.has_permission(self.request, view))

    def test_any_parent_page_still_requires_the_correct_action(self):
        self.request.method = "PATCH"
        view = SimpleNamespace(
            required_rbac_paths=("/admin/users", "/admin/radius"),
            action=None,
        )
        self.user.custom_allowed_paths = ["/admin/users::view"]
        self.assertFalse(self.permission.has_permission(self.request, view))

        self.user.custom_allowed_paths.append("/admin/users::edit")
        self.assertTrue(self.permission.has_permission(self.request, view))

    def test_staff_management_cannot_be_delegated(self):
        serializer = UserCreateSerializer()

        with self.assertRaisesMessage(ValidationError, "cannot be delegated"):
            serializer.validate_custom_allowed_paths(["/admin/staff::view"])

    def test_dashboard_access_tokens_are_normalized(self):
        serializer = UserCreateSerializer()

        self.assertEqual(
            serializer.validate_custom_allowed_paths([
                "/admin/users::edit",
                "/admin/users::view",
                "/admin/users::edit",
                "/admin/leads",
            ]),
            [
                "/admin/users::view",
                "/admin/users::edit",
                "/admin/leads::view",
            ],
        )

    def test_dashboard_access_tokens_reject_unknown_actions(self):
        serializer = UserCreateSerializer()

        with self.assertRaisesMessage(ValidationError, "unknown action"):
            serializer.validate_custom_allowed_paths(["/admin/users::approve_everything"])

    @patch("apps.core.models.RoleAccessPolicy.objects.filter", side_effect=RuntimeError("db unavailable"))
    def test_policy_lookup_error_does_not_grant_unrestricted_access(self, _policy_filter):
        self.assertFalse(
            self.permission.has_permission(self.request, self._view("/admin/settings"))
        )

    def test_missing_view_path_is_inferred_from_api_url(self):
        self.user.custom_allowed_paths = ["/admin/payments::view"]
        view = SimpleNamespace(action="list")

        self.assertTrue(
            self.permission.has_permission(
                self._request("/api/v1/billing/payments/"),
                view,
            )
        )
        self.assertFalse(
            self.permission.has_permission(
                self._request("/api/v1/billing/invoices/"),
                view,
            )
        )

    def test_inferred_api_url_still_requires_matching_action(self):
        self.user.custom_allowed_paths = ["/admin/payments::view"]
        view = SimpleNamespace(action=None)

        self.assertFalse(
            self.permission.has_permission(
                self._request("/api/v1/billing/payments/", method="POST"),
                view,
            )
        )

        self.user.custom_allowed_paths.append("/admin/payments::add")
        self.assertTrue(
            self.permission.has_permission(
                self._request("/api/v1/billing/payments/", method="POST"),
                view,
            )
        )

    def test_explicit_none_path_remains_bootstrap_safe(self):
        self.user.custom_allowed_paths = ["/admin/payments::view"]
        view = SimpleNamespace(
            action="me",
            get_required_rbac_path=lambda request: None,
        )

        self.assertTrue(
            self.permission.has_permission(
                self._request("/api/v1/core/users/me/"),
                view,
            )
        )


class CompanyObjectAccessTests(SimpleTestCase):
    def test_tenant_staff_can_access_router_owned_by_tenant_subdomain(self):
        user = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
            company=None,
            company_name="Acme ISP",
        )
        request = SimpleNamespace(
            user=user,
            tenant=SimpleNamespace(schema_name="tenant_acme", subdomain="acme"),
        )
        router = SimpleNamespace(company_name="Acme ISP", tenant_subdomain="acme")

        self.assertTrue(HasCompanyAccess().has_object_permission(request, None, router))

    def test_router_from_another_tenant_is_denied(self):
        user = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
            company=None,
            company_name="Acme ISP",
        )
        request = SimpleNamespace(
            user=user,
            tenant=SimpleNamespace(schema_name="tenant_acme", subdomain="acme"),
        )
        router = SimpleNamespace(company_name="Other ISP", tenant_subdomain="other")

        self.assertFalse(HasCompanyAccess().has_object_permission(request, None, router))


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
