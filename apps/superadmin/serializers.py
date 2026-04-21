"""
Superadmin Serializers
──────────────────────
Read-heavy serializers for the platform-owner dashboard.
All queries target the PUBLIC schema (Tenant, Company, Domain, User, subscriptions).
"""

from rest_framework import serializers
from django.utils import timezone
from django.utils.text import slugify
from decimal import Decimal

from apps.core.models import Tenant, Company, Domain, User
from apps.subscriptions.models import BillingCycle, CompanySubscription, TenantUserLedger


# ──────────────────────────────────────────
# TENANT USER LEDGER (IMMUTABLE AUDIT TRAIL)
# ──────────────────────────────────────────

class TenantUserLedgerSerializer(serializers.ModelSerializer):
    tenant_name = serializers.CharField(source='tenant.company.name', read_only=True)
    tenant_subdomain = serializers.CharField(source='tenant.schema_name', read_only=True)

    class Meta:
        model = TenantUserLedger
        fields = [
            'id', 'tenant', 'tenant_name', 'tenant_subdomain',
            'event', 'user_type',
            'customer_code', 'customer_name', 'username',
            'phone_number', 'plan_name',
            'pppoe_count_after', 'hotspot_count_after',
            'created_at',
        ]
        read_only_fields = fields


# ──────────────────────────────────────────
# TENANT / COMPANY
# ──────────────────────────────────────────

class CompanyBriefSerializer(serializers.ModelSerializer):
    total_customers = serializers.SerializerMethodField()

    class Meta:
        model = Company
        fields = [
            "id", "name", "slug", "company_type", "email", "phone_number",
            "address", "city", "county", "registration_number", "tax_pin",
            "website", "logo", "subscription_plan", "subscription_expiry",
            "is_active", "created_at", "updated_at", "total_customers",
        ]

    def get_total_customers(self, obj):
        try:
            return obj.employees.filter(role="customer").count()
        except Exception:
            return 0


class CompanyUpdateSerializer(serializers.ModelSerializer):
    """For superadmin editing company details."""

    class Meta:
        model = Company
        fields = [
            "name", "email", "phone_number", "address", "city", "county",
            "postal_code", "registration_number", "tax_pin",
            "website", "logo", "company_type", "subscription_plan",
        ]
        extra_kwargs = {f: {"required": False} for f in fields}


class DomainSerializer(serializers.ModelSerializer):
    class Meta:
        model = Domain
        fields = ["id", "domain", "is_primary"]


class TenantListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for the tenant table."""
    company_name = serializers.CharField(source="company.name", read_only=True)
    company_email = serializers.EmailField(source="company.email", read_only=True)
    company_phone = serializers.CharField(source="company.phone_number", read_only=True)
    company_type = serializers.CharField(source="company.company_type", read_only=True)
    company_logo = serializers.ImageField(source="company.logo", read_only=True)
    subscription_plan = serializers.SerializerMethodField()
    subscription_status = serializers.SerializerMethodField()
    days_left = serializers.SerializerMethodField()
    domains = DomainSerializer(many=True, read_only=True)
    
    # NEW: Billing cycle metrics for active cycle
    raw_active_pppoe_count = serializers.SerializerMethodField()
    billed_pppoe_count = serializers.SerializerMethodField()
    current_cycle_status = serializers.SerializerMethodField()
    current_cycle_start = serializers.SerializerMethodField()
    current_cycle_end = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = [
            "id", "subdomain", "schema_name", "status",
            "trial_start", "trial_days", "subscription_expiry",
            "max_users", "max_customers", "features",
            "billing_cycle", "monthly_rate", "next_billing_date",
            "company_name", "company_email", "company_phone",
            "company_type", "company_logo", "subscription_plan", "subscription_status",
            "days_left", "domains",
            "is_active", "created_at", "updated_at",
            # NEW FIELDS
            "raw_active_pppoe_count", "billed_pppoe_count",
            "current_cycle_status", "current_cycle_start", "current_cycle_end",
        ]

    def get_days_left(self, obj):
        if not obj.subscription_expiry:
            return None
        delta = obj.subscription_expiry - timezone.now().date()
        return max(delta.days, 0)

    def get_subscription_plan(self, obj):
        """Read plan name from CompanySubscription (authoritative source)."""
        try:
            sub = CompanySubscription.objects.select_related('plan').filter(company=obj.company).first()
            if sub and sub.plan:
                return sub.plan.name
        except Exception:
            pass
        return getattr(obj.company, 'subscription_plan', None)

    def get_subscription_status(self, obj):
        """Return actual subscription status from CompanySubscription."""
        try:
            sub = CompanySubscription.objects.filter(company=obj.company).first()
            if sub:
                return sub.status
        except Exception:
            pass
        return obj.status
    
    def get_active_cycle(self, obj):
        """Helper to get the active billing cycle for a tenant."""
        try:
            # Get the most recent active cycle for this tenant
            return BillingCycle.objects.filter(
                tenant=obj,
                status='active'
            ).order_by('-start_date').first()
        except Exception:
            return None
    
    def get_raw_active_pppoe_count(self, obj):
        """Return the actual number of unique physical PPPoE users."""
        active_cycle = self.get_active_cycle(obj)
        if active_cycle:
            try:
                return active_cycle.get_raw_pppoe_count()
            except Exception:
                return 0
        return 0
    
    def get_billed_pppoe_count(self, obj):
        """Return the billed count (all active users with no minimum floor)."""
        active_cycle = self.get_active_cycle(obj)
        if active_cycle:
            try:
                return active_cycle.calculate_total_pppoe()
            except Exception:
                return 0
        return 0
    
    def get_current_cycle_status(self, obj):
        """Return the status of the current billing cycle."""
        active_cycle = self.get_active_cycle(obj)
        if active_cycle:
            return active_cycle.status
        return None
    
    def get_current_cycle_start(self, obj):
        """Return the start date of the current billing cycle."""
        active_cycle = self.get_active_cycle(obj)
        if active_cycle:
            return active_cycle.start_date
        return None
    
    def get_current_cycle_end(self, obj):
        """Return the end date of the current billing cycle."""
        active_cycle = self.get_active_cycle(obj)
        if active_cycle:
            return active_cycle.end_date
        return None


class TenantDetailSerializer(TenantListSerializer):
    """Adds company details for the detail view."""
    company = CompanyBriefSerializer(read_only=True)
    
    # NEW: Add billing cycle details with more comprehensive information
    billing_cycle_details = serializers.SerializerMethodField()

    class Meta(TenantListSerializer.Meta):
        fields = TenantListSerializer.Meta.fields + ["company", "domain", "billing_cycle_details"]
    
    def get_billing_cycle_details(self, obj):
        """Provide comprehensive billing cycle details for the detail view."""
        active_cycle = self.get_active_cycle(obj)
        if not active_cycle:
            return None
        
        try:
            # Get plan details from subscription
            plan = None
            subscription = active_cycle.subscription
            if subscription:
                plan = subscription.plan
            
            return {
                "cycle_id": str(active_cycle.id),
                "start_date": active_cycle.start_date,
                "end_date": active_cycle.end_date,
                "status": active_cycle.status,
                "snapshot_base_fee": active_cycle.snapshot_base_fee,
                "snapshot_pppoe_price": active_cycle.snapshot_pppoe_price,
                "snapshot_hotspot_share_pct": active_cycle.snapshot_hotspot_share_pct,
                "raw_pppoe_count": active_cycle.get_raw_pppoe_count(),
                "billed_pppoe_count": active_cycle.calculate_total_pppoe(),
                "pppoe_charge": active_cycle.calculate_pppoe_charge(),
                "hotspot_revenue_accumulated": active_cycle.hotspot_revenue_accumulated,
                "hotspot_share": (active_cycle.hotspot_revenue_accumulated * active_cycle.snapshot_hotspot_share_pct / Decimal('100.0')).quantize(Decimal('0.01')) if active_cycle.hotspot_revenue_accumulated else Decimal('0.00'),
                "total_charge": active_cycle.calculate_total_charge(),
                "invoice_reference": active_cycle.invoice_reference,
                "plan_name": plan.name if plan else None,
                "plan_code": plan.code if plan else None,
                "is_metered": plan.is_metered if plan else False,
            }
        except Exception as e:
            return {"error": str(e)}


class TenantUpdateSerializer(serializers.ModelSerializer):
    """Fields the superadmin is allowed to change on a tenant."""
    # Nested company fields for inline editing
    company_name = serializers.CharField(required=False, write_only=True)
    company_email = serializers.EmailField(required=False, write_only=True)
    company_phone = serializers.CharField(required=False, write_only=True)
    company_address = serializers.CharField(required=False, write_only=True)
    company_city = serializers.CharField(required=False, write_only=True)

    class Meta:
        model = Tenant
        fields = [
            "status", "max_users", "max_customers",
            "trial_days", "subscription_expiry",
            "billing_cycle", "monthly_rate", "next_billing_date",
            "features", "is_active",
            # Write-only company fields
            "company_name", "company_email", "company_phone",
            "company_address", "company_city",
        ]

    def update(self, instance, validated_data):
        # Extract company fields
        company_fields = {}
        for key in list(validated_data.keys()):
            if key.startswith("company_"):
                field = key.replace("company_", "")
                if field == "phone":
                    field = "phone_number"
                company_fields[field] = validated_data.pop(key)

        # Update company if needed
        if company_fields and instance.company:
            for k, v in company_fields.items():
                setattr(instance.company, k, v)
            instance.company.save()

        return super().update(instance, validated_data)


class TenantCreateSerializer(serializers.Serializer):
    """
    Create a new tenant (Company + Tenant + Domain + admin user).
    """
    # Company
    company_name = serializers.CharField(max_length=255)
    company_email = serializers.EmailField()
    company_phone = serializers.CharField(max_length=20)
    company_type = serializers.ChoiceField(
        choices=["isp", "corporate", "reseller"], default="isp"
    )
    address = serializers.CharField(required=False, default="")
    city = serializers.CharField(required=False, default="")
    county = serializers.CharField(required=False, default="")

    # Tenant
    subdomain = serializers.SlugField(max_length=100)
    status = serializers.ChoiceField(
        choices=["trial", "active"], default="trial"
    )
    max_users = serializers.IntegerField(default=10)
    max_customers = serializers.IntegerField(default=100)
    billing_cycle = serializers.ChoiceField(
        choices=["monthly", "quarterly", "yearly"], default="monthly"
    )
    monthly_rate = serializers.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )

    # Admin user
    admin_email = serializers.EmailField()
    admin_password = serializers.CharField(min_length=8, write_only=True)
    admin_first_name = serializers.CharField(max_length=30, required=False, default="Admin")
    admin_last_name = serializers.CharField(max_length=30, required=False, default="")
    admin_phone = serializers.CharField(max_length=20)

    def validate_subdomain(self, value):
        if Tenant.objects.filter(subdomain=value).exists():
            raise serializers.ValidationError("Subdomain already taken.")
        return value

    def validate_company_name(self, value):
        if Company.objects.filter(name=value).exists():
            raise serializers.ValidationError("Company name already taken.")
        return value

    def validate_admin_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("User email already exists.")
        return value


# ──────────────────────────────────────────
# PLAN
# ──────────────────────────────────────────

class NetilyPlanSerializer(serializers.Serializer):
    """Read/write for NetilyPlan."""
    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(max_length=100)
    code = serializers.CharField(max_length=50)
    description = serializers.CharField(required=False, allow_blank=True)
    tagline = serializers.CharField(required=False, allow_blank=True)
    
    # Flat Pricing
    price_monthly = serializers.DecimalField(max_digits=10, decimal_places=2)
    price_yearly = serializers.DecimalField(max_digits=10, decimal_places=2)
    
    # NEW: Metered Pricing Fields
    is_metered = serializers.BooleanField(default=False, required=False)
    base_license_fee = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)
    pppoe_unit_price = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)
    pppoe_min_clients = serializers.IntegerField(required=False, default=20)
    hotspot_revenue_share_pct = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, allow_null=True)
    
    currency = serializers.CharField(max_length=3, default="KES")
    max_subscribers = serializers.IntegerField()
    max_routers = serializers.IntegerField()
    max_staff = serializers.IntegerField()
    features = serializers.JSONField(required=False, default=list)
    is_active = serializers.BooleanField(default=True)
    is_popular = serializers.BooleanField(default=False)
    sort_order = serializers.IntegerField(default=0)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)
    # Computed
    subscriber_count = serializers.IntegerField(read_only=True, required=False)


# ──────────────────────────────────────────
# USER  (cross-tenant view)
# ──────────────────────────────────────────

class UserListSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "email", "full_name", "first_name", "last_name",
            "phone_number", "role", "is_active", "is_staff",
            "is_superuser", "is_verified",
            "company_name", "tenant_subdomain",
            "date_joined", "last_login",
        ]

    def get_full_name(self, obj):
        return f"{obj.first_name or ''} {obj.last_name or ''}".strip() or obj.email


class UserDetailSerializer(UserListSerializer):
    class Meta(UserListSerializer.Meta):
        fields = UserListSerializer.Meta.fields + [
            "id_number", "gender", "date_of_birth", "profile_picture",
        ]


# ──────────────────────────────────────────
# DASHBOARD KPI
# ──────────────────────────────────────────

class DashboardKPISerializer(serializers.Serializer):
    total_tenants = serializers.IntegerField()
    active_tenants = serializers.IntegerField()
    trial_tenants = serializers.IntegerField()
    suspended_tenants = serializers.IntegerField()
    total_users = serializers.IntegerField()
    total_revenue = serializers.DecimalField(max_digits=15, decimal_places=2)
    mrr = serializers.DecimalField(max_digits=15, decimal_places=2)
    recent_signups = serializers.IntegerField()


# ──────────────────────────────────────────
# AUDIT LOG
# ──────────────────────────────────────────

class AuditLogSerializer(serializers.Serializer):
    id = serializers.CharField()
    timestamp = serializers.DateTimeField()
    actor_email = serializers.CharField()
    action = serializers.CharField()
    model_name = serializers.CharField()
    object_repr = serializers.CharField(allow_null=True)
    ip_address = serializers.CharField(allow_null=True)
    changes = serializers.JSONField(allow_null=True)


# ──────────────────────────────────────────
# BILLING CYCLE
# ──────────────────────────────────────────

class BillingCycleSerializer(serializers.ModelSerializer):
    tenant_name = serializers.SerializerMethodField()
    plan_name = serializers.SerializerMethodField()
    pppoe_clients = serializers.SerializerMethodField()
    estimated_total = serializers.SerializerMethodField()

    class Meta:
        model = BillingCycle
        fields = [
            "id", "tenant_name", "plan_name", "status",
            "start_date", "end_date", "grace_ends_at",
            "hotspot_revenue_accumulated",
            "snapshot_base_fee", "snapshot_pppoe_price",
            "snapshot_min_clients", "snapshot_hotspot_share_pct",
            "pppoe_clients", "estimated_total",
            "invoice_reference",
        ]

    def get_tenant_name(self, obj):
        try:
            return obj.tenant.company.name
        except Exception:
            return obj.tenant.subdomain

    def get_plan_name(self, obj):
        try:
            return obj.subscription.plan.name
        except Exception:
            return ""

    def get_pppoe_clients(self, obj):
        try:
            return obj.calculate_total_pppoe()
        except Exception:
            return 0

    def get_estimated_total(self, obj):
        try:
            return float(obj.calculate_total_charge())
        except Exception:
            return 0.0