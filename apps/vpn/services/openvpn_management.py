"""
OpenVPN Management Interface Client

Connects to the OpenVPN management socket (telnet-style) to:
- Query connected clients and their status
- Disconnect specific clients (kill by CN)
- Parse real-time traffic and connection stats

Management interface is enabled via: --management localhost 7505
"""

import logging
import socket
import time
from typing import List, Dict, Optional
from dataclasses import dataclass, field

from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass
class VPNClientInfo:
    """Parsed info about a connected VPN client."""
    common_name: str
    real_address: str  # e.g. '197.232.x.x:54321'
    vpn_ip: str        # e.g. '10.8.0.55'
    bytes_received: int = 0
    bytes_sent: int = 0
    connected_since: str = ''


class OpenVPNManagementError(Exception):
    """Raised when management interface communication fails."""
    pass


class OpenVPNManagementClient:
    """
    Communicates with the OpenVPN management interface via TCP socket.
    
    Usage:
        client = OpenVPNManagementClient()
        clients = client.get_connected_clients()
        client.kill_client('netily-router-5-nairobi')
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        timeout: int = 10,
    ):
        self.host = host or getattr(settings, 'OPENVPN_MANAGEMENT_HOST', '127.0.0.1')
        self.port = port or getattr(settings, 'OPENVPN_MANAGEMENT_PORT', 7505)
        self.timeout = timeout

    def _send_command(self, command: str) -> str:
        """
        Sends a command to the management interface and returns the response.
        Each command is terminated by \\n. Response ends with 'END' or '>'.
        """
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect((self.host, self.port))
                
                # Read the welcome banner
                self._recv_until(sock, b'\r\n')
                
                # Send command
                sock.sendall(f"{command}\r\n".encode())
                
                # Read response until END marker
                response = self._recv_until(sock, b'END\r\n', b'ERROR:')
                
                return response.decode('utf-8', errors='replace')
                
        except socket.timeout:
            logger.error(f"Timeout connecting to OpenVPN management at {self.host}:{self.port}")
            raise OpenVPNManagementError("Connection to OpenVPN management interface timed out")
        except ConnectionRefusedError:
            logger.error(f"Connection refused to OpenVPN management at {self.host}:{self.port}")
            raise OpenVPNManagementError("OpenVPN management interface connection refused")
        except Exception as e:
            logger.error(f"Error communicating with OpenVPN management: {e}")
            raise OpenVPNManagementError(f"Management interface error: {e}")

    def _recv_until(self, sock: socket.socket, *terminators: bytes) -> bytes:
        """Receive data until one of the terminator strings is found."""
        data = b''
        deadline = time.time() + self.timeout
        
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                for terminator in terminators:
                    if terminator in data:
                        return data
            except socket.timeout:
                break
        
        return data

    def get_connected_clients(self) -> List[VPNClientInfo]:
        """
        Get list of currently connected VPN clients.
        
        Parses the 'status' command output which has format:
            HEADER,Updated,...
            HEADER FIELDS,Common Name,Real Address,Bytes Received,...
            client_cn,1.2.3.4:port,12345,67890,timestamp
            ...
            ROUTING TABLE
            Virtual Address,Common Name,...
            10.8.0.55,client_cn,...
        """
        try:
            raw = self._send_command('status 2')
        except OpenVPNManagementError:
            logger.warning("Cannot reach OpenVPN management interface — returning empty client list")
            return []
        
        clients: Dict[str, VPNClientInfo] = {}
        
        section = None
        for line in raw.split('\n'):
            line = line.strip()
            
            if line.startswith('HEADER,CLIENT_LIST'):
                section = 'clients'
                continue
            elif line.startswith('HEADER,ROUTING_TABLE'):
                section = 'routing'
                continue
            elif line.startswith('GLOBAL_STATS') or line.startswith('END'):
                section = None
                continue
            
            if section == 'clients' and line.startswith('CLIENT_LIST,'):
                parts = line.split(',')
                if len(parts) >= 5:
                    cn = parts[1]
                    real_addr = parts[2]
                    bytes_recv = int(parts[3]) if parts[3].isdigit() else 0
                    bytes_sent = int(parts[4]) if parts[4].isdigit() else 0
                    connected_since = parts[7] if len(parts) > 7 else ''
                    
                    clients[cn] = VPNClientInfo(
                        common_name=cn,
                        real_address=real_addr,
                        vpn_ip='',  # Filled from routing table
                        bytes_received=bytes_recv,
                        bytes_sent=bytes_sent,
                        connected_since=connected_since,
                    )
            
            elif section == 'routing' and line.startswith('ROUTING_TABLE,') is False and ',' in line:
                # Routing table format: Virtual Address, CN, Real Address, Last Ref
                # Or with status 2: ROUTING_TABLE,Virtual Address,CN,...
                if line.startswith('ROUTING_TABLE,'):
                    parts = line.split(',')
                    if len(parts) >= 3:
                        vpn_ip = parts[1]
                        cn = parts[2]
                        if cn in clients:
                            clients[cn].vpn_ip = vpn_ip
        
        return list(clients.values())

    def kill_client(self, common_name: str) -> bool:
        """
        Disconnect a client by its certificate Common Name.
        
        Returns True if the kill command was acknowledged.
        """
        try:
            response = self._send_command(f"kill {common_name}")
            success = 'SUCCESS' in response or 'client(s) killed' in response
            if success:
                logger.info(f"VPN client killed: {common_name}")
            else:
                logger.warning(f"Kill command for {common_name} may not have succeeded: {response}")
            return success
        except OpenVPNManagementError as e:
            logger.error(f"Failed to kill VPN client {common_name}: {e}")
            return False

    def get_server_stats(self) -> Dict[str, int]:
        """Get basic server statistics."""
        clients = self.get_connected_clients()
        total_bytes_in = sum(c.bytes_received for c in clients)
        total_bytes_out = sum(c.bytes_sent for c in clients)
        
        return {
            'connected_clients': len(clients),
            'total_bytes_in': total_bytes_in,
            'total_bytes_out': total_bytes_out,
        }

    def is_client_connected(self, common_name: str) -> bool:
        """Check if a specific router is currently connected."""
        clients = self.get_connected_clients()
        return any(c.common_name == common_name for c in clients)

    def get_client_by_cn(self, common_name: str) -> Optional[VPNClientInfo]:
        """Get info about a specific connected client."""
        clients = self.get_connected_clients()
        for c in clients:
            if c.common_name == common_name:
                return c
        return None

    def ping(self) -> bool:
        """Quick health check — can we reach the management interface?"""
        try:
            self._send_command('version')
            return True
        except OpenVPNManagementError:
            return False
