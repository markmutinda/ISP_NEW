"""
Constants for ISP Management System
"""

# User Roles
USER_ROLES = {
    'admin': 'Administrator',
    'staff': 'Staff Member',
    'technician': 'Technician',
    'customer': 'Customer',
    'accountant': 'Accountant',
    'support': 'Support Agent',
}

# Customer Status
CUSTOMER_STATUS = {
    'active': 'Active',
    'suspended': 'Suspended',
    'terminated': 'Terminated',
    'pending': 'Pending Activation',
    'inactive': 'Inactive',
}

# Service Status
SERVICE_STATUS = {
    'active': 'Active',
    'suspended': 'Suspended',
    'terminated': 'Terminated',
    'pending': 'Pending',
    'provisioning': 'Provisioning',
}

# Invoice Status
INVOICE_STATUS = {
    'draft': 'Draft',
    'pending': 'Pending',
    'paid': 'Paid',
    'overdue': 'Overdue',
    'cancelled': 'Cancelled',
    'partially_paid': 'Partially Paid',
}

# Payment Methods
PAYMENT_METHODS = {
    'mpesa': 'M-Pesa',
    'cash': 'Cash',
    'bank_transfer': 'Bank Transfer',
    'cheque': 'Cheque',
    'card': 'Credit/Debit Card',
    'airtel_money': 'Airtel Money',
    'tkash': 'T-Kash',
    'equitel': 'Equitel',
}

# Payment Status
PAYMENT_STATUS = {
    'pending': 'Pending',
    'completed': 'Completed',
    'failed': 'Failed',
    'cancelled': 'Cancelled',
    'refunded': 'Refunded',
}

# Ticket Status
TICKET_STATUS = {
    'open': 'Open',
    'in_progress': 'In Progress',
    'resolved': 'Resolved',
    'closed': 'Closed',
    'pending_customer': 'Pending Customer',
    'pending_vendor': 'Pending Vendor',
}

# Ticket Priority
TICKET_PRIORITY = {
    'low': 'Low',
    'medium': 'Medium',
    'high': 'High',
    'critical': 'Critical',
}

# Ticket Categories
TICKET_CATEGORIES = {
    'billing': 'Billing',
    'technical': 'Technical',
    'service': 'Service',
    'account': 'Account',
    'general': 'General',
    'complaint': 'Complaint',
}

# Network Device Types
DEVICE_TYPES = {
    'router': 'Router',
    'switch': 'Switch',
    'firewall': 'Firewall',
    'access_point': 'Access Point',
    'olt': 'OLT',
    'onu': 'ONU',
    'modem': 'Modem',
}

# Network Device Status
DEVICE_STATUS = {
    'online': 'Online',
    'offline': 'Offline',
    'maintenance': 'Under Maintenance',
    'faulty': 'Faulty',
}

# Bandwidth Packages
BANDWIDTH_PACKAGES = {
    'home_basic': 'Home Basic (5 Mbps)',
    'home_standard': 'Home Standard (10 Mbps)',
    'home_premium': 'Home Premium (20 Mbps)',
    'business_basic': 'Business Basic (50 Mbps)',
    'business_standard': 'Business Standard (100 Mbps)',
    'business_premium': 'Business Premium (500 Mbps)',
    'enterprise': 'Enterprise (1 Gbps+)',
}

# Service Types
SERVICE_TYPES = {
    'fibre': 'Fibre Internet',
    'wireless': 'Wireless Internet',
    'voip': 'VoIP Services',
    'iptv': 'IPTV Services',
    'wifi': 'Wi-Fi Hotspot',
    'dedicated': 'Dedicated Line',
}

# Billing Cycles
BILLING_CYCLES = {
    'monthly': 'Monthly',
    'quarterly': 'Quarterly',
    'semi_annual': 'Semi-Annual',
    'annual': 'Annual',
    'one_time': 'One Time',
}

# Invoice Types
INVOICE_TYPES = {
    'recurring': 'Recurring',
    'one_time': 'One Time',
    'credit': 'Credit Note',
    'debit': 'Debit Note',
    'adjustment': 'Adjustment',
}

# Tax Rates in Kenya
TAX_RATES = {
    'VAT': 16.0,  # Value Added Tax
    'WHT_INDIVIDUAL': 5.0,  # Withholding Tax for Individuals
    'WHT_COMPANY': 3.0,  # Withholding Tax for Companies
    'EXCISE_INTERNET': 15.0,  # Excise Duty for Internet Services
    'EXCISE_VOICE': 12.0,  # Excise Duty for Voice Calls
    'EXCISE_SMS': 12.0,  # Excise Duty for SMS
}

# Tax Types
TAX_TYPES = [
    ('VAT', 'Value Added Tax'),
    ('WHT', 'Withholding Tax'),
    ('EXCISE', 'Excise Duty'),
    ('INCOME_TAX', 'Income Tax'),
    ('OTHER', 'Other'),
]
# Notification Types
NOTIFICATION_TYPES = {
    'email': 'Email',
    'sms': 'SMS',
    'push': 'Push Notification',
    'in_app': 'In-App Notification',
}

# Notification Categories
NOTIFICATION_CATEGORIES = {
    'billing': 'Billing',
    'service': 'Service',
    'system': 'System',
    'security': 'Security',
    'marketing': 'Marketing',
}

# Audit Actions
AUDIT_ACTIONS = {
    'create': 'Create',
    'update': 'Update',
    'delete': 'Delete',
    'login': 'Login',
    'logout': 'Logout',
    'view': 'View',
    'export': 'Export',
    'import': 'Import',
}

# Gender Choices
GENDER_CHOICES = {
    'M': 'Male',
    'F': 'Female',
    'O': 'Other',
}

# Marital Status
MARITAL_STATUS = {
    'single': 'Single',
    'married': 'Married',
    'divorced': 'Divorced',
    'widowed': 'Widowed',
}

# ID Types
ID_TYPES = {
    'national_id': 'National ID',
    'passport': 'Passport',
    'alien_id': 'Alien ID',
    'driving_license': 'Driving License',
}

# Employment Status
EMPLOYMENT_STATUS = {
    'employed': 'Employed',
    'self_employed': 'Self Employed',
    'unemployed': 'Unemployed',
    'student': 'Student',
    'retired': 'Retired',
}

# Connection Types
CONNECTION_TYPES = {
    'pppoe': 'PPPoE',
    'static_ip': 'Static IP',
    'dhcp': 'DHCP',
    'hotspot': 'Hotspot',
}

# Payment Gateway
PAYMENT_GATEWAYS = {
    'mpesa_daraja': 'M-Pesa Daraja',
    'pesapal': 'Pesapal',
    'iPay': 'iPay',
    'flutterwave': 'Flutterwave',
    'stripe': 'Stripe',
}

# Document Types
DOCUMENT_TYPES = {
    'id_copy': 'ID Copy',
    'passport_photo': 'Passport Photo',
    'utility_bill': 'Utility Bill',
    'lease_agreement': 'Lease Agreement',
    'business_registration': 'Business Registration',
    'kra_pin': 'KRA PIN Certificate',
}

# Rating Scale
RATING_SCALE = {
    1: 'Very Poor',
    2: 'Poor',
    3: 'Average',
    4: 'Good',
    5: 'Excellent',
}

# SLA Levels
SLA_LEVELS = {
    'basic': 'Basic (99.5%)',
    'standard': 'Standard (99.9%)',
    'premium': 'Premium (99.99%)',
}

# Fault Types
FAULT_TYPES = {
    'outage': 'Network Outage',
    'slow_speed': 'Slow Speed',
    'no_connectivity': 'No Connectivity',
    'intermittent': 'Intermittent Connection',
    'hardware': 'Hardware Fault',
}

# Maintenance Types
MAINTENANCE_TYPES = {
    'planned': 'Planned Maintenance',
    'emergency': 'Emergency Maintenance',
    'upgrade': 'System Upgrade',
}

# Inventory Status
INVENTORY_STATUS = {
    'in_stock': 'In Stock',
    'out_of_stock': 'Out of Stock',
    'ordered': 'Ordered',
    'delivered': 'Delivered',
    'faulty': 'Faulty',
    'retired': 'Retired',
}

# Default Settings
DEFAULT_SETTINGS = {
    'company_name': 'ISP Management System',
    'currency': 'KES',
    'timezone': 'Africa/Nairobi',
    'date_format': 'YYYY-MM-DD',
    'items_per_page': 20,
    'session_timeout': 30,
    'password_expiry_days': 90,
    'max_login_attempts': 5,
}

# API Response Codes
API_RESPONSE_CODES = {
    'success': 200,
    'created': 201,
    'bad_request': 400,
    'unauthorized': 401,
    'forbidden': 403,
    'not_found': 404,
    'conflict': 409,
    'server_error': 500,
}

# Kenyan Counties (Complete List)
KENYAN_COUNTIES = [
    'Mombasa', 'Kwale', 'Kilifi', 'Tana River', 'Lamu', 'Taita Taveta',
    'Garissa', 'Wajir', 'Mandera', 'Marsabit', 'Isiolo', 'Meru', 'Tharaka Nithi',
    'Embu', 'Kitui', 'Machakos', 'Makueni', 'Nyandarua', 'Nyeri', 'Kirinyaga',
    'Murang\'a', 'Kiambu', 'Turkana', 'West Pokot', 'Samburu', 'Trans Nzoia',
    'Uasin Gishu', 'Elgeyo Marakwet', 'Nandi', 'Baringo', 'Laikipia', 'Nakuru',
    'Narok', 'Kajiado', 'Kericho', 'Bomet', 'Kakamega', 'Vihiga', 'Bungoma',
    'Busia', 'Siaya', 'Kisumu', 'Homa Bay', 'Migori', 'Kisii', 'Nyamira', 'Nairobi'
]

# M-Pesa Transaction Types
MPESA_TRANSACTION_TYPES = {
    'customer_pay_bill_online': 'Pay Bill',
    'customer_buy_goods_online': 'Buy Goods',
    'salary_payment': 'Salary Payment',
    'business_payment': 'Business Payment',
    'promotion_payment': 'Promotion Payment',
    'account_balance': 'Account Balance',
    'reversal': 'Reversal',
}

# Network Ports
NETWORK_PORTS = {
    'http': 80,
    'https': 443,
    'ftp': 21,
    'ssh': 22,
    'telnet': 23,
    'smtp': 25,
    'dns': 53,
    'dhcp': 67,
    'tftp': 69,
    'http_alt': 8080,
    'mysql': 3306,
    'postgresql': 5432,
    'redis': 6379,
    'mongodb': 27017,
}

# Color Codes for UI
COLOR_CODES = {
    'primary': '#3B82F6',
    'secondary': '#6B7280',
    'success': '#10B981',
    'danger': '#EF4444',
    'warning': '#F59E0B',
    'info': '#3B82F6',
    'light': '#F9FAFB',
    'dark': '#111827',
}

# File Size Limits (in bytes)
FILE_SIZE_LIMITS = {
    'profile_picture': 5 * 1024 * 1024,  # 5MB
    'document': 10 * 1024 * 1024,  # 10MB
    'logo': 2 * 1024 * 1024,  # 2MB
}

# Allowed File Extensions
ALLOWED_EXTENSIONS = {
    'images': ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'],
    'documents': ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'],
    'archives': ['.zip', '.rar', '.7z'],
}

# Regex Patterns
REGEX_PATTERNS = {
    'email': r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$',
    'phone': r'^\+?1?\d{9,15}$',
    'username': r'^[a-zA-Z0-9_]{3,30}$',
    'password': r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$',
    'url': r'^https?://(?:www\.)?[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/\S*)?$',
    'ipv4': r'^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$',
    'mac': r'^([0-9A-F]{2}[:-]){5}([0-9A-F]{2})$',
}

# Add these to existing constants

# Customer Status
CUSTOMER_STATUS_CHOICES = [
    ('LEAD', 'Lead'),
    ('PENDING', 'Pending Approval'),
    ('ACTIVE', 'Active'),
    ('SUSPENDED', 'Suspended'),
    ('TERMINATED', 'Terminated'),
    ('INACTIVE', 'Inactive'),
]

# Address Types
ADDRESS_TYPE_CHOICES = [
    ('BILLING', 'Billing Address'),
    ('INSTALLATION', 'Installation Address'),
    ('HOME', 'Home Address'),
    ('BUSINESS', 'Business Address'),
    ('ALTERNATIVE', 'Alternative Address'),
]

# Document Types
DOCUMENT_TYPE_CHOICES = [
    ('NATIONAL_ID', 'National ID'),
    ('PASSPORT', 'Passport'),
    ('DRIVER_LICENSE', 'Driver License'),
    ('KRA_PIN', 'KRA PIN Certificate'),
    ('BUSINESS_REG', 'Business Registration'),
    ('CONTRACT', 'Service Contract'),
    ('LETTER', 'Introduction Letter'),
    ('UTILITY_BILL', 'Utility Bill'),
    ('OTHER', 'Other'),
]

# Service Types
SERVICE_TYPE_CHOICES = [
    ('INTERNET', 'Internet Service'),
    ('VOIP', 'VoIP Service'),
    ('IPTV', 'IP TV'),
    ('DEDICATED', 'Dedicated Line'),
    ('WIFI', 'WiFi Hotspot'),
    ('VPN', 'VPN Service'),
    ('COLOCATION', 'Colocation'),
]

# Service Status
SERVICE_STATUS_CHOICES = [
    ('PENDING', 'Pending'),
    ('ACTIVE', 'Active'),
    ('SUSPENDED', 'Suspended'),
    ('TERMINATED', 'Terminated'),
    ('CANCELLED', 'Cancelled'),
]

# Marital Status
MARITAL_STATUS_CHOICES = [
    ('SINGLE', 'Single'),
    ('MARRIED', 'Married'),
    ('DIVORCED', 'Divorced'),
    ('WIDOWED', 'Widowed'),
    ('SEPARATED', 'Separated'),
]

# ID Types
ID_TYPE_CHOICES = [
    ('NATIONAL_ID', 'National ID'),
    ('PASSPORT', 'Passport'),
    ('ALIEN_ID', 'Alien ID'),
    ('REFUGEE_ID', 'Refugee ID'),
    ('BIRTH_CERTIFICATE', 'Birth Certificate'),
]

# Kenyan Counties
KENYAN_COUNTIES = [
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
    # Add all 47 counties...
]

VOUCHER_TYPES = [
    ('PREPAID', 'Prepaid Internet'),
    ('VOICE', 'Voice Calling'),
    ('DATA', 'Mobile Data'),
    ('GENERAL', 'General Purpose'),
    ('PROMOTIONAL', 'Promotional'),
    ('LOYALTY', 'Loyalty Reward'),
]

