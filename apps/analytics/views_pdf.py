import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.pdf_reports import pdf_response
from apps.core.permissions import HasRoleAccessPolicy, IsAdminOrStaff

from .frontend_contract_views import AnalyticsReportsView, DailyRevenueSplitView
from .pdf_report import build_business_report_pdf

logger = logging.getLogger(__name__)


class AnalyticsReportPDFView(APIView):
    """GET /api/v1/analytics/reports-pdf/?time_range=7d|30d|90d
    Reuses the cached payloads behind the Reports page, so it adds almost no DB load."""
    permission_classes = [IsAuthenticated, IsAdminOrStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/analytics"

    def get(self, request):
        time_range = request.query_params.get('time_range', '30d')

        resp = AnalyticsReportsView().get(request)
        if resp.status_code != 200:
            return resp
        payload = resp.data

        split = {}
        try:
            sresp = DailyRevenueSplitView().get(request)
            if sresp.status_code == 200:
                split = sresp.data
        except Exception:
            logger.warning("Revenue split unavailable for PDF", exc_info=True)

        try:
            pdf = build_business_report_pdf(request, payload, split, time_range)
        except Exception:
            logger.exception("Business report PDF failed")
            return Response({'detail': 'Could not generate the report. Please try again.'}, status=500)

        return pdf_response(pdf, f"business-report-{time_range}.pdf")