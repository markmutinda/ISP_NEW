"""
RADIUS Signals - Auto-sync Django models to RADIUS tables

This module provides automatic synchronization between Django's
customer/router models and FreeRADIUS database tables when:
- Customer is created/updated
- Service connection changes
- Router is created/updated
- Billing status changes (suspend/resume)
"""

import logging
from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


def get_radius_sync_service():
    """Lazy import to avoid circular imports"""
    from .services.radius_sync_service import RadiusSyncService
    return RadiusSyncService()


# ────────────────────────────────────────────────────────────────
# SERVICE CONNECTION SIGNALS
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='customers.ServiceConnection')
def sync_service_connection_to_radius(sender, instance, created, **kwargs):
    """
    Sync service connection to RADIUS when created or updated.
    
    Triggers when:
    - New PPPoE service is activated
    - Service plan changes (bandwidth update)
    - Service status changes (active/suspended)
    """
    try:
        # Only sync if connection has PPPoE credentials
        if not instance.pppoe_username:
            return
        
        service = get_radius_sync_service()
        
        if instance.status == 'active':
            # Get bandwidth profile from service plan
            profile = None
            if instance.service_plan:
                from .models import RadiusBandwidthProfile
                profile = RadiusBandwidthProfile.objects.filter(
                    service_plan=instance.service_plan
                ).first()
            
            # Create or update RADIUS user
            result = service.create_radius_user(
                username=instance.pppoe_username,
                password=instance.pppoe_password,
                customer=instance.customer,
                profile=profile
            )
            logger.info(f"Synced service connection to RADIUS: {result}")
            
        elif instance.status == 'suspended':
            # Disable user in RADIUS
            service.disable_radius_user(instance.pppoe_username)
            logger.info(f"Disabled RADIUS user: {instance.pppoe_username}")
            
        elif instance.status == 'terminated':
            # Remove user from RADIUS
            service.delete_radius_user(instance.pppoe_username)
            logger.info(f"Removed RADIUS user: {instance.pppoe_username}")
            
    except Exception as e:
        logger.error(f"Failed to sync service connection to RADIUS: {e}")


@receiver(post_delete, sender='customers.ServiceConnection')
def remove_service_from_radius(sender, instance, **kwargs):
    """Remove RADIUS user when service connection is deleted"""
    try:
        if not instance.pppoe_username:
            return
        
        service = get_radius_sync_service()
        service.delete_radius_user(instance.pppoe_username)
        logger.info(f"Removed RADIUS user on delete: {instance.pppoe_username}")
        
    except Exception as e:
        logger.error(f"Failed to remove RADIUS user: {e}")


# ────────────────────────────────────────────────────────────────
# ROUTER/NAS SIGNALS
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='network.Router')
def sync_router_to_nas(sender, instance, created, **kwargs):
    """
    Sync router to RADIUS NAS table when created or updated.
    
    Each router that sends RADIUS requests must be registered as a NAS
    with its shared secret.
    """
    try:
        # Only sync if router has RADIUS enabled
        if not getattr(instance, 'radius_enabled', True):
            return
        
        service = get_radius_sync_service()
        result = service.register_nas(instance)
        logger.info(f"Synced router to NAS: {result}")
        
    except Exception as e:
        logger.error(f"Failed to sync router to NAS: {e}")


@receiver(post_delete, sender='network.Router')
def remove_router_from_nas(sender, instance, **kwargs):
    """Remove NAS entry when router is deleted"""
    try:
        from .models import Nas
        Nas.objects.filter(router=instance).delete()
        logger.info(f"Removed NAS entry for router: {instance.name}")
        
    except Exception as e:
        logger.error(f"Failed to remove NAS entry: {e}")


# ────────────────────────────────────────────────────────────────
# SERVICE PLAN SIGNALS
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='billing.ServicePlan')
def sync_service_plan_to_radius_profile(sender, instance, created, **kwargs):
    """
    Create/update RADIUS bandwidth profile when service plan changes.
    
    This ensures bandwidth limits are updated in RADIUS when an
    ISP modifies their service plans.
    """
    try:
        from .models import RadiusBandwidthProfile
        
        # Create or update bandwidth profile
        profile, profile_created = RadiusBandwidthProfile.objects.update_or_create(
            service_plan=instance,
            defaults={
                'name': f"plan_{instance.id}",
                'download_speed': getattr(instance, 'download_speed', 10),
                'upload_speed': getattr(instance, 'upload_speed', 5),
                'burst_download': getattr(instance, 'burst_download', None),
                'burst_upload': getattr(instance, 'burst_upload', None),
            }
        )
        
        action = "Created" if profile_created else "Updated"
        logger.info(f"{action} RADIUS profile for plan: {instance.name}")
        
        # If plan was updated, sync all affected users
        if not created and not profile_created:
            service = get_radius_sync_service()
            service.bulk_update_plan_users(instance)
            
    except Exception as e:
        logger.error(f"Failed to sync service plan to RADIUS: {e}")


# ────────────────────────────────────────────────────────────────
# CUSTOMER SIGNALS
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='customers.Customer')
def sync_customer_status_to_radius(sender, instance, **kwargs):
    """
    Sync customer status changes to RADIUS.
    
    If customer is suspended (e.g., non-payment), disable all
    their RADIUS users.
    """
    try:
        # Check if status field exists and changed
        if not hasattr(instance, 'status'):
            return
        
        # Get all service connections for this customer
        connections = instance.service_connections.filter(
            pppoe_username__isnull=False
        ).exclude(pppoe_username='')
        
        if not connections.exists():
            return
        
        service = get_radius_sync_service()
        
        if instance.status in ['suspended', 'inactive']:
            # Disable all RADIUS users
            for conn in connections:
                service.disable_radius_user(conn.pppoe_username)
            logger.info(f"Disabled RADIUS for suspended customer: {instance.id}")
            
        elif instance.status == 'active':
            # Re-enable RADIUS users for active connections
            for conn in connections.filter(status='active'):
                service.enable_radius_user(conn.pppoe_username)
            logger.info(f"Enabled RADIUS for active customer: {instance.id}")
            
    except Exception as e:
        logger.error(f"Failed to sync customer status to RADIUS: {e}")


# ────────────────────────────────────────────────────────────────
# INVOICE/PAYMENT SIGNALS (Billing Integration)
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='billing.Invoice')
def handle_invoice_status_change(sender, instance, **kwargs):
    """
    Handle RADIUS access based on invoice status.
    
    - Overdue invoice: Suspend RADIUS access
    - Paid invoice: Restore RADIUS access
    """
    try:
        customer = instance.customer
        service = get_radius_sync_service()
        
        if instance.status == 'overdue' and getattr(instance, 'auto_suspend', True):
            # Suspend all customer's RADIUS users
            connections = customer.service_connections.filter(
                pppoe_username__isnull=False
            )
            for conn in connections:
                service.disable_radius_user(conn.pppoe_username)
            logger.info(f"Suspended RADIUS for overdue invoice: {instance.id}")
            
        elif instance.status == 'paid':
            # Check if all invoices are paid
            pending = customer.invoices.filter(
                status__in=['pending', 'overdue']
            ).exclude(id=instance.id).exists()
            
            if not pending:
                # Restore RADIUS access
                connections = customer.service_connections.filter(
                    status='active',
                    pppoe_username__isnull=False
                )
                for conn in connections:
                    service.enable_radius_user(conn.pppoe_username)
                logger.info(f"Restored RADIUS after payment: {instance.id}")
                
    except Exception as e:
        logger.error(f"Failed to handle invoice status change: {e}")
