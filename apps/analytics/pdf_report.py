"""Business report PDF built from the existing cached analytics payload."""
from datetime import datetime

from django.utils import timezone
from reportlab.lib.units import cm
from reportlab.platypus import KeepTogether, Spacer

from apps.core.pdf_reports import (
    BRAND, CONTENT_W, EMERALD, GREEN, MUTED, RED, SLATE, VIOLET, bar_chart, brand_header,
    build_pdf, clip, data_table, fmt_date, get_company_profile, kpi_cards, legend, money, section,
)

_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
_TYPE = {'PPPOE': 'PPPoE', 'HOTSPOT': 'Hotspot'}


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _chg(v):
    v = _f(v)
    return f"{'+' if v >= 0 else ''}{v:.1f}%"


def _pct_color(v):
    return GREEN if _f(v) >= 0 else RED


def _short_date(iso):
    try:
        return datetime.strptime(str(iso)[:10], '%Y-%m-%d').strftime('%d %b')
    except Exception:
        return str(iso or '-')


def build_business_report_pdf(request, payload, split, time_range):
    prof = get_company_profile(request)
    cur = prof['currency']
    today = timezone.localdate()
    days = {'7d': 7, '30d': 30, '90d': 90}.get(time_range, 30)
    period = f"Last {days} days (to {fmt_date(today)})"

    ov = payload.get('overview') or {}
    users = payload.get('users') or {}
    net = payload.get('network') or {}

    story = brand_header(prof, 'BUSINESS REPORT', [period, f"Generated {fmt_date(timezone.now(), True)}"])

    # 1 ── Revenue snapshot
    story += section('Revenue snapshot')
    story.append(kpi_cards([
        ("Today's revenue", money(ov.get('today_revenue'), cur, 0), f"{_chg(ov.get('today_change'))} vs yesterday", None, _pct_color(ov.get('today_change'))),
        ('This week', money(ov.get('week_revenue'), cur, 0), f"{_chg(ov.get('week_change'))} vs last week", None, _pct_color(ov.get('week_change'))),
        ('This month', money(ov.get('month_revenue'), cur, 0), f"{_chg(ov.get('month_change'))} vs last month", None, _pct_color(ov.get('month_change'))),
        ('Transactions today', f"{int(_f(ov.get('total_transactions_today'))):,}", 'completed payments'),
    ]))

    # 2 ── PPPoE vs Hotspot
    daily = (split or {}).get('daily_revenue') or []
    ssum = (split or {}).get('summary') or {}
    if daily:
        t_pppoe, t_hot = _f(ssum.get('total_pppoe')), _f(ssum.get('total_hotspot'))
        combined = (t_pppoe + t_hot) or 1
        story += section(f"Revenue by service ({days} days)")
        story.append(kpi_cards([
            ('PPPoE (Fiber/DSL)', money(t_pppoe, cur, 0), f"{t_pppoe / combined * 100:.1f}% of revenue", BRAND),
            ('Hotspot', money(t_hot, cur, 0), f"{t_hot / combined * 100:.1f}% of revenue", EMERALD),
            ('Combined', money(t_pppoe + t_hot, cur, 0), period),
        ]))
        story.append(Spacer(1, 6))
        story.append(KeepTogether([
            legend([('PPPoE', BRAND), ('Hotspot', EMERALD)]),
            bar_chart([d['date'] for d in daily],
                      [[d.get('pppoe_revenue') for d in daily], [d.get('hotspot_revenue') for d in daily]],
                      [BRAND, EMERALD], stacked=True),
        ]))

    # 3 ── This year vs last year
    ly = {r['month']: _f(r['amount']) for r in (ov.get('last_year_earnings') or [])}
    ty = {r['month']: _f(r['amount']) for r in (ov.get('monthly_earnings') or [])}
    if ly or ty:
        rows, s_ly, s_ty = [], [], []
        for m in _MONTHS:
            l, has_t, t = ly.get(m, 0.0), m in ty, ty.get(m, 0.0)
            s_ly.append(l)
            s_ty.append(t)
            ch = _chg((t - l) / l * 100) if (has_t and l) else '-'
            rows.append([m, money(l, '', 0), money(t, '', 0) if has_t else '-', ch])
        tot = ['TOTAL', money(sum(s_ly), '', 0), money(sum(s_ty), '', 0), '']
        story += section(f"Monthly earnings: {today.year} vs {today.year - 1}")
        story.append(KeepTogether([
            legend([(str(today.year - 1), SLATE), (str(today.year), BRAND)]),
            bar_chart(_MONTHS, [s_ly, s_ty], [SLATE, BRAND], height=5 * cm),
        ]))
        story.append(Spacer(1, 6))
        story.append(data_table(['Month', f'{today.year - 1} ({cur})', f'{today.year} ({cur})', 'Change'],
                                rows, [3.0 * cm, 5.0 * cm, 5.0 * cm, 5.0 * cm], right_cols=(1, 2, 3),
                                font_size=8, total_row=tot))

    # 4 ── This week vs last week
    tw = {r['day']: _f(r['amount']) for r in (ov.get('weekly_income') or [])}
    lw = {r['day']: _f(r['amount']) for r in (ov.get('last_week_income') or [])}
    if tw or lw:
        days_lbl = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        wrows = []
        for d in days_lbl:
            a, b = lw.get(d, 0.0), tw.get(d, 0.0)
            wrows.append([d, money(a, '', 0), money(b, '', 0), _chg((b - a) / a * 100) if a else '-'])
        story += section('Weekly income: this week vs last week')
        story.append(data_table(['Day', f'Last week ({cur})', f'This week ({cur})', 'Change'], wrows,
                                [3.0 * cm, 5.0 * cm, 5.0 * cm, 5.0 * cm], right_cols=(1, 2, 3),
                                total_row=['TOTAL', money(sum(lw.values()), '', 0), money(sum(tw.values()), '', 0), '']))

    # 5 ── Customer growth
    regs = users.get('registration_trends') or []
    usum = users.get('summary') or {}
    story += section(f"New customer registrations ({days} days)")
    story.append(kpi_cards([
        ('Total registrations', f"{int(_f(usum.get('total_registrations'))):,}", period),
        ('Average per day', f"{_f(usum.get('avg_per_day')):.1f}", 'new customers'),
        ('Best day', _short_date(usum.get('peak_day')), 'highest sign-ups'),
    ]))
    story.append(Spacer(1, 6))
    story.append(KeepTogether([bar_chart([_short_date(r['date']) for r in regs], [[r['count'] for r in regs]],
                                         [VIOLET], height=4.2 * cm, money_axis=False)]))

    # 6 ── Network
    us, perf = net.get('usage_summary') or {}, net.get('performance') or {}
    if us or perf:
        story += section('Network usage')
        story.append(kpi_cards([
            ('Total download', f"{_f(us.get('total_download')):,.1f} GB", period),
            ('Total upload', f"{_f(us.get('total_upload')):,.1f} GB", period),
            ('Avg daily usage', str(perf.get('avg_daily_usage') or '-'), f"Peak: {_short_date(perf.get('peak_usage_day'))}"),
            ('Download : upload', str(perf.get('download_upload_ratio') or '-'), 'traffic ratio'),
        ]))

    # 7 ── Top customers
    top = payload.get('top_customers') or []
    if top:
        trows = [[str(i), clip(c.get('display_name'), 34), _TYPE.get(c.get('type'), c.get('type') or '-'),
                  f"{int(_f(c.get('tx_count'))):,}", money(c.get('total_amount'), '', 2)]
                 for i, c in enumerate(top[:10], 1)]
        story += section('Top 10 customers by lifetime spend')
        story.append(data_table(['#', 'Customer', 'Type', 'Payments', f'Lifetime spend ({cur})'], trows,
                                [1.0 * cm, 7.0 * cm, 2.8 * cm, 2.6 * cm, 4.6 * cm], right_cols=(3, 4)))

    # 8 ── Plan performance
    plans = [p for p in (payload.get('plan_analytics') or [])][:15]
    if plans:
        prows = [[clip(p.get('name'), 36), _TYPE.get(str(p.get('connection_type')).upper(), p.get('connection_type') or '-'),
                  money(p.get('base_price'), '', 0), f"{int(_f(p.get('total_transactions'))):,}",
                  money(p.get('total_revenue'), '', 2)] for p in plans]
        story += section('Plan performance (lifetime)')
        story.append(data_table(['Plan', 'Type', f'Price ({cur})', 'Transactions', f'Revenue ({cur})'], prows,
                                [6.4 * cm, 2.4 * cm, 2.6 * cm, 2.6 * cm, 4.0 * cm], right_cols=(2, 3, 4)))

    return build_pdf(story, title='Business report', prof=prof, running_title=f"Business report | {period}")