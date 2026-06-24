"""
RADIUS Auto-Sync Signals - Complete Customer → RADIUS Integration

This module provides AUTOMATIC synchronization between Django and RADIUS:
1. When Customer is created → Auto-create RADIUS credentials (if PPPoE/Hotspot/Static)
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
# CUSTOMER RADIUS CREDENTIALS SIGNALS
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
    
    NOTE: Welcome SMS (pppoe_welcome) is sent from service_views.activate,
    NOT from this signal, to avoid duplicate SMS.
    
    NOTE: New subscription SMS has been REMOVED - toggle no longer exists.
    """
    # 🛑 RECURSION GUARD for ServiceConnection as well
    if getattr(instance, '_is_processing_radius', False):
        return

    from .models import CustomerRadiusCredentials

    try:
        instance._is_processing_radius = True
        
        # ✅ FIX: Process PPPoE, Hotspot, AND Static connections
        auth_type = (instance.auth_connection_type or '').upper()
        if auth_type not in ['PPPOE', 'HOTSPOT', 'STATIC']:
            return
        
        customer = instance.customer
        
        # Check if customer already has RADIUS credentials
        if hasattr(customer, 'radius_credentials'):
            credentials = customer.radius_credentials
            needs_save = False
            
            # ── NEW: Update username/password if explicitly provided ──
            explicit_username = getattr(instance, '_radius_username', None)
            explicit_password = getattr(instance, '_radius_password', None)

            if explicit_username and credentials.username != explicit_username:
                credentials.username = explicit_username
                needs_save = True
                logger.info(f"Updated RADIUS username to explicit value: {explicit_username}")
            
            if explicit_password and credentials.password != explicit_password:
                credentials.password = explicit_password
                needs_save = True
                logger.info(f"Updated RADIUS password for user: {credentials.username}")
            # ─────────────────────────────────────────────────────────
            
            # 🎯 Handle RENEWAL: When status changes from non-ACTIVE to ACTIVE
            # This is the key moment to reset the expiration date and activation timestamp
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
                
                # 🎯 CRITICAL: Stamp subscription_activated_at on renewal
                # This resets the usage counter for the new period
                credentials.subscription_activated_at = timezone.now()
                logger.info(f"Stamped subscription_activated_at for {credentials.username} on renewal: {credentials.subscription_activated_at}")
                
                needs_save = True
                logger.info(f"Re-enabled RADIUS for customer: {customer.customer_code}")
                
                # NOTE: pppoe_resumed SMS has been REMOVED - toggle no longer exists
                
            elif instance.status in ['SUSPENDED', 'TERMINATED'] and credentials.is_enabled:
                credentials.is_enabled = False
                credentials.disabled_reason = f"Service {instance.status.lower()}"
                needs_save = True
                logger.info(f"Disabled RADIUS for customer: {customer.customer_code}")
                
                # NOTE: pppoe_suspended SMS has been REMOVED - toggle no longer exists
            
            # 🎯 Handle PLAN CHANGE: Update bandwidth profile and expiration
            if instance.plan:
                profile = _get_or_create_bandwidth_profile(instance)
                if profile and credentials.bandwidth_profile != profile:
                    credentials.bandwidth_profile = profile
                    needs_save = True
                    logger.info(f"Updated bandwidth profile for: {credentials.username}")
                    
                    # NOTE: pppoe_plan_changed SMS has been REMOVED - toggle no longer exists
            
            # 🎯 Handle ROUTER / IP POOL / ASSIGNED IP update from service creation form
            radius_router_id = getattr(instance, '_radius_router_id', None)
            radius_ip_pool = getattr(instance, '_radius_ip_pool', None)
            radius_assigned_ip_id = getattr(instance, '_radius_assigned_ip_id', None)
            
            if radius_router_id is not None:
                from apps.network.models.router_models import Router
                try:
                    new_router = Router.objects.get(pk=radius_router_id)
                    if credentials.router != new_router:
                        credentials.router = new_router
                        needs_save = True
                        logger.info(f"Updated router for {credentials.username}: {new_router.name}")
                except Router.DoesNotExist:
                    logger.warning(f"Router {radius_router_id} not found for update")
            
            if radius_ip_pool is not None and credentials.ip_pool != radius_ip_pool:
                credentials.ip_pool = radius_ip_pool
                needs_save = True
                logger.info(f"Updated ip_pool for {credentials.username}: {radius_ip_pool}")
            
            # 🎯 Cloud-Led IPAM: Assign a specific IP address
            if radius_assigned_ip_id is not None:
                from apps.network.models.ipam_models import IPAddress
                try:
                    ip_addr = IPAddress.objects.get(pk=radius_assigned_ip_id)
                    if credentials.assigned_ip_address != ip_addr:
                        # Release old IP if any
                        if credentials.assigned_ip_address:
                            credentials.assigned_ip_address.release()
                        credentials.assigned_ip_address = ip_addr
                        credentials.static_ip = ip_addr.ip_address  # Sync legacy field
                        ip_addr.assign_to_customer(customer, instance)
                        needs_save = True
                        logger.info(f"Assigned IP {ip_addr.ip_address} to {credentials.username} (Cloud-Led)")
                except IPAddress.DoesNotExist:
                    logger.warning(f"IPAddress {radius_assigned_ip_id} not found for assignment")
            
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
        # Username: Check for explicit username stashed by the serializer, else fallback to phone
        explicit_username = getattr(instance, '_radius_username', None)
        if explicit_username:
            username = explicit_username
            logger.info(f"Using explicit RADIUS username: {username}")
        else:
            username = generate_pppoe_username(customer)
            logger.info(f"Auto-generated RADIUS username from phone: {username}")
        
        # Password: Try to use the radius_password passed via instance, 
        # or fallback to generating one
        # Note: The frontend should pass radius_password during service creation
        password = getattr(instance, '_radius_password', None)
        if not password:
            password = generate_password(8)  # 8 char for easier testing
            logger.info(f"Auto-generated RADIUS password for: {username}")
        else:
            logger.info(f"Using explicit RADIUS password for: {username}")
        
        # ✅ FIX: Set connection type for STATIC as well
        if auth_type == 'PPPOE':
            conn_type = 'PPPOE'
        elif auth_type == 'HOTSPOT':
            conn_type = 'HOTSPOT'
        else:
            conn_type = 'STATIC'
        
        profile = _get_or_create_bandwidth_profile(instance) if instance.plan else None
        
        # 🎯 Calculate Expiration Date based on Plan
        expiration_date = calculate_expiration_from_plan(instance.plan)
        
        if expiration_date:
            logger.info(
                f"Setting RADIUS expiration for {username}: "
                f"Plan={instance.plan.name if instance.plan else 'No Plan'}, "
                f"ValidityType={instance.plan.validity_type if instance.plan else 'N/A'}, "
                f"Expires={expiration_date.strftime('%b %d %Y %H:%M:%S')}"
            )
        else:
            logger.info(f"RADIUS user {username} has unlimited validity (no expiration)")
        
        # Create the credentials (triggers the sync_credentials_to_radius signal above)
        # Pick up router, ip_pool, and assigned IP if stashed by the service serializer
        radius_router_id = getattr(instance, '_radius_router_id', None)
        radius_ip_pool = getattr(instance, '_radius_ip_pool', '') or ''
        radius_assigned_ip_id = getattr(instance, '_radius_assigned_ip_id', None)
        
        create_kwargs = dict(
            customer=customer,
            username=username,
            password=password,
            bandwidth_profile=profile,
            connection_type=conn_type,
            is_enabled=instance.status == 'ACTIVE',
            simultaneous_use=1,
            expiration_date=expiration_date,  # 🎯 CRITICAL: Set expiration
            # 🎯 CRITICAL: Stamp subscription_activated_at on creation if service is ACTIVE
            subscription_activated_at=timezone.now() if instance.status == 'ACTIVE' else None,
        )
        
        if radius_router_id:
            from apps.network.models.router_models import Router
            try:
                create_kwargs['router'] = Router.objects.get(pk=radius_router_id)
                logger.info(f"Assigned router ID {radius_router_id} to {username}")
            except Router.DoesNotExist:
                logger.warning(f"Router {radius_router_id} not found for RADIUS cred creation")
        
        if radius_ip_pool:
            create_kwargs['ip_pool'] = radius_ip_pool
            logger.info(f"Assigned IP pool '{radius_ip_pool}' to {username}")
        
        # 🎯 Cloud-Led IPAM: Assign specific IP
        assigned_ip_obj = None
        if radius_assigned_ip_id:
            from apps.network.models.ipam_models import IPAddress
            try:
                assigned_ip_obj = IPAddress.objects.get(pk=radius_assigned_ip_id)
                create_kwargs['assigned_ip_address'] = assigned_ip_obj
                create_kwargs['static_ip'] = assigned_ip_obj.ip_address  # Sync legacy field
                logger.info(f"Assigned static IP {assigned_ip_obj.ip_address} to {username}")
            except IPAddress.DoesNotExist:
                logger.warning(f"IPAddress {radius_assigned_ip_id} not found for RADIUS cred creation")
        
        creds = CustomerRadiusCredentials.objects.create(**create_kwargs)
        
        # Log activation timestamp if set
        if create_kwargs.get('subscription_activated_at'):
            logger.info(f"Stamped subscription_activated_at for {creds.username} on creation: {creds.subscription_activated_at}")
        
        # Mark the IP as ASSIGNED after credentials are created
        if assigned_ip_obj:
            assigned_ip_obj.assign_to_customer(customer, instance)
            logger.info(f"Cloud-Led: Assigned IP {assigned_ip_obj.ip_address} to {creds.username}")
        
        logger.info(f"Auto-created RADIUS credentials: username={username}"
                     f"{f', router_id={radius_router_id}' if radius_router_id else ''}"
                     f"{f', ip_pool={radius_ip_pool}' if radius_ip_pool else ''}"
                     f"{f', assigned_ip={assigned_ip_obj.ip_address if assigned_ip_obj else ''}' if assigned_ip_obj else ''}")
        
        # ============================================================
        # REMOVED: pppoe_new_subscription SMS call (toggle no longer exists)
        # New subscription SMS has been deprecated. Only the merged
        # pppoe_payment template should be used for payment confirmations.
        # ============================================================
        
        # NOTE: Welcome SMS (pppoe_welcome) is sent from service_views.activate,
        # NOT from this signal, to avoid duplicate SMS.
        
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
    
    # 🟢 FIXED: Handle speed unit conversion properly
    speed_unit = getattr(plan, 'speed_unit', 'MBPS') or 'MBPS'
    
    if speed_unit == 'KBPS':
        download_kbps = plan.download_speed or 10
        upload_kbps = plan.upload_speed or 5
    elif speed_unit == 'GBPS':
        download_kbps = (plan.download_speed or 1) * 1_000_000  # Gbps → Kbps
        upload_kbps = (plan.upload_speed or 1) * 1_000_000
    else:  # MBPS default
        download_kbps = (plan.download_speed or 10) * 1000
        upload_kbps = (plan.upload_speed or 5) * 1000
    
    profile_name = f"plan_{plan.id}_{plan.code or 'auto'}"
    
    # Build defaults including burst and priority from the Plan
    defaults = {
        'description': f"Auto-created from plan: {plan.name}",
        'download_speed': download_kbps,
        'upload_speed': upload_kbps,
        'priority': getattr(plan, 'priority', 8) or 8,
        'is_active': True,
    }
    
    # 🟢 FIXED: Propagate burst settings with proper unit conversion
    if getattr(plan, 'burst_enabled', False) and plan.burst_download and plan.burst_upload:
        speed_unit = getattr(plan, 'speed_unit', 'MBPS') or 'MBPS'
        
        if speed_unit == 'KBPS':
            multiplier = 1
        elif speed_unit == 'GBPS':
            multiplier = 1_000_000  # Gbps → Kbps
        else:  # MBPS
            multiplier = 1000
        
        defaults['burst_download'] = plan.burst_download * multiplier
        defaults['burst_upload'] = plan.burst_upload * multiplier
        defaults['burst_threshold'] = (plan.burst_threshold or 0) * multiplier
        defaults['burst_time'] = plan.burst_time or 10
    
    profile, created = RadiusBandwidthProfile.objects.get_or_create(
        name=profile_name,
        defaults=defaults,
    )
    
    # Update if speeds/burst/priority changed
    if not created:
        needs_update = False
        if profile.download_speed != download_kbps or profile.upload_speed != upload_kbps:
            profile.download_speed = download_kbps
            profile.upload_speed = upload_kbps
            needs_update = True
        
        plan_priority = getattr(plan, 'priority', 8) or 8
        if profile.priority != plan_priority:
            profile.priority = plan_priority
            needs_update = True
        
        # 🟢 FIXED: Update burst settings with proper unit conversion
        if getattr(plan, 'burst_enabled', False) and plan.burst_download and plan.burst_upload:
            speed_unit = getattr(plan, 'speed_unit', 'MBPS') or 'MBPS'
            
            if speed_unit == 'KBPS':
                multiplier = 1
            elif speed_unit == 'GBPS':
                multiplier = 1_000_000  # Gbps → Kbps
            else:  # MBPS
                multiplier = 1000
            
            new_burst_dl = plan.burst_download * multiplier
            new_burst_ul = plan.burst_upload * multiplier
            if profile.burst_download != new_burst_dl or profile.burst_upload != new_burst_ul:
                profile.burst_download = new_burst_dl
                profile.burst_upload = new_burst_ul
                profile.burst_threshold = (plan.burst_threshold or 0) * multiplier
                profile.burst_time = plan.burst_time or 10
                needs_update = True
        
        if needs_update:
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
        cust_status = (instance.status or '').upper()
        
        if cust_status in ['SUSPENDED', 'INACTIVE', 'TERMINATED']:
            if credentials.is_enabled:
                credentials.is_enabled = False
                credentials.disabled_reason = f"Customer {cust_status.lower()}"
                credentials.save()
                logger.info(f"Disabled RADIUS for {cust_status.lower()} customer: {instance.customer_code}")
                
                # NOTE: pppoe_suspended SMS has been REMOVED - toggle no longer exists
                    
        elif cust_status == 'ACTIVE':
            if not credentials.is_enabled:
                credentials.is_enabled = True
                credentials.disabled_reason = ''
                credentials.save()
                logger.info(f"Enabled RADIUS for active customer: {instance.customer_code}")
                
                # NOTE: pppoe_resumed SMS has been REMOVED - toggle no longer exists
                
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
        
        # 🟢 FIXED: Handle plan bandwidth update with proper unit conversion
        speed_unit = getattr(instance, 'speed_unit', 'MBPS') or 'MBPS'
        
        if speed_unit == 'KBPS':
            download_kbps = instance.download_speed or 10
            upload_kbps = instance.upload_speed or 5
        elif speed_unit == 'GBPS':
            download_kbps = (instance.download_speed or 1) * 1_000_000  # Gbps → Kbps
            upload_kbps = (instance.upload_speed or 1) * 1_000_000
        else:  # MBPS default
            download_kbps = (instance.download_speed or 10) * 1000
            upload_kbps = (instance.upload_speed or 5) * 1000
        
        profile_name = f"plan_{instance.id}_{instance.code or 'auto'}"
        
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
# ⚠️ ROUTER/NAS SIGNALS - DISABLED ⚠️
# ────────────────────────────────────────────────────────────────
# These signals have been REMOVED because they cause conflicts with the
# main router provisioning flow. The Router model's save() method already
# handles syncing to the Nas table with the correct data (VPN IP, proper secret).
# 
# If you re-enable these, you risk overwriting the Nas table with:
# - Wrong IP address (using router.ip_address instead of router.vpn_ip_address)
# - Wrong secret (fallback 'testing123' instead of the actual radius_secret)
# - Causing RADIUS authentication failures for connected users
#
# The Router model's save() method is the SOURCE OF TRUTH for NAS records.
# All NAS entries should be created/updated/deleted from there.
# ────────────────────────────────────────────────────────────────
# @receiver(post_save, sender='network.Router')  # DISABLED
# def sync_router_to_nas(sender, instance, created, **kwargs):
#     """Sync router to RADIUS NAS table - DISABLED (handled by Router.save())."""
#     pass


# @receiver(post_delete, sender='network.Router')  # DISABLED
# def remove_router_from_nas(sender, instance, **kwargs):
#     """Remove router from RADIUS NAS table - DISABLED (handled by Router.delete())."""
#     pass


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
                
                # NOTE: pppoe_suspended SMS has been REMOVED - toggle no longer exists
                
        elif status == 'PAID':
            pending = customer.invoices.filter(
                status__in=['PENDING', 'OVERDUE', 'pending', 'overdue']
            ).exclude(id=instance.id).exists()
            
            if not pending and not credentials.is_enabled:
                credentials.is_enabled = True
                credentials.disabled_reason = ''
                credentials.save()
                logger.info(f"Restored RADIUS after payment: {instance.id}")
                
                # NOTE: pppoe_resumed SMS has been REMOVED - toggle no longer exists
                
    except Exception as e:
        logger.error(f"Failed to handle invoice status for RADIUS: {e}")
    finally:
        instance._is_processing_invoice = False


# ────────────────────────────────────────────────────────────────
# TENANT SIGNALS
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender='core.Tenant')
def configure_radius_for_new_tenant(sender, instance, created, **kwargs):
    # Tenant RADIUS onboarding now happens explicitly after successful tenant
    # schema migration. Avoid post_save provisioning to prevent schema-race
    # errors during registration.
    return