# utils/phone.py
from utils.constants import get_country_phone_config


def normalize_phone_number(raw_phone: str, country_code: str = 'KE') -> str:
    """
    Normalize any phone number to full international format with no '+',
    e.g. '0712345678' + 'KE' -> '254712345678'.

    Accepts local format (0XXXXXXXXX), international with '+', or already
    normalized (254XXXXXXXXX). Returns the digits-only normalized form,
    or the raw digit string unchanged if it doesn't match any known pattern
    (caller should validate before trusting the result for payment APIs).
    """
    cfg = get_country_phone_config(country_code)
    dial_code = cfg['dial_code']
    national_length = cfg['national_length']
    full_length = cfg['full_length']

    digits = ''.join(ch for ch in (raw_phone or '') if ch.isdigit())

    if not digits:
        return ''

    # Already in full international format, e.g. 254712345678
    if digits.startswith(dial_code) and len(digits) == full_length:
        return digits

    # Local format with leading 0, e.g. 0712345678
    if digits.startswith('0') and len(digits) == national_length + 1:
        return dial_code + digits[1:]

    # Bare national number with no leading 0, e.g. 712345678
    if len(digits) == national_length:
        return dial_code + digits

    # Doesn't match expected shape for this country — return as-is,
    # let the caller's own validation reject it explicitly.
    return digits


def is_valid_phone_number(raw_phone: str, country_code: str = 'KE') -> bool:
    """True if the number normalizes to the expected full length for its country."""
    cfg = get_country_phone_config(country_code)
    normalized = normalize_phone_number(raw_phone, country_code)
    return normalized.startswith(cfg['dial_code']) and len(normalized) == cfg['full_length']


def format_phone_local(raw_phone: str, country_code: str = 'KE') -> str:
    """Convert to local display format, e.g. 254712345678 -> 0712345678."""
    cfg = get_country_phone_config(country_code)
    normalized = normalize_phone_number(raw_phone, country_code)
    if normalized.startswith(cfg['dial_code']):
        return '0' + normalized[len(cfg['dial_code']):]
    return normalized