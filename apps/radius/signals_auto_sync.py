"""
RADIUS Auto-Sync Signals - Complete Customer → RADIUS Integration

This module provides AUTOMATIC synchronization between Django and RADIUS:
1. When Customer is created → Auto-create RADIUS credentials (if PPPoE/Hotspot)
2. When CustomerRadiusCredentials is saved → Sync to RadCheck/RadReply
3. When ServiceConnection changes → Update RADIUS status
4. When Plan is updated → Update bandwidth for all users on that plan
5. When Invoice is overdue → Suspend RADIUS access
6. When Payment received → Restore RADIUS access
7. When Service is activated → Calculate expiration based on Plan validity
"""

import logging
import secrets
import string
from datetime import timedelta
from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

def generate_pppoe_username(customer) -> str:
    """
    Generate a simple PPPoE username from customer phone number.
    
    Uses phone number (last 9 digits) for simplicity in testing.
    Format: 712345678 (without country code prefix)
    """
    phone = customer.user.phone_number or ''
    # Remove any non-digit characters
    digits = ''.join(c for c in phone if c.isdigit())
    # Take last 9 digits (Kenya phone without country code)
    if len(digits) >= 9:
        return digits[-9:]
    # Fallback to customer code if no phone
    return customer.customer_code.lower().replace(' ', '_')[:20]

def generate_password(length=8) -> str:
    """Generate a simple alphanumeric password (easier to type)."""
    alphabet = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def get_radius_sync_service():
    """Lazy import to avoid circular imports."""
    from .services.radius_sync_service import RadiusSyncService
    return RadiusSyncService()


def calculate_expiration_from_plan(plan, start_time=None):
    """
    Calculate expiration datetime based on Plan validity settings.
    
    Args:
        plan: Plan instance with validity_type and validity fields
        start_time: Optional start time (defaults to now)
        
    Returns:
        datetime: Expiration datetime, or None for unlimited plans
    """
    if not plan:
        return None
    
    now = start_time or timezone.now()
    
    validity_type = (plan.validity_type or 'DAYS').upper()
    
    if validity_type == 'UNLIMITED':
        return None
    
    elif validity_type == 'MINUTES' and plan.validity_minutes:
        return now + timedelta(minutes=plan.validity_minutes)
    
    elif validity_type == 'HOURS' and plan.validity_hours:
        return now + timedelta(hours=plan.validity_hours)
    
    elif validity_type == 'DAYS':
        days = plan.duration_days or 30
        return now + timedelta(days=days)
    
    else:
        # Default to 30 days if validity_type not recognized
        return now + timedelta(days=30)


# ────────────────────────────────────────────────────────────────
# CUSTOMER RADIUS CREDENTIALS SIGNALS (The Fix is Here)
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='radius.CustomerRadiusCredentials')
def sync_credentials_to_radius(sender, instance, created, **kwargs):
    """
    Sync CustomerRadiusCredentials to RADIUS tables (RadCheck/RadReply).
    Includes RECURSION GUARD to prevent infinite loops.
    """
    # 🛑 RECURSION GUARD: Stop if we are already syncing this instance
    if getattr(instance, '_is_syncing', False):
        return

    try:
        # Set the flag to indicate we are busy
        instance._is_syncing = True

        # Use transaction to ensure atomicity
        with transaction.atomic():
            instance.sync_to_radius()
        
        action = "Created" if created else "Updated"
        logger.info(f"{action} RADIUS sync for: {instance.username}")
        
    except Exception as e:
        logger.error(f"Failed to sync RADIUS credentials {instance.username}: {e}")
    finally:
        # Always release the flag, even if it failed
        instance._is_syncing = False


@receiver(post_delete, sender='radius.CustomerRadiusCredentials')
def delete_credentials_from_radius(sender, instance, **kwargs):
    """Remove RADIUS entries when credentials are deleted."""
    try:
        service = get_radius_sync_service()
        service.delete_radius_user(instance.username)
        logger.info(f"Deleted RADIUS user: {instance.username}")
    except Exception as e:
        logger.error(f"Failed to delete RADIUS user {instance.username}: {e}")


# ────────────────────────────────────────────────────────────────
# SERVICE CONNECTION SIGNALS
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='customers.ServiceConnection')
def auto_create_radius_for_service(sender, instance, created, **kwargs):
    """
    Automatically create RADIUS credentials when a service connection is created.
    """
    # 🛑 RECURSION GUARD for ServiceConnection as well
    if getattr(instance, '_is_processing_radius', False):
        return

    from .models import CustomerRadiusCredentials

    try:
        instance._is_processing_radius = True
        
        # Only process PPPoE or Hotspot connections
        auth_type = (instance.auth_connection_type or '').upper()
        if auth_type not in ['PPPOE', 'HOTSPOT']:
            return
        
        customer = instance.customer
        
        # Check if customer already has RADIUS credentials
        if hasattr(customer, 'radius_credentials'):
            credentials = customer.radius_credentials
            needs_save = False
            
            # 🎯 Handle RENEWAL: When status changes from non-ACTIVE to ACTIVE
            # This is the key moment to reset the expiration date
            if instance.status == 'ACTIVE' and not credentials.is_enabled:
                credentials.is_enabled = True
                credentials.disabled_reason = ''
                
                # 🎯 RENEWAL LOGIC: Recalculate expiration when re-activating
                if instance.plan:
                    new_expiration = calculate_expiration_from_plan(instance.plan)
                    credentials.expiration_date = new_expiration
                    if new_expiration:
                        logger.info(
                            f"Renewed RADIUS for {credentials.username}: "
                            f"New expiration={new_expiration.strftime('%b %d %Y %H:%M:%S')}"
                        )
                    else:
                        logger.info(f"Renewed RADIUS for {credentials.username}: Unlimited validity")
                
                needs_save = True
                logger.info(f"Re-enabled RADIUS for customer: {customer.customer_code}")
                
            elif instance.status in ['SUSPENDED', 'TERMINATED'] and credentials.is_enabled:
                credentials.is_enabled = False
                credentials.disabled_reason = f"Service {instance.status.lower()}"
                needs_save = True
                logger.info(f"Disabled RADIUS for customer: {customer.customer_code}")
            
            # 🎯 Handle PLAN CHANGE: Update bandwidth profile and expiration
            if instance.plan:
                profile = _get_or_create_bandwidth_profile(instance)
                if profile and credentials.bandwidth_profile != profile:
                    credentials.bandwidth_profile = profile
                    needs_save = True
                    logger.info(f"Updated bandwidth profile for: {credentials.username}")
            
            # Save all changes in one go
            if needs_save:
                credentials.save()
            
            return
        
        # Create new RADIUS credentials for this customer
        # Allow creation when:
        # 1. created=True (first save of a new service), OR
        # 2. _force_radius_creation flag is set (second save from serializer
        #    that attaches _radius_password after initial create)
        force_creation = getattr(instance, '_force_radius_creation', False)
        if not created and not force_creation:
            return
        
        # 🎯 P4 "Activate Later": Do NOT create RADIUS credentials for PENDING services
        # The timer should not start until the admin clicks "Activate"
        if instance.status == 'PENDING':
            logger.info(
                f"Skipping RADIUS creation for PENDING service {instance.id} "
                f"(customer: {customer.customer_code}). Use /activate/ to start timer."
            )
            return
        
        # Generate credentials
        # Username: simplified to phone number (last 9 digits)
        username = generate_pppoe_username(customer)
        
        # Password: Try to use the radius_password passed via instance, 
        # or fallback to generating one
        # Note: The frontend should pass radius_password during service creation
        password = getattr(instance, '_radius_password', None)
        if not password:
            password = generate_password(8)  # 8 char for easier testing
        
        conn_type = 'PPPOE' if auth_type == 'PPPOE' else 'HOTSPOT'
        profile = _get_or_create_bandwidth_profile(instance) if instance.plan else None
        
        # 🎯 Calculate Expiration Date based on Plan
        expiration_date = calculate_expiration_from_plan(instance.plan)
        
        if expiration_date:
            logger.info(
                f"Setting RADIUS expiration for {username}: "
                f"Plan={instance.plan.name}, "
                f"ValidityType={instance.plan.validity_type}, "
                f"Expires={expiration_date.strftime('%b %d %Y %H:%M:%S')}"
            )
        else:
            logger.info(f"RADIUS user {username} has unlimited validity (no expiration)")
        
        # Create the credentials (triggers the sync_credentials_to_radius signal above)
        CustomerRadiusCredentials.objects.create(
            customer=customer,
            username=username,
            password=password,
            bandwidth_profile=profile,
            connection_type=conn_type,
            is_enabled=instance.status == 'ACTIVE',
            simultaneous_use=1,
            expiration_date=expiration_date,  # 🎯 CRITICAL: Set expiration
        )
        
        logger.info(f"Auto-created RADIUS credentials: username={username}")
        
    except Exception as e:
        logger.error(f"Failed to auto-create RADIUS for service {instance.id}: {e}")
    finally:
        instance._is_processing_radius = False


def _get_or_create_bandwidth_profile(service_connection):
    """Get or create a bandwidth profile from a service connection's plan."""
    from .models import RadiusBandwidthProfile
    
    plan = service_connection.plan
    if not plan:
        return None
    
    # Convert Mbps to kbps
    download_kbps = (plan.download_speed or service_connection.download_speed or 10) * 1000
    upload_kbps = (plan.upload_speed or service_connection.upload_speed or 5) * 1000
    
    profile_name = f"plan_{plan.id}_{plan.code or 'auto'}"
    
    profile, created = RadiusBandwidthProfile.objects.get_or_create(
        name=profile_name,
        defaults={
            'description': f"Auto-created from plan: {plan.name}",
            'download_speed': download_kbps,
            'upload_speed': upload_kbps,
            'is_active': True,
        }
    )
    
    # Update if speeds changed
    if not created:
        if profile.download_speed != download_kbps or profile.upload_speed != upload_kbps:
            profile.download_speed = download_kbps
            profile.upload_speed = upload_kbps
            profile.save()
    
    return profile


# ────────────────────────────────────────────────────────────────
# CUSTOMER STATUS SIGNALS
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='customers.Customer')
def sync_customer_status_to_radius(sender, instance, **kwargs):
    """Sync customer status changes (SUSPENDED/ACTIVE) to RADIUS."""
    if getattr(instance, '_is_syncing_radius', False):
        return

    try:
        instance._is_syncing_radius = True
        if not hasattr(instance, 'radius_credentials'):
            return
        
        credentials = instance.radius_credentials
        status = (instance.status or '').upper()
        
        if status in ['SUSPENDED', 'INACTIVE', 'TERMINATED']:
            if credentials.is_enabled:
                credentials.is_enabled = False
                credentials.disabled_reason = f"Customer {status.lower()}"
                credentials.save()
                logger.info(f"Disabled RADIUS for {status.lower()} customer: {instance.customer_code}")
                
        elif status == 'ACTIVE':
            if not credentials.is_enabled:
                credentials.is_enabled = True
                credentials.disabled_reason = ''
                credentials.save()
                logger.info(f"Enabled RADIUS for active customer: {instance.customer_code}")
                
    except Exception as e:
        logger.error(f"Failed to sync customer status to RADIUS: {e}")
    finally:
        instance._is_syncing_radius = False


# ────────────────────────────────────────────────────────────────
# PLAN SIGNALS
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='billing.Plan')
def sync_plan_bandwidth_to_radius(sender, instance, created, **kwargs):
    """Update RADIUS bandwidth profiles when a plan is modified."""
    from .models import RadiusBandwidthProfile, CustomerRadiusCredentials
    
    try:
        if created:
            return
        
        profile_name = f"plan_{instance.id}_{instance.code or 'auto'}"
        download_kbps = (instance.download_speed or 10) * 1000
        upload_kbps = (instance.upload_speed or 5) * 1000
        
        profile, _ = RadiusBandwidthProfile.objects.update_or_create(
            name=profile_name,
            defaults={
                'description': f"Auto-updated from plan: {instance.name}",
                'download_speed': download_kbps,
                'upload_speed': upload_kbps,
                'is_active': instance.is_active if hasattr(instance, 'is_active') else True,
            }
        )
        
        # Re-sync all credentials using this profile
        credentials = CustomerRadiusCredentials.objects.filter(
            bandwidth_profile=profile,
            is_enabled=True
        )
        
        count = 0
        for cred in credentials:
            # We call sync directly here, which is safer than save()
            cred.sync_to_radius()
            count += 1
        
        if count > 0:
            logger.info(f"Updated RADIUS for {count} users after plan change: {instance.name}")
            
    except Exception as e:
        logger.error(f"Failed to sync plan to RADIUS: {e}")


# ────────────────────────────────────────────────────────────────
# ROUTER/NAS SIGNALS
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='network.Router')
def sync_router_to_nas(sender, instance, created, **kwargs):
    """Sync router to RADIUS NAS table."""
    from .models import Nas
    try:
        if not getattr(instance, 'ip_address', None):
            return
        
        Nas.objects.update_or_create(
            nasname=str(instance.ip_address),
            defaults={
                'shortname': instance.name or f"router_{instance.id}",
                'type': 'mikrotik',
                'secret': getattr(instance, 'radius_secret', None) or 'testing123',
                'description': f"Auto-synced from router: {instance.name}",
                'router': instance,
            }
        )
        logger.info(f"Synced NAS entry for router: {instance.name}")
    except Exception as e:
        logger.error(f"Failed to sync router to NAS: {e}")


@receiver(post_delete, sender='network.Router')
def remove_router_from_nas(sender, instance, **kwargs):
    from .models import Nas
    try:
        deleted, _ = Nas.objects.filter(router=instance).delete()
        if deleted:
            logger.info(f"Removed NAS entry for router: {instance.name}")
    except Exception as e:
        logger.error(f"Failed to remove NAS entry: {e}")


# ────────────────────────────────────────────────────────────────
# BILLING SIGNALS
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='billing.Invoice')
def handle_invoice_status_radius(sender, instance, **kwargs):
    """Handle RADIUS access based on invoice status (Overdue/Paid)."""
    if getattr(instance, '_is_processing_invoice', False):
        return

    try:
        instance._is_processing_invoice = True
        customer = instance.customer
        if not hasattr(customer, 'radius_credentials'):
            return
        
        credentials = customer.radius_credentials
        status = (instance.status or '').upper()
        
        if status == 'OVERDUE':
            auto_suspend = getattr(instance, 'auto_suspend', True)
            if auto_suspend and credentials.is_enabled:
                credentials.is_enabled = False
                credentials.disabled_reason = f"Invoice #{instance.id} overdue"
                credentials.save()
                logger.info(f"Suspended RADIUS for overdue invoice: {instance.id}")
                
        elif status == 'PAID':
            pending = customer.invoices.filter(
                status__in=['PENDING', 'OVERDUE', 'pending', 'overdue']
            ).exclude(id=instance.id).exists()
            
            if not pending and not credentials.is_enabled:
                credentials.is_enabled = True
                credentials.disabled_reason = ''
                credentials.save()
                logger.info(f"Restored RADIUS after payment: {instance.id}")
                
    except Exception as e:
        logger.error(f"Failed to handle invoice status for RADIUS: {e}")
    finally:
        instance._is_processing_invoice = False


# ────────────────────────────────────────────────────────────────
# TENANT SIGNALS
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='core.Tenant')
def configure_radius_for_new_tenant(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        from .services.tenant_radius_service import tenant_radius_service
        
        schema_name = instance.schema_name
        if schema_name == 'public':
            return
        
        result = tenant_radius_service.configure_tenant_radius(
            schema_name=schema_name,
            tenant_name=getattr(instance, 'name', None) or schema_name
        )
        logger.info(f"Auto-configured RADIUS for tenant: {schema_name}")
        
    except Exception as e:
        logger.error(f"Failed to configure RADIUS for tenant {instance.schema_name}: {e}")