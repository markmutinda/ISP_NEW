"""
Customer Management Models for ISP System
"""
from django.db import models
from django.contrib.auth import get_user_model
from django.core.validators import MinLengthValidator
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.conf import settings

from apps.core.models import Company

# Use Django's settings.AUTH_USER_MODEL for foreign keys
User = get_user_model()


# Constants for choices - defined here for now, can be moved to utils/constants.py
GENDER_CHOICES = (
    ('M', 'Male'),
    ('F', 'Female'),
    ('O', 'Other'),
)

MARITAL_STATUS_CHOICES = (
    ('SINGLE', 'Single'),
    ('MARRIED', 'Married'),
    ('DIVORCED', 'Divorced'),
    ('WIDOWED', 'Widowed'),
    ('SEPARATED', 'Separated'),
)

ID_TYPE_CHOICES = (
    ('NATIONAL_ID', 'National ID'),
    ('PASSPORT', 'Passport'),
    ('ALIEN_ID', 'Alien ID'),
    ('DRIVER_LICENSE', 'Driver License'),
    ('BIRTH_CERTIFICATE', 'Birth Certificate'),
)

CUSTOMER_STATUS_CHOICES = (
    ('LEAD', 'Lead'),
    ('PENDING', 'Pending Approval'),
    ('ACTIVE', 'Active'),
    ('SUSPENDED', 'Suspended'),
    ('TERMINATED', 'Terminated'),
    ('INACTIVE', 'Inactive'),
)

ADDRESS_TYPE_CHOICES = (
    ('BILLING', 'Billing Address'),
    ('INSTALLATION', 'Installation Address'),
    ('HOME', 'Home Address'),
    ('BUSINESS', 'Business Address'),
    ('ALTERNATIVE', 'Alternative Address'),
)

DOCUMENT_TYPE_CHOICES = (
    ('NATIONAL_ID', 'National ID'),
    ('PASSPORT', 'Passport'),
    ('DRIVER_LICENSE', 'Driver License'),
    ('KRA_PIN', 'KRA PIN Certificate'),
    ('BUSINESS_REG', 'Business Registration'),
    ('CONTRACT', 'Service Contract'),
    ('LETTER', 'Introduction Letter'),
    ('UTILITY_BILL', 'Utility Bill'),
    ('OTHER', 'Other'),
)

SERVICE_TYPE_CHOICES = (
    ('INTERNET', 'Internet Service'),
    ('VOIP', 'VoIP Service'),
    ('IPTV', 'IP TV'),
    ('DEDICATED', 'Dedicated Line'),
    ('WIFI', 'WiFi Hotspot'),
    ('VPN', 'VPN Service'),
    ('COLOCATION', 'Colocation'),
)

SERVICE_STATUS_CHOICES = (
    ('PENDING', 'Pending'),
    ('ACTIVE', 'Active'),
    ('SUSPENDED', 'Suspended'),
    ('TERMINATED', 'Terminated'),
    ('CANCELLED', 'Cancelled'),
)

KENYAN_COUNTIES = (
    ('NAIROBI', 'Nairobi'),
    ('MOMBASA', 'Mombasa'),
    ('KISUMU', 'Kisumu'),
    ('NAKURU', 'Nakuru'),
    ('ELDORET', 'Eldoret'),
    ('THIKA', 'Thika'),
    ('MALINDI', 'Malindi'),
    ('KITALE', 'Kitale'),
    ('KERICHO', 'Kericho'),
    ('KAKAMEGA', 'Kakamega'),
    ('KISII', 'Kisii'),
    ('NYERI', 'Nyeri'),
    ('MERU', 'Meru'),
    ('MURANGA', 'Muranga'),
    ('KIRINYAGA', 'Kirinyaga'),
    ('NYANDARUA', 'Nyandarua'),
    ('LAIKIPIA', 'Laikipia'),
    ('NAKURU', 'Nakuru'),
    ('NAROK', 'Narok'),
    ('KAJIADO', 'Kajiado'),
    ('MACHAKOS', 'Machakos'),
    ('MAKUENI', 'Makueni'),
    ('KIAMBU', 'Kiambu'),
)


class Customer(models.Model):
    """Main customer model"""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,  # FIXED: Use settings.AUTH_USER_MODEL
        on_delete=models.CASCADE, 
        related_name='customer_profile'
    )
    company = models.ForeignKey(
        Company, 
        on_delete=models.CASCADE, 
        related_name='customers'
    )
    
    # Personal Information
    customer_code = models.CharField(
        max_length=50, 
        unique=True, 
        db_index=True
    )
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(
        max_length=1, 
        choices=GENDER_CHOICES, 
        default='M'
    )
    
    # Identification
    id_type = models.CharField(
        max_length=20, 
        choices=ID_TYPE_CHOICES, 
        default='NATIONAL_ID'
    )
    id_number = models.CharField(
        max_length=50, 
        unique=True, 
        db_index=True
    )
    
    # Contact Information
    
    alternative_phone = models.CharField(max_length=20, blank=True)
    
    # Customer Details
    marital_status = models.CharField(
        max_length=20, 
        choices=MARITAL_STATUS_CHOICES, 
        blank=True
    )
    occupation = models.CharField(max_length=100, blank=True)
    employer = models.CharField(max_length=200, blank=True)
    
    # Customer Classification
    customer_type = models.CharField(
        max_length=20, 
        choices=(
            ('RESIDENTIAL', 'Residential'),
            ('BUSINESS', 'Business'),
            ('CORPORATE', 'Corporate'),
            ('INSTITUTION', 'Institution'),
        ),
        default='RESIDENTIAL'
    )
    status = models.CharField(
        max_length=20, 
        choices=CUSTOMER_STATUS_CHOICES, 
        default='ACTIVE'
    )
    category = models.CharField(
        max_length=20,
        choices=(
            ('PREPAID', 'Prepaid'),
            ('POSTPAID', 'Postpaid'),
            ('CORPORATE', 'Corporate'),
        ),
        default='PREPAID'
    )
    
    # Service Information
    activation_date = models.DateField(default=timezone.now)
    deactivation_date = models.DateField(null=True, blank=True)
    referral_source = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    
    # Billing Information
    billing_cycle = models.PositiveIntegerField(
        default=1,
        help_text="Day of month for billing (1-31)"
    )
    credit_limit = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=0.00
    )
    outstanding_balance = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=0.00
    )
    
    # Marketing Preferences
    receive_sms = models.BooleanField(default=True)
    receive_email = models.BooleanField(default=True)
    receive_promotions = models.BooleanField(default=True)
    
    # Audit fields (replacing AuditMixin)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,  # FIXED
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_customers'
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,  # FIXED
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='updated_customers'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['customer_code']),
            models.Index(fields=['id_number']),
            models.Index(fields=['status']),
            models.Index(fields=['customer_type']),
        ]
    
    def __str__(self):
        return f"{self.customer_code} - {self.user.get_full_name()}"
    
    @property
    def full_name(self):
        return self.user.get_full_name()
    
    @property
    def email(self):
        return self.user.email
    
    @property
    def is_active(self):
        return self.status == 'ACTIVE'
    
    def update_balance(self, amount):
        """Update customer's outstanding balance"""
        self.outstanding_balance += amount
        self.save(update_fields=['outstanding_balance', 'updated_at'])
    
    def save(self, *args, **kwargs):
        # Generate customer code if not exists
        if not self.customer_code:
            # Format: CUS-{company_id}-{sequence}
            last_customer = Customer.objects.filter(company=self.company).order_by('id').last()
            sequence = 1
            if last_customer and last_customer.customer_code:
                try:
                    sequence = int(last_customer.customer_code.split('-')[-1]) + 1
                except (IndexError, ValueError):
                    sequence = Customer.objects.filter(company=self.company).count() + 1
            self.customer_code = f"CUS-{self.company.id}-{sequence}"
        super().save(*args, **kwargs)


class CustomerAddress(models.Model):
    """Customer addresses (billing, installation, etc.)"""
    customer = models.ForeignKey(
        Customer, 
        on_delete=models.CASCADE, 
        related_name='addresses'
    )
    address_type = models.CharField(
        max_length=20, 
        choices=ADDRESS_TYPE_CHOICES, 
        default='BILLING'
    )
    is_primary = models.BooleanField(default=False)
    
    # Address Details
    building_name = models.CharField(max_length=200, blank=True)
    floor = models.CharField(max_length=50, blank=True)
    room = models.CharField(max_length=50, blank=True)
    street_address = models.TextField()
    landmark = models.CharField(max_length=200, blank=True)
    
    # Location
    county = models.CharField(
        max_length=50, 
        choices=KENYAN_COUNTIES
    )
    sub_county = models.CharField(max_length=100)
    ward = models.CharField(max_length=100)
    estate = models.CharField(max_length=200, blank=True)
    
    # Contact at address
    contact_person = models.CharField(max_length=200, blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    
    # Coordinates
    latitude = models.DecimalField(
        max_digits=9, 
        decimal_places=6, 
        null=True, 
        blank=True
    )
    longitude = models.DecimalField(
        max_digits=9, 
        decimal_places=6, 
        null=True, 
        blank=True
    )
    
    # Additional info
    installation_notes = models.TextField(blank=True)
    
    # Audit fields
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,  # FIXED
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_customer_addresses'
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,  # FIXED
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='updated_customer_addresses'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-is_primary', 'address_type']
        verbose_name_plural = "Customer addresses"
        unique_together = ['customer', 'address_type']
    
    def __str__(self):
        return f"{self.customer.customer_code} - {self.address_type} Address"
    
    def save(self, *args, **kwargs):
        # Ensure only one primary address per type
        if self.is_primary:
            CustomerAddress.objects.filter(
                customer=self.customer,
                address_type=self.address_type
            ).update(is_primary=False)
        super().save(*args, **kwargs)


class CustomerDocument(models.Model):
    """Customer documents (ID, contract, etc.)"""
    customer = models.ForeignKey(
        Customer, 
        on_delete=models.CASCADE, 
        related_name='documents'
    )
    document_type = models.CharField(
        max_length=30, 
        choices=DOCUMENT_TYPE_CHOICES
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    
    # File storage
    document_file = models.FileField(
        upload_to='customer_documents/%Y/%m/%d/',
        validators=[MinLengthValidator(1)]
    )
    file_size = models.PositiveIntegerField(null=True, blank=True)
    mime_type = models.CharField(max_length=100, blank=True)
    
    # Verification
    verified = models.BooleanField(default=False)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,  # FIXED
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='verified_documents'
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    verification_notes = models.TextField(blank=True)
    
    # Expiry
    expiry_date = models.DateField(null=True, blank=True)
    is_expired = models.BooleanField(default=False)
    
    # Audit fields
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,  # FIXED
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_customer_documents'
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,  # FIXED
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='updated_customer_documents'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.customer.customer_code} - {self.document_type}"
    
    def save(self, *args, **kwargs):
        if not self.title:
            self.title = f"{self.document_type} - {self.customer.customer_code}"
        super().save(*args, **kwargs)
    
    @property
    def file_url(self):
        return self.document_file.url if self.document_file else None


class NextOfKin(models.Model):
    """Customer's next of kin information"""
    customer = models.OneToOneField(
        Customer, 
        on_delete=models.CASCADE, 
        related_name='next_of_kin'
    )
    
    # Personal Information
    full_name = models.CharField(max_length=200)
    relationship = models.CharField(
        max_length=50,
        choices=(
            ('SPOUSE', 'Spouse'),
            ('PARENT', 'Parent'),
            ('CHILD', 'Child'),
            ('SIBLING', 'Sibling'),
            ('OTHER', 'Other'),
        )
    )
    
    # Contact Information
    phone_number = models.CharField(max_length=20)
    email = models.EmailField(blank=True)
    
    # Identification
    id_type = models.CharField(
        max_length=20, 
        choices=ID_TYPE_CHOICES, 
        blank=True
    )
    id_number = models.CharField(max_length=50, blank=True)
    
    # Address
    address = models.TextField(blank=True)
    county = models.CharField(
        max_length=50, 
        choices=KENYAN_COUNTIES,
        blank=True
    )
    
    # Emergency contact priority
    is_primary_contact = models.BooleanField(default=True)
    
    # Audit fields
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,  # FIXED
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_next_of_kins'
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,  # FIXED
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='updated_next_of_kins'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name_plural = "Next of kin"
    
    def __str__(self):
        return f"{self.customer.customer_code} - {self.full_name}"


class CustomerNotes(models.Model):
    """Internal notes about customers"""
    customer = models.ForeignKey(
        Customer, 
        on_delete=models.CASCADE, 
        related_name='notes_history'
    )
    note = models.TextField()
    
    # Note categorization
    note_type = models.CharField(
        max_length=30,
        choices=(
            ('GENERAL', 'General Note'),
            ('COMPLAINT', 'Complaint'),
            ('COMPLIMENT', 'Compliment'),
            ('SERVICE_ISSUE', 'Service Issue'),
            ('BILLING_ISSUE', 'Billing Issue'),
            ('FOLLOW_UP', 'Follow Up Required'),
            ('RESOLUTION', 'Resolution'),
        ),
        default='GENERAL'
    )
    
    # Priority and status
    priority = models.CharField(
        max_length=20,
        choices=(
            ('LOW', 'Low'),
            ('MEDIUM', 'Medium'),
            ('HIGH', 'High'),
            ('URGENT', 'Urgent'),
        ),
        default='MEDIUM'
    )
    
    # Follow-up information
    requires_followup = models.BooleanField(default=False)
    followup_date = models.DateField(null=True, blank=True)
    followup_completed = models.BooleanField(default=False)
    
    # Internal tracking
    internal_only = models.BooleanField(
        default=False,
        help_text="If True, note won't be visible to customer"
    )
    
    # Attachment
    attachment = models.FileField(
        upload_to='customer_notes/%Y/%m/%d/',
        blank=True,
        null=True
    )
    
    # Audit fields
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,  # FIXED
        on_delete=models.CASCADE,
        related_name='created_customer_notes'
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,  # FIXED
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='updated_customer_notes'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = "Customer notes"
    
    def __str__(self):
        return f"Note for {self.customer.customer_code}"


class ServiceConnection(models.Model):
    """Customer service connection details"""
    customer = models.ForeignKey(
        Customer, 
        on_delete=models.CASCADE, 
        related_name='services'
    )
    
    # Service Details
    service_type = models.CharField(
        max_length=30, 
        choices=SERVICE_TYPE_CHOICES, 
        default='INTERNET'
    )

    service_plan = models.CharField(max_length=100)

    plan = models.ForeignKey(
    'billing.Plan',
    on_delete=models.SET_NULL,          # or PROTECT if you don't want to allow plan deletion
    null=True,
    blank=True,
    related_name='service_connections'  # ← this creates the reverse accessor we need!
    )
    
    # Connection Details
    connection_type = models.CharField(
        max_length=30,
        choices=(
            ('FIBER', 'Fiber Optic'),
            ('WIRELESS', 'Wireless'),
            ('COPPER', 'Copper (DSL)'),
            ('SATELLITE', 'Satellite'),
        ),
        default='FIBER'
    )
    
    # Status and Dates
    status = models.CharField(
        max_length=20, 
        choices=SERVICE_STATUS_CHOICES, 
        default='PENDING'
    )
    activation_date = models.DateTimeField(null=True, blank=True)
    suspension_date = models.DateTimeField(null=True, blank=True)
    termination_date = models.DateTimeField(null=True, blank=True)
    
    # Network Details
    ip_address = models.GenericIPAddressField(
        protocol='both',
        unpack_ipv4=False,
        null=True,
        blank=True
    )
    mac_address = models.CharField(max_length=17, blank=True)
    vlan_id = models.PositiveIntegerField(null=True, blank=True)
    
    # Equipment Details
    router_model = models.CharField(max_length=100, blank=True)
    router_serial = models.CharField(max_length=100, blank=True)
    ont_model = models.CharField(max_length=100, blank=True)
    ont_serial = models.CharField(max_length=100, blank=True)
    
    # Bandwidth and QoS
    download_speed = models.PositiveIntegerField(
        help_text="Speed in Mbps"
    )
    upload_speed = models.PositiveIntegerField(
        help_text="Speed in Mbps"
    )
    data_cap = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Data cap in GB (null = unlimited)"
    )
    qos_profile = models.CharField(max_length=50, blank=True)
    
    # Installation Details
    installation_address = models.ForeignKey(
        CustomerAddress,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='service_installations'
    )
    installation_notes = models.TextField(blank=True)
    installed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,  # FIXED
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='installed_services'
    )
    
    # Billing Information
    monthly_price = models.DecimalField(
        max_digits=10, 
        decimal_places=2
    )
    setup_fee = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=0.00
    )
    prorated_billing = models.BooleanField(default=True)
    
    # Auto-renewal
    auto_renew = models.BooleanField(default=True)
    contract_period = models.PositiveIntegerField(
        default=12,
        help_text="Contract period in months"
    )
    
    # Audit fields
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,  # FIXED
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_service_connections'
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,  # FIXED
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='updated_service_connections'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.customer.customer_code} - {self.service_type}"
    
    @property
    def is_active(self):
        return self.status == 'ACTIVE'
    
    @property
    def days_active(self):
        if self.activation_date:
            return (timezone.now() - self.activation_date).days
        return 0
    
    def activate_service(self, user=None):
        """Activate the service"""
        if self.status != 'ACTIVE':
            self.status = 'ACTIVE'
            self.activation_date = timezone.now()
            if user:
                self.installed_by = user
            self.save()
    
    def suspend_service(self, reason=""):
        """Suspend the service"""
        self.status = 'SUSPENDED'
        self.suspension_date = timezone.now()
        self.save()
        # Create a note about suspension
        CustomerNotes.objects.create(
            customer=self.customer,
            note=f"Service suspended. Reason: {reason}",
            note_type='SERVICE_ISSUE',
            priority='HIGH'
        )
    
    def terminate_service(self, reason=""):
        """Terminate the service"""
        self.status = 'TERMINATED'
        self.termination_date = timezone.now()
        self.save()
        # Create a note about termination
        CustomerNotes.objects.create(
            customer=self.customer,
            note=f"Service terminated. Reason: {reason}",
            note_type='SERVICE_ISSUE',
            priority='HIGH'
        )