from datetime import timedelta
from django.utils import timezone
from django.db.models import Q
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from ..permissions import CustomerOnlyPermission
from apps.radius.models import RadAcct

class UsageView(APIView):
    """
    Customer usage data and analytics directly from live RADIUS Accounting.
    """
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    
    def get(self, request):
        customer = request.user.customer_profile
        
        # 1. Determine if they have a data limit from their active plan
        active_service = customer.services.filter(status__in=['ACTIVE', 'SUSPENDED']).first()
        data_limit_gb = None
        if active_service and active_service.plan:
            # Assuming data_limit is stored in GB on the plan model
            data_limit_gb = getattr(active_service.plan, 'data_limit', None)

        # 2. Get their RADIUS username (fallback for robust querying)
        radius_username = None
        if hasattr(customer, 'radius_credentials'):
            radius_username = customer.radius_credentials.username

        # 3. Fetch all sessions from the last 30 days from the LIVE RadAcct table
        start_date = timezone.now() - timedelta(days=30)
        
        # Query by Customer FK, or by Username just to be absolutely safe
        query = Q(customer=customer)
        if radius_username:
            query |= Q(username=radius_username)
            
        sessions = RadAcct.objects.filter(query, acctstarttime__gte=start_date).order_by('-acctstarttime')

        # 4. Calculate exact real-time totals
        # Note: In RADIUS, 'output' = NAS sending to User (Download), 'input' = User sending to NAS (Upload)
        total_download_bytes = sum(session.acctoutputoctets or 0 for session in sessions)
        total_upload_bytes = sum(session.acctinputoctets or 0 for session in sessions)
        total_bytes = total_download_bytes + total_upload_bytes

        used_gb = round(total_bytes / (1024**3), 2)
        download_gb = round(total_download_bytes / (1024**3), 2)
        upload_gb = round(total_upload_bytes / (1024**3), 2)

        # 5. Calculate data cap percentage
        percentage = 0
        if data_limit_gb and data_limit_gb > 0:
            percentage = min(100, (used_gb / data_limit_gb) * 100)

        # 6. Format Top 10 Recent Sessions for the Frontend Table
        recent_sessions = []
        for s in sessions[:10]:
            down_mb = round((s.acctoutputoctets or 0) / (1024**2), 2)
            up_mb = round((s.acctinputoctets or 0) / (1024**2), 2)
            
            recent_sessions.append({
                'id': s.radacctid,
                'start_time': s.acctstarttime.isoformat() if s.acctstarttime else None,
                'stop_time': s.acctstoptime.isoformat() if s.acctstoptime else None,
                'download': f"{down_mb} MB" if down_mb < 1024 else f"{round(down_mb/1024, 2)} GB",
                'upload': f"{up_mb} MB" if up_mb < 1024 else f"{round(up_mb/1024, 2)} GB",
                'duration': s.duration_formatted,
                'nas_ip': s.nasipaddress
            })

        # 7. Return exactly what the React frontend expects
        return Response({
            'data_used': f"{used_gb} GB",
            'data_limit': f"{data_limit_gb} GB" if data_limit_gb else None,
            'percentage': percentage,
            'download_total': f"{download_gb} GB",
            'upload_total': f"{upload_gb} GB",
            'sessions': recent_sessions
        })