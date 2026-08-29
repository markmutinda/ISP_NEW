# apps/billing/utils/payment_errors.py
"""
Translates raw Safaricom/Daraja/Tuma failure strings into plain-language
messages shown to end users on the captive portal / payment status pages.
"""
import re

_PATTERNS = [
    (r"ds timeout|cannot be reached", 
     "We couldn't reach your phone. Make sure it has signal and try again."),
    (r"duplicated msisdn|existing ussd session", 
     "You already have a pending M-Pesa prompt on this number. Complete or cancel it, then try again."),
    (r"no response from user", 
     "You didn't respond to the M-Pesa prompt in time. Please try again."),
    (r"cancel(l)?ed by user|request cancelled", 
     "Payment was cancelled. Please try again to complete your purchase."),
    (r"insufficient (balance|funds)", 
     "Insufficient M-Pesa balance. Please top up and try again."),
    (r"invalid.*pin|wrong pin", 
     "Incorrect M-Pesa PIN entered. Please try again."),
    (r"push request|error occurred while sending", 
     "Could not send the payment prompt. Please try again in a moment."),
    (r"not registered on m-?pesa|invalid phone", 
     "This number doesn't look M-Pesa registered. Check the number and try again."),
    (r"timeout", 
     "The payment request timed out. Please try again."),
]

DEFAULT_MESSAGE = "Payment could not be completed. Please try again."


def humanize_payment_failure(reason: str | None) -> str:
    """Map a raw gateway failure string to a friendly, user-facing message."""
    if not reason:
        return DEFAULT_MESSAGE
    text = reason.lower()
    for pattern, friendly in _PATTERNS:
        if re.search(pattern, text):
            return friendly
    return DEFAULT_MESSAGE