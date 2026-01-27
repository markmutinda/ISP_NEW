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

    # ────────────────────────────────────────────────────────────────
    # HOTSPOT SERVER CONFIGURATION
    # ────────────────────────────────────────────────────────────────
    
    def get_ports_with_usage(self) -> List[Dict[str, Any]]:
        """
        Get all interfaces (ports) with their current usage context.
        Returns ethernet, wireless, and bridge interfaces with usage info.
        """
        try:
            if not self.connect():
                return []
            
            # Get all interfaces
            interfaces = self._execute('/interface')
            
            # Get IP addresses to determine which interfaces have IPs
            ip_addresses = self._execute('/ip/address')
            ip_interface_map = {}
            for ip in ip_addresses:
                iface = ip.get('interface', '')
                if iface:
                    ip_interface_map[iface] = ip.get('address', '')
            
            # Get hotspot servers to identify hotspot interfaces
            hotspot_servers = []
            try:
                hotspot_servers = self._execute('/ip/hotspot')
            except:
                pass
            hotspot_interfaces = {hs.get('interface', '') for hs in hotspot_servers}
            
            # Get PPPoE servers
            pppoe_servers = []
            try:
                pppoe_servers = self._execute('/interface/pppoe-server/server')
            except:
                pass
            pppoe_interfaces = {ps.get('interface', '') for ps in pppoe_servers}
            
            # Get DHCP servers
            dhcp_servers = []
            try:
                dhcp_servers = self._execute('/ip/dhcp-server')
            except:
                pass
            dhcp_interfaces = {ds.get('interface', '') for ds in dhcp_servers}
            
            # Get wireless interfaces for additional info
            wireless_interfaces = {}
            try:
                wireless = self._execute('/interface/wireless')
                for w in wireless:
                    wireless_interfaces[w.get('name', '')] = {
                        'ssid': w.get('ssid', ''),
                        'mode': w.get('mode', ''),
                        'band': w.get('band', ''),
                        'frequency': w.get('frequency', ''),
                    }
            except:
                pass
            
            ports = []
            for iface in interfaces:
                name = iface.get('name', '')
                iface_type = iface.get('type', 'ether')
                
                # Skip certain interface types
                if iface_type in ['pppoe-out', 'pppoe-in', 'pptp-out', 'l2tp-out', 'sstp-out', 'ovpn-out']:
                    continue
                
                # Determine current usage
                current_use = 'unused'
                if name in hotspot_interfaces:
                    current_use = 'hotspot'
                elif name in pppoe_interfaces:
                    current_use = 'pppoe'
                elif name in dhcp_interfaces:
                    current_use = 'dhcp'
                elif name in ip_interface_map:
                    # Has an IP but no specific server - likely WAN or LAN
                    ip = ip_interface_map[name]
                    # Check if it's likely the WAN (gateway route uses it)
                    try:
                        routes = self._execute('/ip/route')
                        for route in routes:
                            if route.get('gateway', '') and route.get('dst-address', '') == '0.0.0.0/0':
                                # This is a default route
                                gw_interface = route.get('interface', route.get('gateway', ''))
                                if gw_interface == name or name in str(gw_interface):
                                    current_use = 'wan'
                                    break
                    except:
                        pass
                    if current_use == 'unused':
                        current_use = 'lan'
                
                # Determine interface category
                if iface_type in ['ether', 'ethernet']:
                    port_type = 'ethernet'
                elif iface_type in ['wlan', 'wireless']:
                    port_type = 'wireless'
                elif iface_type == 'bridge':
                    port_type = 'bridge'
                elif iface_type in ['vlan']:
                    port_type = 'vlan'
                else:
                    port_type = iface_type
                
                port_info = {
                    'name': name,
                    'type': port_type,
                    'mac_address': iface.get('mac-address', ''),
                    'running': iface.get('running', 'false') == 'true',
                    'disabled': iface.get('disabled', 'false') == 'true',
                    'speed': iface.get('speed', ''),
                    'current_use': current_use,
                    'ip_address': ip_interface_map.get(name, ''),
                }
                
                # Add wireless info if applicable
                if name in wireless_interfaces:
                    port_info['wireless'] = wireless_interfaces[name]
                
                ports.append(port_info)
            
            return ports
            
        except Exception as e:
            logger.error(f"Failed to get ports with usage: {str(e)}")
            raise
        finally:
            self.disconnect()
    
    def get_hotspot_config(self) -> Dict[str, Any]:
        """
        Get current hotspot configuration from the router.
        Returns hotspot servers, profiles, and related configuration.
        """
        try:
            if not self.connect():
                return {'configured': False, 'error': 'Connection failed'}
            
            # Get hotspot servers
            servers = []
            try:
                hotspot_servers = self._execute('/ip/hotspot')
                for hs in hotspot_servers:
                    servers.append({
                        'id': hs.get('.id', ''),
                        'name': hs.get('name', ''),
                        'interface': hs.get('interface', ''),
                        'address_pool': hs.get('address-pool', ''),
                        'profile': hs.get('profile', ''),
                        'idle_timeout': hs.get('idle-timeout', ''),
                        'keepalive_timeout': hs.get('keepalive-timeout', ''),
                        'disabled': hs.get('disabled', 'false') == 'true',
                    })
            except Exception as e:
                logger.warning(f"No hotspot servers found: {e}")
            
            if not servers:
                return {'configured': False}
            
            # Get hotspot profiles
            profiles = []
            try:
                hotspot_profiles = self._execute('/ip/hotspot/profile')
                for hp in hotspot_profiles:
                    profiles.append({
                        'id': hp.get('.id', ''),
                        'name': hp.get('name', ''),
                        'hotspot_address': hp.get('hotspot-address', ''),
                        'dns_name': hp.get('dns-name', ''),
                        'html_directory': hp.get('html-directory', ''),
                        'login_by': hp.get('login-by', ''),
                        'mac_auth_mode': hp.get('mac-auth-mode', ''),
                    })
            except:
                pass
            
            # Get IP pools
            pools = []
            try:
                ip_pools = self._execute('/ip/pool')
                for pool in ip_pools:
                    pools.append({
                        'id': pool.get('.id', ''),
                        'name': pool.get('name', ''),
                        'ranges': pool.get('ranges', ''),
                    })
            except:
                pass
            
            # Get walled garden entries
            walled_garden = []
            try:
                wg_entries = self._execute('/ip/hotspot/walled-garden')
                for wg in wg_entries:
                    walled_garden.append({
                        'id': wg.get('.id', ''),
                        'dst_host': wg.get('dst-host', ''),
                        'action': wg.get('action', 'allow'),
                        'comment': wg.get('comment', ''),
                    })
            except:
                pass
            
            # Get active sessions count
            active_users = 0
            try:
                active = self._execute('/ip/hotspot/active')
                active_users = len(active)
            except:
                pass
            
            return {
                'configured': True,
                'servers': servers,
                'profiles': profiles,
                'pools': pools,
                'walled_garden': walled_garden,
                'active_users': active_users,
            }
            
        except Exception as e:
            logger.error(f"Failed to get hotspot config: {str(e)}")
            return {'configured': False, 'error': str(e)}
        finally:
            self.disconnect()
    
    def configure_hotspot(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Configure hotspot on the router.
        
        Args:
            config: {
                'interface': 'ether3',
                'network': {
                    'network_address': '10.5.50.1',
                    'network_mask': '24',
                    'pool_name': 'hotspot-pool',
                    'pool_range': '10.5.50.10-10.5.50.254',
                    'dns_server': '8.8.8.8'
                },
                'server': {
                    'name': 'hotspot-server',
                    'idle_timeout': '5m',
                    'keepalive_timeout': '2m',
                    'login_by': ['mac', 'http-chap']
                },
                'branding': {
                    'company_name': 'ISP Name',
                    'logo_url': '',
                    'primary_color': '#3B82F6',
                    'welcome_message': 'Welcome',
                    'terms_url': ''
                }
            }
        """
        try:
            if not self.connect():
                return {'success': False, 'error': 'Connection failed'}
            
            interface = config.get('interface')
            network = config.get('network', {})
            server = config.get('server', {})
            branding = config.get('branding', {})
            
            results = {'steps': [], 'success': True}
            
            # Step 1: Add IP address to interface
            try:
                ip_address = f"{network.get('network_address')}/{network.get('network_mask', '24')}"
                
                # Check if IP already exists on this interface
                existing_ips = self._execute('/ip/address')
                ip_exists = False
                for ip in existing_ips:
                    if ip.get('interface') == interface and ip.get('address') == ip_address:
                        ip_exists = True
                        break
                
                if not ip_exists:
                    self.api.path('/ip/address').add(
                        address=ip_address,
                        interface=interface
                    )
                    results['steps'].append({'step': 'add_ip', 'status': 'created'})
                else:
                    results['steps'].append({'step': 'add_ip', 'status': 'exists'})
            except Exception as e:
                results['steps'].append({'step': 'add_ip', 'status': 'error', 'error': str(e)})
                # Continue anyway - IP might already exist
            
            # Step 2: Create IP pool
            pool_name = network.get('pool_name', 'hotspot-pool')
            pool_range = network.get('pool_range')
            try:
                # Check if pool exists
                existing_pools = self._execute('/ip/pool')
                pool_exists = any(p.get('name') == pool_name for p in existing_pools)
                
                if not pool_exists:
                    self.api.path('/ip/pool').add(
                        name=pool_name,
                        ranges=pool_range
                    )
                    results['steps'].append({'step': 'create_pool', 'status': 'created'})
                else:
                    # Update existing pool
                    for p in existing_pools:
                        if p.get('name') == pool_name:
                            self.api.path('/ip/pool').set(
                                **{'.id': p.get('.id'), 'ranges': pool_range}
                            )
                            break
                    results['steps'].append({'step': 'create_pool', 'status': 'updated'})
            except Exception as e:
                results['steps'].append({'step': 'create_pool', 'status': 'error', 'error': str(e)})
                results['success'] = False
                return results
            
            # Step 3: Create hotspot profile
            profile_name = f"{server.get('name', 'hotspot')}-profile"
            login_by = ','.join(server.get('login_by', ['mac', 'http-chap']))
            try:
                existing_profiles = self._execute('/ip/hotspot/profile')
                profile_exists = any(p.get('name') == profile_name for p in existing_profiles)
                
                if not profile_exists:
                    self.api.path('/ip/hotspot/profile').add(
                        name=profile_name,
                        **{'hotspot-address': network.get('network_address')},
                        **{'dns-name': f"{server.get('name', 'hotspot')}.local"},
                        **{'login-by': login_by}
                    )
                    results['steps'].append({'step': 'create_profile', 'status': 'created'})
                else:
                    results['steps'].append({'step': 'create_profile', 'status': 'exists'})
            except Exception as e:
                results['steps'].append({'step': 'create_profile', 'status': 'error', 'error': str(e)})
            
            # Step 4: Create hotspot server
            server_name = server.get('name', 'hotspot-server')
            try:
                existing_servers = self._execute('/ip/hotspot')
                server_exists = any(s.get('name') == server_name for s in existing_servers)
                
                if not server_exists:
                    self.api.path('/ip/hotspot').add(
                        name=server_name,
                        interface=interface,
                        **{'address-pool': pool_name},
                        profile=profile_name,
                        **{'idle-timeout': server.get('idle_timeout', '5m')},
                        **{'keepalive-timeout': server.get('keepalive_timeout', '2m')},
                        disabled='no'
                    )
                    results['steps'].append({'step': 'create_server', 'status': 'created'})
                else:
                    results['steps'].append({'step': 'create_server', 'status': 'exists'})
            except Exception as e:
                results['steps'].append({'step': 'create_server', 'status': 'error', 'error': str(e)})
                results['success'] = False
                return results
            
            # Step 5: Add walled garden entries for payment gateway (PayHero)
            try:
                walled_garden_entries = [
                    'api.payhero.co.ke',
                    'payhero.co.ke',
                    '*.safaricom.co.ke',
                ]
                
                existing_wg = self._execute('/ip/hotspot/walled-garden')
                existing_hosts = {wg.get('dst-host', '') for wg in existing_wg}
                
                for host in walled_garden_entries:
                    if host not in existing_hosts:
                        try:
                            self.api.path('/ip/hotspot/walled-garden').add(
                                **{'dst-host': host},
                                action='allow',
                                comment='PayHero/M-Pesa payment gateway'
                            )
                        except:
                            pass
                
                results['steps'].append({'step': 'walled_garden', 'status': 'configured'})
            except Exception as e:
                results['steps'].append({'step': 'walled_garden', 'status': 'error', 'error': str(e)})
            
            # Store branding info (this would typically be stored in the database, not router)
            results['branding'] = branding
            results['server_name'] = server_name
            results['interface'] = interface
            
            return results
            
        except Exception as e:
            logger.error(f"Failed to configure hotspot: {str(e)}")
            return {'success': False, 'error': str(e)}
        finally:
            self.disconnect()
    
    def disable_hotspot(self, server_name: str = None) -> Dict[str, Any]:
        """
        Disable or remove hotspot server.
        
        Args:
            server_name: Name of hotspot server to disable. If None, disables all.
        """
        try:
            if not self.connect():
                return {'success': False, 'error': 'Connection failed'}
            
            servers = self._execute('/ip/hotspot')
            disabled_count = 0
            
            for server in servers:
                if server_name is None or server.get('name') == server_name:
                    try:
                        self.api.path('/ip/hotspot').set(
                            **{'.id': server.get('.id'), 'disabled': 'yes'}
                        )
                        disabled_count += 1
                    except Exception as e:
                        logger.error(f"Failed to disable hotspot {server.get('name')}: {e}")
            
            return {
                'success': True,
                'disabled_count': disabled_count,
                'message': f'Disabled {disabled_count} hotspot server(s)'
            }
            
        except Exception as e:
            logger.error(f"Failed to disable hotspot: {str(e)}")
            return {'success': False, 'error': str(e)}
        finally:
            self.disconnect()