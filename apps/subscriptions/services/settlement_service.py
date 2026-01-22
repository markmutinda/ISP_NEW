"""
Settlement Service

Handles the automated settlement process where Netily pays out
the ISP's share (95%) of collected customer payments.

This can be run as a scheduled task (celery beat) or manually triggered.
"""

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import List, Optional, Tuple

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from django_tenants.utils import schema_context, get_public_schema_name

from apps.billing.services.payhero import PayHeroClient, PayHeroError
from apps.subscriptions.models import (
    ISPPayoutConfig,
    ISPSettlement,
    CommissionLedger,
)
from apps.core.models import Company

logger = logging.getLogger(__name__)


class SettlementService:
    """
    Service for processing ISP settlements.
    
    Usage:
        service = SettlementService()
        
        # Process all due settlements
        results = service.process_all_due_settlements()
        
        # Process specific company
        result = service.process_company_settlement(company_id)
    """
    
    def __init__(self):
        self.payhero_client = PayHeroClient()
    
    def get_companies_due_for_settlement(self) -> List[Company]:
        """
        Get list of companies due for settlement based on their schedule.
        
        Returns:
            List of Company objects that are due for payout
        """
        today = date.today()
        due_companies = []
        
        with schema_context(get_public_schema_name()):
            # Get all verified payout configs
            configs = ISPPayoutConfig.objects.filter(
                is_verified=True
            ).select_related('company')
            
            for config in configs:
                # Check if pending balance meets minimum
                if config.pending_balance < config.minimum_payout:
                    continue
                
                # Check if due based on frequency
                is_due = self._is_settlement_due(config, today)
                
                if is_due:
                    due_companies.append(config.company)
        
        return due_companies
    
    def _is_settlement_due(self, config: ISPPayoutConfig, today: date) -> bool:
        """Check if a company is due for settlement based on frequency"""
        frequency = config.settlement_frequency
        
        # Get last settlement
        last_settlement = ISPSettlement.objects.filter(
            company=config.company,
            status='completed'
        ).order_by('-processed_at').first()
        
        if not last_settlement:
            # Never settled before - settle if balance meets minimum
            return True
        
        last_date = last_settlement.processed_at.date()
        days_since_last = (today - last_date).days
        
        if frequency == 'daily':
            return days_since_last >= 1
        elif frequency == 'weekly':
            return days_since_last >= 7
        elif frequency == 'biweekly':
            return days_since_last >= 14
        elif frequency == 'monthly':
            return days_since_last >= 28
        
        return False
    
    @transaction.atomic
    def process_company_settlement(
        self,
        company_id: str,
        force: bool = False
    ) -> Tuple[bool, str, Optional[ISPSettlement]]:
        """
        Process settlement for a specific company.
        
        Args:
            company_id: UUID of the company
            force: If True, bypass minimum payout check
            
        Returns:
            Tuple of (success, message, settlement_object)
        """
        with schema_context(get_public_schema_name()):
            try:
                company = Company.objects.get(id=company_id)
            except Company.DoesNotExist:
                return False, "Company not found", None
            
            try:
                config = ISPPayoutConfig.objects.get(company=company)
            except ISPPayoutConfig.DoesNotExist:
                return False, "Payout configuration not found", None
            
            if not config.is_verified:
                return False, "Payout destination not verified", None
            
            if not force and config.pending_balance < config.minimum_payout:
                return False, f"Pending balance ({config.pending_balance}) below minimum ({config.minimum_payout})", None
            
            # Get unsettled commission entries
            entries = CommissionLedger.objects.filter(
                company=company,
                is_settled=False
            )
            
            if not entries.exists():
                return False, "No unsettled payments found", None
            
            # Calculate totals
            totals = entries.aggregate(
                gross=Sum('gross_amount'),
                commission=Sum('commission_amount'),
                isp=Sum('isp_amount')
            )
            
            gross_amount = totals['gross'] or Decimal('0.00')
            commission_amount = totals['commission'] or Decimal('0.00')
            net_amount = totals['isp'] or Decimal('0.00')
            transaction_count = entries.count()
            
            # Get period
            period_start = entries.earliest('created_at').created_at
            period_end = entries.latest('created_at').created_at
            
            # Create settlement record
            settlement = ISPSettlement.objects.create(
                company=company,
                period_start=period_start,
                period_end=period_end,
                gross_amount=gross_amount,
                commission_amount=commission_amount,
                net_amount=net_amount,
                payout_method=config.payout_method,
                payout_destination=config.payout_destination,
                transaction_count=transaction_count,
                status='processing',
            )
            
            # Initiate payout
            payout_success, payout_message, payout_reference = self._initiate_payout(
                config=config,
                amount=net_amount,
                reference=f"SETTLE-{settlement.id.hex[:8].upper()}"
            )
            
            if payout_success:
                # Mark settlement as completed
                settlement.mark_completed(payout_reference)
                
                # Mark all commission entries as settled
                entries.update(is_settled=True, settlement=settlement)
                
                logger.info(
                    f"Settlement completed for {company.name}: "
                    f"KES {net_amount} ({transaction_count} transactions)"
                )
                
                return True, f"Settlement of KES {net_amount} completed", settlement
            else:
                # Mark settlement as failed
                settlement.mark_failed(payout_message)
                
                logger.error(
                    f"Settlement failed for {company.name}: {payout_message}"
                )
                
                return False, payout_message, settlement
    
    def _initiate_payout(
        self,
        config: ISPPayoutConfig,
        amount: Decimal,
        reference: str
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Initiate the actual payout via PayHero.
        
        Returns:
            Tuple of (success, message, transaction_reference)
        """
        try:
            if config.payout_method == 'mpesa_b2c':
                # M-Pesa B2C payout
                response = self.payhero_client.b2c_payout(
                    phone_number=config.mpesa_phone,
                    amount=amount,
                    reference=reference,
                    reason="ISP Revenue Settlement"
                )
                
                if response.success:
                    return True, "Payout initiated", response.transaction_id
                else:
                    return False, response.message, None
            
            elif config.payout_method == 'bank_transfer':
                # Bank transfer
                response = self.payhero_client.bank_transfer(
                    bank_code=config.bank_code,
                    account_number=config.bank_account_number,
                    account_name=config.bank_account_name,
                    amount=amount,
                    reference=reference,
                    narration="ISP Revenue Settlement"
                )
                
                if response.success:
                    return True, "Bank transfer initiated", response.transaction_id
                else:
                    return False, response.message, None
            
            else:
                return False, f"Unsupported payout method: {config.payout_method}", None
        
        except PayHeroError as e:
            logger.error(f"PayHero payout error: {e.message}")
            return False, str(e.message), None
    
    def process_all_due_settlements(self) -> List[dict]:
        """
        Process settlements for all companies that are due.
        
        Returns:
            List of results for each company processed
        """
        results = []
        due_companies = self.get_companies_due_for_settlement()
        
        logger.info(f"Processing settlements for {len(due_companies)} companies")
        
        for company in due_companies:
            success, message, settlement = self.process_company_settlement(
                company_id=str(company.id)
            )
            
            results.append({
                'company_id': str(company.id),
                'company_name': company.name,
                'success': success,
                'message': message,
                'settlement_id': str(settlement.id) if settlement else None,
                'amount': float(settlement.net_amount) if settlement else None,
            })
        
        return results
    
    def get_settlement_report(
        self,
        company_id: str = None,
        start_date: date = None,
        end_date: date = None
    ) -> dict:
        """
        Generate a settlement report.
        
        Args:
            company_id: Optional - filter by company
            start_date: Optional - start of period
            end_date: Optional - end of period
            
        Returns:
            Report dictionary
        """
        with schema_context(get_public_schema_name()):
            # Build query
            settlements = ISPSettlement.objects.all()
            
            if company_id:
                settlements = settlements.filter(company_id=company_id)
            
            if start_date:
                settlements = settlements.filter(created_at__date__gte=start_date)
            
            if end_date:
                settlements = settlements.filter(created_at__date__lte=end_date)
            
            # Aggregate
            totals = settlements.filter(status='completed').aggregate(
                total_gross=Sum('gross_amount'),
                total_commission=Sum('commission_amount'),
                total_net=Sum('net_amount'),
            )
            
            return {
                'period': {
                    'start': start_date,
                    'end': end_date,
                },
                'totals': {
                    'gross_collected': totals['total_gross'] or Decimal('0.00'),
                    'netily_commission': totals['total_commission'] or Decimal('0.00'),
                    'isp_payouts': totals['total_net'] or Decimal('0.00'),
                },
                'settlement_count': settlements.filter(status='completed').count(),
                'pending_count': settlements.filter(status='pending').count(),
                'failed_count': settlements.filter(status='failed').count(),
            }


# Celery task for automated settlements (if using Celery)
# from celery import shared_task
#
# @shared_task
# def process_scheduled_settlements():
#     """Celery task to process all due settlements"""
#     service = SettlementService()
#     results = service.process_all_due_settlements()
#     return results
