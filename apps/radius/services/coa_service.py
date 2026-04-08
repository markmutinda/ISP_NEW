"""
RADIUS Change of Authorization (CoA) Service

Sends CoA Disconnect-Request packets to FreeRADIUS, which in turn
tells the MikroTik NAS to disconnect a user immediately.

Used for:
- Admin disconnects a hotspot user
- Session expires while user is online
- Plan downgrade/upgrade (disconnect → reconnect with new limits)
- Fraud detection (kill session immediately)

CoA flow:
  Django → FreeRADIUS (CoA port 3799) → MikroTik NAS

Requires: pyrad library (pip install pyrad)
"""

import logging
import socket
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# CoA port (RFC 5176 default)
COA_PORT = getattr(settings, 'RADIUS_COA_PORT', 3799)
COA_TIMEOUT = getattr(settings, 'RADIUS_COA_TIMEOUT', 5)


class CoAService:
    """
    Sends RADIUS Change of Authorization (CoA) packets.
    
    In the Cloud Controller architecture, CoA goes:
    Django (Cloud) → FreeRADIUS (CoA proxy) → MikroTik NAS
    
    This avoids needing direct access to the MikroTik from Django —
    everything goes through the VPN tunnel to FreeRADIUS.
    """
    
    def __init__(self, nas_ip: str = None, secret: str = None):
        """
        Args:
            nas_ip: IP of the FreeRADIUS server (default: VPN gateway 10.8.0.1)
            secret: RADIUS shared secret
        """
        self.nas_ip = nas_ip or getattr(settings, 'VPN_SERVER_IP', '10.8.0.1')
        self.secret = (secret or 'testing123').encode('utf-8')
    
    def disconnect_user(
        self,
        username: str,
        nas_ip_address: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> bool:
        """
        Send a Disconnect-Request (CoA) to terminate a user's session.
        
        Args:
            username: RADIUS username (access code or MAC)
            nas_ip_address: The NAS (router) IP where user is connected
            session_id: RADIUS Acct-Session-Id (more precise targeting)
        
        Returns:
            True if disconnect was acknowledged
        """
        try:
            return self.disconnect_user_via_coa(
                username=username,
                nas_ip=nas_ip_address,
                session_id=session_id,
            )
        except Exception as e:
            logger.error(f"CoA disconnect failed for {username}: {e}")
            return False
    
    def disconnect_user_via_coa(self, username: str, nas_ip: str, session_id: str = None) -> bool:
        """
        Sends a CoA Disconnect-Request UDP packet directly to the NAS.
        More reliable than the existing method - handles timeout gracefully.
        
        Args:
            username: RADIUS username to disconnect
            nas_ip: IP address of the NAS (router)
            session_id: Optional Acct-Session-Id for more precise targeting
        
        Returns:
            True if disconnect was acknowledged OR timeout (treat as disconnected)
        """
        import hashlib
        import os
        import socket
        import struct

        COA_CODE = 40  # Disconnect-Request
        COA_PORT = 3799

        # Build minimal RADIUS disconnect packet
        identifier = os.urandom(1)[0]
        authenticator = b'\x00' * 16  # Zeroed for request

        # Attributes
        def make_attr(attr_type: int, value: bytes) -> bytes:
            return bytes([attr_type, 2 + len(value)]) + value

        attrs = make_attr(1, username.encode())  # User-Name = type 1
        if nas_ip:
            import ipaddress
            packed_ip = ipaddress.IPv4Address(nas_ip).packed
            attrs += make_attr(4, packed_ip)  # NAS-IP-Address = type 4
        if session_id:
            attrs += make_attr(44, session_id.encode())  # Acct-Session-Id = type 44

        length = 20 + len(attrs)
        header = struct.pack('!BBH16s', COA_CODE, identifier, length, authenticator)
        packet = header + attrs

        # Compute proper authenticator: MD5(code+id+length+zeros+attrs+secret)
        md5 = hashlib.md5(packet + self.secret).digest()
        packet = struct.pack('!BBH', COA_CODE, identifier, length) + md5 + attrs

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(3)
            sock.sendto(packet, (nas_ip, COA_PORT))
            response, _ = sock.recvfrom(4096)
            sock.close()
            # 41 = Disconnect-ACK, 42 = Disconnect-NAK
            if response[0] == 41:
                logger.info(f"CoA disconnect ACK for {username}@{nas_ip}")
                return True
            else:
                logger.warning(f"CoA disconnect NAK for {username}@{nas_ip}: code={response[0]}")
                return False
        except socket.timeout:
            # Timeout means NAS is unreachable — session is already dead
            # Still return True so we close the DB record
            logger.info(f"CoA timeout for {username}@{nas_ip} — treating as disconnected")
            return True
        except Exception as e:
            logger.error(f"CoA error for {username}: {e}")
            return False
    
    def disconnect_mac(
        self,
        mac_address: str,
        nas_ip_address: Optional[str] = None,
    ) -> bool:
        """
        Disconnect a device by MAC address (Calling-Station-Id).
        
        Args:
            mac_address: Client MAC (format: AA:BB:CC:DD:EE:FF or AA-BB-CC-DD-EE-FF)
        """
        try:
            return self._send_coa_packet(
                code='disconnect',
                calling_station_id=mac_address.upper().replace(':', '-'),
                nas_ip_address=nas_ip_address,
            )
        except Exception as e:
            logger.error(f"CoA MAC disconnect failed for {mac_address}: {e}")
            return False
    
    def change_authorization(
        self,
        username: str,
        new_rate_limit: Optional[str] = None,
        new_session_timeout: Optional[int] = None,
        nas_ip_address: Optional[str] = None,
    ) -> bool:
        """
        Send a CoA packet to change a user's authorization attributes
        (e.g., bandwidth change without disconnecting).
        
        Note: MikroTik support for CoA attribute changes is limited.
        Often it's simpler to disconnect and let the user reconnect
        with new attributes from RADIUS.
        """
        try:
            reply_attrs = {}
            if new_rate_limit:
                reply_attrs['Mikrotik-Rate-Limit'] = new_rate_limit
            if new_session_timeout:
                reply_attrs['Session-Timeout'] = str(new_session_timeout)
            
            return self._send_coa_packet(
                code='coa',
                username=username,
                nas_ip_address=nas_ip_address,
                reply_attributes=reply_attrs,
            )
        except Exception as e:
            logger.error(f"CoA change auth failed for {username}: {e}")
            return False
    
    def _send_coa_packet(
        self,
        code: str = 'disconnect',
        username: Optional[str] = None,
        calling_station_id: Optional[str] = None,
        nas_ip_address: Optional[str] = None,
        session_id: Optional[str] = None,
        reply_attributes: Optional[dict] = None,
    ) -> bool:
        """
        Build and send a CoA/Disconnect-Request packet.
        
        Uses pyrad if available, falls back to raw socket.
        """
        try:
            return self._send_with_pyrad(
                code=code,
                username=username,
                calling_station_id=calling_station_id,
                nas_ip_address=nas_ip_address,
                session_id=session_id,
                reply_attributes=reply_attributes,
            )
        except ImportError:
            logger.warning("pyrad not installed, falling back to raw CoA packet")
            return self._send_raw_disconnect(username, nas_ip_address, session_id)
    
    def _send_with_pyrad(
        self,
        code: str,
        username: Optional[str] = None,
        calling_station_id: Optional[str] = None,
        nas_ip_address: Optional[str] = None,
        session_id: Optional[str] = None,
        reply_attributes: Optional[dict] = None,
    ) -> bool:
        """Send CoA using pyrad library."""
        from pyrad.client import Client
        from pyrad.dictionary import Dictionary
        from pyrad import packet
        
        # Use the default FreeRADIUS dictionary
        # In production, point to the actual dictionary file
        import os
        dict_path = os.path.join(
            settings.BASE_DIR, 'radius_config', 'dictionary'
        )
        
        # Fallback to pyrad default if our dictionary doesn't exist
        if not os.path.exists(dict_path):
            dict_path = None
        
        try:
            if dict_path:
                radius_dict = Dictionary(dict_path)
            else:
                # Create a minimal dictionary for the attributes we need
                radius_dict = self._get_minimal_dictionary()
            
            # Create client targeting FreeRADIUS
            client = Client(
                server=self.nas_ip,
                secret=self.secret,
                dict=radius_dict,
            )
            client.timeout = COA_TIMEOUT
            
            # Create packet
            if code == 'disconnect':
                pkt = client.CreateCoAPacket(code=packet.DisconnectRequest)
            else:
                pkt = client.CreateCoAPacket(code=packet.CoARequest)
            
            # Add attributes
            if username:
                pkt['User-Name'] = username
            if calling_station_id:
                pkt['Calling-Station-Id'] = calling_station_id
            if nas_ip_address:
                pkt['NAS-IP-Address'] = nas_ip_address
            if session_id:
                pkt['Acct-Session-Id'] = session_id
            
            # Add reply attributes for CoA changes
            if reply_attributes:
                for attr, value in reply_attributes.items():
                    try:
                        pkt[attr] = value
                    except Exception:
                        logger.warning(f"Could not add CoA attribute: {attr}={value}")
            
            # Send and get response
            response = client.SendPacket(pkt)
            
            if response.code in (packet.DisconnectACK, packet.CoAACK):
                logger.info(
                    f"CoA {code} success: user={username} mac={calling_station_id}"
                )
                return True
            else:
                logger.warning(
                    f"CoA {code} rejected: user={username} code={response.code}"
                )
                return False
                
        except Exception as e:
            logger.error(f"pyrad CoA error: {e}", exc_info=True)
            return False
    
    def _send_raw_disconnect(self, username: str, nas_ip: str = None, session_id: str = None) -> bool:
        """
        Send a raw RADIUS Disconnect-Request via UDP.
        Improved version that properly handles CoA packets directly to NAS.
        
        Args:
            username: RADIUS username to disconnect
            nas_ip: IP address of the NAS (router)
            session_id: Optional Acct-Session-Id for more precise targeting
        """
        import hashlib
        import os
        import socket
        import struct
        import ipaddress

        # RADIUS Disconnect-Request (Code 40)
        DISCONNECT_REQUEST = 40
        COA_PORT = 3799
        
        # Use provided NAS IP or fallback to configured
        target_ip = nas_ip or self.nas_ip
        
        # Build minimal RADIUS disconnect packet
        identifier = os.urandom(1)[0]
        
        # Attributes
        def make_attr(attr_type: int, value: bytes) -> bytes:
            return bytes([attr_type, 2 + len(value)]) + value
        
        attrs = make_attr(1, username.encode())  # User-Name = type 1
        
        if target_ip and target_ip != self.nas_ip:
            # Only add NAS-IP-Address if targeting a specific NAS
            try:
                packed_ip = ipaddress.IPv4Address(target_ip).packed
                attrs += make_attr(4, packed_ip)  # NAS-IP-Address = type 4
            except Exception:
                pass
        
        if session_id:
            attrs += make_attr(44, session_id.encode())  # Acct-Session-Id = type 44
        
        length = 20 + len(attrs)
        authenticator = b'\x00' * 16  # Zeroed for request
        
        # Build packet without auth
        header = struct.pack('!BBH16s', DISCONNECT_REQUEST, identifier, length, authenticator)
        packet = header + attrs
        
        # Calculate Response Authenticator
        # Auth = MD5(Code + ID + Length + Request-Auth + Attributes + Secret)
        md5 = hashlib.md5(packet + self.secret).digest()
        
        # Build final packet with proper auth
        final_packet = struct.pack('!BBH', DISCONNECT_REQUEST, identifier, length) + md5 + attrs
        
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(COA_TIMEOUT)
            sock.sendto(final_packet, (target_ip, COA_PORT))
            
            # Wait for response
            response, _ = sock.recvfrom(4096)
            sock.close()
            
            # Check response code (41 = Disconnect-ACK, 42 = Disconnect-NAK)
            resp_code = response[0]
            if resp_code == 41:
                logger.info(f"Raw CoA disconnect success: user={username}@{target_ip}")
                return True
            else:
                logger.warning(f"Raw CoA disconnect rejected: user={username} code={resp_code}")
                return False
                
        except socket.timeout:
            # Timeout means NAS is unreachable — session is already dead
            # Still return True so we close the DB record
            logger.info(f"Raw CoA timeout for {username}@{target_ip} — treating as disconnected")
            return True
        except Exception as e:
            logger.error(f"Raw CoA error for {username}: {e}")
            return False
    
    @staticmethod
    def _build_radius_attribute(attr_type: int, value: bytes) -> bytes:
        """Build a single RADIUS attribute TLV."""
        import struct
        length = 2 + len(value)
        return struct.pack('!BB', attr_type, length) + value
    
    def _get_minimal_dictionary(self):
        """Create a minimal pyrad Dictionary for CoA packets."""
        from pyrad.dictionary import Dictionary
        import tempfile
        import os
        
        dict_content = """
ATTRIBUTE	User-Name		1	string
ATTRIBUTE	NAS-IP-Address		4	ipaddr
ATTRIBUTE	Calling-Station-Id	31	string
ATTRIBUTE	Acct-Session-Id		44	string
ATTRIBUTE	Session-Timeout		27	integer
ATTRIBUTE	Idle-Timeout		28	integer

# MikroTik vendor attributes
VENDOR		Mikrotik	14988

BEGIN-VENDOR	Mikrotik
ATTRIBUTE	Mikrotik-Rate-Limit	8	string
ATTRIBUTE	Mikrotik-Total-Limit	17	integer
END-VENDOR	Mikrotik
"""
        # Write to a temp file for pyrad to parse
        fd, path = tempfile.mkstemp(suffix='.dict')
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(dict_content)
            return Dictionary(path)
        finally:
            os.unlink(path)