"""
RADIUS Sync Service - Synchronize customers with RADIUS database

This service handles:
1. Creating RADIUS users from customers
2. Updating RADIUS attributes when subscription changes
3. Disabling/enabling users based on payment status
4. Syncing bandwidth profiles to RADIUS groups
5. Registering routers as NAS entries
6. DUAL-WRITE: Syncing to public schema for multi-tenant RADIUS auth
"""

import re
import logging
from typing import Optional, Dict, List, Any
from django.utils import timezone
from django.db import transaction, connection

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
    
    MULTI-TENANT ARCHITECTURE:
    - ORM writes go to tenant schema (for Admin UI visibility)
    - Raw SQL writes go to public schema (for FreeRADIUS authentication)
    - This "dual-write" enables single RADIUS server for all tenants
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
    # PUBLIC SCHEMA SYNC (MULTI-TENANT RADIUS SUPPORT)
    # ────────────────────────────────────────────────────────────────
    
    def _get_tenant_schema(self) -> str:
        """Get current tenant schema name from Django connection."""
        try:
            return connection.schema_name
        except AttributeError:
            return 'public'
    
    def _generate_unique_username(self, base_username: str, tenant_schema: str = None) -> str:
        """
        Generate globally unique RADIUS username for multi-tenant environment.
        
        Format: {tenant_prefix}_{username}
        Example: yellow_254712345678, blue_254798765432
        
        This prevents username collisions across tenants when all users
        are stored in public.radcheck for FreeRADIUS.
        """
        schema = tenant_schema or self._get_tenant_schema()
        prefix = schema.replace('tenant_', '').lower()
        
        # Sanitize username (alphanumeric, underscore, hyphen, @ only)
        clean_username = re.sub(r'[^a-zA-Z0-9_@.-]', '', str(base_username))
        
        # Max 64 chars for RADIUS username
        max_base_len = 64 - len(prefix) - 1
        clean_username = clean_username[:max_base_len]
        
        return f"{prefix}_{clean_username}"
    
    def _sync_to_public_schema(
        self,
        username: str,
        password: str,
        check_attributes: Dict[str, str] = None,
        reply_attributes: Dict[str, str] = None
    ) -> bool:
        """
        Write RADIUS user to PUBLIC schema for FreeRADIUS authentication.
        
        This is the CRITICAL method for multi-tenant RADIUS support.
        FreeRADIUS only queries public schema, so all users must be synced here.
        
        Args:
            username: Globally unique username (e.g., 'yellow_254712345678')
            password: Cleartext password
            check_attributes: Additional check attributes (Expiration, etc.)
            reply_attributes: Reply attributes (Rate-Limit, Framed-IP, etc.)
            
        Returns:
            True if sync succeeded
        """
        tenant_schema = self._get_tenant_schema()
        check_attributes = check_attributes or {}
        reply_attributes = reply_attributes or {}
        
        try:
            with connection.cursor() as cursor:
                # 1. Delete existing entries for this user in public schema
                cursor.execute(
                    "DELETE FROM public.radcheck WHERE username = %s",
                    [username]
                )
                cursor.execute(
                    "DELETE FROM public.radreply WHERE username = %s",
                    [username]
                )
                
                # 2. Insert Password (Cleartext-Password)
                cursor.execute(
                    """
                    INSERT INTO public.radcheck 
                        (username, attribute, op, value, tenant_schema, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
                    """,
                    [username, self.ATTR_PASSWORD, ':=', password, tenant_schema]
                )
                
                # 3. Insert additional check attributes
                for attr, value in check_attributes.items():
                    cursor.execute(
                        """
                        INSERT INTO public.radcheck 
                            (username, attribute, op, value, tenant_schema, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
                        """,
                        [username, attr, ':=', str(value), tenant_schema]
                    )
                
                # 4. Insert reply attributes (bandwidth, IP, etc.)
                for attr, value in reply_attributes.items():
                    cursor.execute(
                        """
                        INSERT INTO public.radreply 
                            (username, attribute, op, value, tenant_schema, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
                        """,
                        [username, attr, '=', str(value), tenant_schema]
                    )
            
            logger.info(f"[PUBLIC SYNC] User {username} synced to public schema (tenant: {tenant_schema})")
            return True
            
        except Exception as e:
            logger.error(f"[PUBLIC SYNC] Failed to sync {username} to public schema: {e}")
            return False
    
    def _delete_from_public_schema(self, username: str) -> bool:
        """
        Remove a RADIUS user from public schema.
        
        Called when deleting a user from tenant or during cleanup.
        """
        try:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM public.radcheck WHERE username = %s", [username])
                cursor.execute("DELETE FROM public.radreply WHERE username = %s", [username])
                cursor.execute("DELETE FROM public.radusergroup WHERE username = %s", [username])
            
            logger.info(f"[PUBLIC SYNC] Deleted user {username} from public schema")
            return True
            
        except Exception as e:
            logger.error(f"[PUBLIC SYNC] Failed to delete {username} from public schema: {e}")
            return False
    
    def _update_public_schema_status(self, username: str, enabled: bool) -> bool:
        """
        Enable or disable a user in public schema.
        
        Disabled users get Auth-Type := Reject which blocks authentication.
        """
        tenant_schema = self._get_tenant_schema()
        
        try:
            with connection.cursor() as cursor:
                if enabled:
                    # Remove Auth-Type := Reject to enable
                    cursor.execute(
                        """
                        DELETE FROM public.radcheck 
                        WHERE username = %s AND attribute = 'Auth-Type' AND value = 'Reject'
                        """,
                        [username]
                    )
                    logger.info(f"[PUBLIC SYNC] Enabled user {username} in public schema")
                else:
                    # Check if already disabled
                    cursor.execute(
                        """
                        SELECT id FROM public.radcheck 
                        WHERE username = %s AND attribute = 'Auth-Type' AND value = 'Reject'
                        """,
                        [username]
                    )
                    if not cursor.fetchone():
                        # Add Auth-Type := Reject to disable
                        cursor.execute(
                            """
                            INSERT INTO public.radcheck 
                                (username, attribute, op, value, tenant_schema, created_at, updated_at)
                            VALUES (%s, 'Auth-Type', ':=', 'Reject', %s, NOW(), NOW())
                            """,
                            [username, tenant_schema]
                        )
                    logger.info(f"[PUBLIC SYNC] Disabled user {username} in public schema")
            
            return True
            
        except Exception as e:
            logger.error(f"[PUBLIC SYNC] Failed to update status for {username}: {e}")
            return False
    
    def _sync_nas_to_public_schema(self, nasname: str, shortname: str, secret: str, 
                                    nas_type: str = 'mikrotik', description: str = None) -> bool:
        """
        Register a NAS (router) in public schema for FreeRADIUS.
        
        NAS entries must be in public.nas for FreeRADIUS to accept RADIUS
        requests from routers.
        """
        tenant_schema = self._get_tenant_schema()
        
        try:
            with connection.cursor() as cursor:
                # Upsert NAS entry
                cursor.execute(
                    """
                    INSERT INTO public.nas 
                        (nasname, shortname, type, secret, description, tenant_schema)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (nasname) DO UPDATE SET
                        shortname = EXCLUDED.shortname,
                        type = EXCLUDED.type,
                        secret = EXCLUDED.secret,
                        description = EXCLUDED.description,
                        tenant_schema = EXCLUDED.tenant_schema
                    """,
                    [nasname, shortname[:32], nas_type, secret, description or '', tenant_schema]
                )
            
            logger.info(f"[PUBLIC SYNC] NAS {nasname} synced to public schema")
            return True
            
        except Exception as e:
            logger.error(f"[PUBLIC SYNC] Failed to sync NAS {nasname}: {e}")
            return False
    
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
            
            logger.info(f"Created RADIUS user in tenant schema: {username}")
            
            # ════════════════════════════════════════════════════════════
            # DUAL-WRITE: Sync to public schema for FreeRADIUS
            # ════════════════════════════════════════════════════════════
            self._sync_to_public_schema(
                username=username,
                password=password,
                check_attributes=attributes,
                reply_attributes=reply_attributes
            )
            
            return {
                'username': username,
                'customer_id': str(customer.id) if customer else None,
                'profile': profile.name if profile else None,
                'groupname': groupname,
                'check_attributes': len(attributes) + 1,  # +1 for password
                'reply_attributes': len(reply_attributes),
                'public_sync': True  # Indicates dual-write was performed
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
            
            logger.info(f"Updated RADIUS user in tenant schema: {username}")
            
            # ════════════════════════════════════════════════════════════
            # DUAL-WRITE: Sync changes to public schema
            # ════════════════════════════════════════════════════════════
            if password or reply_attributes:
                # Get current password if not provided
                current_password = password
                if not current_password:
                    pwd_entry = RadCheck.objects.filter(
                        username=username,
                        attribute=self.ATTR_PASSWORD
                    ).first()
                    current_password = pwd_entry.value if pwd_entry else ''
                
                self._sync_to_public_schema(
                    username=username,
                    password=current_password,
                    check_attributes=attributes or {},
                    reply_attributes=reply_attributes or {}
                )
            
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
        
        # Add Auth-Type := Reject in tenant schema
        RadCheck.objects.update_or_create(
            username=username,
            attribute=self.ATTR_AUTH_TYPE,
            defaults={'op': ':=', 'value': 'Reject'}
        )
        
        # ════════════════════════════════════════════════════════════
        # DUAL-WRITE: Disable in public schema
        # ════════════════════════════════════════════════════════════
        self._update_public_schema_status(username, enabled=False)
        
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
        
        # ════════════════════════════════════════════════════════════
        # DUAL-WRITE: Enable in public schema
        # ════════════════════════════════════════════════════════════
        self._update_public_schema_status(username, enabled=True)
        
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
        
        # ════════════════════════════════════════════════════════════
        # DUAL-WRITE: Delete from public schema
        # ════════════════════════════════════════════════════════════
        self._delete_from_public_schema(username)
        
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

    def bulk_update_plan_users(self, plan, profile=None) -> Dict[str, Any]:
        """
        Update all RADIUS users on a plan when the plan changes.
        
        Called when bandwidth or other plan attributes are modified.
        
        Args:
            plan: billing.Plan model instance
            profile: Optional RadiusBandwidthProfile to use
            
        Returns:
            Dict with update statistics
        """
        from apps.radius.models import RadCheck
        
        # Find all active service connections on this plan
        # ServiceConnection uses 'plan' FK to billing.Plan with related_name='service_connections'
        connections = plan.service_connections.filter(
            status='ACTIVE'
        )
        
        # Get all customer IDs from these connections
        customer_ids = connections.values_list('customer_id', flat=True).distinct()
        
        # Get all RADIUS usernames for these customers
        usernames = RadCheck.objects.filter(
            customer_id__in=customer_ids,
            attribute='Cleartext-Password'
        ).values_list('username', flat=True).distinct()
        
        stats = {
            'total': len(usernames),
            'updated': 0,
            'errors': 0
        }
        
        if not usernames:
            logger.info(f"No RADIUS users found for plan {plan.name}")
            return stats
        
        # Get bandwidth from plan (in Mbps) and convert to kbps
        download_kbps = (plan.download_speed or 10) * 1000
        upload_kbps = (plan.upload_speed or 5) * 1000
        
        # Use profile if provided, otherwise use plan values
        if profile:
            download_kbps = profile.download_speed
            upload_kbps = profile.upload_speed
        
        for username in usernames:
            try:
                self.set_user_bandwidth(
                    username=username,
                    download_kbps=download_kbps,
                    upload_kbps=upload_kbps,
                )
                stats['updated'] += 1
            except Exception as e:
                logger.error(f"Error updating RADIUS user {username}: {e}")
                stats['errors'] += 1
        
        logger.info(f"Plan update sync complete for {plan.name}: {stats}")
        return stats
