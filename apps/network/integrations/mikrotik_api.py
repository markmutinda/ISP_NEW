# apps/network/integrations/mikrotik_api.py
from librouteros import connect
from librouteros.query import Key
import logging
from typing import Dict, List, Optional, Any
import time

logger = logging.getLogger(__name__)


class MikrotikAPI:
    """Mikrotik RouterOS API Client"""
    
    def __init__(self, mikrotik_device):
        self.device = mikrotik_device
        self.api = None
    
    def connect(self) -> bool:
        """Connect to Mikrotik device"""
        try:
            self.api = connect(
                username=self.device.api_username,
                password=self.device.api_password,
                host=self.device.ip_address,
                port=self.device.api_port,
                timeout=10
            )
            logger.info(f"Connected to Mikrotik {self.device.name}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Mikrotik {self.device.name}: {str(e)}")
            return False
    
    def disconnect(self):
        """Disconnect from Mikrotik device"""
        if self.api:
            self.api.close()
            self.api = None
    
    def _execute_command(self, path: str, command: str, **kwargs) -> List[Dict]:
        """Execute Mikrotik API command"""
        if not self.api:
            if not self.connect():
                raise ConnectionError(f"Failed to connect to Mikrotik {self.device.name}")
        
        try:
            # Get the appropriate API path
            api_path = self.api.path(path)
            
            # Execute command
            if command == 'print':
                result = list(api_path)
            elif command == 'add':
                result = api_path.add(**kwargs)
            elif command == 'set':
                result = api_path.update(**kwargs)
            elif command == 'remove':
                result = api_path.remove(**kwargs)
            elif command == 'enable':
                result = api_path.update(disabled='no', **kwargs)
            elif command == 'disable':
                result = api_path.update(disabled='yes', **kwargs)
            else:
                raise ValueError(f"Unknown command: {command}")
            
            return result if isinstance(result, list) else [result]
            
        except Exception as e:
            logger.error(f"Mikrotik API command failed: {str(e)}")
            raise
    
    def sync_device_info(self) -> Dict[str, Any]:
        """Sync device information from Mikrotik"""
        try:
            # Get system resources
            resources = self._execute_command('/system/resource', 'print')[0]
            
            # Get system identity
            identity = self._execute_command('/system/identity', 'print')[0]
            
            # Get interfaces
            interfaces = self._execute_command('/interface', 'print')
            
            # Parse interface data
            interface_list = []
            for iface in interfaces:
                interface_list.append({
                    'name': iface.get('name', ''),
                    'type': iface.get('type', 'ether'),
                    'mac_address': iface.get('mac-address', ''),
                    'mtu': iface.get('mtu', 1500),
                    'rx_bytes': iface.get('rx-byte', 0),
                    'tx_bytes': iface.get('tx-byte', 0),
                    'admin_state': iface.get('disabled', 'true') == 'false',
                    'operational_state': iface.get('running', 'false') == 'true',
                })
            
            return {
                'identity': identity.get('name', 'Unknown'),
                'model': resources.get('board-name', 'Unknown'),
                'architecture': resources.get('architecture-name', 'Unknown'),
                'firmware_version': resources.get('version', 'Unknown'),
                'uptime': resources.get('uptime', '0s'),
                'cpu_load': float(resources.get('cpu-load', 0)),
                'memory_usage': self._parse_memory_usage(resources.get('free-memory', '0'), resources.get('total-memory', '1')),
                'disk_usage': self._parse_disk_usage(resources.get('free-hdd-space', '0'), resources.get('total-hdd-space', '1')),
                'interfaces': interface_list,
            }
            
        except Exception as e:
            logger.error(f"Failed to sync device info: {str(e)}")
            raise
    
    def get_hotspot_users(self) -> List[Dict[str, Any]]:
        """Get all hotspot users"""
        try:
            users = self._execute_command('/ip/hotspot/user', 'print')
            
            user_list = []
            for user in users:
                user_list.append({
                    'name': user.get('name', ''),
                    'password': user.get('password', ''),
                    'profile': user.get('profile', 'default'),
                    'disabled': user.get('disabled', 'true') == 'true',
                    'bytes_in': int(user.get('bytes-in', 0)),
                    'bytes_out': int(user.get('bytes-out', 0)),
                    'limit_uptime': user.get('limit-uptime', ''),
                    'limit_bytes_in': user.get('limit-bytes-in', '0'),
                    'limit_bytes_out': user.get('limit-bytes-out', '0'),
                })
            
            return user_list
            
        except Exception as e:
            logger.error(f"Failed to get hotspot users: {str(e)}")
            raise
    
    def get_hotspot_user_stats(self, username: str) -> Optional[Dict[str, Any]]:
        """Get hotspot user active session stats"""
        try:
            # Get active hosts
            active_hosts = self._execute_command('/ip/hotspot/active', 'print')
            
            for host in active_hosts:
                if host.get('user', '').lower() == username.lower():
                    return {
                        'address': host.get('address', ''),
                        'mac_address': host.get('mac-address', ''),
                        'bytes_in': int(host.get('bytes-in', 0)),
                        'bytes_out': int(host.get('bytes-out', 0)),
                        'packets_in': int(host.get('packets-in', 0)),
                        'packets_out': int(host.get('packets-out', 0)),
                        'session_time': host.get('uptime', '0s'),
                        'idle_time': host.get('idle-time', '0s'),
                        'server': host.get('server', 'hotspot1'),
                    }
            
            return None
            
        except Exception as e:
            logger.error(f"Failed to get hotspot user stats: {str(e)}")
            raise
    
    def create_hotspot_user(self, username: str, password: str, profile: str = 'default', 
                           limit_uptime: str = '', limit_bytes: str = '') -> bool:
        """Create hotspot user"""
        try:
            result = self._execute_command(
                '/ip/hotspot/user',
                'add',
                name=username,
                password=password,
                profile=profile,
                **({'limit-uptime': limit_uptime} if limit_uptime else {}),
                **({'limit-bytes-total': limit_bytes} if limit_bytes else {})
            )
            return 'id' in result[0]
        except Exception as e:
            logger.error(f"Failed to create hotspot user: {str(e)}")
            raise
    
    def enable_hotspot_user(self, username: str) -> bool:
        """Enable hotspot user"""
        try:
            # Find user ID
            users = self._execute_command('/ip/hotspot/user', 'print')
            user_id = None
            
            for user in users:
                if user.get('name', '').lower() == username.lower():
                    user_id = user.get('.id')
                    break
            
            if not user_id:
                raise ValueError(f"Hotspot user {username} not found")
            
            # Enable user
            self._execute_command('/ip/hotspot/user', 'enable', **{'.id': user_id})
            return True
            
        except Exception as e:
            logger.error(f"Failed to enable hotspot user: {str(e)}")
            raise
    
    def disable_hotspot_user(self, username: str) -> bool:
        """Disable hotspot user"""
        try:
            # Find user ID
            users = self._execute_command('/ip/hotspot/user', 'print')
            user_id = None
            
            for user in users:
                if user.get('name', '').lower() == username.lower():
                    user_id = user.get('.id')
                    break
            
            if not user_id:
                raise ValueError(f"Hotspot user {username} not found")
            
            # Disable user
            self._execute_command('/ip/hotspot/user', 'disable', **{'.id': user_id})
            return True
            
        except Exception as e:
            logger.error(f"Failed to disable hotspot user: {str(e)}")
            raise
    
    def get_pppoe_users(self) -> List[Dict[str, Any]]:
        """Get all PPPoE users"""
        try:
            secrets = self._execute_command('/ppp/secret', 'print')
            
            user_list = []
            for secret in secrets:
                if secret.get('service', '') == 'pppoe':
                    user_list.append({
                        'name': secret.get('name', ''),
                        'password': secret.get('password', ''),
                        'service': secret.get('service', 'pppoe'),
                        'profile': secret.get('profile', 'default-encryption'),
                        'disabled': secret.get('disabled', 'true') == 'true',
                        'caller_id': secret.get('caller-id', ''),
                        'local_address': secret.get('local-address', ''),
                        'remote_address': secret.get('remote-address', ''),
                    })
            
            return user_list
            
        except Exception as e:
            logger.error(f"Failed to get PPPoE users: {str(e)}")
            raise
    
    def get_pppoe_user_stats(self, username: str) -> Optional[Dict[str, Any]]:
        """Get PPPoE user active session stats"""
        try:
            # Get active PPPoE sessions
            active_sessions = self._execute_command('/ppp/active', 'print')
            
            for session in active_sessions:
                if session.get('name', '').lower() == username.lower():
                    return {
                        'name': session.get('name', ''),
                        'service': session.get('service', 'pppoe'),
                        'address': session.get('address', ''),
                        'local_address': session.get('local-address', ''),
                        'remote_address': session.get('remote-address', ''),
                        'bytes_in': int(session.get('bytes-in', 0)),
                        'bytes_out': int(session.get('bytes-out', 0)),
                        'session_time': session.get('uptime', '0s'),
                        'connected': True,
                        'last_connection': time.time(),
                    }
            
            return {
                'name': username,
                'connected': False,
                'last_connection': None,
            }
            
        except Exception as e:
            logger.error(f"Failed to get PPPoE user stats: {str(e)}")
            raise
    
    def create_pppoe_user(self, username: str, password: str, profile: str = 'default-encryption',
                         local_address: str = '', remote_address: str = '') -> bool:
        """Create PPPoE user"""
        try:
            result = self._execute_command(
                '/ppp/secret',
                'add',
                name=username,
                password=password,
                service='pppoe',
                profile=profile,
                **({'local-address': local_address} if local_address else {}),
                **({'remote-address': remote_address} if remote_address else {})
            )
            return 'id' in result[0]
        except Exception as e:
            logger.error(f"Failed to create PPPoE user: {str(e)}")
            raise
    
    def get_interfaces(self) -> List[Dict[str, Any]]:
        """Get all interfaces"""
        try:
            interfaces = self._execute_command('/interface', 'print')
            
            interface_list = []
            for iface in interfaces:
                interface_list.append({
                    'id': iface.get('.id', ''),
                    'name': iface.get('name', ''),
                    'type': iface.get('type', 'ether'),
                    'mtu': iface.get('mtu', 1500),
                    'mac_address': iface.get('mac-address', ''),
                    'running': iface.get('running', 'false') == 'true',
                    'disabled': iface.get('disabled', 'false') == 'true',
                    'rx_bytes': int(iface.get('rx-byte', 0)),
                    'tx_bytes': int(iface.get('tx-byte', 0)),
                    'rx_packets': int(iface.get('rx-packet', 0)),
                    'tx_packets': int(iface.get('tx-packet', 0)),
                    'rx_errors': int(iface.get('rx-error', 0)),
                    'tx_errors': int(iface.get('tx-error', 0)),
                })
            
            return interface_list
            
        except Exception as e:
            logger.error(f"Failed to get interfaces: {str(e)}")
            raise
    
    def enable_interface(self, interface_name: str) -> bool:
        """Enable interface"""
        try:
            self._execute_command('/interface', 'enable', **{'.id': interface_name})
            return True
        except Exception as e:
            logger.error(f"Failed to enable interface {interface_name}: {str(e)}")
            raise
    
    def disable_interface(self, interface_name: str) -> bool:
        """Disable interface"""
        try:
            self._execute_command('/interface', 'disable', **{'.id': interface_name})
            return True
        except Exception as e:
            logger.error(f"Failed to disable interface {interface_name}: {str(e)}")
            raise
    
    def get_queues(self) -> List[Dict[str, Any]]:
        """Get all queues"""
        try:
            queues = self._execute_command('/queue/simple', 'print')
            
            queue_list = []
            for queue in queues:
                queue_list.append({
                    'name': queue.get('name', ''),
                    'target': queue.get('target', ''),
                    'max_limit': queue.get('max-limit', ''),
                    'burst_limit': queue.get('burst-limit', ''),
                    'burst_threshold': queue.get('burst-threshold', ''),
                    'burst_time': queue.get('burst-time', ''),
                    'priority': queue.get('priority', '8'),
                    'disabled': queue.get('disabled', 'false') == 'true',
                    'packet_mark': queue.get('packet-marks', ''),
                })
            
            return queue_list
            
        except Exception as e:
            logger.error(f"Failed to get queues: {str(e)}")
            raise
    
    def create_queue(self, name: str, target: str, max_limit: str, 
                    burst_limit: str = '', priority: str = '8') -> bool:
        """Create queue"""
        try:
            result = self._execute_command(
                '/queue/simple',
                'add',
                name=name,
                target=target,
                max_limit=max_limit,
                **({'burst-limit': burst_limit} if burst_limit else {}),
                priority=priority
            )
            return 'id' in result[0]
        except Exception as e:
            logger.error(f"Failed to create queue: {str(e)}")
            raise
    
    def enable_queue(self, queue_name: str) -> bool:
        """Enable queue"""
        try:
            self._execute_command('/queue/simple', 'enable', **{'.id': queue_name})
            return True
        except Exception as e:
            logger.error(f"Failed to enable queue {queue_name}: {str(e)}")
            raise
    
    def disable_queue(self, queue_name: str) -> bool:
        """Disable queue"""
        try:
            self._execute_command('/queue/simple', 'disable', **{'.id': queue_name})
            return True
        except Exception as e:
            logger.error(f"Failed to disable queue {queue_name}: {str(e)}")
            raise
    
    def get_dhcp_leases(self) -> List[Dict[str, Any]]:
        """Get DHCP leases"""
        try:
            leases = self._execute_command('/ip/dhcp-server/lease', 'print')
            
            lease_list = []
            for lease in leases:
                lease_list.append({
                    'address': lease.get('address', ''),
                    'mac_address': lease.get('mac-address', ''),
                    'hostname': lease.get('host-name', ''),
                    'status': lease.get('status', 'unknown'),
                    'expires_after': lease.get('expires-after', '0s'),
                    'last_seen': lease.get('last-seen', ''),
                    'server': lease.get('server', 'dhcp1'),
                    'disabled': lease.get('disabled', 'false') == 'true',
                })
            
            return lease_list
            
        except Exception as e:
            logger.error(f"Failed to get DHCP leases: {str(e)}")
            raise
    
    def add_firewall_rule(self, chain: str, action: str, src_address: str = '',
                         dst_address: str = '', protocol: str = '', 
                         dst_port: str = '', comment: str = '') -> bool:
        """Add firewall rule"""
        try:
            params = {
                'chain': chain,
                'action': action,
            }
            
            if src_address:
                params['src-address'] = src_address
            if dst_address:
                params['dst-address'] = dst_address
            if protocol:
                params['protocol'] = protocol
            if dst_port:
                params['dst-port'] = dst_port
            if comment:
                params['comment'] = comment
            
            result = self._execute_command('/ip/firewall/filter', 'add', **params)
            return 'id' in result[0]
            
        except Exception as e:
            logger.error(f"Failed to add firewall rule: {str(e)}")
            raise
    
    def reboot_device(self) -> bool:
        """Reboot Mikrotik device"""
        try:
            self._execute_command('/system', 'reboot')
            return True
        except Exception as e:
            logger.error(f"Failed to reboot device: {str(e)}")
            raise
    
    def backup_config(self) -> str:
        """Backup configuration"""
        try:
            # Export configuration
            result = self._execute_command('/system/backup', 'save', name='backup')
            
            # Get backup file (simplified - in reality you'd need to download it)
            return "Backup created successfully"
            
        except Exception as e:
            logger.error(f"Failed to backup configuration: {str(e)}")
            raise
    
    # Helper methods
    def _parse_memory_usage(self, free_memory: str, total_memory: str) -> float:
        """Parse memory usage percentage"""
        try:
            free = self._parse_size(free_memory)
            total = self._parse_size(total_memory)
            
            if total > 0:
                used = total - free
                return (used / total) * 100
            return 0.0
        except:
            return 0.0
    
    def _parse_disk_usage(self, free_space: str, total_space: str) -> float:
        """Parse disk usage percentage"""
        try:
            free = self._parse_size(free_space)
            total = self._parse_size(total_space)
            
            if total > 0:
                used = total - free
                return (used / total) * 100
            return 0.0
        except:
            return 0.0
    
    def _parse_size(self, size_str: str) -> int:
        """Parse size string (e.g., '10.5MiB', '1GiB') to bytes"""
        try:
            size_str = size_str.lower().replace(' ', '')
            
            # Remove non-numeric characters
            import re
            number = float(re.findall(r'[\d.]+', size_str)[0])
            
            # Convert based on unit
            if 'tib' in size_str:
                return int(number * 1024 ** 4)
            elif 'gib' in size_str:
                return int(number * 1024 ** 3)
            elif 'mib' in size_str:
                return int(number * 1024 ** 2)
            elif 'kib' in size_str:
                return int(number * 1024)
            elif 'b' in size_str:
                return int(number)
            else:
                return int(number)
        except:
            return 0
