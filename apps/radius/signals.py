"""
RADIUS Signals - Auto-sync Django models to RADIUS tables

This module provides automatic synchronization between Django's
customer/router models and FreeRADIUS database tables when:
- Service connection changes
- Router is created/updated
- Customer status changes (suspend/resume)
- Plan is updated (bandwidth changes)

Note: The ServiceConnection model doesn't have pppoe_username/password fields.
RADIUS users are created through the RadiusSyncService directly when 
setting up PPPoE/RADIUS authentication for customers.
"""

import logging
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

logger = logging.getLogger(__name__)


def get_radius_sync_service():
    """Lazy import to avoid circular imports"""
    from .services.radius_sync_service import RadiusSyncService
    return RadiusSyncService()


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
        # Only sync if router has RADIUS secret configured
        if not getattr(instance, 'radius_secret', None):
            logger.debug(f"Router {instance.name} has no RADIUS secret, skipping NAS sync")
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
        deleted, _ = Nas.objects.filter(router=instance).delete()
        if deleted:
            logger.info(f"Removed NAS entry for router: {instance.name}")
        
    except Exception as e:
        logger.error(f"Failed to remove NAS entry: {e}")


# ────────────────────────────────────────────────────────────────
# SERVICE CONNECTION SIGNALS
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='customers.ServiceConnection')
def sync_service_connection_status(sender, instance, created, **kwargs):
    """
    Sync service connection status changes to RADIUS.
    
    When a service is suspended/terminated, disable the corresponding
    RADIUS user. When activated, ensure RADIUS is enabled.
    
    Note: RADIUS user must already exist (created via RadiusSyncService
    when setting up the customer's PPPoE/RADIUS credentials).
    """
    try:
        from .models import RadCheck
        
        # Find RADIUS user linked to this service connection's customer
        customer = instance.customer
        radius_users = RadCheck.objects.filter(
            customer=customer,
            attribute='Cleartext-Password'
        ).values_list('username', flat=True).distinct()
        
        if not radius_users:
            return
        
        service = get_radius_sync_service()
        
        # Status is uppercase in the model (ACTIVE, SUSPENDED, etc.)
        status = instance.status.upper() if instance.status else ''
        
        if status == 'ACTIVE':
            for username in radius_users:
                service.enable_radius_user(username)
            logger.info(f"Enabled RADIUS for active service: {instance.id}")
            
        elif status in ['SUSPENDED', 'TERMINATED']:
            for username in radius_users:
                service.disable_radius_user(username)
            logger.info(f"Disabled RADIUS for {status.lower()} service: {instance.id}")
            
    except Exception as e:
        logger.error(f"Failed to sync service connection to RADIUS: {e}")


@receiver(post_delete, sender='customers.ServiceConnection')
def handle_service_connection_delete(sender, instance, **kwargs):
    """
    Handle RADIUS cleanup when service connection is deleted.
    
    Note: This doesn't delete the RADIUS user automatically as 
    the customer may have other services or the RADIUS user may
    be managed independently.
    """
    try:
        logger.info(f"Service connection {instance.id} deleted - RADIUS user cleanup may be needed")
    except Exception as e:
        logger.error(f"Error handling service connection delete: {e}")


# ────────────────────────────────────────────────────────────────
# PLAN SIGNALS
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='billing.Plan')
def sync_plan_to_radius_profile(sender, instance, created, **kwargs):
    """
    Create/update RADIUS bandwidth profile when plan changes.
    
    This ensures bandwidth limits are updated in RADIUS when an
    ISP modifies their service plans.
    """
    try:
        from .models import RadiusBandwidthProfile
        
        # Only sync PPPoE/RADIUS-related plans
        if instance.plan_type not in ['PPPOE', 'INTERNET', 'STATIC']:
            return
        
        # Convert Mbps to kbps for RADIUS
        download_kbps = (instance.download_speed or 10) * 1000
        upload_kbps = (instance.upload_speed or 5) * 1000
        
        # Create or update bandwidth profile
        profile, profile_created = RadiusBandwidthProfile.objects.update_or_create(
            name=f"plan_{instance.id}_{instance.code or 'default'}",
            defaults={
                'description': f"Auto-synced from plan: {instance.name}",
                'download_speed': download_kbps,
                'upload_speed': upload_kbps,
                'is_active': instance.is_active,
            }
        )
        
        action = "Created" if profile_created else "Updated"
        logger.info(f"{action} RADIUS profile for plan: {instance.name}")
        
        # If plan was updated (not created), sync all affected users
        if not created and not profile_created:
            service = get_radius_sync_service()
            service.bulk_update_plan_users(instance, profile)
            
    except Exception as e:
        logger.error(f"Failed to sync plan to RADIUS: {e}")


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
        from .models import RadCheck
        
        # Check if status field exists
        if not hasattr(instance, 'status'):
            return
        
        # Get all RADIUS usernames for this customer
        usernames = RadCheck.objects.filter(
            customer=instance,
            attribute='Cleartext-Password'
        ).values_list('username', flat=True).distinct()
        
        if not usernames:
            return
        
        service = get_radius_sync_service()
        status = instance.status.upper() if instance.status else ''
        
        if status in ['SUSPENDED', 'INACTIVE']:
            # Disable all RADIUS users
            for username in usernames:
                service.disable_radius_user(username)
            logger.info(f"Disabled RADIUS for suspended customer: {instance.id}")
            
        elif status == 'ACTIVE':
            # Re-enable RADIUS users
            for username in usernames:
                service.enable_radius_user(username)
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
        from .models import RadCheck
        
        customer = instance.customer
        service = get_radius_sync_service()
        
        # Get RADIUS usernames for this customer
        usernames = RadCheck.objects.filter(
            customer=customer,
            attribute='Cleartext-Password'
        ).values_list('username', flat=True).distinct()
        
        if not usernames:
            return
        
        status = instance.status.upper() if instance.status else ''
        
        if status == 'OVERDUE' and getattr(instance, 'auto_suspend', True):
            # Suspend all customer's RADIUS users
            for username in usernames:
                service.disable_radius_user(username)
            logger.info(f"Suspended RADIUS for overdue invoice: {instance.id}")
            
        elif status == 'PAID':
            # Check if all invoices are paid
            pending = customer.invoices.filter(
                status__in=['PENDING', 'OVERDUE', 'pending', 'overdue']
            ).exclude(id=instance.id).exists()
            
            if not pending:
                # Restore RADIUS access
                for username in usernames:
                    service.enable_radius_user(username)
                logger.info(f"Restored RADIUS after payment: {instance.id}")
                
    except Exception as e:
        logger.error(f"Failed to handle invoice status change: {e}")
                
    except Exception as e:
        logger.error(f"Failed to handle invoice status change: {e}")
