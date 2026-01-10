"""
Individual analytics endpoints - Thin wrappers around dashboard methods
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAdminUser
from django.http import HttpResponse
import csv
import json
from io import StringIO
from .views_v1 import AnalyticsDashboardView


class AnalyticsKPIsView(APIView):
    permission_classes = [IsAdminUser]
    
    def get(self, request):
        time_range = request.query_params.get('time_range', '30d')
        dashboard = AnalyticsDashboardView()
        start_date = dashboard.get_date_range(time_range)
        kpis = dashboard.get_kpis(start_date)
        return Response(kpis)


class AnalyticsRevenueView(APIView):
    permission_classes = [IsAdminUser]
    
    def get(self, request):
        time_range = request.query_params.get('time_range', '30d')
        dashboard = AnalyticsDashboardView()
        start_date = dashboard.get_date_range(time_range)
        revenue_data = dashboard.get_revenue_data(start_date)
        return Response(revenue_data)


class AnalyticsUserGrowthView(APIView):
    permission_classes = [IsAdminUser]
    
    def get(self, request):
        time_range = request.query_params.get('time_range', '30d')
        dashboard = AnalyticsDashboardView()
        start_date = dashboard.get_date_range(time_range)
        growth_data = dashboard.get_user_growth_data(start_date)
        return Response(growth_data)


class AnalyticsPlanPerformanceView(APIView):
    permission_classes = [IsAdminUser]
    
    def get(self, request):
        time_range = request.query_params.get('time_range', '30d')
        dashboard = AnalyticsDashboardView()
        start_date = dashboard.get_date_range(time_range)
        plan_data = dashboard.get_plan_performance(start_date)
        return Response(plan_data)


class AnalyticsLocationsView(APIView):
    permission_classes = [IsAdminUser]
    
    def get(self, request):
        time_range = request.query_params.get('time_range', '30d')
        dashboard = AnalyticsDashboardView()
        start_date = dashboard.get_date_range(time_range)
        location_data = dashboard.get_location_analytics(start_date)
        return Response(location_data)


class AnalyticsRoutersView(APIView):
    permission_classes = [IsAdminUser]
    
    def get(self, request):
        router_data = AnalyticsDashboardView().get_router_analytics()
        return Response(router_data)


class AnalyticsPaymentMethodsView(APIView):
    permission_classes = [IsAdminUser]
    
    def get(self, request):
        time_range = request.query_params.get('time_range', '30d')
        dashboard = AnalyticsDashboardView()
        start_date = dashboard.get_date_range(time_range)
        payment_methods = dashboard.get_payment_methods(start_date)
        return Response(payment_methods)


class AnalyticsPaymentStatsView(APIView):
    permission_classes = [IsAdminUser]
    
    def get(self, request):
        time_range = request.query_params.get('time_range', '30d')
        dashboard = AnalyticsDashboardView()
        start_date = dashboard.get_date_range(time_range)
        payment_stats = dashboard.get_payment_stats(start_date)
        return Response(payment_stats)


class AnalyticsUserDistributionView(APIView):
    permission_classes = [IsAdminUser]
    
    def get(self, request):
        user_dist = AnalyticsDashboardView().get_user_distribution()
        return Response(user_dist)


class AnalyticsRevenueByTypeView(APIView):
    permission_classes = [IsAdminUser]
    
    def get(self, request):
        time_range = request.query_params.get('time_range', '30d')
        dashboard = AnalyticsDashboardView()
        start_date = dashboard.get_date_range(time_range)
        revenue_by_type = dashboard.get_revenue_by_type(start_date)
        return Response(revenue_by_type)


class AnalyticsRevenueForecastView(APIView):
    permission_classes = [IsAdminUser]
    
    def get(self, request):
        forecast = AnalyticsDashboardView().get_revenue_forecast()
        return Response(forecast)


class AnalyticsRevenueTargetView(APIView):
    permission_classes = [IsAdminUser]
    
    def get(self, request):
        target = AnalyticsDashboardView().get_revenue_target()
        return Response(target)


class AnalyticsNetworkStatsView(APIView):
    permission_classes = [IsAdminUser]
    
    def get(self, request):
        network_stats = AnalyticsDashboardView().get_network_stats()
        return Response(network_stats)


class AnalyticsExportView(APIView):
    permission_classes = [IsAdminUser]
    
    def get(self, request):
        time_range = request.query_params.get('time_range', '30d')
        format_type = request.query_params.get('format', 'csv')
        
        # Get dashboard data
        dashboard = AnalyticsDashboardView()
        start_date = dashboard.get_date_range(time_range)
        
        if format_type == 'csv':
            return self.export_csv(dashboard, start_date, time_range)
        elif format_type == 'json':
            return self.export_json(dashboard, start_date, time_range)
        else:
            return Response({'error': 'Unsupported format'}, status=400)
    
    def export_csv(self, dashboard, start_date, time_range):
        """Export analytics data as CSV"""
        output = StringIO()
        writer = csv.writer(output)
        
        # Write header
        writer.writerow(['ISP Analytics Report', f'Time Range: {time_range}'])
        writer.writerow(['Generated', timezone.now().strftime('%Y-%m-%d %H:%M:%S')])
        writer.writerow([])
        
        # Write KPIs
        writer.writerow(['Key Performance Indicators'])
        writer.writerow(['Metric', 'Value'])
        kpis = dashboard.get_kpis(start_date)
        for key, value in kpis.items():
            writer.writerow([key.replace('_', ' ').title(), value])
        
        writer.writerow([])
        
        # Write Revenue Data
        writer.writerow(['Monthly Revenue Data'])
        writer.writerow(['Month', 'Revenue', 'Target', 'Users'])
        revenue_data = dashboard.get_revenue_data(start_date)
        for item in revenue_data:
            writer.writerow([item['month'], item['revenue'], item['target'], item['users']])
        
        writer.writerow([])
        
        # Write User Growth
        writer.writerow(['User Growth Data'])
        writer.writerow(['Month', 'New Users', 'Churn', 'Net Growth'])
        growth_data = dashboard.get_user_growth_data(start_date)
        for item in growth_data:
            writer.writerow([item['month'], item['new_users'], item['churn'], item['net_growth']])
        
        output.seek(0)
        response = HttpResponse(output, content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename=analytics_{time_range}_{timezone.now().date()}.csv'
        return response
    
    def export_json(self, dashboard, start_date, time_range):
        """Export analytics data as JSON"""
        data = {
            'time_range': time_range,
            'generated_at': timezone.now().isoformat(),
            'kpis': dashboard.get_kpis(start_date),
            'revenue_data': dashboard.get_revenue_data(start_date),
            'user_growth_data': dashboard.get_user_growth_data(start_date),
            'plan_performance': dashboard.get_plan_performance(start_date),
            'payment_methods': dashboard.get_payment_methods(start_date),
            'payment_stats': dashboard.get_payment_stats(start_date),
        }
        
        response = HttpResponse(
            json.dumps(data, indent=2),
            content_type='application/json'
        )
        response['Content-Disposition'] = f'attachment; filename=analytics_{time_range}_{timezone.now().date()}.json'
        return response