from django.db import models
from django.contrib.auth import get_user_model
from django.core.validators import MinValueValidator
from django.utils import timezone
import uuid


User = get_user_model()


class Supplier(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    contact_person = models.CharField(max_length=255)
    contact_name = models.CharField(max_length=255, blank=True, null=True)  # alias
    email = models.EmailField()
    phone = models.CharField(max_length=20)
    address = models.TextField()
    website = models.URLField(blank=True, null=True)
    tax_id = models.CharField(max_length=50, blank=True, null=True)
    payment_terms = models.CharField(max_length=100, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = "Supplier"
        verbose_name_plural = "Suppliers"
        app_label = 'inventory'

    def __str__(self):
        return self.name


class EquipmentType(models.Model):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20, unique=True, blank=True)
    description = models.TextField(blank=True, null=True)
    parent = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='subtypes'
    )
    is_network_equipment = models.BooleanField(default=False)
    has_serial_numbers = models.BooleanField(default=True)
    requires_assignment = models.BooleanField(default=False)
    min_stock_level = models.PositiveIntegerField(default=5)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'inventory'
        ordering = ['name']
        verbose_name = "Equipment Type"
        verbose_name_plural = "Equipment Types"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = self.name[:3].upper().replace(' ', '_')
        super().save(*args, **kwargs)


class EquipmentItem(models.Model):
    STATUS_CHOICES = [
        ('in_stock', 'In Stock'),
        ('assigned', 'Assigned'),
        ('in_use', 'In Use'),
        ('maintenance', 'Under Maintenance'),
        ('faulty', 'Faulty'),
        ('retired', 'Retired'),
        ('lost', 'Lost'),
        ('disposed', 'Disposed'),
    ]

    CONDITION_CHOICES = [
        ('new', 'New'),
        ('good', 'Good'),
        ('fair', 'Fair'),
        ('poor', 'Poor'),
        ('faulty', 'Faulty'),
    ]

    equipment_type = models.ForeignKey(
        EquipmentType, on_delete=models.PROTECT, related_name='items'
    )
    name = models.CharField(max_length=255)
    model = models.CharField(max_length=100, blank=True, null=True)
    serial_number = models.CharField(max_length=100, unique=True, blank=True, null=True)
    asset_tag = models.CharField(max_length=50, unique=True, blank=True, null=True)
    mac_address = models.CharField(max_length=17, blank=True, null=True)

    supplier = models.ForeignKey(
        Supplier, on_delete=models.SET_NULL, null=True, blank=True, related_name='equipment'
    )
    purchase_date = models.DateField(blank=True, null=True)
    purchase_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0)]
    )
    warranty_expiry = models.DateField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='in_stock')
    condition = models.CharField(max_length=20, choices=CONDITION_CHOICES, default='good')

    location = models.CharField(max_length=255, blank=True, null=True)
    shelf = models.CharField(max_length=50, blank=True, null=True)

    assigned_to = models.ForeignKey(
        'staff.Employee', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='assigned_equipment'
    )

    # Customer deployment tracking
    assigned_to_customer = models.ForeignKey(
        'customers.Customer', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='assigned_equipment'
    )

    notes = models.TextField(blank=True, null=True)
    qr_code = models.ImageField(upload_to='inventory/qr_codes/', null=True, blank=True)
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    firmware_version = models.CharField(max_length=50, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'inventory'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['serial_number']),
            models.Index(fields=['asset_tag']),
            models.Index(fields=['status']),
            models.Index(fields=['equipment_type']),
        ]

    def __str__(self):
        return f"{self.name} - {self.serial_number or self.asset_tag or 'No ID'}"

    @property
    def is_available(self):
        return self.status == 'in_stock' and self.condition in ['new', 'good', 'fair']

    @property
    def age_in_months(self):
        if self.purchase_date:
            today = timezone.now().date()
            months = (today.year - self.purchase_date.year) * 12 + (today.month - self.purchase_date.month)
            return max(0, months)
        return 0

    @property
    def assigned_to_name(self):
        if self.assigned_to:
            return self.assigned_to.get_full_name()
        return None

    @property
    def assigned_to_customer_name(self):
        if self.assigned_to_customer:
            return self.assigned_to_customer.full_name
        return None

    def save(self, *args, **kwargs):
        if not self.asset_tag:
            prefix = self.equipment_type.code if self.equipment_type else 'EQP'
            count = EquipmentItem.objects.filter(equipment_type=self.equipment_type).count() + 1
            self.asset_tag = f"{prefix}-{count:06d}"
        super().save(*args, **kwargs)


class Assignment(models.Model):
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('returned', 'Returned'),
        ('overdue', 'Overdue'),
    ]

    equipment = models.ForeignKey(
        EquipmentItem, on_delete=models.CASCADE, related_name='assignments'
    )
    assigned_to = models.ForeignKey(
        'staff.Employee', on_delete=models.CASCADE, related_name='equipment_assignments'
    )
    assigned_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name='assignments_made'
    )
    assigned_date = models.DateField(default=timezone.now)
    expected_return_date = models.DateField(null=True, blank=True)
    actual_return_date = models.DateField(null=True, blank=True)

    condition_at_assignment = models.CharField(max_length=20, choices=EquipmentItem.CONDITION_CHOICES)
    condition_at_return = models.CharField(
        max_length=20, choices=EquipmentItem.CONDITION_CHOICES, null=True, blank=True
    )

    purpose = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'inventory'
        ordering = ['-assigned_date']
        verbose_name = "Equipment Assignment"
        verbose_name_plural = "Equipment Assignments"

    def __str__(self):
        return f"{self.equipment} → {self.assigned_to} on {self.assigned_date}"

    @property
    def status(self):
        if self.actual_return_date:
            return 'returned'
        if self.expected_return_date and self.expected_return_date < timezone.now().date():
            return 'overdue'
        return 'active'

    @property
    def employee_id(self):
        return self.assigned_to.employee_id if self.assigned_to else None

    @property
    def employee_name(self):
        return self.assigned_to.get_full_name() if self.assigned_to else None

    @property
    def equipment_name(self):
        return self.equipment.name if self.equipment else None

    @property
    def equipment_serial(self):
        return self.equipment.serial_number or self.equipment.asset_tag if self.equipment else None

    def mark_returned(self, condition, return_date=None):
        self.actual_return_date = return_date or timezone.now().date()
        self.condition_at_return = condition
        self.save()
        self.equipment.status = 'in_stock'
        self.equipment.assigned_to = None
        self.equipment.condition = condition
        self.equipment.save()


class PurchaseOrder(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('pending', 'Pending Approval'),
        ('approved', 'Approved'),
        ('ordered', 'Ordered'),
        ('received', 'Received'),
        ('cancelled', 'Cancelled'),
    ]

    po_number = models.CharField(max_length=50, unique=True, blank=True)
    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, related_name='purchase_orders'
    )
    order_date = models.DateField(default=timezone.now)
    expected_delivery = models.DateField(null=True, blank=True)
    actual_delivery = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    total_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    tax_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    prepared_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name='purchase_orders_prepared'
    )
    approved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='purchase_orders_approved'
    )
    approved_date = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'inventory'
        ordering = ['-order_date']

    def __str__(self):
        return f"PO-{self.po_number}"

    def save(self, *args, **kwargs):
        if not self.po_number:
            today = timezone.now()
            count = PurchaseOrder.objects.filter(
                order_date__year=today.year, order_date__month=today.month
            ).count() + 1
            self.po_number = f"{today.strftime('%Y%m')}-{count:04d}"
        if self.pk:
            self.total_amount = sum(item.total_price for item in self.items.all())
        super().save(*args, **kwargs)


class PurchaseOrderItem(models.Model):
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.CASCADE, related_name='items'
    )
    equipment_type = models.ForeignKey(EquipmentType, on_delete=models.PROTECT)
    description = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(0)]
    )
    received_quantity = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'inventory'

    @property
    def total_price(self):
        return self.quantity * self.unit_price

    @property
    def pending_quantity(self):
        return max(0, self.quantity - self.received_quantity)


class MaintenanceRecord(models.Model):
    STATUS_CHOICES = [
        ('scheduled', 'Scheduled'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    equipment = models.ForeignKey(
        EquipmentItem, on_delete=models.CASCADE, related_name='maintenance_records'
    )
    scheduled_date = models.DateField()
    completed_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='scheduled')
    maintenance_type = models.CharField(max_length=100)
    description = models.TextField()
    action_taken = models.TextField(blank=True, null=True)
    cost = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    performed_by = models.ForeignKey(
        'staff.Employee', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='maintenance_performed'
    )
    next_maintenance_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'inventory'
        ordering = ['-scheduled_date']


class StockAlert(models.Model):
    equipment_type = models.ForeignKey(
        EquipmentType, on_delete=models.CASCADE, related_name='stock_alerts'
    )
    threshold = models.PositiveIntegerField()
    current_stock = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True)
    triggered_on = models.DateTimeField(auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'inventory'
        ordering = ['-triggered_on']