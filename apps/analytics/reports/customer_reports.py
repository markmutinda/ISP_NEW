from datetime import datetime, timedelta
from django.db.models import Count, Sum, Avg, Q, F
from django.utils import timezone
from apps.customers.models import Customer
from apps.billing.models import Invoice, Payment
from apps.support.models import SupportTicket


class CustomerReports:
    @staticmethod
    def acquisition_report(start_date, end_date):
        """
        Generate customer acquisition report
        """
        new_customers = Customer.objects.filter(
            created_at__gte=start_date,
            created_at__lte=end_date
        )
        
        # Acquisition by source
        acquisition_by_source = new_customers.values('source').annotate(
            count=Count('id'),
            percentage=Count('id') * 100 / new_customers.count() if new_customers.count() > 0 else 0
        )
        
        # Acquisition by plan
        acquisition_by_plan = new_customers.values('plan__name').annotate(
            count=Count('id'),
            percentage=Count('id') * 100 / new_customers.count() if new_customers.count() > 0 else 0
        )
        
        # Daily signups
        daily_signups = new_customers.annotate(
            date=models.functions.TruncDate('created_at')
        ).values('date').annotate(
            count=Count('id')
        ).order_by('date')
        
        # Monthly trend
        monthly_trend = Customer.objects.annotate(
            month=models.functions.TruncMonth('created_at')
        ).values('month').annotate(
            count=Count('id')
        ).order_by('month')
        
        return {
            'period': {'start': start_date, 'end': end_date},
            'total_acquisitions': new_customers.count(),
            'acquisition_by_source': list(acquisition_by_source),
            'acquisition_by_plan': list(acquisition_by_plan),
            'daily_signups': list(daily_signups),
            'monthly_trend': list(monthly_trend),
        }
    
    @staticmethod
    def churn_report(start_date, end_date):
        """
        Generate customer churn analysis report
        """
        churned_customers = Customer.objects.filter(
            status='terminated',
            termination_date__gte=start_date,
            termination_date__lte=end_date
        )
        
        # Churn rate calculation
        total_customers_start = Customer.objects.filter(
            created_at__lt=start_date
        ).count()
        
        churn_rate = (churned_customers.count() / total_customers_start * 100) if total_customers_start > 0 else 0
        
        # Churn reasons
        churn_by_reason = churned_customers.values('termination_reason').annotate(
            count=Count('id'),
            percentage=Count('id') * 100 / churned_customers.count() if churned_customers.count() > 0 else 0
        )
        
        # Churn by tenure
        churn_by_tenure = []
        for customer in churned_customers:
            tenure = (customer.termination_date - customer.created_at).days
            churn_by_tenure.append({
                'customer_id': customer.id,
                'customer_name': customer.name,
                'tenure_days': tenure,
                'plan': customer.plan.name if customer.plan else None,
            })
        
        # Retention by plan
        retention_by_plan = Customer.objects.values('plan__name').annotate(
            total=Count('id'),
            active=Count('id', filter=Q(status='active')),
            retention_rate=Count('id', filter=Q(status='active')) * 100 / Count('id')
        )
        
        return {
            'period': {'start': start_date, 'end': end_date},
            'total_churned': churned_customers.count(),
            'churn_rate': churn_rate,
            'churn_by_reason': list(churn_by_reason),
            'churn_by_tenure': churn_by_tenure,
            'retention_by_plan': list(retention_by_plan),
            'lifetime_value_lost': churned_customers.aggregate(
                total=Sum('plan__price') * 12
            )['total'] or 0,  # Assuming annual revenue lost
        }
    
    @staticmethod
    def satisfaction_report(start_date, end_date):
        """
        Generate customer satisfaction metrics
        """
        # Ticket resolution metrics
        tickets = Ticket.objects.filter(
            created_at__gte=start_date,
            created_at__lte=end_date
        )
        
        resolution_metrics = tickets.aggregate(
            total=Count('id'),
            resolved=Count('id', filter=Q(status='resolved')),
            avg_resolution_time=Avg(F('resolved_at') - F('created_at'))
        )
        
        # First response time
        first_response_metrics = tickets.filter(
            first_response_at__isnull=False
        ).aggregate(
            avg_first_response=Avg(F('first_response_at') - F('created_at'))
        )
        
        # Customer feedback (assuming you have a feedback system)
        feedback_data = {
            'average_rating': 4.2,  # Replace with actual data
            'total_feedback': 150,
            'positive_feedback': 120,
            'negative_feedback': 30,
        }
        
        # NPS Calculation (simplified)
        nps_data = {
            'promoters': 75,
            'passives': 20,
            'detractors': 5,
            'nps_score': 70,  # Promoters % - Detractors %
        }
        
        return {
            'period': {'start': start_date, 'end': end_date},
            'ticket_metrics': {
                'total_tickets': resolution_metrics['total'],
                'resolved_tickets': resolution_metrics['resolved'],
                'resolution_rate': (resolution_metrics['resolved'] / resolution_metrics['total'] * 100) if resolution_metrics['total'] > 0 else 0,
                'avg_resolution_hours': resolution_metrics['avg_resolution_time'].total_seconds() / 3600 if resolution_metrics['avg_resolution_time'] else 0,
                'avg_first_response_hours': first_response_metrics['avg_first_response'].total_seconds() / 3600 if first_response_metrics['avg_first_response'] else 0,
            },
            'feedback_metrics': feedback_data,
            'nps_metrics': nps_data,
            'csat_score': 85,  # Customer Satisfaction Score
        }