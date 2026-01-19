"""
Kenyan-specific utilities and validators for ISP Management System
"""
import re
from datetime import datetime
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


def validate_kenyan_phone(phone_number):
    """
    Validate Kenyan phone number format.
    
    Args:
        phone_number (str): Phone number to validate
    
    Returns:
        str: Formatted phone number
    
    Raises:
        ValidationError: If phone number is invalid
    """
    if not phone_number:
        raise ValidationError("Phone number is required")
    
    # Remove all non-digit characters
    digits = re.sub(r'\D', '', phone_number)
    
    # Check length
    if len(digits) < 9:
        raise ValidationError("Phone number must have at least 9 digits")
    
    # Format based on Kenyan phone number patterns
    if digits.startswith('0'):
        # 07... to +2547...
        if len(digits) == 10:  # 07XXXXXXXX
            formatted = f"+254{digits[1:]}"
        else:
            raise ValidationError("Invalid phone number format")
    
    elif digits.startswith('254'):
        # Already has country code
        if len(digits) == 12:  # 2547XXXXXXXX
            formatted = f"+{digits}"
        else:
            raise ValidationError("Invalid phone number format")
    
    elif digits.startswith('7'):
        # Missing country code but starts with 7
        if len(digits) == 9:  # 7XXXXXXXX
            formatted = f"+254{digits}"
        else:
            raise ValidationError("Invalid phone number format")
    
    else:
        # Try to validate as is
        if digits.startswith('1') and len(digits) == 12:
            formatted = f"+{digits}"
        else:
            raise ValidationError("Invalid phone number format. Use Kenyan format: +2547XXXXXXXX or 07XXXXXXXX")
    
    # Final validation with Kenyan phone regex
    phone_regex = r'^\+254(7\d{8}|1\d{8})$'
    if not re.match(phone_regex, formatted):
        raise ValidationError("Invalid Kenyan phone number. Must be in format: +2547XXXXXXXX or +2541XXXXXXXX")
    
    return formatted


def validate_id_number(id_number, id_type='NATIONAL_ID'):
    """
    Validate Kenyan ID number based on type.
    
    Args:
        id_number (str): ID number to validate
        id_type (str): Type of ID (NATIONAL_ID, PASSPORT, etc.)
    
    Returns:
        str: Validated ID number
    
    Raises:
        ValidationError: If ID number is invalid
    """
    if not id_number:
        raise ValidationError("ID number is required")
    
    id_number = str(id_number).strip().upper()
    
    if id_type == 'NATIONAL_ID':
        # Kenyan National ID: 8 digits, sometimes 7 or 9
        if not re.match(r'^\d{7,9}$', id_number):
            raise ValidationError("Invalid National ID. Must be 7-9 digits")
        
        # Additional validation for Kenyan ID format
        if len(id_number) == 8:
            # Validate check digit for 8-digit IDs
            pass  # You can implement Luhn algorithm or other validation here
    
    elif id_type == 'PASSPORT':
        # Kenyan passport: Letter followed by 7 or 8 digits
        if not re.match(r'^[A-Z]\d{7,8}$', id_number):
            raise ValidationError("Invalid Passport number. Format: Letter followed by 7-8 digits")
    
    elif id_type == 'ALIEN_ID':
        # Alien ID: Starts with letter, contains digits
        if not re.match(r'^[A-Z]\d+$', id_number):
            raise ValidationError("Invalid Alien ID")
    
    elif id_type == 'DRIVER_LICENSE':
        # Driver's license: Various formats
        if len(id_number) < 5:
            raise ValidationError("Invalid Driver's License number")
    
    elif id_type == 'BIRTH_CERTIFICATE':
        # Birth certificate number validation
        if not re.match(r'^\d+[A-Z]*$', id_number):
            raise ValidationError("Invalid Birth Certificate number")
    
    return id_number


def validate_kenyan_passport(passport_number):
    """
    Validate Kenyan passport number.
    
    Args:
        passport_number (str): Passport number to validate
    
    Returns:
        str: Validated passport number
    
    Raises:
        ValidationError: If passport number is invalid
    """
    if not passport_number:
        raise ValidationError("Passport number is required")
    
    passport_number = str(passport_number).strip().upper()
    
    # Kenyan passport format: Letter followed by 7 or 8 digits
    if not re.match(r'^[A-Z]\d{7,8}$', passport_number):
        raise ValidationError("Invalid Kenyan passport number. Format: Letter followed by 7-8 digits")
    
    return passport_number


def validate_kra_pin(kra_pin):
    """
    Validate KRA PIN number.
    
    Args:
        kra_pin (str): KRA PIN to validate
    
    Returns:
        str: Validated KRA PIN
    
    Raises:
        ValidationError: If KRA PIN is invalid
    """
    if not kra_pin:
        raise ValidationError("KRA PIN is required")
    
    kra_pin = str(kra_pin).strip().upper()
    
    # KRA PIN format: Letter followed by 9 digits followed by a letter
    if not re.match(r'^[A-Z]\d{9}[A-Z]$', kra_pin):
        raise ValidationError("Invalid KRA PIN. Format: Letter + 9 digits + Letter")
    
    return kra_pin


def get_kenyan_county_name(county_code):
    """
    Get county name from county code.
    
    Args:
        county_code (str): County code
    
    Returns:
        str: County name
    """
    counties = {
        'NAIROBI': 'Nairobi',
        'MOMBASA': 'Mombasa',
        'KISUMU': 'Kisumu',
        'NAKURU': 'Nakuru',
        'ELDORET': 'Eldoret',
        'THIKA': 'Thika',
        'MALINDI': 'Malindi',
        'KITALE': 'Kitale',
        'KERICHO': 'Kericho',
        'KAKAMEGA': 'Kakamega',
        'KISII': 'Kisii',
        'NYERI': 'Nyeri',
        'MERU': 'Meru',
        'MURANGA': 'Muranga',
        'KIRINYAGA': 'Kirinyaga',
        'NYANDARUA': 'Nyandarua',
        'LAIKIPIA': 'Laikipia',
        'NAROK': 'Narok',
        'KAJIADO': 'Kajiado',
        'MACHAKOS': 'Machakos',
        'MAKUENI': 'Makueni',
        'KIAMBU': 'Kiambu',
    }
    
    return counties.get(county_code, county_code)


def format_kenyan_currency(amount):
    """
    Format amount as Kenyan Shillings.
    
    Args:
        amount (float|Decimal|int): Amount to format
    
    Returns:
        str: Formatted currency string
    """
    try:
        amount = float(amount)
        return f"KES {amount:,.2f}"
    except (ValueError, TypeError):
        return "KES 0.00"


def is_valid_kenyan_date(date_str):
    """
    Check if date string is valid Kenyan date format.
    
    Args:
        date_str (str): Date string
    
    Returns:
        bool: True if valid date
    """
    try:
        # Try to parse date in various formats
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%d.%m.%Y'):
            try:
                datetime.strptime(date_str, fmt)
                return True
            except ValueError:
                continue
        return False
    except Exception:
        return False


def validate_kenyan_address(address_data):
    """
    Validate Kenyan address components.
    
    Args:
        address_data (dict): Address data
    
    Returns:
        dict: Validated address data
    
    Raises:
        ValidationError: If address is invalid
    """
    required_fields = ['county', 'sub_county', 'ward', 'street_address']
    
    for field in required_fields:
        if field not in address_data or not address_data[field]:
            raise ValidationError(f"{field.replace('_', ' ').title()} is required")
    
    # Validate county
    counties = ['NAIROBI', 'MOMBASA', 'KISUMU', 'NAKURU', 'ELDORET', 'THIKA', 
                'MALINDI', 'KITALE', 'KERICHO', 'KAKAMEGA', 'KISII', 'NYERI', 
                'MERU', 'MURANGA', 'KIRINYAGA', 'NYANDARUA', 'LAIKIPIA', 
                'NAROK', 'KAJIADO', 'MACHAKOS', 'MAKUENI', 'KIAMBU']
    
    if address_data.get('county') and address_data['county'].upper() not in counties:
        raise ValidationError(f"Invalid county: {address_data['county']}")
    
    return address_data


def generate_kenyan_reference(prefix='REF'):
    """
    Generate a Kenyan-style reference number.
    
    Args:
        prefix (str): Reference prefix
    
    Returns:
        str: Generated reference number
    """
    from datetime import datetime
    import random
    import string
    
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    random_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    
    return f"{prefix}-{timestamp}-{random_str}"


def parse_kenyan_phone_prefix(phone_number):
    """
    Parse Kenyan phone number to get service provider.
    
    Args:
        phone_number (str): Phone number
    
    Returns:
        str: Service provider name
    """
    safaricom_prefixes = ['70', '71', '72', '74', '79', '010', '011']
    airtel_prefixes = ['73', '075', '076', '078']
    telkom_prefixes = ['077']
    
    # Extract the digits after +254
    if phone_number.startswith('+254'):
        prefix = phone_number[4:6]  # First 2 digits after country code
    elif phone_number.startswith('0'):
        prefix = phone_number[1:3]  # First 2 digits after 0
    else:
        return 'Unknown'
    
    if prefix in safaricom_prefixes:
        return 'Safaricom'
    elif prefix in airtel_prefixes:
        return 'Airtel'
    elif prefix in telkom_prefixes:
        return 'Telkom'
    else:
        return 'Other'


def calculate_kenyan_vat(amount, vat_rate=16.0):
    """
    Calculate VAT amount for Kenyan prices.
    
    Args:
        amount (float): Amount before VAT
        vat_rate (float): VAT rate percentage
    
    Returns:
        tuple: (vat_amount, total_amount)
    """
    try:
        amount = float(amount)
        vat_rate = float(vat_rate)
        
        vat_amount = amount * (vat_rate / 100)
        total_amount = amount + vat_amount
        
        return round(vat_amount, 2), round(total_amount, 2)
    except (ValueError, TypeError):
        return 0.0, 0.0
