from django.core import mail
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from datetime import timedelta
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.serializers import ValidationError

from apps.core.models import Company, User
from apps.core.authentication import SessionBoundJWTAuthentication
from apps.core.email_delivery import send_transactional_email
from apps.core.otp_service import OTPService, OTPRateLimitedError, OTPError
from apps.core.permissions import HasCompanyAccess, HasRoleAccessPolicy, IsAdmin, IsAdminOrStaff
from apps.core.payment_method_pin import payment_method_pin_is_unlocked, validate_new_pin
from apps.core.rbac_defaults import DEFAULT_ROLE_ACCESS_POLICIES
from apps.core.serializers import CompanyBrandingSerializer, UserCreateSerializer
from apps.core.session_tokens import (
    SESSION_HASH_CLAIM,
    bind_token_to_user,
    token_matches_user_session,
)
from apps.core.views import (
    CompanyRegisterView,
    CompanyViewSet,
    PlatformCompanyRegisterView,
    PlatformCompanyRegistrationStatusView,
    RoleAccessPolicyViewSet,
    TenantViewSet,
    UserViewSet,
)
from apps.notifications.services.lead_alert_service import build_lead_alert_message
from apps.superadmin.permissions import IsSuperAdmin


class CompanyBrandingLocaleTests(SimpleTestCase):
    def test_branding_exposes_locale_without_allowing_it_to_be_edited(self):
        company = Company(name="Uganda ISP", country="UG", base_currency="UGX")
        serializer = CompanyBrandingSerializer(company)

        self.assertEqual(serializer.data["country"], "UG")
        self.assertEqual(serializer.data["base_currency"], "UGX")
        self.assertTrue(serializer.fields["country"].read_only)
        self.assertTrue(serializer.fields["base_currency"].read_only)


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


class PaymentMethodPinTests(SimpleTestCase):
    def test_pin_must_be_six_digits(self):
        self.assertEqual(validate_new_pin("491827"), "491827")
        with self.assertRaises(ValueError):
            validate_new_pin("49182")
        with self.assertRaises(ValueError):
            validate_new_pin("49A827")

    def test_predictable_pins_are_rejected(self):
        for pin in ("000000", "111111", "123456", "654321"):
            with self.subTest(pin=pin), self.assertRaises(ValueError):
                validate_new_pin(pin)

    def test_superadmin_bypasses_tenant_pin(self):
        request = SimpleNamespace(user=SimpleNamespace(is_superuser=True))
        self.assertTrue(payment_method_pin_is_unlocked(request))


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


class PlatformMutationPermissionTests(SimpleTestCase):
    def test_tenant_list_uses_declared_permissions(self):
        view = TenantViewSet()
        view.action = "list"

        permission_types = {type(permission) for permission in view.get_permissions()}

        self.assertIn(IsAuthenticated, permission_types)
        self.assertIn(IsAdminOrStaff, permission_types)

    def test_tenant_creation_requires_platform_superadmin(self):
        view = TenantViewSet()
        view.action = "create"

        permission_types = {type(permission) for permission in view.get_permissions()}

        self.assertIn(IsAuthenticated, permission_types)
        self.assertIn(IsSuperAdmin, permission_types)

    def test_company_creation_requires_platform_superadmin(self):
        view = CompanyViewSet()
        view.action = "create"

        permission_types = {type(permission) for permission in view.get_permissions()}

        self.assertIn(IsAuthenticated, permission_types)
        self.assertIn(IsSuperAdmin, permission_types)


class StaffAccountSecurityTests(SimpleTestCase):
    @patch("rest_framework_simplejwt.authentication.JWTAuthentication.get_user")
    def test_existing_token_for_role_only_admin_is_rejected(self, base_get_user):
        base_get_user.return_value = User(
            role="admin",
            is_staff=False,
            is_superuser=False,
            is_active=True,
        )

        with self.assertRaises(AuthenticationFailed):
            SessionBoundJWTAuthentication().get_user({})

    def test_role_string_alone_does_not_grant_admin_access(self):
        user = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
            is_staff=False,
            role="admin",
            company=SimpleNamespace(pk=1),
        )
        request = SimpleNamespace(user=user)

        self.assertFalse(IsAdmin().has_permission(request, None))

    def test_role_string_alone_does_not_grant_staff_access(self):
        user = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
            is_staff=False,
            role="support",
            company=SimpleNamespace(pk=1),
        )
        request = SimpleNamespace(user=user)

        self.assertFalse(IsAdminOrStaff().has_permission(request, None))

    def test_unapproved_dashboard_role_is_not_trusted(self):
        user = User(role="admin", is_staff=False, is_superuser=False)

        self.assertFalse(user.dashboard_role_is_trusted)

    def test_staff_account_creation_requires_a_tenant_admin(self):
        view = UserViewSet()
        view.action = "create"

        permission_types = {type(permission) for permission in view.get_permissions()}

        self.assertIn(IsAuthenticated, permission_types)
        self.assertIn(IsAdmin, permission_types)
        self.assertNotIn(IsAdminOrStaff, permission_types)

    def test_staff_account_updates_require_a_tenant_admin(self):
        view = UserViewSet()
        view.action = "partial_update"

        permission_types = {type(permission) for permission in view.get_permissions()}

        self.assertIn(IsAuthenticated, permission_types)
        self.assertIn(IsAdmin, permission_types)
        self.assertNotIn(IsAdminOrStaff, permission_types)

    def test_staff_account_creation_rejects_principal_admin_role(self):
        serializer = UserCreateSerializer(data={
            "email": "new-admin@example.com",
            "phone_number": "+254700000099",
            "first_name": "New",
            "last_name": "Admin",
            "password": "StrongPassword123!",
            "confirm_password": "StrongPassword123!",
            "role": "admin",
        })

        with patch("apps.core.serializers.User.objects.filter") as queryset:
            queryset.return_value.exists.return_value = False
            self.assertFalse(serializer.is_valid())

        self.assertIn("role", serializer.errors)

    def test_staff_account_creation_rejects_client_owned_tenant_fields(self):
        serializer = UserCreateSerializer(data={
            "email": "staff@example.com",
            "phone_number": "+254700000098",
            "first_name": "Tenant",
            "last_name": "Staff",
            "password": "StrongPassword123!",
            "confirm_password": "StrongPassword123!",
            "role": "support",
            "company": "39f5d662-e188-4e4f-bb49-a53013b0c701",
        })

        with patch("apps.core.serializers.User.objects.filter") as queryset:
            queryset.return_value.exists.return_value = False
            self.assertFalse(serializer.is_valid())

        self.assertIn("company", serializer.errors)

    def test_technician_is_consistently_treated_as_staff_member(self):
        user = User(role="technician", is_staff=True)

        self.assertTrue(user.is_staff_member)

    def test_unapproved_technician_is_not_treated_as_staff_member(self):
        user = User(role="technician", is_staff=False)

        self.assertFalse(user.is_staff_member)


class PlatformTenantRegistrationAccessTests(SimpleTestCase):
    @override_settings(PUBLIC_TENANT_REGISTRATION_ENABLED=True)
    def test_dashboard_registration_never_allows_anonymous_access(self):
        factory = APIRequestFactory()

        create_response = PlatformCompanyRegisterView.as_view()(
            factory.post("/api/v1/superadmin/tenants/register/", {}, format="json")
        )
        status_response = PlatformCompanyRegistrationStatusView.as_view()(
            factory.get("/api/v1/superadmin/tenants/register/status/")
        )

        self.assertEqual(create_response.status_code, 401)
        self.assertEqual(status_response.status_code, 401)

    @patch("apps.core.views.get_public_schema_name", return_value="public")
    @patch("apps.core.views.connection")
    def test_authenticated_superadmin_reaches_registration_validation(self, db_connection, _public_schema):
        db_connection.schema_name = "public"
        user = User(email="owner@example.com", is_active=True, is_superuser=True)
        request = APIRequestFactory().post(
            "/api/v1/superadmin/tenants/register/", {}, format="json"
        )
        force_authenticate(request, user=user)

        response = PlatformCompanyRegisterView.as_view()(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("company_name", response.data)

    @override_settings(PUBLIC_TENANT_REGISTRATION_ENABLED=False)
    @patch("apps.core.views.get_public_schema_name", return_value="public")
    @patch("apps.core.views.connection")
    @patch("django.db.connection")
    def test_authenticated_superadmin_can_check_registration_status(self, _django_connection, db_connection, _public_schema):
        db_connection.schema_name = "public"
        user = User(email="owner@example.com", is_active=True, is_superuser=True)
        request = APIRequestFactory().get("/api/v1/superadmin/tenants/register/status/")
        force_authenticate(request, user=user)

        response = PlatformCompanyRegistrationStatusView.as_view()(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("company_name and company_email are required", response.data["detail"])

    @patch("apps.core.views.get_public_schema_name", return_value="public")
    @patch("apps.core.views.connection")
    def test_public_schema_superadmin_can_use_selfie_registration(self, db_connection, _public_schema):
        db_connection.schema_name = "public"
        request = SimpleNamespace(user=SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            is_superuser=True,
        ))

        self.assertTrue(CompanyRegisterView._platform_operator_can_register(request))

    @patch("apps.core.views.get_public_schema_name", return_value="public")
    @patch("apps.core.views.connection")
    def test_authorized_support_operator_can_use_selfie_registration(self, db_connection, _public_schema):
        db_connection.schema_name = "public"
        request = SimpleNamespace(user=SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            is_superuser=False,
            platform_support_profile=SimpleNamespace(
                is_active=True,
                can_register_tenants=True,
            ),
        ))

        self.assertTrue(CompanyRegisterView._platform_operator_can_register(request))

    @patch("apps.core.views.get_public_schema_name", return_value="public")
    @patch("apps.core.views.connection")
    def test_support_without_registration_grant_is_rejected(self, db_connection, _public_schema):
        db_connection.schema_name = "public"
        request = SimpleNamespace(user=SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            is_superuser=False,
            platform_support_profile=SimpleNamespace(
                is_active=True,
                can_register_tenants=False,
            ),
        ))

        self.assertFalse(CompanyRegisterView._platform_operator_can_register(request))


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
