"""
Hotspot plan availability schedule (days + optional per-day time windows).

valid_time_windows format (HotspotPlan.valid_time_windows):
    {"monday": {"start": "18:00", "end": "19:00"}, ...}
A day with no entry = available all day. start > end = overnight window.
"""
import re

from django.utils import timezone

DAY_KEYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
_HHMM = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')


def normalize_time_windows(raw):
    """Validate + normalise input from the API. Raises ValueError on bad data."""
    if raw in (None, ''):
        return {}
    if not isinstance(raw, dict):
        raise ValueError('valid_time_windows must be an object keyed by day name.')
    out = {}
    for day, win in raw.items():
        d = str(day).lower()
        if d not in DAY_KEYS:
            raise ValueError(f'Unknown day "{day}".')
        if not win:
            continue
        if not isinstance(win, dict):
            raise ValueError(f'Invalid window for {d}.')
        start = str(win.get('start', '')).strip()[:5]
        end = str(win.get('end', '')).strip()[:5]
        if not _HHMM.match(start) or not _HHMM.match(end):
            raise ValueError(f'{d.capitalize()}: times must be HH:MM (24h).')
        if start == end:
            raise ValueError(f'{d.capitalize()}: start and end time cannot be the same.')
        out[d] = {'start': start, 'end': end}
    return out


def _mins(hhmm):
    h, m = hhmm.split(':')
    return int(h) * 60 + int(m)


def _window(windows, day):
    w = (windows or {}).get(day)
    if not w:
        return None
    try:
        s, e = _mins(w['start']), _mins(w['end'])
    except (KeyError, ValueError, AttributeError, TypeError):
        return None
    return None if s == e else (s, e)


def schedule_is_active(days, windows, now=None):
    """days: {'monday': bool,...}  windows: normalised dict. `now` is localised here."""
    now = timezone.localtime(now or timezone.now())
    idx = now.weekday()
    minutes = now.hour * 60 + now.minute
    today, yesterday = DAY_KEYS[idx], DAY_KEYS[(idx - 1) % 7]

    if days.get(today, True):
        win = _window(windows, today)
        if win is None:
            return True                      # whole day
        s, e = win
        if (s < e and s <= minutes < e) or (s > e and minutes >= s):
            return True

    # tail of yesterday's overnight window (e.g. Fri 22:00-02:00 -> Sat 01:00)
    if days.get(yesterday, True):
        win = _window(windows, yesterday)
        if win and win[0] > win[1] and minutes < win[1]:
            return True
    return False


def schedule_payload(plan):
    """Compact schedule dict stored in cached captive-portal payloads."""
    return {
        'days': {d: bool(getattr(plan, f'valid_{d}', True)) for d in DAY_KEYS},
        'windows': getattr(plan, 'valid_time_windows', None) or {},
    }


def plan_is_available_now(plan, now=None):
    s = schedule_payload(plan)
    return schedule_is_active(s['days'], s['windows'], now)


def filter_plan_dicts(plans, now=None):
    """Filter serialized plan dicts (those carrying a 'schedule' key)."""
    now = timezone.localtime(now or timezone.now())
    out = []
    for p in plans:
        s = p.get('schedule')
        if not s or schedule_is_active(s.get('days', {}), s.get('windows', {}), now):
            out.append(p)
    return out