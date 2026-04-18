"""
Customer Signals — RADIUS cleanup, IP release, User cleanup on customer deletion,
and auto-generation of billing account numbers for PPPoE/Static services.

When a Customer is deleted (via API or admin):
1. pre_delete: Remove RADIUS credentials from radcheck/radreply (FreeRADIUS)
2. pre_delete: Release any assigned IP addresses back to the pool
3. pre_delete: Stash the User reference for post-delete cleanup
4. post_delete: Delete the orphaned Django User account

When a ServiceConnection is terminated or deleted:
- Release any associated IP addresses back to the available pool

When a ServiceConnection is created (PPPoE/Static):
- Auto-generate a billing_account_number if one hasn't been assigned yet

This ensures no orphaned RADIUS entries, IP addresses, User accounts, or missing billing numbers.
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
    Auto-generate billing_account_number for PPPoE/Static services
    if one hasn't been assigned yet.
    
    This runs before save so the account number is available immediately
    after creation.
    """
    # Only for PPPoE and Static connection types
    auth_type = (instance.auth_connection_type or '').upper()
    if auth_type not in ('PPPOE', 'STATIC'):
        return

    # Don't overwrite if already set
    if instance.billing_account_number and instance.billing_account_number.strip():
        return

    # Skip if no PK yet on customer (shouldn't happen, but guard)
    if not instance.customer_id:
        return

    try:
        from apps.customers.billing_account import generate_billing_account_number
        account_number = generate_billing_account_number(instance.customer, instance)
        instance.billing_account_number = account_number
        logger.info(
            f"Auto-generated billing account number {account_number} "
            f"for customer {instance.customer_id}"
        )
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