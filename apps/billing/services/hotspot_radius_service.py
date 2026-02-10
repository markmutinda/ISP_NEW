"""
Hotspot RADIUS Service — Creates RADIUS credentials for hotspot sessions.

This is the critical piece that closes the loop:
  Payment confirmed → RADIUS credentials created → User can authenticate

Also handles:
- MAC-based auto-authentication for authorized devices (Smart TVs)
- Session expiration (FreeRADIUS Expiration attribute)
- Bandwidth limits (Mikrotik-Rate-Limit reply attribute)
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from django.conf import settings
from django.db import connection
from django.utils import timezone

from apps.radius.models import RadCheck, RadReply, RadUserGroup
from apps.radius.services.radius_sync_service import RadiusSyncService

logger = logging.getLogger(__name__)


class HotspotRadiusService:
    """
    Creates and manages RADIUS credentials for hotspot sessions.
    
    Uses the existing RadiusSyncService for dual-write (tenant + public schema).
    """
    
    def __init__(self):
        self.sync_service = RadiusSyncService()
    
    def create_hotspot_credentials(
        self,
        username: str,
        password: str,
        router,
        plan,
        expires_at: datetime,
        mac_address: str = '',
    ) -> bool:
        """
        Create RADIUS credentials for a hotspot session after payment.
        
        This writes to both tenant schema (for admin visibility) and
        public schema (for FreeRADIUS to actually authenticate).
        
        Args:
            username: The access code (e.g., "HS-A7B3C2")
            password: Same as username for hotspot simplicity
            router: Router model instance
            plan: HotspotPlan model instance
            expires_at: When the session expires (UTC)
            mac_address: Client MAC for Calling-Station-Id binding
        
        Returns:
            True if credentials were created successfully
        """
        try:
            # Build check attributes (authentication)
            check_attributes = {
                'Cleartext-Password': password,
            }
            
            # Simultaneous-Use: limit to 1 device per access code
            # (unless it's a MAC-auth entry which inherits from parent session)
            check_attributes['Simultaneous-Use'] = '1'
            
            # Build reply attributes (what the NAS enforces)
            reply_attributes = {}
            
            # Bandwidth limit (MikroTik format: rx/tx)
            if plan.speed_limit_mbps:
                speed_kbps = int(plan.speed_limit_mbps * 1024)
                # MikroTik-Rate-Limit format: rx-rate[/tx-rate] [rx-burst-rate/tx-burst-rate] [rx-burst-threshold/tx-burst-threshold] [rx-burst-time/tx-burst-time] [priority] [min-rx-rate/min-tx-rate]
                reply_attributes['Mikrotik-Rate-Limit'] = f'{speed_kbps}k/{speed_kbps}k'
            
            # Session timeout (duration in seconds)
            if plan.duration_minutes:
                reply_attributes['Session-Timeout'] = str(plan.duration_minutes * 60)
            
            # Data limit (if applicable)
            if plan.data_limit_mb and plan.data_limit_mb > 0:
                # Mikrotik-Total-Limit in bytes
                data_bytes = plan.data_limit_mb * 1024 * 1024
                reply_attributes['Mikrotik-Total-Limit'] = str(int(data_bytes))
            
            # Idle timeout (disconnect after 5 min idle)
            reply_attributes['Idle-Timeout'] = '300'
            
            # Create the RADIUS user via the sync service
            self.sync_service.create_radius_user(
                username=username,
                password=password,
                customer=None,  # Hotspot users don't have a customer record
                profile=None,   # We set attributes directly
                attributes=check_attributes,
                reply_attributes=reply_attributes,
            )
            
            # Set expiration (sync service handles UTC conversion)
            self.sync_service.set_user_expiration(username, expires_at)
            
            logger.info(
                f"Hotspot RADIUS credentials created: user={username} "
                f"plan={plan.name} expires={expires_at} mac={mac_address}"
            )
            return True
            
        except Exception as e:
            logger.error(f"Failed to create hotspot RADIUS credentials: {e}", exc_info=True)
            return False
    
    def create_mac_auth_entry(
        self,
        mac_address: str,
        router,
        plan,
        expires_at: datetime,
    ) -> bool:
        """
        Create a MAC-based authentication entry for Smart TVs and
        other devices that can't do interactive login.
        
        FreeRADIUS authenticates using the MAC as both username and password.
        Format: AA-BB-CC-DD-EE-FF (dashes, lowercase)
        
        Args:
            mac_address: Device MAC address
            router: Router model instance
            plan: HotspotPlan for bandwidth/data limits
            expires_at: When access expires
        
        Returns:
            True if entry was created
        """
        # FreeRADIUS MAC auth format: lowercase with dashes
        mac_username = mac_address.upper().replace(':', '-')
        mac_password = mac_username  # MAC auth uses MAC as password
        
        return self.create_hotspot_credentials(
            username=mac_username,
            password=mac_password,
            router=router,
            plan=plan,
            expires_at=expires_at,
            mac_address=mac_address,
        )
    
    def revoke_credentials(self, username: str) -> bool:
        """
        Revoke RADIUS credentials (e.g., on session expiry or admin disconnect).
        Also triggers CoA disconnect if the user is currently online.
        """
        try:
            self.sync_service.disable_radius_user(username)
            logger.info(f"Hotspot RADIUS credentials revoked: user={username}")
            return True
        except Exception as e:
            logger.error(f"Failed to revoke RADIUS credentials: {e}", exc_info=True)
            return False
    
    def extend_session(
        self,
        username: str,
        additional_minutes: int,
        new_expires_at: datetime,
    ) -> bool:
        """
        Extend an active session's expiration (e.g., user buys more time).
        """
        try:
            self.sync_service.set_user_expiration(username, new_expires_at)
            
            # Also update Session-Timeout
            new_timeout = str(additional_minutes * 60)
            self._update_reply_attribute(username, 'Session-Timeout', new_timeout)
            
            logger.info(
                f"Hotspot session extended: user={username} "
                f"new_expires={new_expires_at}"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to extend session: {e}", exc_info=True)
            return False
    
    def _update_reply_attribute(self, username: str, attribute: str, value: str):
        """Update a single reply attribute in both schemas."""
        # Tenant schema (ORM)
        RadReply.objects.filter(
            username=username, attribute=attribute
        ).update(value=value)
        
        # Public schema (raw SQL)
        tenant_schema = connection.schema_name
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE public.radreply
                SET value = %s
                WHERE username = %s AND attribute = %s AND tenant_schema = %s
                """,
                [value, username, attribute, tenant_schema]
            )
    
    def cleanup_expired_sessions(self) -> int:
        """
        Remove RADIUS entries for expired hotspot sessions.
        Called by the periodic Celery task.
        
        Returns: Number of entries cleaned up.
        """
        from apps.billing.models.hotspot_models import HotspotSession
        
        expired_sessions = HotspotSession.objects.filter(
            status='active',
            expires_at__lt=timezone.now()
        )
        
        count = 0
        for session in expired_sessions:
            if session.access_code:
                self.revoke_credentials(session.access_code)
                count += 1
            session.mark_expired()
        
        if count:
            logger.info(f"Cleaned up {count} expired hotspot RADIUS entries")
        
        return count
