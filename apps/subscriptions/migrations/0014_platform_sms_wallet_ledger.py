from decimal import Decimal

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


def create_platform_sms_wallet(apps, schema_editor):
    PlatformSMSWallet = apps.get_model('subscriptions', 'PlatformSMSWallet')
    PlatformSMSWallet.objects.get_or_create(
        pk=1,
        defaults={
            'sms_units': Decimal('0.0000'),
            'sell_price_per_unit': Decimal('0.4000'),
            'enforce_balance': False,
            'is_active': True,
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ('subscriptions', '0013_subscription_reminder_template_invoice_number'),
    ]

    operations = [
        migrations.CreateModel(
            name='PlatformSMSWallet',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sms_units', models.DecimalField(decimal_places=4, default=Decimal('0.0000'), max_digits=14)),
                ('sell_price_per_unit', models.DecimalField(decimal_places=4, default=Decimal('0.4000'), max_digits=10, validators=[django.core.validators.MinValueValidator(Decimal('0.0000'))])),
                ('enforce_balance', models.BooleanField(default=False, help_text='When enabled, platform SMS sends fail if this wallet has insufficient units.')),
                ('is_active', models.BooleanField(default=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Platform SMS Wallet',
                'verbose_name_plural': 'Platform SMS Wallets',
            },
        ),
        migrations.CreateModel(
            name='PlatformSMSLedger',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('entry_type', models.CharField(choices=[('credit', 'Credit'), ('debit', 'Debit'), ('refund', 'Refund'), ('adjustment', 'Adjustment')], max_length=20)),
                ('units', models.DecimalField(decimal_places=4, help_text='Positive for credits/refunds, negative for debits.', max_digits=14)),
                ('unit_price', models.DecimalField(decimal_places=4, default=Decimal('0.0000'), max_digits=10)),
                ('amount', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12)),
                ('reference', models.CharField(blank=True, default='', max_length=120)),
                ('provider_message_id', models.CharField(blank=True, default='', max_length=255)),
                ('notes', models.TextField(blank=True, default='')),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('reminder_delivery', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='platform_sms_ledger_entries', to='subscriptions.subscriptioninvoicereminderdelivery')),
                ('wallet', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='entries', to='subscriptions.platformsmswallet')),
            ],
            options={
                'verbose_name': 'Platform SMS Ledger Entry',
                'verbose_name_plural': 'Platform SMS Ledger Entries',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='platformsmsledger',
            index=models.Index(fields=['entry_type', 'created_at'], name='subscriptio_entry_t_6c9147_idx'),
        ),
        migrations.AddIndex(
            model_name='platformsmsledger',
            index=models.Index(fields=['reference'], name='subscriptio_referen_f2e59a_idx'),
        ),
        migrations.RunPython(create_platform_sms_wallet, migrations.RunPython.noop),
    ]
