"""
Custom validators for ISP Management System
"""
import re
from django.core.validators import ValidationError
from django.utils.translation import gettext_lazy as _
from datetime import datetime
import phonenumbers
from phonenumbers import PhoneNumberFormat


def validate_phone_number(value):
    """
    Validate phone number using phonenumbers library.
    """
    if not value:
        raise ValidationError(_('Phone number is required'))
    
    try:
        # Parse phone number
        phone_number = phonenumbers.parse(value, "KE")
        
        # Check if number is valid
        if not phonenumbers.is_valid_number(phone_number):
            raise ValidationError(_('Invalid phone number'))
        
        # Format number in international format
        formatted = phonenumbers.format_number(phone_number, PhoneNumberFormat.E164)
        return formatted
    except phonenumbers.NumberParseException:
        raise ValidationError(_('Invalid phone number format'))


def validate_id_number(value):
    """
    Validate Kenyan National ID number.
    """
    if not value:
        return value
    
    # Remove any spaces or dashes
    value = value.strip().replace(' ', '').replace('-', '')
    
    # Check length (old IDs: 8 digits, new IDs: 9 digits)
    if not (len(value) == 8 or len(value) == 9):
        raise ValidationError(_('ID number must be 8 or 9 digits'))
    
    # Check if all characters are digits
    if not value.isdigit():
        raise ValidationError(_('ID number must contain only digits'))
    
    # Basic validation for old ID format (8 digits)
    if len(value) == 8:
        # Check if it's a valid date (first 6 digits: YYMMDD)
        try:
            year = int(value[:2])
            month = int(value[2:4])
            day = int(value[4:6])
            
            # Convert 2-digit year to 4-digit
            if year > 20:  # Assuming years 00-20 are 2000-2020
                year += 1900
            else:
                year += 2000
            
            # Validate date
            datetime(year, month, day)
        except ValueError:
            raise ValidationError(_('Invalid ID number format'))
    
    return value


def validate_passport_number(value):
    """
    Validate passport number.
    """
    if not value:
        return value
    
    value = value.strip().upper()
    
    # Basic passport number validation
    # Format: Letter followed by 6-8 digits
    pattern = r'^[A-Z]{1}\d{6,8}$'
    if not re.match(pattern, value):
        raise ValidationError(_('Invalid passport number format'))
    
    return value


def validate_password_strength(value):
    """
    Validate password strength.
    """
    if len(value) < 8:
        raise ValidationError(_('Password must be at least 8 characters long'))
    
    # Check for at least one digit
    if not re.search(r'\d', value):
        raise ValidationError(_('Password must contain at least one digit'))
    
    # Check for at least one uppercase letter
    if not re.search(r'[A-Z]', value):
        raise ValidationError(_('Password must contain at least one uppercase letter'))
    
    # Check for at least one lowercase letter
    if not re.search(r'[a-z]', value):
        raise ValidationError(_('Password must contain at least one lowercase letter'))
    
    # Check for at least one special character
    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', value):
        raise ValidationError(_('Password must contain at least one special character'))
    
    return value


def validate_email_domain(value):
    """
    Validate email domain against common free email providers.
    """
    if not value:
        raise ValidationError(_('Email is required'))
    
    # List of free email domains to restrict
    free_domains = [
        'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com',
        'aol.com', 'icloud.com', 'protonmail.com', 'zoho.com',
        'yandex.com', 'mail.com', 'gmx.com'
    ]
    
    # Extract domain from email
    domain = value.split('@')[-1].lower()
    
    if domain in free_domains:
        raise ValidationError(_('Business email address is required'))
    
    return value


def validate_mpesa_code(value):
    """
    Validate M-Pesa transaction code.
    """
    if not value:
        raise ValidationError(_('M-Pesa code is required'))
    
    value = value.strip().upper()
    
    # M-Pesa transaction code format: Letters and numbers, 10 characters
    pattern = r'^[A-Z0-9]{10}$'
    if not re.match(pattern, value):
        raise ValidationError(_('Invalid M-Pesa transaction code format'))
    
    return value


def validate_date_of_birth(value):
    """
    Validate date of birth (must be at least 18 years old).
    """
    if not value:
        return value
    
    from datetime import date
    today = date.today()
    age = today.year - value.year - ((today.month, today.day) < (value.month, value.day))
    
    if age < 18:
        raise ValidationError(_('Must be at least 18 years old'))
    
    if age > 120:
        raise ValidationError(_('Invalid date of birth'))
    
    return value


def validate_positive_number(value):
    """
    Validate that number is positive.
    """
    if value <= 0:
        raise ValidationError(_('Value must be positive'))
    return value


def validate_percentage(value):
    """
    Validate percentage value (0-100).
    """
    if value < 0 or value > 100:
        raise ValidationError(_('Percentage must be between 0 and 100'))
    return value


def validate_mac_address(value):
    """
    Validate MAC address format.
    """
    if not value:
        return value
    
    value = value.strip().upper()
    
    # Common MAC address formats
    patterns = [
        r'^([0-9A-F]{2}[:-]){5}([0-9A-F]{2})$',  # 00:1A:2B:3C:4D:5E
        r'^([0-9A-F]{4}[.]){2}([0-9A-F]{4})$',   # 001A.2B3C.4D5E
        r'^([0-9A-F]{12})$',                     # 001A2B3C4D5E
    ]
    
    for pattern in patterns:
        if re.match(pattern, value):
            return value
    
    raise ValidationError(_('Invalid MAC address format'))


def validate_ip_address(value):
    """
    Validate IP address (IPv4 or IPv6).
    """
    if not value:
        return value
    
    # IPv4 pattern
    ipv4_pattern = r'^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$'
    
    # Basic IPv6 pattern (simplified)
    ipv6_pattern = r'^([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}$'
    
    if re.match(ipv4_pattern, value) or re.match(ipv6_pattern, value):
        return value
    
    raise ValidationError(_('Invalid IP address format'))


def validate_postal_code(value):
    """
    Validate Kenyan postal code.
    """
    if not value:
        return value
    
    value = value.strip()
    
    # Kenyan postal codes are 5 digits
    pattern = r'^\d{5}$'
    if not re.match(pattern, value):
        raise ValidationError(_('Postal code must be 5 digits'))
    
    return value


def validate_website_url(value):
    """
    Validate website URL.
    """
    if not value:
        return value
    
    # Basic URL pattern
    pattern = r'^https?://(?:www\.)?[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/\S*)?$'
    
    if not re.match(pattern, value):
        raise ValidationError(_('Invalid website URL'))
    
    return value


def validate_currency_amount(value):
    """
    Validate currency amount (positive, up to 2 decimal places).
    """
    from decimal import Decimal, InvalidOperation
    
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError):
        raise ValidationError(_('Invalid amount'))
    
    if amount < 0:
        raise ValidationError(_('Amount cannot be negative'))
    
    # Check decimal places
    if abs(amount.as_tuple().exponent) > 2:
        raise ValidationError(_('Amount can have maximum 2 decimal places'))
    
    return amount


def validate_kenyan_county(value):
    """
    Validate Kenyan county name.
    """
    from .kenyan_utils import get_kenyan_counties
    
    counties = get_kenyan_counties()
    
    if value not in counties:
        raise ValidationError(_('Invalid Kenyan county'))
    
    return value