"""Invoice PDF + Payments statement PDF."""
import logging
from datetime import timedelta
from decimal import Decimal

from django.db.models import Avg, Count, Max, Sum
from django.db.models.functions import TruncDate, TruncMonth
from reportlab.lib.units import cm
from reportlab.platypus import KeepTogether, Paragraph, Spacer, Table, TableStyle

from apps.core.pdf_reports import (
    BRAND, CONTENT_W, GREEN, INK, LINE, MUTED, SOFT, S, bar_chart, brand_header,
    build_pdf, clip, data_table, esc, fmt_date, get_company_profile, kpi_cards,
    legend, money, section, two_columns,
)
import reportlab.lib.colors as rl_colors

logger = logging.getLogger(__name__)

_STATUS_HEX = {
    'PAID': '#16a34a', 'PARTIAL': '#d97706', 'OVERDUE': '#dc2626',
    'ISSUED': '#2563eb', 'SENT': '#2563eb', 'DRAFT': '#64748b',
    'VOIDED': '#64748b', 'WRITTEN_OFF': '#64748b',
}
_SERVICE_LABEL = {'HOTSPOT': 'Hotspot', 'PPPOE': 'PPPoE', 'OTHER': 'Other', '': 'Other', None: 'Other'}


def _dec(v):
    try:
        return Decimal(str(v if v not in (None, '') else 0))
    except Exception:
        return Decimal('0')


# ───────────────────────── INVOICE ─────────────────────────

def _payment_lines(invoice):
    """'How to pay' lines from the tenant's active payment method. Never raises."""
    try:
        from django.db import connection
        from apps.billing.models.payment_models import InvoiceItemPayment

        m = (
            InvoiceItemPayment.objects
            .filter(schema_name=connection.schema_name, is_active=True)
            .exclude(code__startswith='HOTSPOT_')
            .select_related('mpesa_configuration')
            .order_by('-is_default', '-updated_at')
            .first()
        )
        if not m:
            return []
        cfg = m.config_json or {}

        def g(key, attr):
            return str(cfg.get(key) or getattr(m, attr, '') or '').strip()

        account = ''
        try:
            sc = invoice.service_connection
            account = str(getattr(sc, 'effective_mpesa_account', '') or '') if sc else ''
        except Exception:
            account = ''

        lines = []
        mc = m.mpesa_configuration
        if mc is not None:
            if mc.shortcode_type == 'TILL':
                lines.append(('M-Pesa Till (Buy Goods)', mc.business_shortcode))
            else:
                lines.append(('M-Pesa Paybill', mc.business_shortcode))
                if account:
                    lines.append(('Account number', account))
        elif m.method_type == 'MPESA_PAYBILL' and g('paybill_number', 'paybill_number'):
            lines.append(('M-Pesa Paybill', g('paybill_number', 'paybill_number')))
            if account:
                lines.append(('Account number', account))
        elif m.method_type == 'MPESA_TILL' and g('till_number', 'till_number'):
            lines.append(('M-Pesa Till (Buy Goods)', g('till_number', 'till_number')))
        elif m.method_type == 'BANK_TRANSFER' and g('bank_name', 'bank_name'):
            lines.append(('Bank', g('bank_name', 'bank_name')))
            if g('account_number', 'account_number'):
                lines.append(('Account number', g('account_number', 'account_number')))
        return lines
    except Exception:
        logger.warning('Invoice payment lines failed', exc_info=True)
        return []


def build_invoice_pdf(invoice, request):
    prof = get_company_profile(request)
    cur = prof['currency']
    customer = invoice.customer
    user = getattr(customer, 'user', None)
    status = (invoice.status or '').upper()
    status_hex = _STATUS_HEX.get(status, '#64748b')
    status_label = status.replace('_', ' ')

    story = brand_header(prof, 'INVOICE', [f"No. {invoice.invoice_number}", f"Issued {fmt_date(invoice.billing_date)}"])

    # ── Bill-to + meta ──
    cust_name = getattr(customer, 'full_name', '') or (user.get_full_name() if user else '') or 'Customer'
    bill = [Paragraph('BILL TO', S['label']), Paragraph(esc(cust_name), S['body_bold'])]
    for line in (
        f"Account: {customer.customer_code}" if getattr(customer, 'customer_code', None) else '',
        getattr(user, 'phone_number', '') or '',
        getattr(user, 'email', '') or '',
    ):
        if line:
            bill.append(Paragraph(esc(line), S['body']))

    meta = [('Invoice no.', invoice.invoice_number),
            ('Issue date', fmt_date(invoice.billing_date)),
            ('Due date', fmt_date(invoice.due_date))]
    if invoice.service_period_start and invoice.service_period_end:
        meta.append(('Service period', f"{fmt_date(invoice.service_period_start)} - {fmt_date(invoice.service_period_end)}"))
    if invoice.plan_id and invoice.plan:
        meta.append(('Plan', clip(invoice.plan.name, 34)))
    meta_rows = [[Paragraph(esc(k), S['small']), Paragraph(esc(v), S['body_bold'])] for k, v in meta]
    status_markup = '<font color="%s"><b>%s</b></font>' % (status_hex, esc(status_label))
    meta_rows.append([Paragraph('Status', S['small']), Paragraph(status_markup, S['body'])])
    mt = Table(meta_rows, colWidths=[2.5 * cm, 4.5 * cm])
    mt.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), SOFT), ('BOX', (0, 0), (-1, -1), 0.5, LINE),
        ('LINEBELOW', (0, 0), (-1, -2), 0.25, LINE), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 7), ('RIGHTPADDING', (0, 0), (-1, -1), 5),
    ]))
    top = Table([[bill, mt]], colWidths=[CONTENT_W - 7.0 * cm, 7.0 * cm])
    top.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
                             ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                             ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0)]))
    story += [top, Spacer(1, 14)]

    # ── Items ──
    items = list(invoice.items.all())
    rows = []
    for i, it in enumerate(items, 1):
        desc = esc(it.description)
        if it.service_period_start and it.service_period_end:
            desc += '<br/><font size="7" color="#64748b">%s - %s</font>' % (
                esc(fmt_date(it.service_period_start)), esc(fmt_date(it.service_period_end)))
        rows.append([str(i), Paragraph(desc, S['cell']), f"{_dec(it.quantity):g}",
                     money(it.unit_price), money(it.total)])
    if not rows:
        label = f"{invoice.plan.name} - Internet subscription" if invoice.plan_id and invoice.plan else 'Services'
        amt = invoice.subtotal or invoice.total_amount
        rows.append(['1', Paragraph(esc(label), S['cell']), '1', money(amt), money(amt)])

    story.append(data_table(
        ['#', 'Description', 'Qty', f'Unit price ({cur})', f'Amount ({cur})'],
        rows, [0.9 * cm, 8.4 * cm, 1.7 * cm, 3.5 * cm, 3.5 * cm],
        right_cols=(2, 3, 4), font_size=8.5, header_bg=BRAND,
    ))
    story.append(Spacer(1, 10))

    # ── Totals ──
    total = _dec(invoice.total_amount)
    paid = _dec(invoice.amount_paid)
    balance = _dec(invoice.balance) if invoice.balance is not None else total - paid
    t_rows = [['Subtotal', money(invoice.subtotal or total, cur)]]
    if _dec(invoice.tax_amount) > 0:
        t_rows.append(['Tax', money(invoice.tax_amount, cur)])
    if _dec(invoice.discount_amount) > 0:
        t_rows.append(['Discount', '-' + money(invoice.discount_amount, cur)])
    t_rows.append(['Total', money(total, cur)])
    total_idx = len(t_rows) - 1
    if paid > 0:
        t_rows.append(['Amount paid', money(paid, cur)])
    t_rows.append(['Balance due', money(max(balance, Decimal('0')), cur)])
    bal_idx = len(t_rows) - 1
    tt = Table(t_rows, colWidths=[3.4 * cm, 4.1 * cm])
    tt.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'), ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TEXTCOLOR', (0, 0), (-1, -1), INK), ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LINEBELOW', (0, 0), (-1, -2), 0.25, LINE),
        ('FONTNAME', (0, total_idx), (-1, total_idx), 'Helvetica-Bold'),
        ('LINEABOVE', (0, total_idx), (-1, total_idx), 0.8, INK),
        ('BACKGROUND', (0, bal_idx), (-1, bal_idx), GREEN if balance <= 0 else BRAND),
        ('TEXTCOLOR', (0, bal_idx), (-1, bal_idx), rl_colors.white),
        ('FONTNAME', (0, bal_idx), (-1, bal_idx), 'Helvetica-Bold'),
        ('FONTSIZE', (0, bal_idx), (-1, bal_idx), 10),
    ]))
    wrap = Table([['', tt]], colWidths=[CONTENT_W - 7.5 * cm, 7.5 * cm])
    wrap.setStyle(TableStyle([('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                              ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                              ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story.append(KeepTogether([wrap]))

    # ── How to pay ──
    pay_lines = _payment_lines(invoice)
    if pay_lines and balance > 0:
        pr = [[Paragraph(esc(k), S['small']), Paragraph(esc(v), S['body_bold'])] for k, v in pay_lines]
        pt = Table(pr, colWidths=[4.0 * cm, CONTENT_W - 4.0 * cm])
        pt.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), SOFT), ('BOX', (0, 0), (-1, -1), 0.5, LINE),
                                ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                                ('LEFTPADDING', (0, 0), (-1, -1), 8)]))
        story += [Spacer(1, 12), KeepTogether(section('How to pay') + [pt])]

    if invoice.notes:
        story += [Spacer(1, 10), Paragraph('NOTES', S['label']),
                  Paragraph(esc(invoice.notes).replace('\n', '<br/>'), S['body'])]

    story += [Spacer(1, 22), Paragraph('Thank you for your business.', S['center_muted'])]
    return build_pdf(story, title=f"Invoice {invoice.invoice_number}", prof=prof,
                     running_title=f"Invoice {invoice.invoice_number}")


# ───────────────────────── PAYMENTS STATEMENT ─────────────────────────

def _series(qs, d_from, d_to):
    """Daily series for <= 62 days, monthly otherwise. Returns (labels, values)."""
    labels, values = [], []
    if (d_to - d_from).days <= 62:
        rows = {r['d']: r['total'] for r in
                qs.annotate(d=TruncDate('payment_date')).values('d').annotate(total=Sum('amount')).order_by('d')}
        cur = d_from
        while cur <= d_to:
            labels.append(cur.strftime('%d %b'))
            values.append(float(rows.get(cur, 0) or 0))
            cur += timedelta(days=1)
    else:
        rows = {(r['m'].year, r['m'].month): r['total'] for r in
                qs.annotate(m=TruncMonth('payment_date')).values('m').annotate(total=Sum('amount')).order_by('m')}
        cur = d_from.replace(day=1)
        while cur <= d_to:
            labels.append(cur.strftime('%b %Y'))
            values.append(float(rows.get((cur.year, cur.month), 0) or 0))
            cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return labels, values


def build_payments_report_pdf(request, qs, *, date_from, date_to, service_type=None, search='', max_rows=2000):
    prof = get_company_profile(request)
    cur = prof['currency']
    period = f"{fmt_date(date_from)} - {fmt_date(date_to)}"
    service_label = _SERVICE_LABEL.get(service_type, 'All services') if service_type else 'All services'

    sub = [f"Period: {period}", f"Service: {service_label}"]
    if search:
        sub.append(f"Filter: {clip(search, 30)}")
    story = brand_header(prof, 'PAYMENTS REPORT', sub)

    agg = qs.aggregate(total=Sum('amount'), count=Count('id'), avg=Avg('amount'), biggest=Max('amount'))
    count = agg['count'] or 0
    total = agg['total'] or Decimal('0')
    if count == 0:
        story.append(Paragraph('No completed payments were found for the selected period and filters.', S['body']))
        return build_pdf(story, title='Payments report', prof=prof, running_title=f"Payments report | {period}")

    story.append(kpi_cards([
        ('Total collected', money(total, cur, 0), f"{period}"),
        ('Payments', f"{count:,}", 'completed transactions'),
        ('Average payment', money(agg['avg'] or 0, cur, 0), 'per transaction'),
        ('Largest payment', money(agg['biggest'] or 0, cur, 0), 'single transaction'),
    ]))

    # ── Trend chart ──
    labels, values = _series(qs, date_from, date_to)
    kind = 'Daily' if (date_to - date_from).days <= 62 else 'Monthly'
    story.append(KeepTogether(section(f"{kind} collections") + [bar_chart(labels, [values], [BRAND])]))

    # ── Breakdowns ──
    by_service = list(qs.values('service_type').annotate(total=Sum('amount'), c=Count('id')).order_by('-total'))
    by_method = list(qs.values('payment_method__name').annotate(total=Sum('amount'), c=Count('id')).order_by('-total'))
    denom = total or Decimal('1')

    def _break_table(title_col, rows):
        data = [[clip(name, 22), f"{c:,}", money(t, '', 2), f"{(t / denom * 100):.1f}%"] for name, c, t in rows]
        return data_table([title_col, 'Count', f'Amount ({cur})', 'Share'], data,
                          [3.3 * cm, 1.5 * cm, 2.6 * cm, 1.4 * cm], right_cols=(1, 2, 3), font_size=8)

    svc_rows = [(_SERVICE_LABEL.get(r['service_type'], 'Other'), r['c'], r['total'] or Decimal('0')) for r in by_service]
    mth_rows = [(r['payment_method__name'] or 'Unknown', r['c'], r['total'] or Decimal('0')) for r in by_method[:10]]
    story += section('Where the money came from')
    story.append(two_columns(_break_table('Service', svc_rows), _break_table('Payment method', mth_rows)))

    # ── Ledger ──
    story += section('Transaction statement')
    rows_qs = (
        qs.order_by('payment_date', 'id')
        .values_list('payment_date', 'mpesa_receipt', 'transaction_id', 'payment_number',
                     'customer__user__first_name', 'customer__user__last_name',
                     'payer_name', 'payer_phone', 'payment_method__name', 'service_type', 'amount')[:max_rows]
    )
    ledger = []
    for (pdate, receipt, txn, pnum, fn, ln, payer, phone, method, svc, amount) in rows_qs:
        name = f"{fn or ''} {ln or ''}".strip() or payer or phone or 'Hotspot client'
        ledger.append([
            fmt_date(pdate, True), clip(receipt or txn or pnum, 20), clip(name, 27),
            clip(method or '-', 16), _SERVICE_LABEL.get(svc, 'Other'), money(amount, '', 2),
        ])
    story.append(data_table(
        ['Date', 'Reference', 'Customer', 'Method', 'Service', f'Amount ({cur})'],
        ledger, [2.9 * cm, 2.9 * cm, 4.4 * cm, 2.8 * cm, 1.8 * cm, 3.2 * cm],
        right_cols=(5,), font_size=7.5,
        total_row=['', '', '', '', 'TOTAL', money(total, '', 2)],
    ))
    if count > max_rows:
        story += [Spacer(1, 6), Paragraph(
            f"Showing the first {max_rows:,} of {count:,} payments. Totals above include all {count:,}. "
            f"Narrow the date range for a complete line-by-line statement.", S['small'])]

    return build_pdf(story, title='Payments report', prof=prof, running_title=f"Payments report | {period}")