from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.db.models.signals import post_save
from rest_framework import status
from rest_framework.test import APIClient

from apps.billing.models import Plan
from apps.customers.models import Customer, ServiceConnection
from apps.notifications.signals import create_user_notification_preferences


User = get_user_model()


class CustomerViewSetTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        post_save.disconnect(create_user_notification_preferences, sender=User)

    @classmethod
    def tearDownClass(cls):
        post_save.connect(create_user_notification_preferences, sender=User)
        super().tearDownClass()

    def setUp(self):
        self.client = APIClient()
        unique = str(uuid4().int)[:8]
        self.admin = User.objects.create_user(
            email=f'admin-{unique}@example.com',
            password='StrongPass123!',
            phone_number=f'+2547{unique[:8]}',
            first_name='Admin',
            last_name='User',
            role='admin',
            is_staff=True,
        )
        self.client.force_authenticate(user=self.admin)

        self.customer_user = User.objects.create_user(
            email=f'customer-{unique}@example.com',
            password='StrongPass123!',
            phone_number=f'+2541{unique[:8]}',
            first_name='Jane',
            last_name='Doe',
            role='customer',
        )
        self.customer = Customer.objects.create(
            user=self.customer_user,
            customer_code='CUS-001',
            status='ACTIVE',
        )

        self.plan_basic = Plan.objects.create(
            name='Basic Fiber',
            code='BASIC_FIBER',
            plan_type='PPPOE',
            base_price=Decimal('1500.00'),
            download_speed=15,
            upload_speed=8,
            duration_days=30,
        )
        self.plan_pro = Plan.objects.create(
            name='Pro Fiber',
            code='PRO_FIBER',
            plan_type='PPPOE',
            base_price=Decimal('2500.00'),
            download_speed=40,
            upload_speed=20,
            duration_days=30,
        )
        self.service = ServiceConnection.objects.create(
            customer=self.customer,
            plan=self.plan_basic,
            service_type='INTERNET',
            connection_type='FIBER',
            auth_connection_type='PPPOE',
            status='ACTIVE',
            download_speed=15,
            upload_speed=8,
            monthly_price=Decimal('1500.00'),
        )

    def test_customer_search_matches_plan_name(self):
        response = self.client.get('/api/v1/customers/', {'search': 'Pro Fiber'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

        self.service.plan = self.plan_pro
        self.service.save()

        response = self.client.get('/api/v1/customers/', {'search': 'Pro Fiber'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['customer_code'], self.customer.customer_code)

    def test_change_plan_updates_service_fields(self):
        response = self.client.post(
            f'/api/v1/customers/{self.customer.id}/change_plan/',
            {'plan_id': self.plan_pro.id},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.service.refresh_from_db()
        self.assertEqual(self.service.plan_id, self.plan_pro.id)
        self.assertEqual(self.service.monthly_price, self.plan_pro.base_price)
        self.assertEqual(self.service.download_speed, self.plan_pro.download_speed)
        self.assertEqual(self.service.upload_speed, self.plan_pro.upload_speed)

    def test_available_plans_returns_current_plan_context(self):
        response = self.client.get(f'/api/v1/customers/{self.customer.id}/available_plans/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['service_id'], self.service.id)
        self.assertEqual(response.data['current_plan_id'], self.plan_basic.id)
        returned_plan_ids = {plan['id'] for plan in response.data['plans']}
        self.assertIn(self.plan_basic.id, returned_plan_ids)
        self.assertIn(self.plan_pro.id, returned_plan_ids)
