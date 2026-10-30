import secrets

from django.core.validators import MinValueValidator
from django.db import models


def generate_tracking_token():
    return secrets.token_urlsafe(24)


class Category(models.Model):
    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['sort_order', 'name']
        verbose_name_plural = 'Categories'

    def __str__(self):
        return self.name


class ShopSettings(models.Model):
    """Singleton storefront configuration managed by platform superadmins."""

    store_name = models.CharField(max_length=120, default='Netily Shop')
    announcement_text = models.CharField(max_length=240, blank=True, default='')
    hero_eyebrow = models.CharField(max_length=120, default='Netily Network Equipment')
    hero_title = models.CharField(max_length=180, default='Build a network that is ready to grow')
    hero_description = models.CharField(
        max_length=500,
        default='Carrier-grade routing, fibre, wireless, power, and installation equipment for modern ISPs.',
    )
    hero_image = models.CharField(max_length=500, default='/shop-assets/netily_hero_bg.jpg')
    hero_primary_label = models.CharField(max_length=80, default='Browse equipment')
    hero_primary_href = models.CharField(max_length=240, default='/shop/catalog')
    hero_secondary_label = models.CharField(max_length=80, default='Ask before you buy')
    hero_secondary_href = models.CharField(max_length=240, default='https://wa.me/254100034307')
    support_email = models.EmailField(blank=True, default='')
    support_phone = models.CharField(max_length=32, blank=True, default='')
    whatsapp_number = models.CharField(max_length=32, blank=True, default='254100034307')
    default_shipping_fee = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    free_shipping_threshold = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)]
    )
    allow_checkout = models.BooleanField(default=True)
    checkout_notice = models.CharField(max_length=300, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'Shop settings'

    def save(self, *args, **kwargs):
        self.pk = 1
        return super().save(*args, **kwargs)

    @classmethod
    def get_active(cls):
        settings, _ = cls.objects.get_or_create(pk=1)
        return settings

    def __str__(self):
        return self.store_name


class Product(models.Model):
    slug = models.SlugField(max_length=160, unique=True)          # public product id used by the frontend
    name = models.CharField(max_length=255)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='products')
    price = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    currency = models.CharField(max_length=3, default='KES')
    compare_at_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    image = models.CharField(max_length=500, blank=True, default='')
    hover_image = models.CharField(max_length=500, blank=True, default='')
    long_description = models.TextField(blank=True, default='')
    details = models.JSONField(default=list, blank=True)           # Specifications bullets
    materials = models.JSONField(default=list, blank=True)         # "In the box"
    care = models.JSONField(default=list, blank=True)              # Support & warranty
    specs_sheet = models.JSONField(default=dict, blank=True)
    sizes = models.JSONField(default=list, blank=True)             # [{"size": "...", "available": true}]
    colors = models.JSONField(default=list, blank=True)            # [{"name": "...", "available": true}]
    warranty = models.CharField(max_length=120, blank=True, default='')
    made_in = models.CharField(max_length=120, blank=True, default='')
    track_stock = models.BooleanField(default=True)
    stock_quantity = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name']
        indexes = [
            models.Index(fields=['is_active', 'sort_order', 'name']),
            models.Index(fields=['category', 'is_active']),
        ]

    def __str__(self):
        return self.name


class Order(models.Model):
    PENDING_PAYMENT = 'pending_payment'
    PAID = 'paid'
    PROCESSING = 'processing'
    SHIPPED = 'shipped'
    DELIVERED = 'delivered'
    CANCELLED = 'cancelled'
    EXPIRED = 'expired'
    STATUS_CHOICES = [
        (PENDING_PAYMENT, 'Pending payment'), (PAID, 'Paid'), (PROCESSING, 'Processing'),
        (SHIPPED, 'Shipped'), (DELIVERED, 'Delivered'), (CANCELLED, 'Cancelled'), (EXPIRED, 'Expired'),
    ]

    PAY_PENDING = 'pending'
    PAY_PAID = 'paid'
    PAY_FAILED = 'failed'
    PAYMENT_STATUS_CHOICES = [(PAY_PENDING, 'Pending'), (PAY_PAID, 'Paid'), (PAY_FAILED, 'Failed')]

    METHOD_MPESA = 'mpesa'
    METHOD_CARD = 'card'
    METHOD_CHOICES = [(METHOD_MPESA, 'M-Pesa'), (METHOD_CARD, 'Card')]

    reference = models.CharField(max_length=20, unique=True)
    tracking_token = models.CharField(max_length=48, default=generate_tracking_token, editable=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING_PAYMENT)
    payment_status = models.CharField(max_length=10, choices=PAYMENT_STATUS_CHOICES, default=PAY_PENDING)
    payment_method = models.CharField(max_length=10, choices=METHOD_CHOICES, default=METHOD_MPESA)

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    company = models.CharField(max_length=150, blank=True, default='')
    email = models.EmailField()
    phone = models.CharField(max_length=32)
    address = models.CharField(max_length=255)
    apartment = models.CharField(max_length=255, blank=True, default='')
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100, blank=True, default='')
    zip_code = models.CharField(max_length=20, blank=True, default='')
    country = models.CharField(max_length=100, default='Kenya')
    notes = models.CharField(max_length=1000, blank=True, default='')

    currency = models.CharField(max_length=3, default='KES')
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    shipping_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    fx_rate = models.DecimalField(max_digits=10, decimal_places=4, default=1)
    amount_kes = models.PositiveIntegerField(default=0)

    mpesa_phone = models.CharField(max_length=15, blank=True, default='')
    merchant_request_id = models.CharField(max_length=120, blank=True, default='')
    checkout_request_id = models.CharField(max_length=120, null=True, blank=True, unique=True)
    mpesa_receipt = models.CharField(max_length=30, blank=True, default='')
    payment_failure_reason = models.CharField(max_length=255, blank=True, default='')
    stk_attempts = models.PositiveSmallIntegerField(default=0)
    last_stk_at = models.DateTimeField(null=True, blank=True)

    paid_at = models.DateTimeField(null=True, blank=True)
    stock_released = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['payment_status', 'payment_method', 'last_stk_at']),
            models.Index(fields=['email']),
        ]

    def __str__(self):
        return self.reference

    @staticmethod
    def new_reference():
        # 12 chars on purpose: Daraja AccountReference is limited to 12 characters.
        while True:
            ref = f"NTL-{secrets.token_hex(4).upper()}"
            if not Order.objects.filter(reference=ref).exists():
                return ref


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True, related_name='order_items')
    product_slug = models.CharField(max_length=160)
    name = models.CharField(max_length=255)
    image = models.CharField(max_length=500, blank=True, default='')
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    quantity = models.PositiveIntegerField()
    selected_storage = models.CharField(max_length=120, blank=True, default='')
    selected_color = models.CharField(max_length=120, blank=True, default='')
    line_total = models.DecimalField(max_digits=12, decimal_places=2)

    def __str__(self):
        return f"{self.name} x{self.quantity}"
