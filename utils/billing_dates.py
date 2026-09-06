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

    - No anchor_day yet (first CALENDAR_MONTH cycle — e.g. just switched
      from a DAYS/MONTHS plan): always anchor to *today's* payment date.
      A leftover expiration_date from the old billing scheme is not a
      valid calendar anchor and must never be used as the base.
    - Anchor exists + on-time/early payment (now <= current_expiry):
      keep the existing anchor day, extend one month from current_expiry.
    - Anchor exists + late payment (now > current_expiry): reset the
      anchor to today's day-of-month.
    """
    now = now or timezone.now()

    if not anchor_day:
        resolved_anchor = now.day
        new_expiry = calendar_month_expiration(now, anchor_day=resolved_anchor)
        return resolved_anchor, new_expiry

    if current_expiry and current_expiry > now:
        new_expiry = calendar_month_expiration(current_expiry, anchor_day=anchor_day)
        return anchor_day, new_expiry

    resolved_anchor = now.day
    new_expiry = calendar_month_expiration(now, anchor_day=resolved_anchor)
    return resolved_anchor, new_expiry