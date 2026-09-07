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
    """
    One calendar month from start_time, pinned to anchor_day, end-of-day
    in LOCAL tenant time. Doing the day math in raw UTC pushed the visible
    expiry date forward by a day for anyone paying in the evening
    (23:59:59 UTC == 02:59:59 next day in Africa/Nairobi).
    """
    now = start_time or timezone.now()
    local_now = timezone.localtime(now) if timezone.is_aware(now) else now
    expiry = add_calendar_months(local_now, months=1, anchor_day=anchor_day)
    expiry = expiry.replace(hour=23, minute=59, second=59, microsecond=0)
    if timezone.is_naive(expiry):
        expiry = timezone.make_aware(expiry)
    return expiry


def resolve_calendar_renewal(current_expiry, anchor_day=None, now=None):
    """
    (anchor_day, new_expiry) for a CALENDAR_MONTH renewal.

    Priority matters:
      1. Paying against a still-future expiry (early/on-time renewal) —
         extend from current_expiry and KEEP its day-of-month (use the
         stored anchor if we have one, else derive it from current_expiry
         itself). Checked BEFORE the "no anchor" branch, so a customer who
         just had their plan switched to CALENDAR_MONTH (anchor still NULL)
         but pays early against a real future expiry doesn't get dragged
         back to today's date.
      2. No anchor + no valid future expiry — brand-new cycle, anchor to
         today's payment date.
      3. Anchor exists but subscription already lapsed — reset anchor to
         today's payment date.
    """
    now = now or timezone.now()

    if current_expiry and current_expiry > now:
        resolved_anchor = anchor_day or current_expiry.day
        new_expiry = calendar_month_expiration(current_expiry, anchor_day=resolved_anchor)
        return resolved_anchor, new_expiry

    if not anchor_day:
        resolved_anchor = now.day
        new_expiry = calendar_month_expiration(now, anchor_day=resolved_anchor)
        return resolved_anchor, new_expiry

    resolved_anchor = now.day
    new_expiry = calendar_month_expiration(now, anchor_day=resolved_anchor)
    return resolved_anchor, new_expiry