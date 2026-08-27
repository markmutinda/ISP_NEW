# apps/billing/constants/bank_paybills.py
"""
Maps a bank display name (as shown in the tenant's payment-method dropdown)
to that bank's OWN Paybill shortcode used as PartyB for STK routing.

⚠️ These numbers have been verified — a wrong number here sends
real customer money to the wrong bank. Do not change without verification.
"""

BANK_PAYBILL_MAP = {
    "I&M Bank": "542542",
    "Kenya Commercial Bank": "522522",
    "Equity Bank": "247247",
    "Co-operative Bank": "400200",
    "Absa Bank": "303030",
    "Standard Chartered Bank": "329329",
    "Stanbic Bank": "600100",
    "Diamond Trust Bank": "516600",
    "Family Bank": "222111",
    "National Bank of Kenya": "547700",
    "NCBA Bank": "880100",
    "Faulu Bank": "328585",
    "Kingdom Bank": "529901",
    "Sidian Bank": "111999",
    "SBM Bank": "552800",
    "Bank of Africa": "972900",
}
