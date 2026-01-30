"""
RADIUS Auto-Sync Signals - Complete Customer → RADIUS Integration

This module provides AUTOMATIC synchronization between Django and RADIUS:

1. When Customer is created → Auto-create RADIUS credentials (if PPPoE/Hotspot)
2. When CustomerRadiusCredentials is saved → Sync to RadCheck/RadReply
3. When ServiceConnection changes → Update RADIUS status
4. When Plan is updated → Update bandwidth for all users on that plan
5. When Invoice is overdue → Suspend RADIUS access
6. When Payment received → Restore RADIUS access

Inspired by Splynx's automatic RADIUS integration.
"""

import logging
import secrets
import string
from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver
from django.utils import timezone
from django.db import transaction

logger = logging.getLogger(__name__)


def generate_pppoe_username(customer) -> str:
    """
    Generate a PPPoE username for a customer.
    
    Format options (configurable):
    - customer_code: Uses customer code directly
    - email: Uses email prefix
    - phone: Uses phone number
    - auto: Generates username from customer code + random suffix
    """
    # Use customer code as base username
    base = customer.customer_code.lower().replace(' ', '_')
    return f"ppp_{base}"


def generate_password(length=12) -> str:
    """Generate a secure random password."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def get_radius_sync_service():
    """Lazy import to avoid circular imports."""
    from .services.radius_sync_service import RadiusSyncService
    return RadiusSyncService()


# ────────────────────────────────────────────────────────────────
# CUSTOMER RADIUS CREDENTIALS SIGNALS
# Auto-sync to RadCheck/RadReply when credentials change
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='radius.CustomerRadiusCredentials')
def sync_credentials_to_radius(sender, instance, created, **kwargs):
    """
    Sync CustomerRadiusCredentials to RADIUS tables (RadCheck/RadReply).
    
    This is the main sync point - whenever credentials are created or updated,
    the corresponding RADIUS entries are automatically created/updated.
    """
    try:
        # Use transaction to ensure atomicity
        with transaction.atomic():
            instance.sync_to_radius()
        
        action = "Created" if created else "Updated"
        logger.info(f"{action} RADIUS sync for: {instance.username}")
        
    except Exception as e:
        logger.error(f"Failed to sync RADIUS credentials {instance.username}: {e}")


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
# Auto-create RADIUS credentials when service requires it
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='customers.ServiceConnection')
def auto_create_radius_for_service(sender, instance, created, **kwargs):
    """
    Automatically create RADIUS credentials when a service connection
    is created with PPPoE or Hotspot authentication type.
    
    This is the KEY integration point that makes the system work like Splynx -
    when you create a customer service, RADIUS is automatically configured.
    """
    from .models import CustomerRadiusCredentials, RadiusBandwidthProfile
    
    try:
        # Only process PPPoE or Hotspot connections
        auth_type = (instance.auth_connection_type or '').upper()
        if auth_type not in ['PPPOE', 'HOTSPOT']:
            return
        
        customer = instance.customer
        
        # Check if customer already has RADIUS credentials
        if hasattr(customer, 'radius_credentials'):
            credentials = customer.radius_credentials
            
            # Update if service is active, or status changed
            if instance.status == 'ACTIVE' and not credentials.is_enabled:
                credentials.is_enabled = True
                credentials.disabled_reason = ''
                credentials.save()
                logger.info(f"Re-enabled RADIUS for customer: {customer.customer_code}")
                
            elif instance.status in ['SUSPENDED', 'TERMINATED'] and credentials.is_enabled:
                credentials.is_enabled = False
                credentials.disabled_reason = f"Service {instance.status.lower()}"
                credentials.save()
                logger.info(f"Disabled RADIUS for customer: {customer.customer_code}")
            
            # Update bandwidth profile if plan changed
            if instance.plan:
                profile = _get_or_create_bandwidth_profile(instance)
                if profile and credentials.bandwidth_profile != profile:
                    credentials.bandwidth_profile = profile
                    credentials.save()
                    logger.info(f"Updated bandwidth profile for: {credentials.username}")
            
            return
        
        # Create new RADIUS credentials for this customer
        if not created:
            # Only auto-create on new service connections
            return
        
        # Generate credentials
        username = generate_pppoe_username(customer)
        password = generate_password()
        
        # Determine connection type
        conn_type = 'PPPOE' if auth_type == 'PPPOE' else 'HOTSPOT'
        
        # Get or create bandwidth profile from plan
        profile = _get_or_create_bandwidth_profile(instance) if instance.plan else None
        
        # Create the credentials (this will trigger sync_credentials_to_radius)
        credentials = CustomerRadiusCredentials.objects.create(
            customer=customer,
            username=username,
            password=password,
            bandwidth_profile=profile,
            connection_type=conn_type,
            is_enabled=instance.status == 'ACTIVE',
            simultaneous_use=1,
        )
        
        logger.info(f"Auto-created RADIUS credentials for customer: {customer.customer_code}")
        logger.info(f"  Username: {username}, Type: {conn_type}")
        
    except Exception as e:
        logger.error(f"Failed to auto-create RADIUS for service {instance.id}: {e}")


def _get_or_create_bandwidth_profile(service_connection):
    """Get or create a bandwidth profile from a service connection's plan."""
    from .models import RadiusBandwidthProfile
    
    plan = service_connection.plan
    if not plan:
        return None
    
    # Convert Mbps to kbps for RADIUS
    download_kbps = (plan.download_speed or service_connection.download_speed or 10) * 1000
    upload_kbps = (plan.upload_speed or service_connection.upload_speed or 5) * 1000
    
    # Create profile name from plan
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
# Sync customer status changes to RADIUS
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='customers.Customer')
def sync_customer_status_to_radius(sender, instance, **kwargs):
    """
    Sync customer status changes to RADIUS.
    
    If customer is suspended (e.g., non-payment), disable RADIUS.
    If customer is re-activated, re-enable RADIUS.
    """
    from .models import CustomerRadiusCredentials
    
    try:
        # Check if customer has RADIUS credentials
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


# ────────────────────────────────────────────────────────────────
# PLAN SIGNALS
# Update all affected RADIUS users when plan bandwidth changes
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='billing.Plan')
def sync_plan_bandwidth_to_radius(sender, instance, created, **kwargs):
    """
    Update RADIUS bandwidth profiles when a plan is modified.
    
    This ensures bandwidth limits are updated for all customers
    on the affected plan.
    """
    from .models import RadiusBandwidthProfile, CustomerRadiusCredentials
    
    try:
        if created:
            # New plan - just create the profile
            return
        
        # Get or create the bandwidth profile for this plan
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
            cred.sync_to_radius()
            count += 1
        
        if count > 0:
            logger.info(f"Updated RADIUS for {count} users after plan change: {instance.name}")
            
    except Exception as e:
        logger.error(f"Failed to sync plan to RADIUS: {e}")


# ────────────────────────────────────────────────────────────────
# ROUTER/NAS SIGNALS
# Auto-register routers as NAS entries
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='network.Router')
def sync_router_to_nas(sender, instance, created, **kwargs):
    """
    Sync router to RADIUS NAS table when created or updated.
    
    Each router that sends RADIUS requests must be registered as a NAS.
    """
    from .models import Nas
    
    try:
        # Check if router has required RADIUS fields
        if not getattr(instance, 'ip_address', None):
            return
        
        # Get or create NAS entry
        nas, nas_created = Nas.objects.update_or_create(
            nasname=str(instance.ip_address),
            defaults={
                'shortname': instance.name or f"router_{instance.id}",
                'type': 'mikrotik',
                'secret': getattr(instance, 'radius_secret', None) or 'testing123',
                'description': f"Auto-synced from router: {instance.name}",
                'router': instance,
            }
        )
        
        action = "Created" if nas_created else "Updated"
        logger.info(f"{action} NAS entry for router: {instance.name}")
        
    except Exception as e:
        logger.error(f"Failed to sync router to NAS: {e}")


@receiver(post_delete, sender='network.Router')
def remove_router_from_nas(sender, instance, **kwargs):
    """Remove NAS entry when router is deleted."""
    from .models import Nas
    
    try:
        deleted, _ = Nas.objects.filter(router=instance).delete()
        if deleted:
            logger.info(f"Removed NAS entry for router: {instance.name}")
    except Exception as e:
        logger.error(f"Failed to remove NAS entry: {e}")


# ────────────────────────────────────────────────────────────────
# BILLING SIGNALS
# Handle RADIUS access based on invoice/payment status
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='billing.Invoice')
def handle_invoice_status_radius(sender, instance, **kwargs):
    """
    Handle RADIUS access based on invoice status.
    
    - Overdue invoice → Suspend RADIUS access
    - Paid invoice → Restore RADIUS access (if all invoices paid)
    """
    from .models import CustomerRadiusCredentials
    
    try:
        customer = instance.customer
        
        # Check if customer has RADIUS credentials
        if not hasattr(customer, 'radius_credentials'):
            return
        
        credentials = customer.radius_credentials
        status = (instance.status or '').upper()
        
        if status == 'OVERDUE':
            # Check if auto-suspend is enabled
            auto_suspend = getattr(instance, 'auto_suspend', True)
            if auto_suspend and credentials.is_enabled:
                credentials.is_enabled = False
                credentials.disabled_reason = f"Invoice #{instance.id} overdue"
                credentials.save()
                logger.info(f"Suspended RADIUS for overdue invoice: {instance.id}")
                
        elif status == 'PAID':
            # Check if all invoices are now paid
            pending = customer.invoices.filter(
                status__in=['PENDING', 'OVERDUE', 'pending', 'overdue']
            ).exclude(id=instance.id).exists()
            
            if not pending and not credentials.is_enabled:
                # All paid - restore access
                credentials.is_enabled = True
                credentials.disabled_reason = ''
                credentials.save()
                logger.info(f"Restored RADIUS after payment: {instance.id}")
                
    except Exception as e:
        logger.error(f"Failed to handle invoice status for RADIUS: {e}")


# ────────────────────────────────────────────────────────────────
# TENANT REGISTRATION SIGNAL
# Auto-configure RADIUS when new tenant is created
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='core.Tenant')
def configure_radius_for_new_tenant(sender, instance, created, **kwargs):
    """
    Auto-configure RADIUS when a new tenant is registered.
    
    This creates:
    1. Tenant-specific queries.conf
    2. RADIUS tenant configuration record
    3. Environment file for isolated deployment
    """
    if not created:
        return
    
    try:
        from .models import RadiusTenantConfig
        from .services.tenant_radius_service import tenant_radius_service
        
        schema_name = instance.schema_name
        tenant_name = getattr(instance, 'name', None) or schema_name
        
        # Skip public schema
        if schema_name == 'public':
            return
        
        # Configure RADIUS for this tenant
        result = tenant_radius_service.configure_tenant_radius(
            schema_name=schema_name,
            tenant_name=tenant_name
        )
        
        logger.info(f"Auto-configured RADIUS for new tenant: {schema_name}")
        logger.info(f"  Config path: {result.get('queries_path')}")
        
    except Exception as e:
        logger.error(f"Failed to configure RADIUS for tenant {instance.schema_name}: {e}")
