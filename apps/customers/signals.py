"""
Customer Signals — RADIUS cleanup & User cleanup on customer deletion.

When a Customer is deleted (via API or admin):
1. pre_delete: Remove RADIUS credentials from radcheck/radreply (FreeRADIUS)
2. pre_delete: Stash the User reference for post-delete cleanup
3. post_delete: Delete the orphaned Django User account

This ensures no orphaned RADIUS entries or User accounts remain.
"""

import logging
from django.db.models.signals import pre_delete, post_delete
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(pre_delete, sender='customers.Customer')
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


@receiver(pre_delete, sender='customers.Customer')
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


@receiver(post_delete, sender='customers.Customer')
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
