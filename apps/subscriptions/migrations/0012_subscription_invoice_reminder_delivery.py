import json
import uuid

import django.db.models.deletion
from django.db import migrations, models


def normalize_subscription_reminder_settings(apps, schema_editor):
    SystemSettings = apps.get_model('core', 'SystemSettings')
    key = 'subscription_invoice_reminders'
    defaults = {
        'enabled': True,
        'days_before': [5, 3, 1],
        'channels': ['email', 'sms', 'in_app'],
        'send_expired_notice': True,
    }

    setting = SystemSettings.objects.filter(key=key).first()
    if not setting:
        SystemSettings.objects.create(
            key=key,
            name='Subscription invoice reminders',
            value=json.dumps(defaults),
            setting_type='billing',
            data_type='json',
            is_public=False,
            description='Controls automatic tenant subscription invoice reminders.',
        )
        return

    try:
        payload = json.loads(setting.value) if isinstance(setting.value, str) else dict(setting.value or {})
    except (TypeError, ValueError):
        payload = {}

    days = set()
    for day in payload.get('days_before', []):
        try:
            if int(day) > 0:
                days.add(int(day))
        except (TypeError, ValueError):
            pass
    days.update(defaults['days_before'])

    channels = set(payload.get('channels', []))
    channels.update(defaults['channels'])

    payload.update({
        'enabled': bool(payload.get('enabled', True)),
        'days_before': sorted(days, reverse=True),
        'channels': [ch for ch in ['email', 'sms', 'in_app'] if ch in channels],
        'send_expired_notice': bool(payload.get('send_expired_notice', True)),
    })

    setting.value = json.dumps(payload)
    setting.name = setting.name or 'Subscription invoice reminders'
    setting.setting_type = setting.setting_type or 'billing'
    setting.data_type = 'json'
    setting.is_public = False
    setting.description = setting.description or 'Controls automatic tenant subscription invoice reminders.'
    setting.save(update_fields=[
        'value',
        'name',
        'setting_type',
        'data_type',
        'is_public',
        'description',
        'updated_at',
    ])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0027_emailotp_affiliate_password_reset'),
        ('subscriptions', '0011_subscriptionremindertemplate_subscriptionreminderlog'),
    ]

    operations = [
        migrations.CreateModel(
            name='SubscriptionInvoiceReminderDelivery',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('invoice_reference', models.CharField(max_length=100)),
                ('invoice_number', models.CharField(blank=True, max_length=100)),
                ('milestone', models.CharField(help_text='Reminder milestone, for example 5, 3, 1, or expired.', max_length=20)),
                ('channel', models.CharField(choices=[('email', 'Email'), ('sms', 'SMS'), ('in_app', 'In-app')], max_length=20)),
                ('recipient_user_id', models.CharField(blank=True, max_length=64)),
                ('recipient_name', models.CharField(blank=True, max_length=255)),
                ('recipient_email', models.EmailField(blank=True, max_length=254)),
                ('recipient_phone', models.CharField(blank=True, max_length=32)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('sent', 'Sent'), ('failed', 'Failed'), ('skipped', 'Skipped')], default='pending', max_length=20)),
                ('provider_message_id', models.CharField(blank=True, max_length=255)),
                ('error_message', models.TextField(blank=True)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('billing_cycle', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='invoice_reminder_deliveries', to='subscriptions.billingcycle')),
                ('subscription', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='invoice_reminder_deliveries', to='subscriptions.companysubscription')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='subscription_invoice_reminder_deliveries', to='core.tenant')),
            ],
            options={
                'verbose_name': 'Subscription Invoice Reminder Delivery',
                'verbose_name_plural': 'Subscription Invoice Reminder Deliveries',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='subscriptioninvoicereminderdelivery',
            index=models.Index(fields=['tenant', 'status'], name='subscriptio_tenant__d85e23_idx'),
        ),
        migrations.AddIndex(
            model_name='subscriptioninvoicereminderdelivery',
            index=models.Index(fields=['billing_cycle', 'milestone'], name='subscriptio_billing_4a9d64_idx'),
        ),
        migrations.AddIndex(
            model_name='subscriptioninvoicereminderdelivery',
            index=models.Index(fields=['channel', 'status'], name='subscriptio_channel_443cd2_idx'),
        ),
        migrations.AddIndex(
            model_name='subscriptioninvoicereminderdelivery',
            index=models.Index(fields=['created_at'], name='subscriptio_created_4dfdcb_idx'),
        ),
        migrations.AddConstraint(
            model_name='subscriptioninvoicereminderdelivery',
            constraint=models.UniqueConstraint(fields=('billing_cycle', 'invoice_reference', 'milestone', 'channel', 'recipient_user_id', 'recipient_email', 'recipient_phone'), name='uniq_subscription_invoice_reminder_delivery'),
        ),
        migrations.RunPython(normalize_subscription_reminder_settings, migrations.RunPython.noop),
    ]
