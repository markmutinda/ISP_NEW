# apps/network/integrations/mikrotik_api.py
from librouteros import connect
from librouteros.query import Key
import logging
from typing import Dict, List, Optional, Any
import time
import re

logger = logging.getLogger(__name__)


class MikrotikAPI:
    """Mikrotik RouterOS API Client - Enhanced for ISP Management"""
    
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
                port=self.device.api_port or 8728,  # Use default 8728 if not set
                timeout=10
            )
            logger.info(f"Connected to Mikrotik {self.device.name} ({self.device.ip_address})")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to {self.device.name}: {str(e)}")
            return False
    
    def disconnect(self):
        """Disconnect from Mikrotik device"""
        if self.api:
            self.api.close()
            self.api = None
    
    def _execute(self, path: str, **kwargs) -> Any:
        """Unified execute method with better error handling"""
        if not self.api and not self.connect():
            raise ConnectionError(f"Cannot connect to {self.device.name}")
        try:
            path_obj = self.api.path(path)
            if 'get' in kwargs:
                return list(path_obj(**kwargs['get']))
            elif 'add' in kwargs:
                return path_obj.add(**kwargs['add'])
            elif 'set' in kwargs:
                return path_obj.set(**kwargs['set'])
            elif 'remove' in kwargs:
                return path_obj.remove(**kwargs['remove'])
            else:
                return list(path_obj)
        except Exception as e:
            logger.error(f"API error on {path}: {str(e)}")
            raise
    
    # ────────────────────────────────────────────────────────────────
    # LIVE STATUS & HEALTH MONITORING
    # ────────────────────────────────────────────────────────────────
    
    def get_live_status(self) -> Dict[str, Any]:
        """Get real-time router status"""
        try:
            if not self.connect():
                return {"online": False, "error": "Connection failed"}
            
            resource = self._execute('/system/resource')[0]
            identity = self._execute('/system/identity')[0]
            routerboard = self._execute('/system/routerboard') or [{}]
            
            return {
                "online": True,
                "identity": identity.get('name', 'Unknown'),
                "model": routerboard[0].get('model', resource.get('board-name', 'Unknown')),
                "serial": routerboard[0].get('serial-number', 'Unknown'),
                "firmware": resource.get('version', 'Unknown'),
                "uptime": resource.get('uptime', '0s'),
                "cpu_load": resource.get('cpu-load', '0%'),
                "free_memory": resource.get('free-memory', '0'),
                "total_memory": resource.get('total-memory', '0'),
                "free_hdd": resource.get('free-hdd-space', '0'),
                "architecture": resource.get('architecture-name', 'Unknown'),
            }
        except Exception as e:
            return {"online": False, "error": str(e)}
        finally:
            self.disconnect()
    
    def sync_device_info(self) -> Dict[str, Any]:
        """Sync device information from Mikrotik"""
        try:
            if not self.connect():
                raise ConnectionError(f"Failed to connect to Mikrotik {self.device.name}")
            
            # Get system resources
            resources = self._execute('/system/resource')[0]
            
            # Get system identity
            identity = self._execute('/system/identity')[0]
            
            # Get interfaces
            interfaces = self._execute('/interface')
            
            # Parse interface data
            interface_list = []
            for iface in interfaces:
                interface_list.append({
                    'name': iface.get('name', ''),
                    'type': iface.get('type', 'ether'),
                    'mac_address': iface.get('mac-address', ''),
                    'mtu': iface.get('mtu', 1500),
                    'rx_bytes': int(iface.get('rx-byte', 0)),
                    'tx_bytes': int(iface.get('tx-byte', 0)),
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
        finally:
            self.disconnect()
    
    # ────────────────────────────────────────────────────────────────
    # CONNECTED USERS MONITORING
    # ────────────────────────────────────────────────────────────────
    
    def get_active_hotspot_users(self) -> List[Dict]:
        """Get currently connected hotspot users"""
        try:
            if not self.connect():
                return []
            return list(self._execute('/ip/hotspot/active'))
        finally:
            self.disconnect()
    
    def get_active_pppoe_sessions(self) -> List[Dict]:
        """Get active PPPoE sessions"""
        try:
            if not self.connect():
                return []
            return list(self._execute('/ppp/active'))
        finally:
            self.disconnect()
    
    def get_hotspot_users(self) -> List[Dict[str, Any]]:
        """Get all hotspot users"""
        try:
            if not self.connect():
                return []
            
            users = self._execute('/ip/hotspot/user')
            
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
        finally:
            self.disconnect()
    
    def get_hotspot_user_stats(self, username: str) -> Optional[Dict[str, Any]]:
        """Get hotspot user active session stats"""
        try:
            if not self.connect():
                return None
            
            # Get active hosts
            active_hosts = self._execute('/ip/hotspot/active')
            
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
        finally:
            self.disconnect()
    
    def get_pppoe_users(self) -> List[Dict[str, Any]]:
        """Get all PPPoE users"""
        try:
            if not self.connect():
                return []
            
            secrets = self._execute('/ppp/secret')
            
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
        finally:
            self.disconnect()
    
    def get_pppoe_user_stats(self, username: str) -> Optional[Dict[str, Any]]:
        """Get PPPoE user active session stats"""
        try:
            if not self.connect():
                return None
            
            # Get active PPPoE sessions
            active_sessions = self._execute('/ppp/active')
            
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
        finally:
            self.disconnect()
    
    # ────────────────────────────────────────────────────────────────
    # USER MANAGEMENT
    # ────────────────────────────────────────────────────────────────
    
    def create_hotspot_user(self, username: str, password: str, profile: str = 'default', 
                           limit_uptime: str = '', limit_bytes: str = '') -> bool:
        """Create hotspot user"""
        try:
            if not self.connect():
                return False
            
            add_params = {
                'name': username,
                'password': password,
                'profile': profile,
            }
            
            if limit_uptime:
                add_params['limit-uptime'] = limit_uptime
            if limit_bytes:
                add_params['limit-bytes-total'] = limit_bytes
            
            result = self._execute('/ip/hotspot/user', add=add_params)
            return 'id' in result
        except Exception as e:
            logger.error(f"Failed to create hotspot user: {str(e)}")
            return False
        finally:
            self.disconnect()
    
    def create_pppoe_user(self, username: str, password: str, profile: str = 'default-encryption',
                         local_address: str = '', remote_address: str = '') -> bool:
        """Create PPPoE user"""
        try:
            if not self.connect():
                return False
            
            add_params = {
                'name': username,
                'password': password,
                'service': 'pppoe',
                'profile': profile,
            }
            
            if local_address:
                add_params['local-address'] = local_address
            if remote_address:
                add_params['remote-address'] = remote_address
            
            result = self._execute('/ppp/secret', add=add_params)
            return 'id' in result
        except Exception as e:
            logger.error(f"Failed to create PPPoE user: {str(e)}")
            return False
        finally:
            self.disconnect()
    
    def enable_hotspot_user(self, username: str) -> bool:
        """Enable hotspot user"""
        try:
            if not self.connect():
                return False
            
            # Find user ID
            users = self._execute('/ip/hotspot/user')
            user_id = None
            
            for user in users:
                if user.get('name', '').lower() == username.lower():
                    user_id = user.get('.id')
                    break
            
            if not user_id:
                raise ValueError(f"Hotspot user {username} not found")
            
            # Enable user
            self._execute('/ip/hotspot/user', set={'.id': user_id, 'disabled': 'no'})
            return True
            
        except Exception as e:
            logger.error(f"Failed to enable hotspot user: {str(e)}")
            return False
        finally:
            self.disconnect()
    
    def disable_hotspot_user(self, username: str) -> bool:
        """Disable hotspot user"""
        try:
            if not self.connect():
                return False
            
            # Find user ID
            users = self._execute('/ip/hotspot/user')
            user_id = None
            
            for user in users:
                if user.get('name', '').lower() == username.lower():
                    user_id = user.get('.id')
                    break
            
            if not user_id:
                raise ValueError(f"Hotspot user {username} not found")
            
            # Disable user
            self._execute('/ip/hotspot/user', set={'.id': user_id, 'disabled': 'yes'})
            return True
            
        except Exception as e:
            logger.error(f"Failed to disable hotspot user: {str(e)}")
            return False
        finally:
            self.disconnect()
    
    # ────────────────────────────────────────────────────────────────
    # FIREWALL & QUEUES MANAGEMENT
    # ────────────────────────────────────────────────────────────────
    
    def get_firewall_filter_rules(self) -> List[Dict]:
        """Get all firewall filter rules"""
        try:
            if not self.connect():
                return []
            return list(self._execute('/ip/firewall/filter'))
        finally:
            self.disconnect()
    
    def get_queues(self) -> List[Dict[str, Any]]:
        """Get all queues"""
        try:
            if not self.connect():
                return []
            
            queues = self._execute('/queue/simple')
            
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
        finally:
            self.disconnect()
    
    def add_simple_queue(self, name: str, target: str, max_limit: str = "5M/5M") -> bool:
        """Add a simple queue for rate limiting"""
        try:
            if not self.connect():
                return False
            
            self._execute('/queue/simple', add={
                'name': name,
                'target': target,
                'max-limit': max_limit,
                'comment': 'Added by YourISP backend'
            })
            return True
        except Exception as e:
            logger.error(f"Queue creation failed: {e}")
            return False
        finally:
            self.disconnect()
    
    def create_queue(self, name: str, target: str, max_limit: str, 
                    burst_limit: str = '', priority: str = '8') -> bool:
        """Create queue"""
        try:
            if not self.connect():
                return False
            
            add_params = {
                'name': name,
                'target': target,
                'max-limit': max_limit,
                'priority': priority
            }
            
            if burst_limit:
                add_params['burst-limit'] = burst_limit
            
            result = self._execute('/queue/simple', add=add_params)
            return 'id' in result
        except Exception as e:
            logger.error(f"Failed to create queue: {str(e)}")
            return False
        finally:
            self.disconnect()
    
    def enable_queue(self, queue_name: str) -> bool:
        """Enable queue"""
        try:
            if not self.connect():
                return False
            
            self._execute('/queue/simple', set={'.id': queue_name, 'disabled': 'no'})
            return True
        except Exception as e:
            logger.error(f"Failed to enable queue {queue_name}: {str(e)}")
            return False
        finally:
            self.disconnect()
    
    def disable_queue(self, queue_name: str) -> bool:
        """Disable queue"""
        try:
            if not self.connect():
                return False
            
            self._execute('/queue/simple', set={'.id': queue_name, 'disabled': 'yes'})
            return True
        except Exception as e:
            logger.error(f"Failed to disable queue {queue_name}: {str(e)}")
            return False
        finally:
            self.disconnect()
    
    def add_firewall_rule(self, chain: str, action: str, src_address: str = '',
                         dst_address: str = '', protocol: str = '', 
                         dst_port: str = '', comment: str = '') -> bool:
        """Add firewall rule"""
        try:
            if not self.connect():
                return False
            
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
            
            result = self._execute('/ip/firewall/filter', add=params)
            return 'id' in result
            
        except Exception as e:
            logger.error(f"Failed to add firewall rule: {str(e)}")
            return False
        finally:
            self.disconnect()
    
    # ────────────────────────────────────────────────────────────────
    # INTERFACE MANAGEMENT
    # ────────────────────────────────────────────────────────────────
    
    def get_interfaces(self) -> List[Dict[str, Any]]:
        """Get all interfaces"""
        try:
            if not self.connect():
                return []
            
            interfaces = self._execute('/interface')
            
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
        finally:
            self.disconnect()
    
    def enable_interface(self, interface_name: str) -> bool:
        """Enable interface"""
        try:
            if not self.connect():
                return False
            
            self._execute('/interface', set={'.id': interface_name, 'disabled': 'no'})
            return True
        except Exception as e:
            logger.error(f"Failed to enable interface {interface_name}: {str(e)}")
            return False
        finally:
            self.disconnect()
    
    def disable_interface(self, interface_name: str) -> bool:
        """Disable interface"""
        try:
            if not self.connect():
                return False
            
            self._execute('/interface', set={'.id': interface_name, 'disabled': 'yes'})
            return True
        except Exception as e:
            logger.error(f"Failed to disable interface {interface_name}: {str(e)}")
            return False
        finally:
            self.disconnect()
    
    # ────────────────────────────────────────────────────────────────
    # DHCP MANAGEMENT
    # ────────────────────────────────────────────────────────────────
    
    def get_dhcp_leases(self) -> List[Dict[str, Any]]:
        """Get DHCP leases"""
        try:
            if not self.connect():
                return []
            
            leases = self._execute('/ip/dhcp-server/lease')
            
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
        finally:
            self.disconnect()
    
    # ────────────────────────────────────────────────────────────────
    # BASIC CONTROL & DIAGNOSTICS
    # ────────────────────────────────────────────────────────────────
    
    def reboot_device(self) -> bool:
        """Reboot Mikrotik device"""
        try:
            if not self.connect():
                return False
            
            self._execute('/system/reboot')
            return True
        except Exception as e:
            logger.error(f"Failed to reboot device: {str(e)}")
            return False
        finally:
            self.disconnect()
    
    def reboot(self) -> bool:
        """Reboot Mikrotik device (alias for reboot_device)"""
        return self.reboot_device()
    
    def ping(self, target: str = "8.8.8.8", count: int = 3) -> Dict:
        """Run ping from router"""
        try:
            if not self.connect():
                return {"success": False, "error": "Connection failed"}
            
            result = self._execute('/ping', add={'address': target, 'count': str(count)})
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            self.disconnect()
    
    def backup_config(self) -> str:
        """Backup configuration"""
        try:
            if not self.connect():
                return "Backup failed: Connection failed"
            
            # Export configuration
            result = self._execute('/system/backup/save', add={'name': 'yourisp-backup'})
            
            # Get backup file (simplified - in reality you'd need to download it)
            return "Backup created successfully"
            
        except Exception as e:
            logger.error(f"Failed to backup configuration: {str(e)}")
            return f"Backup failed: {str(e)}"
        finally:
            self.disconnect()
    
    def traceroute(self, target: str = "8.8.8.8") -> Dict:
        """Run traceroute from router"""
        try:
            if not self.connect():
                return {"success": False, "error": "Connection failed"}
            
            result = self._execute('/tool/traceroute', add={'address': target})
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            self.disconnect()
    
    def get_system_logs(self, lines: int = 50) -> List[Dict]:
        """Get system logs"""
        try:
            if not self.connect():
                return []
            
            logs = self._execute('/log/print', get={'lines': str(lines)})
            return list(logs)
        except Exception as e:
            logger.error(f"Failed to get system logs: {str(e)}")
            return []
        finally:
            self.disconnect()
    
    def get_wireless_interfaces(self) -> List[Dict]:
        """Get wireless interface information"""
        try:
            if not self.connect():
                return []
            
            wireless = self._execute('/interface/wireless')
            return list(wireless)
        except Exception as e:
            logger.error(f"Failed to get wireless interfaces: {str(e)}")
            return []
        finally:
            self.disconnect()
    
    def get_wireless_registrations(self) -> List[Dict]:
        """Get wireless client registrations"""
        try:
            if not self.connect():
                return []
            
            registrations = self._execute('/interface/wireless/registration-table')
            return list(registrations)
        except Exception as e:
            logger.error(f"Failed to get wireless registrations: {str(e)}")
            return []
        finally:
            self.disconnect()
    
    # ────────────────────────────────────────────────────────────────
    # HELPER METHODS
    # ────────────────────────────────────────────────────────────────
    
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
    
    def get_interface_traffic(self, interface_name: str) -> Dict:
        """Get traffic statistics for specific interface"""
        try:
            if not self.connect():
                return {"error": "Connection failed"}
            
            # Get interface stats
            interfaces = self._execute('/interface')
            for iface in interfaces:
                if iface.get('name', '') == interface_name:
                    return {
                        'name': interface_name,
                        'rx_bytes': int(iface.get('rx-byte', 0)),
                        'tx_bytes': int(iface.get('tx-byte', 0)),
                        'rx_packets': int(iface.get('rx-packet', 0)),
                        'tx_packets': int(iface.get('tx-packet', 0)),
                        'rx_errors': int(iface.get('rx-error', 0)),
                        'tx_errors': int(iface.get('tx-error', 0)),
                        'running': iface.get('running', 'false') == 'true',
                        'disabled': iface.get('disabled', 'false') == 'true',
                    }
            
            return {"error": f"Interface {interface_name} not found"}
        except Exception as e:
            return {"error": str(e)}
        finally:
            self.disconnect()
    
    def get_system_health(self) -> Dict:
        """Get comprehensive system health information"""
        status = self.get_live_status()
        
        if not status.get('online', False):
            return status
        
        try:
            # Add additional health metrics
            interfaces = self.get_interfaces()
            queues = self.get_queues()
            firewall_rules = len(self.get_firewall_filter_rules())
            dhcp_leases = self.get_dhcp_leases()
            hotspot_active = len(self.get_active_hotspot_users())
            pppoe_active = len(self.get_active_pppoe_sessions())
            
            # Calculate interface health
            total_interfaces = len(interfaces)
            up_interfaces = sum(1 for iface in interfaces if iface.get('running', False))
            
            status.update({
                'interfaces_total': total_interfaces,
                'interfaces_up': up_interfaces,
                'interface_health': f"{up_interfaces}/{total_interfaces}",
                'queues_total': len(queues),
                'firewall_rules': firewall_rules,
                'dhcp_leases': len(dhcp_leases),
                'hotspot_active': hotspot_active,
                'pppoe_active': pppoe_active,
                'total_active_users': hotspot_active + pppoe_active,
                'timestamp': time.time(),
            })
            
            return status
        except Exception as e:
            status['health_error'] = str(e)
            return status