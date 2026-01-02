from decimal import Decimal
from datetime import datetime, timedelta
from django.utils import timezone
from django.db.models import Sum, Q, Count
from apps.customers.models import Customer
from ..models.payment_models import Payment
from ..models.billing_models import Invoice


class CommissionCalculator:
    @staticmethod
    def calculate_sales_agent_commission(agent, start_date, end_date):
        """Calculate commission for sales agent based on customer acquisitions"""
        # Get customers acquired by this agent within period
        customers = Customer.objects.filter(
            created_by=agent,
            created_at__range=[start_date, end_date],
            status='ACTIVE'
        )
        
        total_commission = Decimal('0')
        commission_details = []
        
        for customer in customers:
            # Calculate commission based on customer type and initial payments
            if customer.customer_type == 'RESIDENTIAL':
                commission_rate = Decimal('2.0')  # 2% for residential
            elif customer.customer_type == 'BUSINESS':
                commission_rate = Decimal('3.0')  # 3% for business
            elif customer.customer_type == 'CORPORATE':
                commission_rate = Decimal('5.0')  # 5% for corporate
            else:
                commission_rate = Decimal('1.5')  # 1.5% for others
            
            # Get initial payments from customer
            initial_payments = Payment.objects.filter(
                customer=customer,
                payment_date__range=[start_date, end_date],
                status='COMPLETED'
            ).aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
            
            commission_amount = (initial_payments * commission_rate) / 100
            
            total_commission += commission_amount
            
            commission_details.append({
                'customer': customer.customer_code,
                'customer_type': customer.customer_type,
                'total_payments': initial_payments,
                'commission_rate': commission_rate,
                'commission_amount': commission_amount
            })
        
        return {
            'agent': agent.get_full_name(),
            'period': f"{start_date} to {end_date}",
            'total_customers': customers.count(),
            'total_commission': total_commission.quantize(Decimal('0.01')),
            'details': commission_details
        }

    @staticmethod
    def calculate_referral_commission(referrer, start_date, end_date):
        """Calculate commission for customer referrals"""
        # Get referred customers who became active within period
        referred_customers = Customer.objects.filter(
            referred_by=referrer,
            activation_date__range=[start_date, end_date],
            status='ACTIVE'
        )
        
        total_commission = Decimal('0')
        commission_details = []
        
        for customer in referred_customers:
            # Fixed referral bonus per customer type
            if customer.customer_type == 'RESIDENTIAL':
                referral_bonus = Decimal('500')  # KES 500 for residential
            elif customer.customer_type == 'BUSINESS':
                referral_bonus = Decimal('1000')  # KES 1000 for business
            elif customer.customer_type == 'CORPORATE':
                referral_bonus = Decimal('2000')  # KES 2000 for corporate
            else:
                referral_bonus = Decimal('300')   # KES 300 for others
            
            # Additional commission based on first payment
            first_payment = Payment.objects.filter(
                customer=customer,
                status='COMPLETED'
            ).order_by('payment_date').first()
            
            if first_payment:
                payment_commission = (first_payment.amount * Decimal('1.0')) / 100  # 1% of first payment
                total_customer_commission = referral_bonus + payment_commission
            else:
                total_customer_commission = referral_bonus
            
            total_commission += total_customer_commission
            
            commission_details.append({
                'customer': customer.customer_code,
                'customer_type': customer.customer_type,
                'referral_bonus': referral_bonus,
                'payment_commission': payment_commission if first_payment else Decimal('0'),
                'total_commission': total_customer_commission
            })
        
        return {
            'referrer': referrer.get_full_name(),
            'period': f"{start_date} to {end_date}",
            'total_referrals': referred_customers.count(),
            'total_commission': total_commission.quantize(Decimal('0.01')),
            'details': commission_details
        }

    @staticmethod
    def calculate_collection_commission(collector, start_date, end_date):
        """Calculate commission for payment collection agents"""
        # Get payments collected by this agent within period
        collected_payments = Payment.objects.filter(
            processed_by=collector,
            processed_at__range=[start_date, end_date],
            status='COMPLETED'
        )
        
        total_collected = collected_payments.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        
        # Commission based on collection amount tiers
        if total_collected <= Decimal('100000'):  # Up to 100K
            commission_rate = Decimal('1.5')
        elif total_collected <= Decimal('500000'):  # 100K to 500K
            commission_rate = Decimal('2.0')
        elif total_collected <= Decimal('1000000'):  # 500K to 1M
            commission_rate = Decimal('2.5')
        else:  # Above 1M
            commission_rate = Decimal('3.0')
        
        total_commission = (total_collected * commission_rate) / 100
        
        # Bonus for collecting overdue payments
        overdue_payments = collected_payments.filter(
            invoice__status='OVERDUE'
        ).aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        
        overdue_bonus = (overdue_payments * Decimal('0.5')) / 100  # 0.5% bonus for overdue
        
        total_commission += overdue_bonus
        
        return {
            'collector': collector.get_full_name(),
            'period': f"{start_date} to {end_date}",
            'total_collected': total_collected.quantize(Decimal('0.01')),
            'total_payments': collected_payments.count(),
            'overdue_collected': overdue_payments.quantize(Decimal('0.01')),
            'commission_rate': commission_rate,
            'base_commission': (total_collected * commission_rate / 100).quantize(Decimal('0.01')),
            'overdue_bonus': overdue_bonus.quantize(Decimal('0.01')),
            'total_commission': total_commission.quantize(Decimal('0.01'))
        }

    @staticmethod
    def calculate_retention_commission(support_agent, start_date, end_date):
        """Calculate commission for customer retention/support agents"""
        # Get customers retained (not churned) by this agent
        # This would require tracking which support agents handled which customers
        # For now, using a simplified approach
        
        # Get tickets resolved by this agent that resulted in customer retention
        from support.models import Ticket  # You'll need to import this later
        
        try:
            resolved_tickets = Ticket.objects.filter(
                assigned_to=support_agent,
                resolved_at__range=[start_date, end_date],
                status='RESOLVED',
                priority__in=['HIGH', 'URGENT']
            )
            
            retention_bonus = resolved_tickets.count() * Decimal('200')  # KES 200 per critical ticket resolved
            
            # Bonus for preventing churn (customer stayed active after ticket)
            prevented_churn_count = resolved_tickets.filter(
                customer__status='ACTIVE'
            ).count()
            
            churn_prevention_bonus = prevented_churn_count * Decimal('500')  # KES 500 per churn prevented
            
            total_commission = retention_bonus + churn_prevention_bonus
            
            return {
                'support_agent': support_agent.get_full_name(),
                'period': f"{start_date} to {end_date}",
                'tickets_resolved': resolved_tickets.count(),
                'critical_tickets': resolved_tickets.filter(priority__in=['HIGH', 'URGENT']).count(),
                'churn_prevented': prevented_churn_count,
                'retention_bonus': retention_bonus.quantize(Decimal('0.01')),
                'churn_prevention_bonus': churn_prevention_bonus.quantize(Decimal('0.01')),
                'total_commission': total_commission.quantize(Decimal('0.01'))
            }
        except:
            # Support app not yet implemented
            return {
                'support_agent': support_agent.get_full_name(),
                'period': f"{start_date} to {end_date}",
                'total_commission': Decimal('0')
            }

    @staticmethod
    def generate_commission_report(company, month, year):
        """Generate monthly commission report for all staff"""
        start_date = datetime(year, month, 1)
        if month == 12:
            end_date = datetime(year + 1, 1, 1) - timedelta(days=1)
        else:
            end_date = datetime(year, month + 1, 1) - timedelta(days=1)
        
        # Get all staff in the company
        from core.models import User
        staff_members = User.objects.filter(
            company=company,
            is_staff=True,
            is_active=True
        )
        
        report = {
            'company': company.name,
            'period': f"{start_date.strftime('%B %Y')}",
            'total_commission': Decimal('0'),
            'staff_commissions': []
        }
        
        for staff in staff_members:
            # Calculate different types of commissions based on role
            staff_commission = Decimal('0')
            commission_details = {}
            
            # Sales commission for sales agents
            if staff.groups.filter(name='Sales Agents').exists():
                sales_commission = CommissionCalculator.calculate_sales_agent_commission(
                    staff, start_date, end_date
                )
                staff_commission += sales_commission['total_commission']
                commission_details['sales'] = sales_commission
            
            # Collection commission for collectors
            if staff.groups.filter(name='Collection Agents').exists():
                collection_commission = CommissionCalculator.calculate_collection_commission(
                    staff, start_date, end_date
                )
                staff_commission += collection_commission['total_commission']
                commission_details['collection'] = collection_commission
            
            # Retention commission for support staff
            if staff.groups.filter(name='Support Staff').exists():
                retention_commission = CommissionCalculator.calculate_retention_commission(
                    staff, start_date, end_date
                )
                staff_commission += retention_commission['total_commission']
                commission_details['retention'] = retention_commission
            
            # Referral commission for all staff
            referral_commission = CommissionCalculator.calculate_referral_commission(
                staff, start_date, end_date
            )
            staff_commission += referral_commission['total_commission']
            commission_details['referral'] = referral_commission
            
            if staff_commission > 0:
                report['staff_commissions'].append({
                    'staff': staff.get_full_name(),
                    'email': staff.email,
                    'total_commission': staff_commission.quantize(Decimal('0.01')),
                    'details': commission_details
                })
                report['total_commission'] += staff_commission
        
        report['total_commission'] = report['total_commission'].quantize(Decimal('0.01'))
        return report