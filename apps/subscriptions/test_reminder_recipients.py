from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from .reminder_recipients import _select_recipient, normalize_billing_phone


@override_settings(
    PLATFORM_SUPERADMIN_EMAILS=['admin@netily.co.ke'],
    PLATFORM_SUPERADMIN_NAMES=['peter ouma', 'mark mbolonzi'],
)
class ReminderRecipientTests(SimpleTestCase):
    def setUp(self):
        self.tenant = SimpleNamespace(pk=5, company_id=10, schema_name='tenant_test')

    def user(self, pk=2, **changes):
        values = dict(
            pk=pk, email='owner@example.com', phone_number='0706580196',
            first_name='Daniel', last_name='Chemiat', role='admin',
            is_active=True, is_superuser=False, company_id=10, tenant_id=5,
        )
        values.update(changes)
        return SimpleNamespace(**values)

    def test_platform_and_staff_accounts_are_not_recipients(self):
        users = [
            self.user(1, email='admin@netily.co.ke', phone_number='+254700000001'),
            self.user(2, first_name='Peter', last_name='Ouma'),
            self.user(3, first_name='Mark', last_name='Mbolonzi'),
            self.user(4, role='support'),
            self.user(5, role='accountant'),
            self.user(6, role='staff'),
            self.user(7),
            self.user(8, email='another-admin@example.com'),
        ]
        recipients = _select_recipient(self.tenant, [], users)
        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0]['id'], 7)
        self.assertEqual(recipients[0]['phone_number'], '+254706580196')

    def test_signup_owner_uses_local_id_and_updated_contact(self):
        signup = self.user(99)
        users = [self.user(1, email='staff@example.com'), self.user(8, phone_number='0798899664')]
        result = _select_recipient(self.tenant, [signup], users)[0]
        self.assertEqual(result['id'], 8)
        self.assertEqual(result['phone_number'], '+254798899664')

    def test_public_id_is_never_used_for_tenant_notification(self):
        result = _select_recipient(self.tenant, [self.user(99)], [self.user(99, email='staff@example.com')])[0]
        self.assertIsNone(result['id'])
        self.assertEqual(result['email'], 'owner@example.com')

    def test_disabled_owner_does_not_fall_back_to_staff(self):
        users = [self.user(is_active=False), self.user(3, email='staff@example.com')]
        self.assertEqual(_select_recipient(self.tenant, [self.user()], users), [])

    def test_other_tenant_and_platform_only_accounts_are_excluded(self):
        users = [self.user(company_id=20), self.user(tenant_id=20), self.user(role='superadmin', is_superuser=True)]
        self.assertEqual(_select_recipient(self.tenant, [], users), [])

    def test_registered_tenant_owner_with_superuser_flag_is_selected(self):
        # CompanyRegisterView creates the local owner with null foreign keys
        # and full permissions; the tenant schema supplies the account scope.
        owner = self.user(9, company_id=None, tenant_id=None, is_superuser=True)
        users = [
            self.user(1, email='admin@netily.co.ke', is_superuser=True),
            self.user(2, first_name='Peter', last_name='Ouma', is_superuser=True),
            owner,
            self.user(10, email='technician@example.com', role='staff'),
        ]
        recipients = _select_recipient(self.tenant, [], users)
        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0]['id'], 9)
        self.assertEqual(recipients[0]['phone_number'], '+254706580196')

    def test_signup_mirror_with_tenant_superuser_flag_is_selected(self):
        signup = self.user(90)
        mirror = self.user(8, company_id=None, tenant_id=None, is_superuser=True)
        recipients = _select_recipient(self.tenant, [signup], [mirror])
        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0]['id'], 8)

    def test_inactive_tenant_superuser_is_not_selected(self):
        owner = self.user(is_superuser=True, is_active=False)
        self.assertEqual(_select_recipient(self.tenant, [], [owner]), [])

    def test_configured_platform_superuser_mirrors_are_excluded(self):
        for identity in [
            {'email': 'admin@netily.co.ke'},
            {'first_name': 'Peter', 'last_name': 'Ouma'},
            {'first_name': 'Mark', 'last_name': 'Mbolonzi'},
            {'role': 'super_admin'},
        ]:
            with self.subTest(identity=identity):
                user = self.user(is_superuser=True, company_id=None, tenant_id=None, **identity)
                self.assertEqual(_select_recipient(self.tenant, [], [user]), [])

    def test_invalid_synthetic_phones_are_not_sent_to_provider(self):
        for phone in ['+2547004781488', '+2547003781488', '+254700000001', '', 'call 0706580196']:
            with self.subTest(phone=phone):
                self.assertIsNone(normalize_billing_phone(phone))

    def test_local_and_international_phone_normalization(self):
        for phone in ['0706580196', '706580196', '+254706580196', '254706580196']:
            self.assertEqual(normalize_billing_phone(phone), '+254706580196')
        self.assertEqual(normalize_billing_phone('+256772123456'), '+256772123456')

    def test_all_reminder_paths_share_recipient_selection(self):
        from .tasks import _tenant_invoice_admins
        from .reminder_service import get_tenant_admin_phone
        from apps.superadmin.views import _subscription_invoice_admins
        selected = [{'phone_number': '+254706580196', 'first_name': 'Daniel'}]
        with patch('apps.subscriptions.reminder_recipients.tenant_billing_recipients', return_value=selected) as resolver:
            self.assertEqual(_tenant_invoice_admins(self.tenant), selected)
            self.assertEqual(_subscription_invoice_admins(self.tenant), selected)
            self.assertEqual(get_tenant_admin_phone(self.tenant), ('+254706580196', 'Daniel'))
            self.assertEqual(resolver.call_count, 3)
