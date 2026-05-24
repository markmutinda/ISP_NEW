"""
Loyalty Program Models for ISP System.

Industry best-practice loyalty system with:
- Configurable point-earning rules (per KES spent, signup, referral, tenure)
- 5-tier system: Bronze → Silver → Gold → Platinum → Diamond
- Reward catalog with voucher, credit, data-topup, discount categories
- Full transaction audit trail
- Points expiry with configurable window
- Auto-enrollment for all customers
- Hotspot client support for anonymous WiFi users
"""
from django.db import models
from django.conf import settings
from django.utils import timezone
from django.core.validators import MinValueValidator
from decimal import Decimal


TIER_LEVEL_CHOICES = (
    ('bronze', 'Bronze'),
    ('silver', 'Silver'),
    ('gold', 'Gold'),
    ('platinum', 'Platinum'),
    ('diamond', 'Diamond'),
)

REWARD_STATUS_CHOICES = (
    ('active', 'Active'),
    ('inactive', 'Inactive'),
    ('expired', 'Expired'),
)

REWARD_CATEGORY_CHOICES = (
    ('internet', 'Internet / Data'),
    ('credit', 'Account Credit'),
    ('voucher', 'Hotspot Voucher'),
    ('discount', 'Plan Discount'),
    ('hardware', 'Hardware'),
    ('other', 'Other'),
)

TRANSACTION_TYPE_CHOICES = (
    ('earned', 'Earned'),
    ('redeemed', 'Redeemed'),
    ('expired', 'Expired'),
    ('bonus', 'Bonus'),
    ('adjusted', 'Adjusted'),
)

RULE_TRIGGER_CHOICES = (
    ('payment', 'Payment Made'),
    ('signup', 'Signup / First Join'),
    ('referral', 'Referral'),
    ('tenure_monthly', 'Monthly Tenure Bonus'),
    ('plan_upgrade', 'Plan Upgrade'),
    ('manual', 'Manual Award'),
)


class LoyaltySettings(models.Model):
    """
    Singleton per-tenant loyalty program configuration.
    """
    # Points earning
    points_per_currency = models.IntegerField(
        default=1,
        help_text='Points earned per KES 100 paid'
    )
    currency_unit = models.IntegerField(
        default=100,
        help_text='Currency unit (e.g. 100 means per KES 100)'
    )
    signup_bonus = models.IntegerField(default=50, help_text='Points awarded on enrollment')
    referral_bonus = models.IntegerField(default=100, help_text='Points for each referral')
    tenure_monthly_bonus = models.IntegerField(default=10, help_text='Monthly loyalty bonus')

    # Expiry
    points_expiry_enabled = models.BooleanField(default=True)
    points_expiry_months = models.IntegerField(default=12, help_text='Months until points expire')
    expiry_warning_days = models.IntegerField(default=30, help_text='Days before expiry to warn')

    # Notifications
    notify_points_earned = models.BooleanField(default=True)
    notify_redemption = models.BooleanField(default=True)
    notify_tier_upgrade = models.BooleanField(default=True)
    notify_monthly_summary = models.BooleanField(default=False)

    # Program control
    program_active = models.BooleanField(default=False)
    auto_enroll_new_customers = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'loyalty'
        verbose_name = 'Loyalty Settings'
        verbose_name_plural = 'Loyalty Settings'

    def __str__(self):
        return 'Loyalty Program Settings'

    def save(self, *args, **kwargs):
        # Singleton: only one row per tenant
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class LoyaltyTier(models.Model):
    """
    Tier definitions. ISP admin can customise thresholds and benefits.
    """
    name = models.CharField(max_length=50)
    level = models.CharField(max_length=20, choices=TIER_LEVEL_CHOICES, unique=True)
    min_points = models.IntegerField(default=0)
    max_points = models.IntegerField(null=True, blank=True, help_text='Null = no upper limit')
    points_multiplier = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal('1.00'),
        help_text='Earning multiplier (e.g. 2.0 = double points)'
    )
    benefits = models.JSONField(default=list, blank=True, help_text='List of benefit strings')
    color = models.CharField(max_length=30, default='bg-amber-500', help_text='Tailwind color class')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'loyalty'
        ordering = ['min_points']

    def __str__(self):
        return f'{self.name} ({self.level})'


class LoyaltyMember(models.Model):
    """
    One-to-one link between a Customer (registered) or HotspotClient (anonymous)
    and their loyalty profile.
    
    For hotspot clients, customer will be null and hotspot_client will be set.
    For registered customers, customer will be set and hotspot_client will be null.
    """
    customer = models.OneToOneField(
        'customers.Customer',
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='loyalty_member'
    )
    hotspot_client = models.OneToOneField(
        'billing.HotspotClient',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='loyalty_member',
    )
    tier = models.ForeignKey(
        LoyaltyTier,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='members'
    )
    current_points = models.IntegerField(default=0)
    lifetime_points = models.IntegerField(default=0)
    total_spent = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_payments = models.IntegerField(default=0, help_text='Number of completed payments')
    redemptions_count = models.IntegerField(default=0)

    joined_date = models.DateTimeField(default=timezone.now)
    last_activity = models.DateTimeField(auto_now=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'loyalty'
        ordering = ['-lifetime_points']
        indexes = [
            models.Index(fields=['-lifetime_points']),
            models.Index(fields=['-total_spent']),
            models.Index(fields=['-total_payments']),
        ]

    def __str__(self):
        if self.customer:
            return f'{self.customer} – {self.current_points} pts'
        if self.hotspot_client:
            return f'Hotspot: {self.hotspot_client} – {self.current_points} pts'
        return f'LoyaltyMember {self.id} – {self.current_points} pts'

    @classmethod
    def get_or_create_for_hotspot(cls, hotspot_client):
        """Get or create a LoyaltyMember for a HotspotClient (anonymous WiFi users)."""
        try:
            return cls.objects.get(hotspot_client=hotspot_client), False
        except cls.DoesNotExist:
            settings_obj = LoyaltySettings.load()
            if not settings_obj.program_active:
                return None, False
            bronze = LoyaltyTier.objects.filter(level='bronze').first()
            member = cls.objects.create(
                hotspot_client=hotspot_client,
                tier=bronze,
            )
            if settings_obj.signup_bonus > 0:
                member.award_points(
                    points=settings_obj.signup_bonus,
                    description='Hotspot welcome bonus',
                    transaction_type='bonus',
                )
            return member, True

    def recalculate_tier(self):
        """Promote or maintain tier based on lifetime_points."""
        new_tier = LoyaltyTier.objects.filter(
            min_points__lte=self.lifetime_points
        ).order_by('-min_points').first()
        if new_tier and (self.tier_id != new_tier.id):
            old_tier = self.tier
            self.tier = new_tier
            self.save(update_fields=['tier', 'updated_at'])
            return old_tier, new_tier
        return None, None

    def award_points(self, points, description='', transaction_type='earned', created_by=None):
        """Award points and recalculate tier. Returns the PointsTransaction."""
        # Apply tier multiplier
        multiplier = self.tier.points_multiplier if self.tier else Decimal('1.00')
        final_points = int(points * multiplier) if transaction_type == 'earned' else points

        self.current_points += final_points
        self.lifetime_points += final_points
        self.save(update_fields=['current_points', 'lifetime_points', 'updated_at'])

        txn = PointsTransaction.objects.create(
            member=self,
            transaction_type=transaction_type,
            points=final_points,
            description=description,
            created_by=created_by,
        )

        # Check tier upgrade
        old_tier, new_tier = self.recalculate_tier()
        if new_tier:
            txn.description += f' [Tier upgrade: {old_tier} → {new_tier}]'
            txn.save(update_fields=['description'])

        return txn

    def deduct_points(self, points, description='', created_by=None):
        """Deduct points for a redemption."""
        if points > self.current_points:
            raise ValueError('Insufficient points')
        self.current_points -= points
        self.redemptions_count += 1
        self.save(update_fields=['current_points', 'redemptions_count', 'updated_at'])

        return PointsTransaction.objects.create(
            member=self,
            transaction_type='redeemed',
            points=-points,
            description=description,
            created_by=created_by,
        )


class LoyaltyReward(models.Model):
    """
    Redeemable reward catalog items.
    """
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    points_cost = models.IntegerField(validators=[MinValueValidator(1)])
    category = models.CharField(max_length=20, choices=REWARD_CATEGORY_CHOICES, default='other')
    status = models.CharField(max_length=20, choices=REWARD_STATUS_CHOICES, default='active')
    stock_quantity = models.IntegerField(null=True, blank=True, help_text='Null = unlimited')
    redemption_count = models.IntegerField(default=0)
    valid_until = models.DateField(null=True, blank=True)
    image = models.URLField(blank=True)

    # Voucher integration fields
    voucher_batch_id = models.IntegerField(
        null=True, blank=True,
        help_text='Link to a VoucherBatch for auto-awarding hotspot vouchers'
    )
    credit_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text='Account credit amount (for credit-type rewards)'
    )

    # Hotspot reward fields for immediate internet access rewards
    hotspot_reward_minutes = models.IntegerField(
        null=True, blank=True,
        help_text='Minutes of free hotspot internet access for this reward'
    )
    hotspot_reward_speed_mbps = models.CharField(
        max_length=10, 
        null=True,
        blank=True, 
        default='5',
        help_text='Speed (Mbps) during the reward session, e.g. "5"'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'loyalty'
        ordering = ['points_cost']

    def __str__(self):
        return f'{self.name} ({self.points_cost} pts)'

    @property
    def is_available(self):
        if self.status != 'active':
            return False
        if self.valid_until and self.valid_until < timezone.now().date():
            return False
        if self.stock_quantity is not None and self.stock_quantity <= 0:
            return False
        return True


class PointsTransaction(models.Model):
    """
    Immutable audit log of every points movement.
    """
    member = models.ForeignKey(
        LoyaltyMember,
        on_delete=models.CASCADE,
        related_name='transactions'
    )
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPE_CHOICES)
    points = models.IntegerField(help_text='Positive = earned, negative = redeemed/expired')
    description = models.TextField(blank=True)
    reward = models.ForeignKey(
        LoyaltyReward,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='transactions'
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'loyalty'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['transaction_type']),
        ]

    def __str__(self):
        sign = '+' if self.points > 0 else ''
        return f'{self.member} {sign}{self.points} ({self.transaction_type})'


class PointsRule(models.Model):
    """
    Configurable automation rules for point awarding.
    """
    name = models.CharField(max_length=100)
    trigger = models.CharField(max_length=30, choices=RULE_TRIGGER_CHOICES)
    points = models.IntegerField(help_text='Base points to award when trigger fires')
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    min_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text='Minimum payment amount for payment trigger'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'loyalty'

    def __str__(self):
        return f'{self.name} ({self.trigger}: {self.points} pts)'