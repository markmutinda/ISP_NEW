"""
Utility helper functions for ISP Management System
"""
import uuid
import random
import string
from datetime import datetime, timedelta
from decimal import Decimal
import re
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db.models import Max
import unicodedata
from io import BytesIO
import base64
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


# ============================================================================
# CUSTOMER MANAGEMENT FUNCTIONS
# ============================================================================

def generate_customer_code(company) -> str:
    """
    Generate unique customer code with company prefix.
    Format: {COMPANY_CODE}-{YYYYMM}-{SEQUENCE}
    
    Args:
        company: Company instance
    
    Returns:
        str: Unique customer code
    """
    try:
        year = timezone.now().strftime('%Y')
        month = timezone.now().strftime('%m')
        company_code = company.code.upper() if hasattr(company, 'code') else 'CUST'
        
        # Get last customer number for this company in current month
        from apps.customers.models import Customer
        
        last_customer = Customer.objects.filter(
            customer_code__startswith=f"{company_code}-{year}{month}"
        ).aggregate(Max('customer_code'))
        
        if last_customer['customer_code__max']:
            try:
                last_number = int(last_customer['customer_code__max'].split('-')[-1])
                next_number = last_number + 1
            except (ValueError, IndexError):
                next_number = 1
        else:
            next_number = 1
        
        return f"{company_code}-{year}{month}-{next_number:04d}"
    
    except Exception as e:
        # Fallback to generic format
        timestamp = timezone.now().strftime('%Y%m%d')
        random_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        return f"CUST-{timestamp}-{random_str}"


def generate_service_reference(customer, service_type: str) -> str:
    """
    Generate service reference number.
    Format: {SVC_TYPE}-{CUSTOMER_CODE}-{TIMESTAMP}-{RANDOM}
    
    Args:
        customer: Customer instance
        service_type: Type of service
    
    Returns:
        str: Unique service reference
    """
    prefix = service_type[:3].upper() if len(service_type) >= 3 else 'SVC'
    timestamp = timezone.now().strftime('%Y%m%d%H%M')
    random_str = ''.join(random.choices(string.digits, k=4))
    
    return f"{prefix}-{customer.customer_code}-{timestamp}-{random_str}"


def validate_customer_data(data: Dict[str, Any]) -> Dict[str, str]:
    """
    Validate customer data before saving.
    
    Args:
        data: Customer data dictionary
    
    Returns:
        Dict[str, str]: Dictionary of validation errors
    """
    errors = {}
    
    # Check required fields
    required_fields = ['first_name', 'last_name', 'email', 'phone_number', 'id_number']
    for field in required_fields:
        if field not in data or not data[field]:
            field_name = field.replace('_', ' ').title()
            errors[field] = f"{field_name} is required"
    
    # Validate email format
    if 'email' in data and data['email'] and not is_valid_email(data['email']):
        errors['email'] = "Invalid email format"
    
    # Validate phone number (Kenyan format)
    if 'phone_number' in data and data['phone_number']:
        try:
            validate_phone_number(data['phone_number'])
        except ValidationError as e:
            errors['phone_number'] = str(e)
    
    # Validate date of birth if provided
    if 'date_of_birth' in data and data['date_of_birth']:
        try:
            dob = datetime.strptime(data['date_of_birth'], '%Y-%m-%d').date()
            age = calculate_age(dob)
            if age and age < 18:
                errors['date_of_birth'] = "Customer must be at least 18 years old"
        except ValueError:
            errors['date_of_birth'] = "Invalid date format. Use YYYY-MM-DD"
    
    # Validate ID number based on type
    if 'id_type' in data and 'id_number' in data and data['id_type'] and data['id_number']:
        id_type = data['id_type']
        id_number = data['id_number']
        
        if id_type == 'NATIONAL_ID':
            if not re.match(r'^\d{7,9}$', id_number):
                errors['id_number'] = "Invalid National ID format. Must be 7-9 digits"
        elif id_type == 'PASSPORT':
            if not re.match(r'^[A-Z]\d{7,8}$', id_number):
                errors['id_number'] = "Invalid Passport format"
    
    return errors


# ============================================================================
# BILLING & FINANCIAL FUNCTIONS
# ============================================================================

def generate_invoice_number(prefix: str = 'INV', length: int = 6) -> str:
    """
    Generate a unique invoice number.
    Format: PREFIX-YYYYMMDD-XXXXXX
    
    Args:
        prefix: Invoice prefix (default: 'INV')
        length: Random part length (default: 6)
    
    Returns:
        str: Unique invoice number
    """
    timestamp = timezone.now().strftime('%Y%m%d')
    random_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))
    return f"{prefix}-{timestamp}-{random_str}"


def calculate_prorated_amount(amount: Decimal, start_date, end_date, billing_date=None) -> Decimal:
    """
    Calculate prorated amount for partial billing periods.
    
    Args:
        amount: Full amount
        start_date: Service start date
        end_date: Service end date
        billing_date: Date to calculate from (defaults to today)
    
    Returns:
        Decimal: Prorated amount
    """
    if not billing_date:
        billing_date = timezone.now().date()
    
    # Ensure dates are datetime.date objects
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    if isinstance(end_date, datetime):
        end_date = end_date.date()
    if isinstance(billing_date, datetime):
        billing_date = billing_date.date()
    
    # Calculate total days in billing period
    total_days = (end_date - start_date).days + 1
    
    if total_days <= 0:
        return Decimal('0.00')
    
    # Calculate days used
    if billing_date < start_date:
        days_used = 0
    elif billing_date > end_date:
        days_used = total_days
    else:
        days_used = (billing_date - start_date).days + 1
    
    # Calculate prorated amount
    daily_rate = Decimal(amount) / Decimal(total_days)
    prorated_amount = daily_rate * Decimal(days_used)
    
    return prorated_amount.quantize(Decimal('0.01'))


def calculate_discount(amount: Decimal, discount_percent: Decimal) -> Decimal:
    """
    Calculate discount amount.
    
    Args:
        amount: Original amount
        discount_percent: Discount percentage
    
    Returns:
        Decimal: Discount amount
    """
    discount = (amount * discount_percent) / Decimal(100)
    return discount.quantize(Decimal('0.01'))


def calculate_tax(amount: Decimal, tax_rate: Decimal) -> Decimal:
    """
    Calculate tax amount.
    
    Args:
        amount: Taxable amount
        tax_rate: Tax rate percentage
    
    Returns:
        Decimal: Tax amount
    """
    tax = (amount * tax_rate) / Decimal(100)
    return tax.quantize(Decimal('0.01'))


def format_currency(amount, currency: str = 'KES') -> str:
    """
    Format amount with currency symbol.
    
    Args:
        amount: Amount to format
        currency: Currency code (KES, USD, EUR, etc.)
    
    Returns:
        str: Formatted currency string
    """
    try:
        amount = Decimal(amount)
        if currency == 'KES':
            return f"KES {amount:,.2f}"
        elif currency == 'USD':
            return f"${amount:,.2f}"
        elif currency == 'EUR':
            return f"€{amount:,.2f}"
        else:
            return f"{currency} {amount:,.2f}"
    except (ValueError, TypeError):
        return f"{currency} 0.00"


def format_kes_amount(amount) -> str:
    """
    Format amount to Kenyan Shillings.
    
    Args:
        amount: Amount to format
    
    Returns:
        str: Formatted KES amount
    """
    return format_currency(amount, 'KES')


# ============================================================================
# CONTACT & VALIDATION FUNCTIONS
# ============================================================================

def format_phone_number(phone_number: str) -> Optional[str]:
    """
    Format phone number to Kenyan format (+254...).
    
    Args:
        phone_number: Raw phone number
    
    Returns:
        Optional[str]: Formatted phone number or None
    """
    if not phone_number:
        return None
    
    # Remove all non-digit characters
    digits = re.sub(r'\D', '', phone_number)
    
    if len(digits) < 9:
        return None
    
    if digits.startswith('0'):
        # Convert 07... to +2547...
        return f"+254{digits[1:]}"
    elif digits.startswith('254'):
        return f"+{digits}"
    elif digits.startswith('7') and len(digits) == 9:
        return f"+254{digits}"
    elif len(digits) == 12 and digits.startswith('254'):
        return f"+{digits}"
    else:
        # Return as is with + prefix if it has country code
        if digits.startswith('1') or digits.startswith('2'):
            return f"+{digits}"
        else:
            # Assume it's a Kenyan number without country code
            if len(digits) == 9:
                return f"+254{digits}"
    
    return None


def validate_phone_number(phone_number: str) -> str:
    """
    Validate Kenyan phone number.
    
    Args:
        phone_number: Phone number to validate
    
    Returns:
        str: Formatted phone number
    
    Raises:
        ValidationError: If phone number is invalid
    """
    formatted = format_phone_number(phone_number)
    
    if not formatted:
        raise ValidationError("Phone number is required")
    
    # Validate Kenyan phone number format
    pattern = r'^\+254(7\d{8}|1\d{8})$'
    if not re.match(pattern, formatted):
        raise ValidationError("Invalid Kenyan phone number format. Must be +2547XXXXXXXX or +2541XXXXXXXX")
    
    return formatted


def is_valid_email(email: str) -> bool:
    """
    Validate email format.
    
    Args:
        email: Email address to validate
    
    Returns:
        bool: True if email is valid
    """
    if not email:
        return False
    
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None


# ============================================================================
# GENERATORS & CODE FUNCTIONS
# ============================================================================

def generate_service_id(service_type: str, length: int = 6) -> str:
    """
    Generate a unique service ID based on service type.
    
    Args:
        service_type: Type of service
        length: Random part length
    
    Returns:
        str: Service ID
    """
    service_prefix = {
        'internet': 'INT',
        'voip': 'VOIP',
        'iptv': 'IPTV',
        'wifi': 'WIFI',
        'dedicated': 'DED',
        'vpn': 'VPN',
        'colocation': 'COL',
    }
    prefix = service_prefix.get(service_type.lower(), 'SVC')
    timestamp = timezone.now().strftime('%y%m')
    random_str = ''.join(random.choices(string.digits, k=length))
    return f"{prefix}{timestamp}{random_str}"


def generate_random_password(length: int = 12) -> str:
    """
    Generate a random password with letters, digits, and special characters.
    
    Args:
        length: Password length
    
    Returns:
        str: Random password
    """
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    
    # Ensure at least one of each type
    password_chars = [
        random.choice(string.ascii_lowercase),
        random.choice(string.ascii_uppercase),
        random.choice(string.digits),
        random.choice("!@#$%^&*")
    ]
    
    # Fill remaining characters
    password_chars += [random.choice(chars) for _ in range(length - 4)]
    
    # Shuffle the characters
    random.shuffle(password_chars)
    
    return ''.join(password_chars)


def generate_api_key() -> str:
    """
    Generate a secure API key.
    
    Returns:
        str: API key
    """
    return str(uuid.uuid4()).replace('-', '')


def generate_verification_code(length: int = 6) -> str:
    """
    Generate a numeric verification code.
    
    Args:
        length: Code length
    
    Returns:
        str: Verification code
    """
    return ''.join(random.choices(string.digits, k=length))


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def calculate_age(date_of_birth) -> Optional[int]:
    """
    Calculate age from date of birth.
    
    Args:
        date_of_birth: Date of birth
    
    Returns:
        Optional[int]: Age in years or None
    """
    if not date_of_birth:
        return None
    
    if isinstance(date_of_birth, str):
        try:
            date_of_birth = datetime.strptime(date_of_birth, '%Y-%m-%d').date()
        except ValueError:
            return None
    
    today = timezone.now().date()
    age = today.year - date_of_birth.year
    
    # Adjust if birthday hasn't occurred this year
    if (today.month, today.day) < (date_of_birth.month, date_of_birth.day):
        age -= 1
    
    return age


def slugify_text(text: str) -> str:
    """
    Convert text to URL-friendly slug.
    
    Args:
        text: Text to slugify
    
    Returns:
        str: Slugified text
    """
    if not text:
        return ''
    
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    text = re.sub(r'[^\w\s-]', '', text.lower())
    text = re.sub(r'[-\s]+', '-', text).strip('-')
    return text


def parse_duration(duration_str: str) -> timedelta:
    """
    Parse duration string (e.g., '30d', '2w', '1m') to timedelta.
    
    Args:
        duration_str: Duration string
    
    Returns:
        timedelta: Duration
    
    Raises:
        ValueError: If duration format is invalid
    """
    match = re.match(r'^(\d+)([dwm])$', duration_str.lower())
    if not match:
        raise ValueError(f"Invalid duration format: {duration_str}")
    
    value = int(match.group(1))
    unit = match.group(2)
    
    if unit == 'd':
        return timedelta(days=value)
    elif unit == 'w':
        return timedelta(weeks=value)
    elif unit == 'm':
        return timedelta(days=value * 30)  # Approximate month
    else:
        raise ValueError(f"Unknown duration unit: {unit}")


def format_duration(duration: timedelta) -> str:
    """
    Format timedelta to human-readable string.
    
    Args:
        duration: Time duration
    
    Returns:
        str: Human-readable duration
    """
    if not isinstance(duration, timedelta):
        return ""
    
    total_seconds = int(duration.total_seconds())
    
    if total_seconds < 60:
        return f"{total_seconds} seconds"
    elif total_seconds < 3600:
        minutes = total_seconds // 60
        return f"{minutes} minutes"
    elif total_seconds < 86400:
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        return f"{hours}h {minutes}m"
    else:
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        return f"{days}d {hours}h"


def mask_sensitive_data(data, mask_char: str = '*') -> Any:
    """
    Mask sensitive data for logging.
    
    Args:
        data: Data to mask
        mask_char: Character to use for masking
    
    Returns:
        Masked data
    """
    if isinstance(data, dict):
        masked = {}
        sensitive_keys = ['password', 'token', 'secret', 'key', 'authorization', 'pin']
        for key, value in data.items():
            if any(sensitive in key.lower() for sensitive in sensitive_keys):
                masked[key] = mask_char * 8
            elif isinstance(value, dict):
                masked[key] = mask_sensitive_data(value, mask_char)
            elif isinstance(value, list):
                masked[key] = [
                    mask_sensitive_data(item, mask_char) if isinstance(item, dict) else item 
                    for item in value
                ]
            else:
                masked[key] = value
        return masked
    
    elif isinstance(data, list):
        return [mask_sensitive_data(item, mask_char) if isinstance(item, dict) else item 
                for item in data]
    
    return data


def chunk_list(lst: List, chunk_size: int):
    """
    Split list into chunks of specified size.
    
    Args:
        lst: List to chunk
        chunk_size: Size of each chunk
    
    Yields:
        List chunks
    """
    for i in range(0, len(lst), chunk_size):
        yield lst[i:i + chunk_size]


def get_client_ip(request) -> Optional[str]:
    """
    Get client IP address from request.
    
    Args:
        request: Django request object
    
    Returns:
        Optional[str]: Client IP address
    """
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0].strip()
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


# ============================================================================
# QR CODE & NOTIFICATION FUNCTIONS
# ============================================================================

def generate_qr_code(data: str, size: int = 200) -> Optional[str]:
    """
    Generate QR code data URL.
    
    Args:
        data: Data to encode in QR code
        size: QR code size in pixels
    
    Returns:
        Optional[str]: Data URL for QR code or None
    """
    try:
        import qrcode
        
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(data)
        qr.make(fit=True)
        
        # Create QR code image
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Resize if needed
        if size != 200:
            img = img.resize((size, size))
        
        # Convert to bytes
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        
        # Convert to base64 data URL
        img_str = base64.b64encode(buffer.getvalue()).decode()
        return f"data:image/png;base64,{img_str}"
    
    except ImportError:
        logger.warning("qrcode library not installed. Install with: pip install qrcode[pil]")
        return None
    except Exception as e:
        logger.error(f"QR code generation failed: {str(e)}")
        return None


def generate_qr_code_data(data: str, size: int = 200) -> Optional[str]:
    """
    Alias for generate_qr_code (for backward compatibility).
    """
    return generate_qr_code(data, size)


def send_welcome_email(customer) -> bool:
    """
    Send welcome email to new customer.
    
    Args:
        customer: Customer instance
    
    Returns:
        bool: True if email sent successfully
    """
    try:
        from django.core.mail import EmailMultiAlternatives
        from django.template.loader import render_to_string
        
        subject = f"Welcome to {customer.company.name}!"
        
        # Context for email template
        context = {
            'customer': customer,
            'company': customer.company,
            'activation_date': customer.activation_date,
        }
        
        # Render HTML and text versions
        html_content = render_to_string('emails/welcome.html', context)
        text_content = render_to_string('emails/welcome.txt', context)
        
        # Create email
        email = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=f"{customer.company.name} <noreply@example.com>",
            to=[customer.user.email],
        )
        email.attach_alternative(html_content, "text/html")
        
        # Send email
        email.send()
        
        logger.info(f"Welcome email sent to {customer.user.email}")
        return True
    
    except Exception as e:
        logger.error(f"Failed to send welcome email: {str(e)}")
        return False


def send_service_activation_notification(service) -> bool:
    """
    Send service activation notification.
    
    Args:
        service: ServiceConnection instance
    
    Returns:
        bool: True if notification sent successfully
    """
    try:
        from django.core.mail import EmailMultiAlternatives
        from django.template.loader import render_to_string
        
        customer = service.customer
        
        subject = f"Service Activated - {service.get_service_type_display()}"
        
        # Context for email template
        context = {
            'customer': customer,
            'service': service,
            'company': customer.company,
            'activation_date': service.activation_date or timezone.now(),
        }
        
        # Render HTML and text versions
        html_content = render_to_string('emails/service_activation.html', context)
        text_content = render_to_string('emails/service_activation.txt', context)
        
        # Create email
        email = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=f"{customer.company.name} <noreply@example.com>",
            to=[customer.user.email],
        )
        email.attach_alternative(html_content, "text/html")
        
        # Send email
        email.send()
        
        logger.info(f"Service activation email sent for {service}")
        return True
    
    except Exception as e:
        logger.error(f"Failed to send service activation email: {str(e)}")
        return False


def send_sms_notification(phone_number: str, message: str) -> bool:
    """
    Send SMS notification.
    
    Args:
        phone_number: Recipient phone number
        message: SMS message
    
    Returns:
        bool: True if SMS sent successfully
    """
    try:
        # This is a skeleton - integrate with your SMS provider
        # Example: Africastalking, Twilio, etc.
        
        # Validate phone number
        formatted_phone = validate_phone_number(phone_number)
        
        # TODO: Implement actual SMS sending logic
        # For now, just log
        logger.info(f"SMS to {formatted_phone}: {message}")
        
        return True
    
    except Exception as e:
        logger.error(f"Failed to send SMS: {str(e)}")
        return False


# ============================================================================
# DATABASE & CACHING FUNCTIONS
# ============================================================================

def get_or_create_company(name: str = "Default ISP", code: str = "DEFAULT"):
    """
    Get or create a company instance.
    
    Args:
        name: Company name
        code: Company code
    
    Returns:
        Company instance
    """
    from apps.core.models import Company
    
    company, created = Company.objects.get_or_create(
        code=code,
        defaults={'name': name}
    )
    
    return company


def generate_report_filename(report_type: str, extension: str = 'pdf') -> str:
    """
    Generate filename for reports.
    
    Args:
        report_type: Type of report
        extension: File extension
    
    Returns:
        str: Generated filename
    """
    timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
    return f"{report_type.lower()}_{timestamp}.{extension}"


def calculate_bandwidth_usage(download_mb: float, upload_mb: float) -> Dict[str, float]:
    """
    Calculate bandwidth usage statistics.
    
    Args:
        download_mb: Download in MB
        upload_mb: Upload in MB
    
    Returns:
        Dict: Bandwidth usage statistics
    """
    total_mb = download_mb + upload_mb
    total_gb = total_mb / 1024
    
    return {
        'download_mb': download_mb,
        'upload_mb': upload_mb,
        'total_mb': total_mb,
        'total_gb': total_gb,
        'download_percentage': (download_mb / total_mb * 100) if total_mb > 0 else 0,
        'upload_percentage': (upload_mb / total_mb * 100) if total_mb > 0 else 0,
    }


# ============================================================================
# LEGACY SUPPORT FUNCTIONS
# ============================================================================

def generate_customer_code_legacy(prefix: str = 'CUST', length: int = 8) -> str:
    """
    Legacy function for backward compatibility.
    Use generate_customer_code(company) instead.
    """
    timestamp = timezone.now().strftime('%Y%m')
    random_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))
    return f"{prefix}-{timestamp}-{random_str}"


def format_phone_number_legacy(phone_number: str) -> Optional[str]:
    """
    Legacy function - use validate_phone_number instead.
    """
    return format_phone_number(phone_number)


# Export all functions
__all__ = [
    # Customer Management
    'generate_customer_code',
    'generate_service_reference',
    'validate_customer_data',
    
    # Billing & Financial
    'generate_invoice_number',
    'calculate_prorated_amount',
    'calculate_discount',
    'calculate_tax',
    'format_currency',
    'format_kes_amount',
    
    # Contact & Validation
    'format_phone_number',
    'validate_phone_number',
    'is_valid_email',
    
    # Generators & Code
    'generate_service_id',
    'generate_random_password',
    'generate_api_key',
    'generate_verification_code',
    
    # Utility Functions
    'calculate_age',
    'slugify_text',
    'parse_duration',
    'format_duration',
    'mask_sensitive_data',
    'chunk_list',
    'get_client_ip',
    
    # QR Code & Notifications
    'generate_qr_code',
    'generate_qr_code_data',
    'send_welcome_email',
    'send_service_activation_notification',
    'send_sms_notification',
    
    # Database & Caching
    'get_or_create_company',
    'generate_report_filename',
    'calculate_bandwidth_usage',
    
    # Legacy Support
    'generate_customer_code_legacy',
    'format_phone_number_legacy',
]