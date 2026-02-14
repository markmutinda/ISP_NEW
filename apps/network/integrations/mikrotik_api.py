# ISP_NEW/apps/network/integrations/mikrotik_api.py
from librouteros import connect
from librouteros.query import Key
from librouteros.exceptions import TrapError
import logging
from typing import Dict, List, Optional, Any
import time
import re
import socket

logger = logging.getLogger(__name__)

class MikrotikAPI:
    """Mikrotik RouterOS API Client - Enhanced for ISP Management"""
    
    def __init__(self, mikrotik_device):
        self.device = mikrotik_device
        self.api = None
    
    def connect(self) -> bool:
            """Connect to Mikrotik device via VPN tunnel (preferred) or fallback to WAN IP."""
            try:
                # DYNAMIC IP SELECTION: Always use VPN IP if provisioned,
                # fallback to public/WAN IP. The VPN tunnel bypasses NAT.
                target_ip = (
                    self.device.vpn_ip_address
                    if (self.device.vpn_provisioned and self.device.vpn_ip_address)
                    else self.device.ip_address
                )

                if not target_ip:
                    logger.error(f"Cannot connect: No valid IP or VPN IP for {self.device.name}")
                    return False

                self.api = connect(
                    username=self.device.api_username,
                    password=self.device.api_password,
                    host=target_ip,
                    port=self.device.api_port or 8728,
                    timeout=30,
                    plain_login=True  # Required for ROS v7
                )
                logger.info(f"Connected to Mikrotik {self.device.name} ({target_ip})")
                return True
            except Exception as e:
                logger.error(f"Failed to connect to {self.device.name}: {str(e)}")
                return False
    
    def disconnect(self):
        """Disconnect from Mikrotik device"""
        if self.api:
            try:
                self.api.close()
            except:
                pass
            self.api = None
    
    def _execute(self, path: str, **kwargs) -> Any:
        """Unified execute method for standard resources (Interfaces, Users, etc)"""
        if not self.api and not self.connect():
            raise Exception(f"Cannot connect to {self.device.name}")
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
    # COMMAND EXECUTION (Reboot, Ping, Backup)
    # ────────────────────────────────────────────────────────────────

    def reboot_device(self) -> bool:
        """Reboot Mikrotik device"""
        try:
            if not self.connect(): return False
            
            # FIXED: Execute directly, don't list/print
            try:
                self.api.path('/system/reboot')()
            except (socket.error, socket.timeout):
                # Valid outcome: Connection dies immediately on reboot
                pass
            return True
        except Exception as e:
            logger.error(f"Failed to reboot device: {str(e)}")
            return False
        finally:
            self.disconnect()
            
    def reboot(self) -> bool:
        return self.reboot_device()

    def backup_config(self) -> str:
        """Backup configuration"""
        try:
            if not self.connect(): return "Backup failed: Connection failed"
            
            # FIXED: Execute directly
            self.api.path('/system/backup/save')(name='yourisp-backup')
            return "Backup created successfully"
        except Exception as e:
            logger.error(f"Failed to backup: {str(e)}")
            return f"Backup failed: {str(e)}"
        finally:
            self.disconnect()

    def ping(self, target: str = "8.8.8.8", count: int = 3) -> Dict:
        """Run ping from router"""
        try:
            if not self.connect(): return {"success": False, "error": "Connection failed"}
            
            # FIXED: Ping is a command that returns a generator
            result = list(self.api.path('/ping')(address=target, count=str(count)))
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            self.disconnect()

    def traceroute(self, target: str = "8.8.8.8") -> Dict:
        """Run traceroute"""
        try:
            if not self.connect(): return {"success": False, "error": "Connection failed"}
            
            # FIXED: Traceroute is a command
            result = list(self.api.path('/tool/traceroute')(address=target, count="1"))
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            self.disconnect()

    # ────────────────────────────────────────────────────────────────
    # LIVE STATUS & HEALTH
    # ────────────────────────────────────────────────────────────────
    
    def get_live_status(self) -> Dict[str, Any]:
        try:
            if not self.connect():
                return {"online": False, "error": "Connection failed"}
            
            try:
                # Use list() to fetch single resource items
                resource = list(self.api.path('/system/resource'))[0]
            except: resource = {}

            try:
                identity = list(self.api.path('/system/identity'))[0]
            except: identity = {}

            return {
                "online": True,
                "identity": identity.get('name', 'Unknown'),
                "model": resource.get('board-name', 'Unknown'),
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
        try:
            if not self.connect():
                raise Exception(f"Failed to connect to {self.device.name}")
            
            resources = list(self.api.path('/system/resource'))[0]
            identity = list(self.api.path('/system/identity'))[0]
            
            try:
                interfaces = list(self.api.path('/interface'))
            except: interfaces = []
            
            interface_list = []
            for iface in interfaces:
                interface_list.append({
                    'name': iface.get('name', ''),
                    'type': iface.get('type', 'ether'),
                    'mac_address': iface.get('mac-address', ''),
                    'admin_state': iface.get('disabled', 'true') == 'false',
                    'operational_state': iface.get('running', 'false') == 'true',
                })
            
            return {
                'identity': identity.get('name', 'Unknown'),
                'model': resources.get('board-name', 'Unknown'),
                'architecture': resources.get('architecture-name', 'Unknown'),
                'firmware_version': resources.get('version', 'Unknown'),
                'uptime': resources.get('uptime', '0s'),
                'interfaces': interface_list,
            }
        except Exception as e:
            logger.error(f"Sync failed: {e}")
            raise
        finally:
            self.disconnect()

    # ────────────────────────────────────────────────────────────────
    # DIAGNOSTICS & LOGS (SAFE VERSION)
    # ────────────────────────────────────────────────────────────────

    def get_system_logs(self, lines: int = 50) -> List[Dict]:
        try:
            if not self.connect(): return []
            
            # Fetch all logs (buffer) without arguments to avoid API crash
            try:
                logs = list(self.api.path('/log'))
            except: logs = []

            # Python-side slicing
            return logs[-lines:] if lines and logs else logs
        except Exception as e:
            logger.error(f"Logs failed: {e}")
            return []
        finally:
            self.disconnect()

    def get_wireless_interfaces(self) -> List[Dict]:
        try:
            if not self.connect(): return []
            # Wrap in try-except for virtual routers without wifi
            try:
                return list(self.api.path('/interface/wireless'))
            except: return []
        except: return []
        finally:
            self.disconnect()

    def get_wireless_registrations(self) -> List[Dict]:
        try:
            if not self.connect(): return []
            try:
                return list(self.api.path('/interface/wireless/registration-table'))
            except: return []
        except: return []
        finally:
            self.disconnect()

    # ────────────────────────────────────────────────────────────────
    # STANDARD GETTERS
    # ────────────────────────────────────────────────────────────────

    def get_interfaces(self) -> List[Dict[str, Any]]:
        try:
            if not self.connect(): return []
            return list(self.api.path('/interface'))
        except: return []
        finally: self.disconnect()

    def get_firewall_filter_rules(self) -> List[Dict]:
        try:
            if not self.connect(): return []
            return list(self.api.path('/ip/firewall/filter'))
        except: return []
        finally: self.disconnect()

    def get_queues(self) -> List[Dict[str, Any]]:
        try:
            if not self.connect(): return []
            return list(self.api.path('/queue/simple'))
        except: return []
        finally: self.disconnect()

    def get_dhcp_leases(self) -> List[Dict[str, Any]]:
        try:
            if not self.connect(): return []
            return list(self.api.path('/ip/dhcp-server/lease'))
        except: return []
        finally: self.disconnect()

    def get_active_hotspot_users(self) -> List[Dict]:
        try:
            if not self.connect(): return []
            return list(self.api.path('/ip/hotspot/active'))
        except: return []
        finally: self.disconnect()

    def get_active_pppoe_sessions(self) -> List[Dict]:
        try:
            if not self.connect(): return []
            return list(self.api.path('/ppp/active'))
        except: return []
        finally: self.disconnect()

    def get_hotspot_users(self) -> List[Dict[str, Any]]:
        try:
            if not self.connect(): return []
            return list(self.api.path('/ip/hotspot/user'))
        except: return []
        finally: self.disconnect()

    def get_pppoe_users(self) -> List[Dict[str, Any]]:
        try:
            if not self.connect(): return []
            secrets = list(self.api.path('/ppp/secret'))
            return [s for s in secrets if s.get('service', 'pppoe') == 'pppoe']
        except: return []
        finally: self.disconnect()

    # ────────────────────────────────────────────────────────────────
    # ACTIVE SESSION DISCONNECT (For Expired User Kick)
    # ────────────────────────────────────────────────────────────────
    
    def remove_hotspot_active_user(self, username: str) -> bool:
        """
        Kick an active hotspot user off the network.
        
        Used when:
        - Subscription expires
        - User is disabled
        - Manual disconnect requested
        
        Args:
            username: The hotspot username to disconnect
            
        Returns:
            True if user was disconnected (or wasn't connected)
        """
        try:
            if not self.connect():
                return False
            
            # Find active session for this user
            active_users = list(self.api.path('/ip/hotspot/active'))
            
            for user in active_users:
                if user.get('user') == username:
                    # Remove the active session
                    self.api.path('/ip/hotspot/active').remove(**{'.id': user['.id']})
                    logger.info(f"Kicked hotspot user {username} from {self.device.name}")
                    return True
            
            # User not active - that's fine
            logger.debug(f"Hotspot user {username} not active on {self.device.name}")
            return True
            
        except TrapError as e:
            logger.error(f"MikroTik trap error kicking hotspot user {username}: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to kick hotspot user {username}: {e}")
            return False
        finally:
            self.disconnect()
    
    def remove_pppoe_active_user(self, username: str) -> bool:
        """
        Kick an active PPPoE user off the network.
        
        Used when:
        - Subscription expires
        - User is disabled
        - Manual disconnect requested
        
        Args:
            username: The PPPoE username to disconnect
            
        Returns:
            True if user was disconnected (or wasn't connected)
        """
        try:
            if not self.connect():
                return False
            
            # Find active PPPoE session for this user
            active_sessions = list(self.api.path('/ppp/active'))
            
            for session in active_sessions:
                if session.get('name') == username:
                    # Remove the active session
                    self.api.path('/ppp/active').remove(**{'.id': session['.id']})
                    logger.info(f"Kicked PPPoE user {username} from {self.device.name}")
                    return True
            
            # User not active - that's fine
            logger.debug(f"PPPoE user {username} not active on {self.device.name}")
            return True
            
        except TrapError as e:
            logger.error(f"MikroTik trap error kicking PPPoE user {username}: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to kick PPPoE user {username}: {e}")
            return False
        finally:
            self.disconnect()
    
    def disconnect_user(self, username: str, connection_type: str = 'both') -> Dict[str, bool]:
        """
        Disconnect a user from both hotspot and PPPoE (or specific type).
        
        Args:
            username: Username to disconnect
            connection_type: 'hotspot', 'pppoe', or 'both'
            
        Returns:
            Dict with results for each type attempted
        """
        results = {}
        
        if connection_type in ('hotspot', 'both'):
            results['hotspot'] = self.remove_hotspot_active_user(username)
        
        if connection_type in ('pppoe', 'both'):
            results['pppoe'] = self.remove_pppoe_active_user(username)
        
        return results

    # ────────────────────────────────────────────────────────────────
    # POST-CONNECTION SETUP (Dashboard → Router via VPN)
    # ────────────────────────────────────────────────────────────────

    def add_port_to_bridge(self, interface_name: str, bridge_name: str = "netily-bridge") -> bool:
        """
        Assigns a physical port (e.g., ether2) to the hotspot bridge.
        Called from the dashboard after the VPN tunnel is established.
        Removes the port from any existing bridge first to avoid conflicts.
        """
        try:
            if not self.connect():
                return False
            # Remove from any existing bridge first
            existing = list(self.api.path('/interface/bridge/port'))
            for port in existing:
                if port.get('interface') == interface_name:
                    self.api.path('/interface/bridge/port').remove(**{'.id': port['.id']})
                    logger.info(f"Removed {interface_name} from bridge {port.get('bridge')}")
            # Add to our bridge
            self.api.path('/interface/bridge/port').add(bridge=bridge_name, interface=interface_name)
            logger.info(f"Added {interface_name} to {bridge_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to add port {interface_name} to bridge {bridge_name}: {e}")
            return False
        finally:
            self.disconnect()

    def remove_port_from_bridge(self, interface_name: str) -> bool:
        """Removes a physical port from any bridge it belongs to."""
        try:
            if not self.connect():
                return False
            ports = list(self.api.path('/interface/bridge/port'))
            for port in ports:
                if port.get('interface') == interface_name:
                    self.api.path('/interface/bridge/port').remove(**{'.id': port['.id']})
                    logger.info(f"Removed {interface_name} from bridge {port.get('bridge')}")
            return True
        except Exception as e:
            logger.error(f"Failed to remove port {interface_name} from bridge: {e}")
            return False
        finally:
            self.disconnect()

    def configure_hotspot(self, config_data: dict) -> dict:
        """
        Dynamically update Hotspot IP ranges and DNS names from the dashboard.
        
        Args:
            config_data: Dict with optional keys:
                - dns_name: New DNS name for the hotspot profile
                - pool_range: New IP pool range (e.g., '10.0.0.10-10.0.0.250')
        """
        try:
            if not self.connect():
                return {'success': False, 'error': 'Connection failed'}

            if 'dns_name' in config_data:
                profiles = list(self.api.path('/ip/hotspot/profile'))
                for p in profiles:
                    if p.get('name') == 'netily-profile':
                        self.api.path('/ip/hotspot/profile').set(
                            **{'.id': p['.id'], 'dns-name': config_data['dns_name']}
                        )
                        logger.info(f"Updated hotspot DNS to {config_data['dns_name']}")
                        break

            if 'pool_range' in config_data:
                pools = list(self.api.path('/ip/pool'))
                for pool in pools:
                    if pool.get('name') == 'netily-pool':
                        self.api.path('/ip/pool').set(
                            **{'.id': pool['.id'], 'ranges': config_data['pool_range']}
                        )
                        logger.info(f"Updated pool range to {config_data['pool_range']}")
                        break

            return {'success': True}
        except Exception as e:
            logger.error(f"Failed to configure hotspot: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            self.disconnect()

    def disable_hotspot(self, server_name: str = "netily-hotspot") -> bool:
        """Disable the hotspot server on the router."""
        try:
            if not self.connect():
                return False
            servers = list(self.api.path('/ip/hotspot'))
            for srv in servers:
                if srv.get('name') == server_name:
                    self.api.path('/ip/hotspot').set(**{'.id': srv['.id'], 'disabled': 'yes'})
                    logger.info(f"Disabled hotspot server {server_name}")
                    return True
            logger.warning(f"Hotspot server {server_name} not found")
            return False
        except Exception as e:
            logger.error(f"Failed to disable hotspot: {e}")
            return False
        finally:
            self.disconnect()

    def enable_hotspot(self, server_name: str = "netily-hotspot") -> bool:
        """Enable the hotspot server on the router."""
        try:
            if not self.connect():
                return False
            servers = list(self.api.path('/ip/hotspot'))
            for srv in servers:
                if srv.get('name') == server_name:
                    self.api.path('/ip/hotspot').set(**{'.id': srv['.id'], 'disabled': 'no'})
                    logger.info(f"Enabled hotspot server {server_name}")
                    return True
            logger.warning(f"Hotspot server {server_name} not found")
            return False
        except Exception as e:
            logger.error(f"Failed to enable hotspot: {e}")
            return False
        finally:
            self.disconnect()

    # ────────────────────────────────────────────────────────────────
    # HELPER METHODS
    # ────────────────────────────────────────────────────────────────

    def get_system_health(self) -> Dict:
        status = self.get_live_status()
        if not status.get('online', False): return status
        
        try:
            interfaces = self.get_interfaces()
            queues = self.get_queues()
            firewall_rules = self.get_firewall_filter_rules()
            dhcp_leases = self.get_dhcp_leases()
            
            try: hotspot_active = len(self.get_active_hotspot_users())
            except: hotspot_active = 0
                
            try: pppoe_active = len(self.get_active_pppoe_sessions())
            except: pppoe_active = 0
            
            up_interfaces = sum(1 for iface in interfaces if iface.get('running', False))
            
            status.update({
                'interfaces_total': len(interfaces),
                'interfaces_up': up_interfaces,
                'interface_health': f"{up_interfaces}/{len(interfaces)}",
                'queues_total': len(queues),
                'firewall_rules': len(firewall_rules),
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

    def _parse_size(self, size_str: str) -> int:
        try:
            size_str = str(size_str).lower().replace(' ', '')
            number = float(re.findall(r'[\d.]+', size_str)[0])
            if 'gib' in size_str: return int(number * 1024 ** 3)
            elif 'mib' in size_str: return int(number * 1024 ** 2)
            elif 'kib' in size_str: return int(number * 1024)
            return int(number)
        except: return 0

    def _parse_memory_usage(self, free: str, total: str) -> float:
        try:
            f = self._parse_size(free)
            t = self._parse_size(total)
            return ((t - f) / t * 100) if t > 0 else 0.0
        except: return 0.0

    def _parse_disk_usage(self, free: str, total: str) -> float:
        return self._parse_memory_usage(free, total)

    # ────────────────────────────────────────────────────────────────
    # CRUD OPERATIONS (Create/Update/Delete)
    # ────────────────────────────────────────────────────────────────

    def create_hotspot_user(self, username: str, password: str, profile: str = 'default', 
                           limit_uptime: str = '', limit_bytes: str = '') -> bool:
        try:
            if not self.connect(): return False
            params = {'name': username, 'password': password, 'profile': profile}
            if limit_uptime: params['limit-uptime'] = limit_uptime
            if limit_bytes: params['limit-bytes-total'] = limit_bytes
            self.api.path('/ip/hotspot/user').add(**params)
            return True
        except: return False
        finally: self.disconnect()

    def create_pppoe_user(self, username: str, password: str, profile: str = 'default-encryption',
                         local_address: str = '', remote_address: str = '') -> bool:
        try:
            if not self.connect(): return False
            params = {'name': username, 'password': password, 'service': 'pppoe', 'profile': profile}
            if local_address: params['local-address'] = local_address
            if remote_address: params['remote-address'] = remote_address
            self.api.path('/ppp/secret').add(**params)
            return True
        except: return False
        finally: self.disconnect()

    def enable_hotspot_user(self, username: str) -> bool:
        try:
            if not self.connect(): return False
            users = list(self.api.path('/ip/hotspot/user'))
            for user in users:
                if user.get('name') == username:
                    self.api.path('/ip/hotspot/user').set(**{'.id': user['.id'], 'disabled': 'no'})
                    return True
            return False
        except: return False
        finally: self.disconnect()

    def disable_hotspot_user(self, username: str) -> bool:
        try:
            if not self.connect(): return False
            users = list(self.api.path('/ip/hotspot/user'))
            for user in users:
                if user.get('name') == username:
                    self.api.path('/ip/hotspot/user').set(**{'.id': user['.id'], 'disabled': 'yes'})
                    return True
            return False
        except: return False
        finally: self.disconnect()

    def add_simple_queue(self, name: str, target: str, max_limit: str = "5M/5M") -> bool:
        try:
            if not self.connect(): return False
            self.api.path('/queue/simple').add(name=name, target=target, **{'max-limit': max_limit})
            return True
        except: return False
        finally: self.disconnect()

    def create_queue(self, name: str, target: str, max_limit: str, burst_limit: str = '', priority: str = '8') -> bool:
        try:
            if not self.connect(): return False
            params = {'name': name, 'target': target, 'max-limit': max_limit, 'priority': priority}
            if burst_limit: params['burst-limit'] = burst_limit
            self.api.path('/queue/simple').add(**params)
            return True
        except: return False
        finally: self.disconnect()

    def enable_queue(self, queue_name: str) -> bool:
        try:
            if not self.connect(): return False
            self.api.path('/queue/simple').set(**{'.id': queue_name, 'disabled': 'no'})
            return True
        except: return False
        finally: self.disconnect()

    def disable_queue(self, queue_name: str) -> bool:
        try:
            if not self.connect(): return False
            self.api.path('/queue/simple').set(**{'.id': queue_name, 'disabled': 'yes'})
            return True
        except: return False
        finally: self.disconnect()

    def add_firewall_rule(self, chain: str, action: str, src_address: str = '', dst_address: str = '', 
                         protocol: str = '', dst_port: str = '', comment: str = '') -> bool:
        try:
            if not self.connect(): return False
            params = {'chain': chain, 'action': action}
            if src_address: params['src-address'] = src_address
            if dst_address: params['dst-address'] = dst_address
            if protocol: params['protocol'] = protocol
            if dst_port: params['dst-port'] = dst_port
            if comment: params['comment'] = comment
            self.api.path('/ip/firewall/filter').add(**params)
            return True
        except: return False
        finally: self.disconnect()

    def enable_interface(self, interface_name: str) -> bool:
        try:
            if not self.connect(): return False
            self.api.path('/interface').set(**{'.id': interface_name, 'disabled': 'no'})
            return True
        except: return False
        finally: self.disconnect()

    def disable_interface(self, interface_name: str) -> bool:
        try:
            if not self.connect(): return False
            self.api.path('/interface').set(**{'.id': interface_name, 'disabled': 'yes'})
            return True
        except: return False
        finally: self.disconnect()

    def get_interface_traffic(self, interface_name: str) -> Dict:
        try:
            if not self.connect(): return {"error": "Connection failed"}
            interfaces = list(self.api.path('/interface'))
            for iface in interfaces:
                if iface.get('name') == interface_name:
                    return {
                        'rx_bytes': int(iface.get('rx-byte', 0)),
                        'tx_bytes': int(iface.get('tx-byte', 0)),
                        'rx_packets': int(iface.get('rx-packet', 0)),
                        'tx_packets': int(iface.get('tx-packet', 0)),
                    }
            return {"error": "Interface not found"}
        except Exception as e: return {"error": str(e)}
        finally: self.disconnect()