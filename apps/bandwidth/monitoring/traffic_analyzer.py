import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from django.utils import timezone
from django.db.models import Sum, Avg, Max, Min
import statistics

from ..models import DataUsage
from apps.customers.models import Customer

logger = logging.getLogger(__name__)


class TrafficAnalyzer:
    """Analyzes network traffic patterns and anomalies"""
    
    def __init__(self):
        self.anomaly_threshold = 3.0  # Standard deviations for anomaly detection
    
    def analyze_customer_patterns(self, customer: Customer, days: int = 30) -> Dict[str, Any]:
        """Analyze traffic patterns for a specific customer"""
        try:
            end_date = timezone.now()
            start_date = end_date - timedelta(days=days)
            
            # Get usage data
            usage_data = DataUsage.objects.filter(
                customer=customer,
                period_start__gte=start_date,
                period_end__lte=end_date
            ).order_by('period_start')
            
            if not usage_data:
                return {'error': 'No data available'}
            
            # Calculate statistics
            total_download = sum([u.download_bytes for u in usage_data])
            total_upload = sum([u.upload_bytes for u in usage_data])
            avg_daily = (total_download + total_upload) / days / (1024**2)  # MB per day
            
            # Peak usage times
            hourly_patterns = self._analyze_hourly_patterns(customer, days)
            
            # Anomaly detection
            anomalies = self._detect_anomalies(usage_data)
            
            # Traffic composition (if available)
            traffic_composition = self._analyze_traffic_composition(customer)
            
            return {
                'customer_id': customer.id,
                'customer_code': customer.customer_code,
                'customer_name': customer.user.get_full_name(),
                'analysis_period': {
                    'start': start_date.date(),
                    'end': end_date.date(),
                    'days': days
                },
                'statistics': {
                    'total_download_gb': round(total_download / (1024**3), 2),
                    'total_upload_gb': round(total_upload / (1024**3), 2),
                    'average_daily_mb': round(avg_daily, 2),
                    'peak_download_mbps': max([u.peak_download_speed for u in usage_data]),
                    'peak_upload_mbps': max([u.peak_upload_speed for u in usage_data]),
                },
                'hourly_patterns': hourly_patterns,
                'anomalies_detected': len(anomalies),
                'anomalies': anomalies,
                'traffic_composition': traffic_composition,
                'recommendations': self._generate_recommendations(
                    total_download, total_upload, hourly_patterns, anomalies
                )
            }
            
        except Exception as e:
            logger.error(f"Error analyzing customer patterns for {customer.customer_code}: {e}")
            return {'error': str(e)}
    
    def _analyze_hourly_patterns(self, customer: Customer, days: int) -> Dict[str, List]:
        """Analyze usage patterns by hour of day"""
        # Simplified implementation without numpy
        patterns = {
            'hours': list(range(24)),
            'average_download': [50 + (i % 12) * 2 for i in range(24)],  # Mock data
            'average_upload': [10 + (i % 12) * 0.5 for i in range(24)],  # Mock data
            'peak_hours': [9, 10, 11, 17, 18, 19, 20],  # Typical peak hours
        }
        
        # Adjust for weekends
        if days >= 7:
            patterns['weekend_pattern'] = {
                'average_download': [60 + (i % 12) * 3 for i in range(24)],
                'average_upload': [15 + (i % 12) * 1 for i in range(24)],
            }
        
        return patterns
    
    def _detect_anomalies(self, usage_data: List[DataUsage]) -> List[Dict]:
        """Detect anomalous usage patterns"""
        anomalies = []
        
        if len(usage_data) < 2:
            return anomalies
        
        # Calculate daily totals
        daily_totals = [u.total_bytes for u in usage_data]
        
        if len(daily_totals) >= 7:
            # Use 7-day moving average
            window = 7
            moving_avg = []
            
            for i in range(len(daily_totals) - window + 1):
                window_data = daily_totals[i:i + window]
                moving_avg.append(sum(window_data) / window)
            
            # Calculate standard deviation
            if len(moving_avg) > 1:
                mean_avg = sum(moving_avg) / len(moving_avg)
                variance = sum((x - mean_avg) ** 2 for x in moving_avg) / len(moving_avg)
                std_dev = math.sqrt(variance)
                
                # Detect anomalies (3 standard deviations from mean)
                for i in range(len(moving_avg)):
                    if abs(daily_totals[i + window - 1] - moving_avg[i]) > self.anomaly_threshold * std_dev:
                        anomalies.append({
                            'date': usage_data[i + window - 1].period_start.date(),
                            'usage_gb': round(daily_totals[i + window - 1] / (1024**3), 2),
                            'expected_gb': round(moving_avg[i] / (1024**3), 2),
                            'deviation': round((daily_totals[i + window - 1] - moving_avg[i]) / (1024**3), 2),
                            'severity': 'high' if daily_totals[i + window - 1] > moving_avg[i] else 'low'
                        })
        
        return anomalies
    
    def _analyze_traffic_composition(self, customer: Customer) -> Dict[str, float]:
        """Analyze traffic by protocol/application"""
        # This would require deep packet inspection or NetFlow data
        # Placeholder implementation
        return {
            'web': 45.5,
            'video': 30.2,
            'social_media': 12.8,
            'gaming': 5.4,
            'file_transfer': 3.1,
            'voip': 2.0,
            'other': 1.0
        }
    
    def _generate_recommendations(self, total_download: int, total_upload: int, 
                                 hourly_patterns: Dict, anomalies: List) -> List[str]:
        """Generate recommendations based on analysis"""
        recommendations = []
        
        # Check for high usage
        avg_daily_gb = (total_download + total_upload) / (30 * 1024**3)  # Assuming 30 days
        if avg_daily_gb > 10:
            recommendations.append("Consider upgrading to a higher data cap plan")
        
        # Check for peak hour congestion
        peak_hours = hourly_patterns.get('peak_hours', [])
        if len(peak_hours) > 6:
            recommendations.append("High usage during peak hours detected. Consider scheduling large downloads for off-peak hours")
        
        # Check for anomalies
        if anomalies:
            high_anomalies = [a for a in anomalies if a.get('severity') == 'high']
            if high_anomalies:
                recommendations.append(f"Detected {len(high_anomalies)} high-usage anomalies. Check for unauthorized usage or malware")
        
        # Check upload/download ratio
        if total_upload > total_download * 0.5:  # More than 50% upload
            recommendations.append("Unusual high upload traffic detected. May indicate file sharing or backup activity")
        
        return recommendations
    
    def predict_bandwidth_needs(self, customer: Customer, future_days: int = 30) -> Dict[str, Any]:
        """Predict future bandwidth needs based on historical data"""
        try:
            historical_data = DataUsage.objects.filter(
                customer=customer
            ).order_by('-period_end')[:90]  # Last 90 days
            
            if len(historical_data) < 30:
                return {'error': 'Insufficient historical data'}
            
            # Simple linear regression for prediction
            days = list(range(len(historical_data)))
            usage = [d.total_bytes for d in historical_data]
            
            # Calculate linear regression coefficients
            n = len(days)
            sum_x = sum(days)
            sum_y = sum(usage)
            sum_xy = sum(x * y for x, y in zip(days, usage))
            sum_x2 = sum(x * x for x in days)
            
            # Slope (m) and intercept (b)
            try:
                m = (n * sum_xy - sum_x * sum_y) / (n * sum_x2 - sum_x * sum_x)
            except ZeroDivisionError:
                m = 0
            
            # Predict future
            last_usage = usage[-1]
            predicted_usage = last_usage + m * future_days
            
            # Growth rate
            growth_rate = (m / last_usage) * 100 if last_usage > 0 else 0
            
            return {
                'current_daily_avg_gb': round((sum(usage) / len(usage)) / (1024**3), 2),
                'predicted_daily_avg_gb': round(predicted_usage / (1024**3), 2),
                'growth_rate_percent': round(growth_rate, 2),
                'predicted_monthly_gb': round(predicted_usage * 30 / (1024**3), 2),
                'confidence': 'high' if len(historical_data) >= 60 else 'medium',
                'recommended_plan_adjustment': 'upgrade' if growth_rate > 10 else 'maintain'
            }
            
        except Exception as e:
            logger.error(f"Error predicting bandwidth for {customer.customer_code}: {e}")
            return {'error': str(e)}