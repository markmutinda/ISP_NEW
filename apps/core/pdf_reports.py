"""
Shared branded PDF toolkit (reportlab).
Used by invoices, payments report and analytics report.
"""
import hashlib
import io
import logging
import math
import re
from decimal import Decimal
from xml.sax.saxutils import escape as _xml_escape

from django.core.cache import cache
from django.http import HttpResponse
from django.utils import timezone

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    HRFlowable, Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

logger = logging.getLogger(__name__)

INK = colors.HexColor('#0f172a')
MUTED = colors.HexColor('#64748b')
BRAND = colors.HexColor('#2563eb')
LINE = colors.HexColor('#e2e8f0')
SOFT = colors.HexColor('#f8fafc')
GREEN = colors.HexColor('#16a34a')
RED = colors.HexColor('#dc2626')
AMBER = colors.HexColor('#d97706')
VIOLET = colors.HexColor('#7c3aed')
EMERALD = colors.HexColor('#10b981')
SLATE = colors.HexColor('#94a3b8')

PAGE_W, PAGE_H = A4
MARGIN = 1.5 * cm
CONTENT_W = PAGE_W - 2 * MARGIN


def _ps(name, **kw):
    kw.setdefault('fontName', 'Helvetica')
    return ParagraphStyle(name, **kw)


S = {
    'company': _ps('company', fontName='Helvetica-Bold', fontSize=15, leading=18, textColor=INK),
    'small': _ps('small', fontSize=8, leading=10.5, textColor=MUTED),
    'doc_title': _ps('doc_title', fontName='Helvetica-Bold', fontSize=22, leading=26, textColor=BRAND, alignment=TA_RIGHT),
    'doc_title_sm': _ps('doc_title_sm', fontName='Helvetica-Bold', fontSize=16, leading=20, textColor=BRAND, alignment=TA_RIGHT),
    'doc_sub': _ps('doc_sub', fontSize=8.5, leading=11.5, textColor=MUTED, alignment=TA_RIGHT),
    'h2': _ps('h2', fontName='Helvetica-Bold', fontSize=11, leading=14, textColor=INK),
    'body': _ps('body', fontSize=9, leading=12, textColor=INK),
    'body_bold': _ps('body_bold', fontName='Helvetica-Bold', fontSize=9.5, leading=12.5, textColor=INK),
    'label': _ps('label', fontName='Helvetica-Bold', fontSize=7, leading=9, textColor=MUTED),
    'value': _ps('value', fontName='Helvetica-Bold', fontSize=12, leading=15, textColor=INK),
    'kpi_sub': _ps('kpi_sub', fontSize=7.5, leading=10, textColor=MUTED),
    'initial': _ps('initial', fontName='Helvetica-Bold', fontSize=22, leading=26, textColor=colors.white, alignment=TA_CENTER),
    'cell': _ps('cell', fontSize=8.5, leading=11, textColor=INK),
    'center_muted': _ps('center_muted', fontSize=8.5, leading=12, textColor=MUTED, alignment=TA_CENTER),
}


# ───────────────────────── helpers ─────────────────────────

def esc(value):
    return _xml_escape('' if value is None else str(value))


def clip(value, n):
    s = '' if value is None else str(value).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + '...'


def money(value, currency='', decimals=2):
    try:
        d = Decimal(str(value if value not in (None, '') else 0))
    except Exception:
        d = Decimal('0')
    s = f"{d:,.{decimals}f}"
    return f"{currency} {s}".strip()


def fmt_date(value, with_time=False):
    if not value:
        return '-'
    try:
        if with_time and hasattr(value, 'hour'):
            if timezone.is_aware(value):
                value = timezone.localtime(value)
            return value.strftime('%d %b %Y %H:%M')
        return value.strftime('%d %b %Y')
    except Exception:
        return str(value)


def safe_filename(value, default='document'):
    s = re.sub(r'[^A-Za-z0-9._-]+', '-', str(value or '')).strip('-')
    return s or default


def pdf_response(data, filename):
    resp = HttpResponse(data, content_type='application/pdf')
    resp['Content-Disposition'] = f'attachment; filename="{safe_filename(filename, "report.pdf")}"'
    resp['Cache-Control'] = 'no-store'
    return resp


# ───────────────────────── company profile ─────────────────────────

def _load_logo(company):
    """Return downscaled PNG bytes (cached) or None."""
    try:
        if not company or not getattr(company, 'logo', None) or not company.logo.name:
            return None
        key = 'pdf:logo:' + hashlib.md5(f"{company.pk}:{company.logo.name}".encode()).hexdigest()
        cached = cache.get(key)
        if cached is not None:
            return cached or None

        data = b''
        try:
            from PIL import Image as PILImage
            with company.logo.storage.open(company.logo.name, 'rb') as fh:
                raw = fh.read()
            if raw and len(raw) <= 6 * 1024 * 1024:
                img = PILImage.open(io.BytesIO(raw))
                img.load()
                if img.mode not in ('RGB', 'RGBA'):
                    img = img.convert('RGBA')
                img.thumbnail((480, 480))
                out = io.BytesIO()
                img.save(out, 'PNG', optimize=True)
                data = out.getvalue()
        except Exception:
            logger.warning('PDF logo load failed', exc_info=True)
        cache.set(key, data, 3600)
        return data or None
    except Exception:
        return None


def get_company_profile(request):
    company = getattr(request, 'company', None)
    if company is None:
        try:
            from apps.core.views import get_current_public_company_and_tenant
            company, _tenant = get_current_public_company_and_tenant(request)
        except Exception:
            logger.warning('Could not resolve company for PDF', exc_info=True)
            company = None

    def _g(attr):
        return str(getattr(company, attr, '') or '').strip()

    name = _g('name') or str(getattr(getattr(request, 'user', None), 'company_name', '') or '').strip() or 'Company'
    address = ', '.join(p for p in (_g('address'), _g('city')) if p)
    contact = '  |  '.join(p for p in (
        f"Tel: {_g('phone_number')}" if _g('phone_number') else '',
        _g('email'),
    ) if p)
    lines = [l for l in (
        address,
        contact,
        _g('website'),
        f"PIN: {_g('tax_pin')}" if _g('tax_pin') else '',
    ) if l]
    return {
        'name': name,
        'lines': lines,
        'currency': _g('base_currency') or 'KES',
        'logo': _load_logo(company),
    }


# ───────────────────────── building blocks ─────────────────────────

def _logo_flowable(data, max_w=2.1 * cm, max_h=2.1 * cm):
    if not data:
        return None
    try:
        iw, ih = ImageReader(io.BytesIO(data)).getSize()
        scale = min(max_w / iw, max_h / ih)
        return Image(io.BytesIO(data), width=iw * scale, height=ih * scale)
    except Exception:
        return None


def _initial_mark(name):
    t = Table([[Paragraph(esc((name or 'N')[:1].upper()), S['initial'])]],
              colWidths=[1.7 * cm], rowHeights=[1.7 * cm])
    t.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), BRAND),
                           ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                           ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                           ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0)]))
    return t


_ZERO_PAD = [('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
             ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0)]


def brand_header(prof, title, subtitle_lines=()):
    left_w = CONTENT_W * 0.6
    mark_w = 2.4 * cm
    mark = _logo_flowable(prof.get('logo')) or _initial_mark(prof['name'])
    info = [Paragraph(esc(prof['name']), S['company'])]
    info += [Paragraph(esc(l), S['small']) for l in prof['lines']]
    left = Table([[mark, info]], colWidths=[mark_w, left_w - mark_w])
    left.setStyle(TableStyle(_ZERO_PAD + [('VALIGN', (0, 0), (-1, -1), 'TOP')]))

    t_style = S['doc_title'] if len(title) <= 10 else S['doc_title_sm']
    right = [Paragraph(esc(title), t_style)] + [Paragraph(esc(l), S['doc_sub']) for l in subtitle_lines]
    outer = Table([[left, right]], colWidths=[left_w, CONTENT_W - left_w])
    outer.setStyle(TableStyle(_ZERO_PAD + [('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    return [outer, HRFlowable(width='100%', thickness=2, color=BRAND, spaceBefore=8, spaceAfter=10)]


def section(title):
    return [Spacer(1, 6), Paragraph(esc(title), S['h2']),
            HRFlowable(width='100%', thickness=0.6, color=LINE, spaceBefore=1, spaceAfter=5)]


def kpi_cards(items, width=CONTENT_W, gap=0.25 * cm):
    """items: (label, value, sub, value_color=None, sub_color=None)"""
    n = len(items)
    card_w = (width - gap * (n - 1)) / n
    row, widths, cmds = [], [], []
    for i, item in enumerate(items):
        label, value = item[0], item[1]
        sub = item[2] if len(item) > 2 else ''
        vcolor = item[3] if len(item) > 3 and item[3] is not None else INK
        scolor = item[4] if len(item) > 4 and item[4] is not None else MUTED
        col = i * 2
        row.append([
            Paragraph(esc(label).upper(), S['label']),
            Paragraph(esc(value), ParagraphStyle(f'kv{i}', parent=S['value'], textColor=vcolor)),
            Paragraph(esc(sub) or '&nbsp;', ParagraphStyle(f'ks{i}', parent=S['kpi_sub'], textColor=scolor)),
        ])
        widths.append(card_w)
        cmds += [('BACKGROUND', (col, 0), (col, 0), SOFT), ('BOX', (col, 0), (col, 0), 0.5, LINE),
                 ('LINEBEFORE', (col, 0), (col, 0), 2.5, BRAND),
                 ('LEFTPADDING', (col, 0), (col, 0), 9), ('RIGHTPADDING', (col, 0), (col, 0), 6),
                 ('TOPPADDING', (col, 0), (col, 0), 7), ('BOTTOMPADDING', (col, 0), (col, 0), 7)]
        if i < n - 1:
            row.append('')
            widths.append(gap)
            sp = col + 1
            cmds += [('LEFTPADDING', (sp, 0), (sp, 0), 0), ('RIGHTPADDING', (sp, 0), (sp, 0), 0),
                     ('TOPPADDING', (sp, 0), (sp, 0), 0), ('BOTTOMPADDING', (sp, 0), (sp, 0), 0)]
    t = Table([row], colWidths=widths)
    t.setStyle(TableStyle(cmds + [('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    return t


def data_table(header, rows, col_widths, right_cols=(), font_size=8, total_row=None,
               header_bg=INK, empty_text='No data for this period.'):
    if not rows:
        return Paragraph(esc(empty_text), S['small'])
    data = [header] + rows + ([total_row] if total_row else [])
    t = Table(data, colWidths=col_widths, repeatRows=1)
    cmds = [
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), font_size),
        ('TEXTCOLOR', (0, 1), (-1, -1), INK),
        ('BACKGROUND', (0, 0), (-1, 0), header_bg),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 5), ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('LINEBELOW', (0, 1), (-1, -1), 0.25, LINE),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, SOFT]),
    ]
    for c in right_cols:
        cmds.append(('ALIGN', (c, 0), (c, -1), 'RIGHT'))
    if total_row:
        cmds += [('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
                 ('BACKGROUND', (0, -1), (-1, -1), LINE),
                 ('LINEABOVE', (0, -1), (-1, -1), 0.8, INK)]
    t.setStyle(TableStyle(cmds))
    return t


def two_columns(left, right, gap=0.4 * cm):
    w = (CONTENT_W - gap) / 2
    t = Table([[left, '', right]], colWidths=[w, gap, w])
    t.setStyle(TableStyle(_ZERO_PAD + [('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    return t


def legend(items):
    """items: [(label, color)]"""
    cells, widths, cmds = [], [], []
    for i, (label, color) in enumerate(items):
        base = i * 2
        cells += ['', Paragraph(esc(label), S['small'])]
        widths += [0.28 * cm, 3.2 * cm]
        cmds.append(('BACKGROUND', (base, 0), (base, 0), color))
    t = Table([cells], colWidths=widths, rowHeights=[0.4 * cm])
    t.hAlign = 'LEFT'
    t.setStyle(TableStyle(_ZERO_PAD + [('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                                      ('LEFTPADDING', (1, 0), (-1, -1), 4)] + cmds))
    return t


def _compact(v):
    v = float(v)
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"{v / 1_000:.0f}K"
    return f"{v:.0f}"


def bar_chart(labels, series, series_colors, *, width=CONTENT_W, height=5.2 * cm,
              stacked=False, money_axis=True):
    n = len(labels)
    if not n or not series:
        return Paragraph('No data for this period.', S['small'])
    data = [[float(v or 0) for v in s] for s in series]
    peak = max(sum(col) for col in zip(*data)) if stacked else max(max(s) for s in data)
    if peak <= 0:
        return Paragraph('No activity recorded for this period.', S['small'])

    d = Drawing(width, height)
    bc = VerticalBarChart()
    bc.x, bc.y = 38, 26
    bc.width, bc.height = width - 46, height - 36
    bc.data = data
    step = max(1, math.ceil(n / 14))
    bc.categoryAxis.categoryNames = [str(l) if i % step == 0 else '' for i, l in enumerate(labels)]
    bc.categoryAxis.labels.fontName = 'Helvetica'
    bc.categoryAxis.labels.fontSize = 6.5
    bc.categoryAxis.labels.fillColor = MUTED
    if n > 8:
        bc.categoryAxis.labels.angle = 35
        bc.categoryAxis.labels.boxAnchor = 'ne'
        bc.categoryAxis.labels.dy = -2
    bc.categoryAxis.strokeColor = LINE
    bc.categoryAxis.visibleTicks = 0
    if stacked:
        bc.categoryAxis.style = 'stacked'
    bc.valueAxis.valueMin = 0
    bc.valueAxis.labels.fontName = 'Helvetica'
    bc.valueAxis.labels.fontSize = 6.5
    bc.valueAxis.labels.fillColor = MUTED
    bc.valueAxis.labelTextFormat = _compact if money_axis else (lambda v: f"{v:g}")
    bc.valueAxis.visibleGrid = 1
    bc.valueAxis.gridStrokeColor = LINE
    bc.valueAxis.gridStrokeWidth = 0.4
    bc.valueAxis.visibleAxis = 0
    bc.valueAxis.visibleTicks = 0
    bc.groupSpacing = 3
    bc.barSpacing = 0.5
    bc.strokeColor = None
    for i in range(len(data)):
        bc.bars[i].fillColor = series_colors[i % len(series_colors)]
        bc.bars[i].strokeColor = None
    d.add(bc)
    return d


# ───────────────────────── document assembly ─────────────────────────

class _NumberedCanvas(rl_canvas.Canvas):
    footer_text = ''
    running_title = ''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_states = []

    def showPage(self):
        self._saved_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_states)
        for state in self._saved_states:
            self.__dict__.update(state)
            self._decorate(total)
            super().showPage()
        super().save()

    def _decorate(self, total):
        w, h = self._pagesize
        self.saveState()
        self.setStrokeColor(LINE)
        self.setLineWidth(0.5)
        self.line(MARGIN, 1.3 * cm, w - MARGIN, 1.3 * cm)
        self.setFont('Helvetica', 7.5)
        self.setFillColor(MUTED)
        self.drawString(MARGIN, 0.85 * cm, self.footer_text)
        self.drawRightString(w - MARGIN, 0.85 * cm, f"Page {self._pageNumber} of {total}")
        if self._pageNumber > 1:
            self.setFont('Helvetica-Bold', 8)
            self.setFillColor(INK)
            self.drawString(MARGIN, h - 1.0 * cm, self.running_title)
            self.line(MARGIN, h - 1.25 * cm, w - MARGIN, h - 1.25 * cm)
        self.restoreState()


def build_pdf(story, *, title, prof, running_title=None):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=1.8 * cm, bottomMargin=1.9 * cm,
        title=title, author=prof['name'], creator='Netily',
    )
    stamp = timezone.localtime().strftime('%d %b %Y, %H:%M')
    footer_value = f"{prof['name']}  |  Generated {stamp}"
    header_value = running_title or title

    class _Canvas(_NumberedCanvas):
        footer_text = footer_value
        running_title = header_value

    doc.build(story, canvasmaker=_Canvas)
    return buf.getvalue()