"""Per-ad performance report PDF (tenant-branded)."""
from datetime import datetime, time, timedelta

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone
from reportlab.lib.units import cm
from reportlab.platypus import KeepTogether, Paragraph, Spacer

from apps.billing.models.ad_models import HotspotAdGrant, HotspotAdView
from apps.core.pdf_reports import (
    BRAND, CONTENT_W, GREEN, S, bar_chart, brand_header, build_pdf, clip,
    data_table, esc, fmt_date, get_company_profile, kpi_cards, legend, section,
)

MAX_DAYS = 90


def build_ad_report_pdf(ad, request):
    prof = get_company_profile(request)
    now = timezone.now()
    today = timezone.localdate()

    d_from = max(timezone.localtime(ad.created_at).date(), today - timedelta(days=MAX_DAYS - 1))
    start_dt = timezone.make_aware(datetime.combine(d_from, time.min))

    # ── Aggregates (3 small indexed queries, all filtered by ad) ──
    views_by_day = {
        r['d']: r['c'] for r in
        HotspotAdView.objects.filter(ad=ad, created_at__gte=start_dt)
        .annotate(d=TruncDate('created_at')).values('d').annotate(c=Count('id')).order_by()
    }
    grants_by_day = {
        r['d']: r['c'] for r in
        HotspotAdGrant.objects.filter(ad=ad, granted_at__gte=start_dt)
        .annotate(d=TruncDate('granted_at')).values('d').annotate(c=Count('id')).order_by()
    }
    router_rows = list(
        HotspotAdGrant.objects.filter(ad=ad)
        .values('router__name').annotate(c=Count('id')).order_by('-c')[:15]
    )
    total_grants = sum(r['c'] for r in router_rows) if len(router_rows) < 15 else \
        HotspotAdGrant.objects.filter(ad=ad).count()

    impressions = ad.impressions or 0
    completions = ad.completions or 0
    rate = (completions / impressions * 100) if impressions else 0.0

    # ── Header ──
    story = brand_header(prof, 'AD REPORT', [
        clip(ad.name, 40),
        f"Period: {fmt_date(d_from)} - {fmt_date(today)}",
    ])

    # ── KPIs ──
    story.append(kpi_cards([
        ('Impressions', f"{impressions:,}", 'unique devices'),
        ('Completions', f"{completions:,}", 'watched to the end'),
        ('Completion rate', f"{rate:.1f}%", 'completions / impressions'),
        ('Rewards granted', f"{total_grants:,}",
         f"{ad.reward_minutes} min each" if ad.reward_enabled else 'rewards off'),
    ]))

    # ── Ad details ──
    schedule = 'Always on'
    if ad.start_date or ad.end_date:
        schedule = f"{fmt_date(ad.start_date)} - {fmt_date(ad.end_date)}"
    details = [
        ['Ad name', Paragraph(esc(ad.name), S['cell'])],
        ['Media type', ad.media_type],
        ['Status', 'Active' if ad.is_active else 'Inactive'],
        ['Created', fmt_date(ad.created_at)],
        ['Schedule', schedule],
        ['Reward', f"{ad.reward_minutes} minutes free internet" if ad.reward_enabled else 'None'],
        ['Click-through URL', Paragraph(esc(clip(ad.target_url, 90)) or '-', S['cell'])],
        ['Target router', ad.router.name if ad.router_id else 'All routers'],
    ]
    story += section('Ad details')
    story.append(data_table(['Detail', 'Value'], details, [4.5 * cm, CONTENT_W - 4.5 * cm], font_size=8.5))

    # ── Daily trend chart ──
    labels, imp_series, comp_series, daily_rows = [], [], [], []
    day = d_from
    while day <= today:
        v, c = views_by_day.get(day, 0), grants_by_day.get(day, 0)
        labels.append(day.strftime('%d %b'))
        imp_series.append(v)
        comp_series.append(c)
        if v or c:
            daily_rows.append([fmt_date(day), f"{v:,}", f"{c:,}", f"{(c / v * 100) if v else 0:.1f}%"])
        day += timedelta(days=1)

    story.append(KeepTogether(
        section('Daily performance')
        + [legend([('Impressions', BRAND), ('Rewards claimed', GREEN)]), Spacer(1, 4),
           bar_chart(labels, [imp_series, comp_series], [BRAND, GREEN], money_axis=False)]
    ))

    # ── Daily table ──
    story += section('Daily breakdown')
    story.append(data_table(
        ['Date', 'Impressions', 'Rewards claimed', 'Claim rate'], daily_rows,
        [5 * cm, 4 * cm, 4.5 * cm, CONTENT_W - 13.5 * cm], right_cols=(1, 2, 3),
        empty_text='No activity recorded in this period.',
    ))

    # ── Router breakdown ──
    if router_rows:
        story += section('Rewards by router')
        story.append(data_table(
            ['Router', 'Rewards claimed'],
            [[clip(r['router__name'] or 'Unknown', 40), f"{r['c']:,}"] for r in router_rows],
            [CONTENT_W - 4.5 * cm, 4.5 * cm], right_cols=(1,),
        ))

    story += [Spacer(1, 10), Paragraph(
        'Impressions count unique devices that were shown the ad. Rewards claimed count devices '
        'that watched the ad to the end and received free internet.', S['small'])]

    return build_pdf(story, title=f"Ad report - {ad.name}", prof=prof,
                     running_title=f"Ad report | {clip(ad.name, 40)}")