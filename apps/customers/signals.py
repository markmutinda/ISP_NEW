"""
Customer Signals — RADIUS cleanup, IP release, User cleanup on customer deletion,
auto-generation of billing account numbers for PPPoE/Static services, and
immutable tenant user ledger recording for billing audit.

When a Customer is deleted (via API or admin):
1. pre_delete: Remove RADIUS credentials from radcheck/radreply (FreeRADIUS)
2. pre_delete: Release any assigned IP addresses back to the pool
3. pre_delete: Stash the User reference for post-delete cleanup
4. pre_delete: Record 'customer_deleted' in TenantUserLedger (immutable, public schema)
5. post_delete: Delete the orphaned Django User account

When a ServiceConnection is terminated or deleted:
- Release any associated IP addresses back to the available pool
- Record 'service_deleted'/'service_terminated' in TenantUserLedger

When a ServiceConnection is created (PPPoE/Static):
- Auto-generate a billing_account_number if one hasn't been assigned yet
- Record 'service_created' in TenantUserLedger + insert BillableClientRecord

This ensures no orphaned RADIUS entries, IP addresses, User accounts, or missing billing numbers,
and provides an immutable audit trail that tenants cannot tamper with.
"""

import logging
from django.db.models.signals import pre_delete, post_delete, post_save, pre_save
from django.dispatch import receiver

# Import the actual model classes for direct references
from apps.customers.models import Customer, ServiceConnection

logger = logging.getLogger(__name__)


# ========== CUSTOMER-RELATED SIGNALS ==========

@receiver(pre_delete, sender=Customer)
def cleanup_radius_on_customer_delete(sender, instance, **kwargs):
    """
    Remove RADIUS entries BEFORE the Customer row is deleted.

    Uses pre_delete so CustomerRadiusCredentials FK is still intact
    and we can read the username to delete from radcheck/radreply.
    """
    try:
        if hasattr(instance, 'radius_credentials'):
            credentials = instance.radius_credentials
            username = credentials.username
            
            # Call the model helper which delegates to RadiusSyncService
            credentials.delete_from_radius()
            logger.info(
                f"RADIUS cleanup for customer {instance.customer_code}: "
                f"deleted RADIUS user '{username}'"
            )
    except Exception as e:
        # Log but do NOT block the deletion
        logger.error(
            f"Failed RADIUS cleanup for customer {instance.customer_code}: {e}"
        )


@receiver(pre_delete, sender=Customer)
def release_customer_ips_on_delete(sender, instance, **kwargs):
    """
    Release any IP addresses assigned to this customer BEFORE the Customer is deleted.
    
    This ensures IPs are properly returned to the available pool when
    a customer account is completely removed.
    """
    try:
        # Find all IPs assigned to this customer through the 'assigned_ips' related_name
        if hasattr(instance, 'assigned_ips'):
            assigned_ips = instance.assigned_ips.all()
            
            if assigned_ips.exists():
                ip_count = assigned_ips.count()
                logger.info(f"Releasing {ip_count} IP address(es) for deleted customer {instance.customer_code}")
                
                for ip in assigned_ips:
                    try:
                        ip.release()
                        logger.debug(f"Released IP {ip.ip_address} for customer {instance.customer_code}")
                    except Exception as e:
                        logger.error(f"Failed to release IP {ip.ip_address} for customer {instance.customer_code}: {e}")
                        continue
    except Exception as e:
        logger.error(f"Error releasing IPs for customer {instance.customer_code}: {e}")


@receiver(pre_delete, sender=Customer)
def stash_user_for_cleanup(sender, instance, **kwargs):
    """
    Stash the User ID before Customer is deleted.
    
    Customer.user is OneToOneField(on_delete=CASCADE) which means
    deleting the User cascades to delete the Customer, but deleting
    the Customer does NOT auto-delete the User. We handle that in
    post_delete below.
    """
    try:
        if instance.user_id:
            instance._user_id_to_delete = instance.user_id
    except Exception as e:
        logger.error(f"Error stashing user for cleanup: {e}")


@receiver(post_delete, sender=Customer)
def cleanup_user_on_customer_delete(sender, instance, **kwargs):
    """
    Delete the orphaned Django User after the Customer is gone.
    
    This prevents orphan User accounts from accumulating.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()
    
    user_id = getattr(instance, '_user_id_to_delete', None)
    if not user_id:
        return
    
    try:
        user = User.objects.filter(id=user_id).first()
        if user:
            logger.info(
                f"Deleting orphaned User (id={user.id}, email={user.email}) "
                f"after customer {instance.customer_code} was deleted"
            )
            user.delete()
    except Exception as e:
        logger.error(f"Failed to delete User id={user_id}: {e}")


# ========== SERVICE CONNECTION SIGNALS ==========

@receiver(post_save, sender=ServiceConnection)
def release_ip_on_service_termination(sender, instance, created, **kwargs):
    """
    If a service connection is terminated, release any 
    associated IP addresses back to the available pool.
    
    This prevents IP leaks by ensuring IPs are properly
    returned to the pool when a customer's service ends.
    """
    # Only run if status is being set to TERMINATED (and not on initial creation)
    if not created and instance.status == 'TERMINATED':
        try:
            # Find all IPs assigned to this specific connection through the 'ip_addresses' related_name
            if hasattr(instance, 'ip_addresses'):
                assigned_ips = instance.ip_addresses.all()
                
                if assigned_ips.exists():
                    ip_count = assigned_ips.count()
                    logger.info(f"Releasing {ip_count} IP address(es) for terminated service {instance.id}")
                    
                    for ip in assigned_ips:
                        try:
                            # This calls the release() method in apps/network/models/ipam_models.py
                            ip.release()
                            logger.debug(f"Released IP {ip.ip_address} for service {instance.id}")
                        except Exception as e:
                            logger.error(f"Failed to release IP {ip.ip_address} for service {instance.id}: {e}")
                            # Continue with other IPs even if one fails
                            continue
        except Exception as e:
            logger.error(f"Error releasing IPs for terminated service {instance.id}: {e}")


@receiver(pre_delete, sender=ServiceConnection)
def cleanup_ip_on_service_deletion(sender, instance, **kwargs):
    """
    When a service connection is being deleted, ensure any 
    assigned IPs are released back to the pool.
    
    This handles cases where a service might be deleted
    directly rather than being terminated first.
    """
    try:
        # Find all IPs assigned to this specific connection
        if hasattr(instance, 'ip_addresses'):
            assigned_ips = instance.ip_addresses.all()
            
            if assigned_ips.exists():
                ip_count = assigned_ips.count()
                logger.info(f"Releasing {ip_count} IP address(es) for deleted service {instance.id}")
                
                for ip in assigned_ips:
                    try:
                        ip.release()
                        logger.debug(f"Released IP {ip.ip_address} for deleted service {instance.id}")
                    except Exception as e:
                        logger.error(f"Failed to release IP {ip.ip_address} for deleted service {instance.id}: {e}")
                        continue
    except Exception as e:
        logger.error(f"Error releasing IPs for deleted service {instance.id}: {e}")


# ========== BILLING ACCOUNT NUMBER AUTO-GENERATION ==========

@receiver(pre_save, sender=ServiceConnection)
def auto_generate_billing_account_number(sender, instance, **kwargs):
    """
    Auto-generate billing_account_number for PPPoE/Static services.
    Uses the customer's phone number (last 9 digits, e.g. 712345678).
    Falls back to sequential code if phone unavailable or duplicate.
    """
    auth_type = (instance.auth_connection_type or '').upper()
    if auth_type not in ('PPPOE', 'STATIC'):
        return

    if instance.billing_account_number and instance.billing_account_number.strip():
        return

    if not instance.customer_id:
        return

    try:
        customer = instance.customer
        phone = ''
        if customer.user and customer.user.phone_number:
            # Strip to digits only, take last 9 digits (e.g. 712345678)
            digits = ''.join(ch for ch in customer.user.phone_number if ch.isdigit())
            if digits.startswith('254') and len(digits) >= 12:
                phone = digits[3:]  # remove 254 prefix → 9 digits
            elif digits.startswith('0') and len(digits) >= 10:
                phone = digits[1:]  # remove leading 0 → 9 digits
            else:
                phone = digits[-9:] if len(digits) >= 9 else digits

        if phone:
            # Check uniqueness — if taken, append a suffix
            candidate = phone
            if not ServiceConnection.objects.exclude(pk=instance.pk).filter(
                billing_account_number=candidate
            ).exists():
                instance.billing_account_number = candidate
                logger.info(f"Billing account set to phone {candidate} for customer {instance.customer_id}")
                return
            
            # Phone already taken (e.g. same customer, second service) — append 'B', 'C', etc.
            for suffix in ['B', 'C', 'D', 'E']:
                candidate = phone + suffix
                if not ServiceConnection.objects.exclude(pk=instance.pk).filter(
                    billing_account_number=candidate
                ).exists():
                    instance.billing_account_number = candidate
                    logger.info(f"Billing account set to {candidate} for customer {instance.customer_id}")
                    return

        # Fallback to existing sequential logic
        from apps.customers.billing_account import generate_billing_account_number
        account_number = generate_billing_account_number(instance.customer, instance)
        instance.billing_account_number = account_number
        logger.info(f"Fallback billing account {account_number} for customer {instance.customer_id}")

    except Exception as e:
        logger.error(f"Failed to auto-generate billing account number: {e}")


# ========== OPTIONAL AUDIT SIGNAL ==========

@receiver(post_save, sender=ServiceConnection)
def log_ip_assignment_changes(sender, instance, created, **kwargs):
    """
    Optional: Log when IPs are assigned to services for audit purposes.
    This helps track IP assignment history.
    """
    try:
        if created:
            # New service created - check if it has IPs assigned
            if hasattr(instance, 'ip_addresses'):
                assigned_ips = instance.ip_addresses.all()
                if assigned_ips.exists():
                    logger.info(f"New service {instance.id} created with {assigned_ips.count()} IP address(es)")
    except Exception as e:
        # Don't let logging failures break anything
        pass


# ========== TENANT USER LEDGER — IMMUTABLE AUDIT TRAIL ==========

def _get_tenant_for_ledger():
    """Get the current tenant from the DB connection (set by django-tenants middleware)."""
    try:
        from django.db import connection
        tenant = getattr(connection, 'tenant', None)
        if tenant and tenant.schema_name != 'public':
            return tenant
    except Exception:
        pass
    return None


def _get_user_type(service):
    """Map ServiceConnection.auth_connection_type to ledger user_type."""
    auth_type = (service.auth_connection_type or '').upper()
    mapping = {
        'PPPOE': 'pppoe',
        'HOTSPOT': 'hotspot',
        'STATIC': 'static',
        'DYNAMIC': 'dhcp',
    }
    return mapping.get(auth_type, 'other')


@receiver(post_save, sender=Customer)
def ledger_record_customer_created(sender, instance, created, **kwargs):
    """Record customer creation in the immutable tenant ledger."""
    if not created:
        return
    tenant = _get_tenant_for_ledger()
    if not tenant:
        return
    try:
        from apps.subscriptions.models import TenantUserLedger
        TenantUserLedger.record(
            tenant=tenant,
            event='customer_created',
            user_type='other',
            customer_code=instance.customer_code or '',
            customer_name=instance.full_name or '',
            phone_number=getattr(instance.user, 'phone_number', '') if instance.user_id else '',
        )
    except Exception as e:
        logger.error(f"Ledger: failed to record customer_created for {instance.customer_code}: {e}")


@receiver(pre_delete, sender=Customer)
def ledger_record_customer_deleted(sender, instance, **kwargs):
    """Record customer deletion in the immutable tenant ledger BEFORE deletion."""
    tenant = _get_tenant_for_ledger()
    if not tenant:
        return
    try:
        from apps.subscriptions.models import TenantUserLedger
        TenantUserLedger.record(
            tenant=tenant,
            event='customer_deleted',
            user_type='other',
            customer_code=instance.customer_code or '',
            customer_name=instance.full_name or '',
            phone_number=getattr(instance.user, 'phone_number', '') if instance.user_id else '',
        )
    except Exception as e:
        logger.error(f"Ledger: failed to record customer_deleted for {instance.customer_code}: {e}")


@receiver(post_save, sender=ServiceConnection)
def ledger_record_service_lifecycle(sender, instance, created, **kwargs):
    """
    Record service creation, activation, suspension, and termination
    in the immutable tenant ledger.
    
    For PPPoE service creation: also inserts a BillableClientRecord
    into the active billing cycle so the user is counted immediately.
    """
    tenant = _get_tenant_for_ledger()
    if not tenant:
        return

    try:
        from apps.subscriptions.models import TenantUserLedger

        user_type = _get_user_type(instance)
        customer = instance.customer
        username = ''
        if hasattr(customer, 'radius_credentials'):
            try:
                username = customer.radius_credentials.username or ''
            except Exception:
                pass
        plan_name = instance.plan.name if instance.plan_id else ''

        base_kwargs = dict(
            user_type=user_type,
            customer_code=customer.customer_code or '',
            customer_name=customer.full_name or '',
            username=username,
            phone_number=getattr(customer.user, 'phone_number', '') if customer.user_id else '',
            plan_name=plan_name,
        )

        if created:
            TenantUserLedger.record(tenant=tenant, event='service_created', **base_kwargs)
        else:
            # Track status transitions
            status = (instance.status or '').upper()
            if status == 'ACTIVE':
                TenantUserLedger.record(tenant=tenant, event='service_activated', **base_kwargs)
            elif status == 'SUSPENDED':
                TenantUserLedger.record(tenant=tenant, event='service_suspended', **base_kwargs)
            elif status == 'TERMINATED':
                TenantUserLedger.record(tenant=tenant, event='service_terminated', **base_kwargs)
    except Exception as e:
        logger.error(f"Ledger: failed to record service lifecycle for service {instance.id}: {e}")


@receiver(pre_delete, sender=ServiceConnection)
def ledger_record_service_deleted(sender, instance, **kwargs):
    """Record service deletion in the immutable tenant ledger BEFORE deletion."""
    tenant = _get_tenant_for_ledger()
    if not tenant:
        return
    try:
        from apps.subscriptions.models import TenantUserLedger
        user_type = _get_user_type(instance)
        customer = instance.customer
        username = ''
        if hasattr(customer, 'radius_credentials'):
            try:
                username = customer.radius_credentials.username or ''
            except Exception:
                pass

        TenantUserLedger.record(
            tenant=tenant,
            event='service_deleted',
            user_type=user_type,
            customer_code=customer.customer_code or '',
            customer_name=customer.full_name or '',
            username=username,
            phone_number=getattr(customer.user, 'phone_number', '') if customer.user_id else '',
            plan_name=instance.plan.name if instance.plan_id else '',
        )
    except Exception as e:
        logger.error(f"Ledger: failed to record service_deleted for service {instance.id}: {e}")