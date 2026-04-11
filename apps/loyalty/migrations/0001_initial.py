# Generated migration for apps.loyalty

import decimal
import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('customers', '0003_serviceconnection_billing_account_number_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='LoyaltySettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('points_per_currency', models.IntegerField(default=1, help_text='Points earned per KES 100 paid')),
                ('currency_unit', models.IntegerField(default=100, help_text='Currency unit (e.g. 100 means per KES 100)')),
                ('signup_bonus', models.IntegerField(default=50, help_text='Points awarded on enrollment')),
                ('referral_bonus', models.IntegerField(default=100, help_text='Points for each referral')),
                ('tenure_monthly_bonus', models.IntegerField(default=10, help_text='Monthly loyalty bonus')),
                ('points_expiry_enabled', models.BooleanField(default=True)),
                ('points_expiry_months', models.IntegerField(default=12, help_text='Months until points expire')),
                ('expiry_warning_days', models.IntegerField(default=30, help_text='Days before expiry to warn')),
                ('notify_points_earned', models.BooleanField(default=True)),
                ('notify_redemption', models.BooleanField(default=True)),
                ('notify_tier_upgrade', models.BooleanField(default=True)),
                ('notify_monthly_summary', models.BooleanField(default=False)),
                ('program_active', models.BooleanField(default=True)),
                ('auto_enroll_new_customers', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Loyalty Settings',
                'verbose_name_plural': 'Loyalty Settings',
            },
        ),
        migrations.CreateModel(
            name='LoyaltyTier',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=50)),
                ('level', models.CharField(
                    choices=[
                        ('bronze', 'Bronze'), ('silver', 'Silver'), ('gold', 'Gold'),
                        ('platinum', 'Platinum'), ('diamond', 'Diamond'),
                    ],
                    max_length=20,
                    unique=True,
                )),
                ('min_points', models.IntegerField(default=0)),
                ('max_points', models.IntegerField(blank=True, help_text='Null = no upper limit', null=True)),
                ('points_multiplier', models.DecimalField(
                    decimal_places=2, default=decimal.Decimal('1.00'),
                    help_text='Earning multiplier (e.g. 2.0 = double points)',
                    max_digits=4,
                )),
                ('benefits', models.JSONField(blank=True, default=list, help_text='List of benefit strings')),
                ('color', models.CharField(default='bg-amber-500', help_text='Tailwind color class', max_length=30)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['min_points'],
            },
        ),
        migrations.CreateModel(
            name='LoyaltyMember',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('current_points', models.IntegerField(default=0)),
                ('lifetime_points', models.IntegerField(default=0)),
                ('total_spent', models.DecimalField(decimal_places=2, default=decimal.Decimal('0.00'), max_digits=12)),
                ('total_payments', models.IntegerField(default=0, help_text='Number of completed payments')),
                ('redemptions_count', models.IntegerField(default=0)),
                ('joined_date', models.DateTimeField(default=django.utils.timezone.now)),
                ('last_activity', models.DateTimeField(auto_now=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('customer', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='loyalty_member',
                    to='customers.customer',
                )),
                ('tier', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='members',
                    to='loyalty.loyaltytier',
                )),
            ],
            options={
                'ordering': ['-lifetime_points'],
            },
        ),
        migrations.CreateModel(
            name='LoyaltyReward',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200)),
                ('description', models.TextField(blank=True)),
                ('points_cost', models.IntegerField(validators=[django.core.validators.MinValueValidator(1)])),
                ('category', models.CharField(
                    choices=[
                        ('internet', 'Internet / Data'), ('credit', 'Account Credit'),
                        ('voucher', 'Hotspot Voucher'), ('discount', 'Plan Discount'),
                        ('hardware', 'Hardware'), ('other', 'Other'),
                    ],
                    default='other',
                    max_length=20,
                )),
                ('status', models.CharField(
                    choices=[('active', 'Active'), ('inactive', 'Inactive'), ('expired', 'Expired')],
                    default='active',
                    max_length=20,
                )),
                ('stock_quantity', models.IntegerField(blank=True, help_text='Null = unlimited', null=True)),
                ('redemption_count', models.IntegerField(default=0)),
                ('valid_until', models.DateField(blank=True, null=True)),
                ('image', models.URLField(blank=True)),
                ('voucher_batch_id', models.IntegerField(
                    blank=True,
                    help_text='Link to a VoucherBatch for auto-awarding hotspot vouchers',
                    null=True,
                )),
                ('credit_amount', models.DecimalField(
                    blank=True,
                    decimal_places=2,
                    help_text='Account credit amount (for credit-type rewards)',
                    max_digits=10,
                    null=True,
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['points_cost'],
            },
        ),
        migrations.CreateModel(
            name='PointsTransaction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('transaction_type', models.CharField(
                    choices=[
                        ('earned', 'Earned'), ('redeemed', 'Redeemed'), ('expired', 'Expired'),
                        ('bonus', 'Bonus'), ('adjusted', 'Adjusted'),
                    ],
                    max_length=20,
                )),
                ('points', models.IntegerField(help_text='Positive = earned, negative = redeemed/expired')),
                ('description', models.TextField(blank=True)),
                ('expires_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('member', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='transactions',
                    to='loyalty.loyaltymember',
                )),
                ('reward', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='transactions',
                    to='loyalty.loyaltyreward',
                )),
                ('created_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='PointsRule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('trigger', models.CharField(
                    choices=[
                        ('payment', 'Payment Made'), ('signup', 'Signup / First Join'),
                        ('referral', 'Referral'), ('tenure_monthly', 'Monthly Tenure Bonus'),
                        ('plan_upgrade', 'Plan Upgrade'), ('manual', 'Manual Award'),
                    ],
                    max_length=30,
                )),
                ('points', models.IntegerField(help_text='Base points to award when trigger fires')),
                ('description', models.TextField(blank=True)),
                ('is_active', models.BooleanField(default=True)),
                ('min_amount', models.DecimalField(
                    blank=True,
                    decimal_places=2,
                    help_text='Minimum payment amount for payment trigger',
                    max_digits=10,
                    null=True,
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddIndex(
            model_name='loyaltymember',
            index=models.Index(fields=['-lifetime_points'], name='loyalty_loy_lifetim_idx'),
        ),
        migrations.AddIndex(
            model_name='loyaltymember',
            index=models.Index(fields=['-total_spent'], name='loyalty_loy_total_s_idx'),
        ),
        migrations.AddIndex(
            model_name='loyaltymember',
            index=models.Index(fields=['-total_payments'], name='loyalty_loy_total_p_idx'),
        ),
        migrations.AddIndex(
            model_name='pointstransaction',
            index=models.Index(fields=['-created_at'], name='loyalty_poi_created_idx'),
        ),
        migrations.AddIndex(
            model_name='pointstransaction',
            index=models.Index(fields=['transaction_type'], name='loyalty_poi_transac_idx'),
        ),
    ]
