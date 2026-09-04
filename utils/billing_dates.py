"""
Calendar-month billing helper.
Anchors expiry to a fixed day-of-month, clamping only when the target
month is too short (e.g. Jan 31 -> Feb 28), never permanently shrinking
the anchor day for future cycles.
"""
import calendar
from datetime import datetime
from django.utils import timezone


def add_calendar_months(dt, months=1, anchor_day=None):
    anchor_day = anchor_day or dt.day
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(anchor_day, last_day)
    return dt.replace(year=year, month=month, day=day)


def calendar_month_expiration(start_time=None, anchor_day=None):
    """One calendar month from start_time, pinned to anchor_day, end-of-day."""
    now = start_time or timezone.now()
    expiry = add_calendar_months(now, months=1, anchor_day=anchor_day)
    expiry = expiry.replace(hour=23, minute=59, second=59, microsecond=0)
    if timezone.is_naive(expiry):
        expiry = timezone.make_aware(expiry)
    return expiry


def resolve_calendar_renewal(current_expiry, anchor_day=None, now=None):
    """
    Decide the (anchor_day, new_expiry) pair for a CALENDAR_MONTH renewal.

    - On-time/early payment (now <= current_expiry): keep the existing
      anchor day, extend one calendar month from current_expiry.
    - Late payment (now > current_expiry, or no prior expiry): the
      anchor resets to *today's* day-of-month — future renewals will
      now track the new payment date.
    """
    now = now or timezone.now()

    if current_expiry and current_expiry > now:
        resolved_anchor = anchor_day or current_expiry.day
        new_expiry = calendar_month_expiration(current_expiry, anchor_day=resolved_anchor)
    else:
        resolved_anchor = now.day
        new_expiry = calendar_month_expiration(now, anchor_day=resolved_anchor)

    return resolved_anchor, new_expiry