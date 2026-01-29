"""
RADIUS Sync Service - Synchronize customers with RADIUS database

This service handles:
1. Creating RADIUS users from customers
2. Updating RADIUS attributes when subscription changes
3. Disabling/enabling users based on payment status
4. Syncing bandwidth profiles to RADIUS groups
5. Registering routers as NAS entries
"""

import logging
from typing import Optional, Dict, List, Any
from django.utils import timezone
from django.db import transaction

from ..models import (
    RadCheck,
    RadReply,
    RadUserGroup,
    RadGroupCheck,
    RadGroupReply,
    Nas,
    RadiusBandwidthProfile
)

logger = logging.getLogger(__name__)


class RadiusSyncService:
    """
    Service for synchronizing customers and routers with RADIUS database.
    """
    
    # Common RADIUS attributes
    ATTR_PASSWORD = 'Cleartext-Password'
    ATTR_EXPIRATION = 'Expiration'
    ATTR_SIMULTANEOUS_USE = 'Simultaneous-Use'
    ATTR_AUTH_TYPE = 'Auth-Type'
    
    # Reply attributes
    ATTR_RATE_LIMIT = 'Mikrotik-Rate-Limit'
    ATTR_SESSION_TIMEOUT = 'Session-Timeout'
    ATTR_IDLE_TIMEOUT = 'Idle-Timeout'
    ATTR_FRAMED_IP = 'Framed-IP-Address'
    ATTR_FRAMED_POOL = 'Framed-Pool'
    
    # ────────────────────────────────────────────────────────────────
    # USER MANAGEMENT
    # ────────────────────────────────────────────────────────────────
    
    def create_radius_user(
        self,
        username: str,
        password: str,
        customer=None,
        profile: RadiusBandwidthProfile = None,
        attributes: Dict[str, str] = None,
        reply_attributes: Dict[str, str] = None,
        groupname: str = None
    ) -> Dict[str, Any]:
        """
        Create a RADIUS user with check and reply attributes.
        
        Args:
            username: RADIUS username
            password: Password (stored as Cleartext-Password)
            customer: Optional Customer model instance
            profile: Optional bandwidth profile
            attributes: Additional check attributes
            reply_attributes: Additional reply attributes
            groupname: Optional group to assign user to
            
        Returns:
            Dict with created user info
        """
        attributes = attributes or {}
        reply_attributes = reply_attributes or {}
        
        with transaction.atomic():
            # Remove existing entries for this user
            RadCheck.objects.filter(username=username).delete()
            RadReply.objects.filter(username=username).delete()
            RadUserGroup.objects.filter(username=username).delete()
            
            # Create password check
            RadCheck.objects.create(
                username=username,
                attribute=self.ATTR_PASSWORD,
                op=':=',
                value=password,
                customer=customer
            )
            
            # Add additional check attributes
            for attr, value in attributes.items():
                RadCheck.objects.create(
                    username=username,
                    attribute=attr,
                    op=':=',
                    value=str(value),
                    customer=customer
                )
            
            # Add reply attributes from profile
            if profile:
                profile_attrs = profile.get_radius_attributes()
                reply_attributes.update(profile_attrs)
            
            # Create reply attributes
            for attr, value in reply_attributes.items():
                RadReply.objects.create(
                    username=username,
                    attribute=attr,
                    op=':=',
                    value=str(value),
                    customer=customer
                )
            
            # Assign to group if specified
            if groupname:
                RadUserGroup.objects.create(
                    username=username,
                    groupname=groupname,
                    priority=1
                )
            
            logger.info(f"Created RADIUS user: {username}")
            
            return {
                'username': username,
                'customer_id': str(customer.id) if customer else None,
                'profile': profile.name if profile else None,
                'groupname': groupname,
                'check_attributes': len(attributes) + 1,  # +1 for password
                'reply_attributes': len(reply_attributes)
            }
    
    def update_radius_user(
        self,
        username: str,
        password: str = None,
        attributes: Dict[str, str] = None,
        reply_attributes: Dict[str, str] = None
    ) -> bool:
        """
        Update an existing RADIUS user.
        
        Args:
            username: RADIUS username
            password: New password (optional)
            attributes: Check attributes to update
            reply_attributes: Reply attributes to update
            
        Returns:
            True if user was updated
        """
        # Check if user exists
        if not RadCheck.objects.filter(username=username).exists():
            logger.warning(f"RADIUS user not found: {username}")
            return False
        
        with transaction.atomic():
            # Update password if provided
            if password:
                RadCheck.objects.filter(
                    username=username,
                    attribute=self.ATTR_PASSWORD
                ).update(value=password)
            
            # Update check attributes
            if attributes:
                for attr, value in attributes.items():
                    RadCheck.objects.update_or_create(
                        username=username,
                        attribute=attr,
                        defaults={'op': ':=', 'value': str(value)}
                    )
            
            # Update reply attributes
            if reply_attributes:
                for attr, value in reply_attributes.items():
                    RadReply.objects.update_or_create(
                        username=username,
                        attribute=attr,
                        defaults={'op': ':=', 'value': str(value)}
                    )
            
            logger.info(f"Updated RADIUS user: {username}")
            return True
    
    def disable_radius_user(self, username: str, reason: str = "Disabled") -> bool:
        """
        Disable a RADIUS user by setting Auth-Type to Reject.
        
        Args:
            username: RADIUS username
            reason: Reason for disabling
            
        Returns:
            True if user was disabled
        """
        if not RadCheck.objects.filter(username=username).exists():
            return False
        
        # Add Auth-Type := Reject
        RadCheck.objects.update_or_create(
            username=username,
            attribute=self.ATTR_AUTH_TYPE,
            defaults={'op': ':=', 'value': 'Reject'}
        )
        
        logger.info(f"Disabled RADIUS user: {username} - {reason}")
        return True
    
    def enable_radius_user(self, username: str) -> bool:
        """
        Enable a previously disabled RADIUS user.
        
        Args:
            username: RADIUS username
            
        Returns:
            True if user was enabled
        """
        # Remove Auth-Type := Reject
        deleted, _ = RadCheck.objects.filter(
            username=username,
            attribute=self.ATTR_AUTH_TYPE,
            value='Reject'
        ).delete()
        
        if deleted > 0:
            logger.info(f"Enabled RADIUS user: {username}")
            return True
        return False
    
    def delete_radius_user(self, username: str) -> bool:
        """
        Completely remove a RADIUS user.
        
        Args:
            username: RADIUS username
            
        Returns:
            True if user was deleted
        """
        with transaction.atomic():
            RadCheck.objects.filter(username=username).delete()
            RadReply.objects.filter(username=username).delete()
            RadUserGroup.objects.filter(username=username).delete()
        
        logger.info(f"Deleted RADIUS user: {username}")
        return True
    
    def set_user_bandwidth(
        self,
        username: str,
        download_kbps: int,
        upload_kbps: int,
        burst_download: int = None,
        burst_upload: int = None
    ) -> bool:
        """
        Set bandwidth limit for a RADIUS user.
        
        Args:
            username: RADIUS username
            download_kbps: Download speed in kbps
            upload_kbps: Upload speed in kbps
            burst_download: Burst download in kbps (optional)
            burst_upload: Burst upload in kbps (optional)
            
        Returns:
            True if bandwidth was set
        """
        # Build MikroTik rate limit string
        rate_limit = f"{upload_kbps}k/{download_kbps}k"
        
        if burst_download and burst_upload:
            rate_limit = f"{rate_limit} {burst_upload}k/{burst_download}k 0/0 0/0 8"
        
        RadReply.objects.update_or_create(
            username=username,
            attribute=self.ATTR_RATE_LIMIT,
            defaults={'op': ':=', 'value': rate_limit}
        )
        
        logger.info(f"Set bandwidth for {username}: {rate_limit}")
        return True
    
    def set_user_expiration(self, username: str, expiration: timezone.datetime) -> bool:
        """
        Set expiration date for a RADIUS user.
        
        Args:
            username: RADIUS username
            expiration: Expiration datetime
            
        Returns:
            True if expiration was set
        """
        # Format: "Jan 01 2026 00:00:00"
        exp_str = expiration.strftime("%b %d %Y %H:%M:%S")
        
        RadCheck.objects.update_or_create(
            username=username,
            attribute=self.ATTR_EXPIRATION,
            defaults={'op': ':=', 'value': exp_str}
        )
        
        logger.info(f"Set expiration for {username}: {exp_str}")
        return True
    
    def set_static_ip(self, username: str, ip_address: str) -> bool:
        """
        Assign a static IP to a RADIUS user.
        
        Args:
            username: RADIUS username
            ip_address: Static IP address
            
        Returns:
            True if IP was set
        """
        RadReply.objects.update_or_create(
            username=username,
            attribute=self.ATTR_FRAMED_IP,
            defaults={'op': ':=', 'value': ip_address}
        )
        
        logger.info(f"Set static IP for {username}: {ip_address}")
        return True
    
    # ────────────────────────────────────────────────────────────────
    # GROUP MANAGEMENT
    # ────────────────────────────────────────────────────────────────
    
    def create_bandwidth_group(
        self,
        groupname: str,
        profile: RadiusBandwidthProfile
    ) -> bool:
        """
        Create a RADIUS group from a bandwidth profile.
        
        Args:
            groupname: Group name
            profile: Bandwidth profile
            
        Returns:
            True if group was created
        """
        with transaction.atomic():
            # Remove existing group attributes
            RadGroupReply.objects.filter(groupname=groupname).delete()
            
            # Create group reply attributes
            for attr, value in profile.get_radius_attributes().items():
                RadGroupReply.objects.create(
                    groupname=groupname,
                    attribute=attr,
                    op=':=',
                    value=str(value)
                )
        
        logger.info(f"Created RADIUS group: {groupname} from profile {profile.name}")
        return True
    
    def sync_all_bandwidth_profiles(self) -> int:
        """
        Sync all bandwidth profiles to RADIUS groups.
        
        Returns:
            Number of groups synced
        """
        profiles = RadiusBandwidthProfile.objects.filter(is_active=True)
        count = 0
        
        for profile in profiles:
            groupname = f"profile_{profile.name.lower().replace(' ', '_')}"
            self.create_bandwidth_group(groupname, profile)
            count += 1
        
        logger.info(f"Synced {count} bandwidth profiles to RADIUS groups")
        return count
    
    # ────────────────────────────────────────────────────────────────
    # NAS MANAGEMENT
    # ────────────────────────────────────────────────────────────────
    
    def register_nas(
        self,
        router,
        secret: str = None
    ) -> Nas:
        """
        Register a router as a NAS entry.
        
        Args:
            router: Router model instance
            secret: RADIUS shared secret (uses router.shared_secret if not provided)
            
        Returns:
            Nas instance
        """
        secret = secret or router.shared_secret
        
        nas, created = Nas.objects.update_or_create(
            router=router,
            defaults={
                'nasname': router.ip_address or f"router_{router.id}",
                'shortname': router.name[:32],
                'type': 'mikrotik' if router.router_type == 'mikrotik' else 'other',
                'secret': secret,
                'description': f"Netily Router: {router.name}"
            }
        )
        
        action = "Created" if created else "Updated"
        logger.info(f"{action} NAS entry for router: {router.name}")
        
        return nas
    
    def unregister_nas(self, router) -> bool:
        """
        Remove a router's NAS entry.
        
        Args:
            router: Router model instance
            
        Returns:
            True if NAS was removed
        """
        deleted, _ = Nas.objects.filter(router=router).delete()
        
        if deleted > 0:
            logger.info(f"Removed NAS entry for router: {router.name}")
            return True
        return False
    
    def sync_all_routers(self) -> int:
        """
        Sync all routers to NAS table.
        
        Returns:
            Number of routers synced
        """
        from apps.network.models import Router
        
        routers = Router.objects.filter(
            is_active=True,
            shared_secret__isnull=False
        ).exclude(shared_secret='')
        
        count = 0
        for router in routers:
            self.register_nas(router)
            count += 1
        
        logger.info(f"Synced {count} routers to NAS table")
        return count
    
    # ────────────────────────────────────────────────────────────────
    # CUSTOMER SYNC
    # ────────────────────────────────────────────────────────────────
    
    def sync_customer(self, customer) -> Dict[str, Any]:
        """
        Sync a customer to RADIUS.
        Creates/updates RADIUS user based on customer's subscription.
        
        Args:
            customer: Customer model instance
            
        Returns:
            Dict with sync results
        """
        # Generate username (phone number or custom)
        username = customer.phone_number or f"cust_{customer.id}"
        
        # Get active subscription
        subscription = getattr(customer, 'active_subscription', None)
        
        if not subscription:
            # No active subscription - disable user
            self.disable_radius_user(username, "No active subscription")
            return {'username': username, 'status': 'disabled', 'reason': 'no_subscription'}
        
        # Get plan details
        plan = subscription.plan
        
        # Build attributes
        check_attrs = {}
        reply_attrs = {}
        
        # Set bandwidth from plan
        if hasattr(plan, 'download_speed') and hasattr(plan, 'upload_speed'):
            rate_limit = f"{plan.upload_speed}k/{plan.download_speed}k"
            reply_attrs[self.ATTR_RATE_LIMIT] = rate_limit
        
        # Set expiration
        if subscription.end_date:
            check_attrs[self.ATTR_EXPIRATION] = subscription.end_date.strftime("%b %d %Y %H:%M:%S")
        
        # Set simultaneous use
        simultaneous = getattr(plan, 'simultaneous_sessions', 1)
        check_attrs[self.ATTR_SIMULTANEOUS_USE] = str(simultaneous)
        
        # Get or generate password
        password = getattr(customer, 'pppoe_password', None) or customer.phone_number
        
        # Create/update user
        result = self.create_radius_user(
            username=username,
            password=password,
            customer=customer,
            attributes=check_attrs,
            reply_attributes=reply_attrs
        )
        
        # Enable user if subscription is active
        if subscription.status == 'active':
            self.enable_radius_user(username)
            result['status'] = 'active'
        else:
            self.disable_radius_user(username, f"Subscription status: {subscription.status}")
            result['status'] = 'disabled'
        
        return result
    
    def sync_all_customers(self) -> Dict[str, int]:
        """
        Sync all customers to RADIUS.
        
        Returns:
            Dict with sync statistics
        """
        from apps.customers.models import Customer
        
        customers = Customer.objects.filter(is_active=True)
        
        stats = {
            'total': 0,
            'active': 0,
            'disabled': 0,
            'errors': 0
        }
        
        for customer in customers:
            try:
                result = self.sync_customer(customer)
                stats['total'] += 1
                
                if result.get('status') == 'active':
                    stats['active'] += 1
                else:
                    stats['disabled'] += 1
                    
            except Exception as e:
                logger.error(f"Error syncing customer {customer.id}: {e}")
                stats['errors'] += 1
        
        logger.info(f"Customer sync complete: {stats}")
        return stats
