from datetime import datetime, timedelta
from django.db.models import Sum, Count, Avg, Q, F
from django.utils import timezone
from apps.network.models import OLTDevice, CPEDevice
from apps.bandwidth.models import DataUsage
from utils.helpers import calculate_uptime_percentage


class NetworkReports:
    @staticmethod
    def uptime_report(start_date, end_date):
        """
        Generate network uptime report
        """
        olts = OLTDevice.objects.all()
        
        uptime_data = []
        for olt in olts:
            uptime = calculate_uptime_percentage(olt, start_date, end_date)
            uptime_data.append({
                'device_name': olt.name,
                'device_type': 'OLT',
                'ip_address': olt.ip_address,
                'uptime_percentage': uptime,
                'status': 'up' if uptime >= 99 else 'degraded' if uptime >= 95 else 'down',
                'last_check': olt.last_check,
            })
        
        # CPE devices uptime
        cpe_devices = CPEDevice.objects.filter(
            last_seen__gte=start_date,
            last_seen__lte=end_date
        )
        
        cpe_uptime = cpe_devices.aggregate(
            total=Count('id'),
            online=Count('id', filter=Q(status='online')),
            offline=Count('id', filter=Q(status='offline'))
        )
        
        cpe_uptime_percentage = (cpe_uptime['online'] / cpe_uptime['total'] * 100) if cpe_uptime['total'] > 0 else 0
        
        return {
            'period': {'start': start_date, 'end': end_date},
            'olt_uptime': uptime_data,
            'cpe_statistics': {
                'total_devices': cpe_uptime['total'],
                'online_devices': cpe_uptime['online'],
                'offline_devices': cpe_uptime['offline'],
                'uptime_percentage': cpe_uptime_percentage,
            },
            'overall_uptime': sum(item['uptime_percentage'] for item in uptime_data) / len(uptime_data) if uptime_data else 0,
        }
    
    @staticmethod
    def bandwidth_report(start_date, end_date):
        """
        Generate bandwidth utilization report
        """
        # Get bandwidth usage data
        usage_data = DataUsage.objects.filter(
            timestamp__gte=start_date,
            timestamp__lte=end_date
        )
        
        # Aggregate by hour
        hourly_usage = usage_data.values(
            hour=models.functions.TruncHour('timestamp')
        ).annotate(
            total_upload=Sum('upload_bytes'),
            total_download=Sum('download_bytes')
        ).order_by('hour')
        
        # Peak usage times
        peak_hours = hourly_usage.annotate(
            total_traffic=F('total_upload') + F('total_download')
        ).order_by('-total_traffic')[:10]
        
        # Top bandwidth consumers
        top_consumers = DataUsage.objects.filter(
            timestamp__gte=start_date,
            timestamp__lte=end_date
        ).values('customer__name', 'customer__customer_code').annotate(
            total_upload=Sum('upload_bytes'),
            total_download=Sum('download_bytes'),
            total_traffic=Sum('upload_bytes') + Sum('download_bytes')
        ).order_by('-total_traffic')[:20]
        
        # Bandwidth by OLT
        bandwidth_by_olt = DataUsage.objects.filter(
            timestamp__gte=start_date,
            timestamp__lte=end_date
        ).values('olt__name').annotate(
            total_traffic=Sum('upload_bytes') + Sum('download_bytes'),
            avg_speed=Avg('current_speed')
        )
        
        return {
            'period': {'start': start_date, 'end': end_date},
            'hourly_usage': list(hourly_usage),
            'peak_hours': list(peak_hours),
            'top_consumers': list(top_consumers),
            'bandwidth_by_olt': list(bandwidth_by_olt),
            'total_traffic': usage_data.aggregate(
                total=Sum('upload_bytes') + Sum('download_bytes')
            )['total'] or 0,
        }
    
    @staticmethod
    def device_report():
        """
        Generate device inventory and status report
        """
        # OLT devices
        olt_devices = OLTDevice.objects.values('vendor', 'model', 'status').annotate(
            count=Count('id')
        ).order_by('vendor')
        
        # CPE devices
        cpe_devices = CPEDevice.objects.values('device_type', 'status').annotate(
            count=Count('id')
        ).order_by('device_type')
        
        # Device health
        total_devices = OLTDevice.objects.count() + CPEDevice.objects.count()
        online_devices = OLTDevice.objects.filter(status='online').count() + \
                        CPEDevice.objects.filter(status='online').count()
        
        device_health_percentage = (online_devices / total_devices * 100) if total_devices > 0 else 0
        
        return {
            'timestamp': timezone.now(),
            'olt_devices': list(olt_devices),
            'cpe_devices': list(cpe_devices),
            'device_health': {
                'total_devices': total_devices,
                'online_devices': online_devices,
                'offline_devices': total_devices - online_devices,
                'health_percentage': device_health_percentage,
            },
        }
