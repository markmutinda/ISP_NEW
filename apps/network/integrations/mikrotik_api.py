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
                    timeout=10.0,  # CRITICAL: Short timeout for fast failure/response
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
                # Safely unpack paths like '/ip/pool' into ('ip', 'pool') for librouteros
                if isinstance(path, str) and '/' in path:
                    path_parts = [p for p in path.split('/') if p]
                    path_obj = self.api.path(*path_parts)
                else:
                    path_obj = self.api.path(path)

                if 'get' in kwargs:
                    return list(path_obj(**kwargs['get']))
                elif 'add' in kwargs:
                    return path_obj.add(**kwargs['add'])
                elif 'update' in kwargs:
                    # Handle the new 'update' commands we added to IPAM sync
                    return path_obj.update(**kwargs['update'])
                elif 'set' in kwargs:
                    # Catch old 'set' commands from other parts of your app and redirect them to update
                    return path_obj.update(**kwargs['set'])
                elif 'remove' in kwargs:
                    # librouteros requires ID as a positional argument, not a dict keyword
                    remove_data = kwargs['remove']
                    if isinstance(remove_data, dict) and '.id' in remove_data:
                        return path_obj.remove(remove_data['.id'])
                    elif isinstance(remove_data, (list, tuple)):
                        return path_obj.remove(*remove_data)
                    else:
                        return path_obj.remove(remove_data)
                else:
                    return list(path_obj)
            except Exception as e:
                logger.error(f"API error on {path}: {str(e)}")
                raise
    
    # ────────────────────────────────────────────────────────────────
    # FIXED: MISSING METHODS THAT WERE CAUSING 500 ERRORS
    # ────────────────────────────────────────────────────────────────

    def get_hotspot_config(self):
        """
        Retrieves the current Hotspot configuration (Servers and Profiles).
        """
        try:
            if not self.connect():
                return {"servers": [], "profiles": []}
            
            # Get Profiles
            profiles = self.api.path("ip", "hotspot", "profile")
            profiles_data = tuple(profiles)
            
            # Get Servers
            servers = self.api.path("ip", "hotspot")
            servers_data = tuple(servers)

            return {
                "servers": servers_data,
                "profiles": profiles_data
            }
        except Exception as e:
            logger.error(f"Error fetching hotspot config: {e}")
            return {"servers": [], "profiles": []}
        finally:
            self.disconnect()

    def get_ports_with_usage(self):
        """
        Retrieves Interface stats (Traffic) for the dashboard.
        Filters to only show relevant port types (ether, bridge, vlan).
        """
        try:
            if not self.connect():
                return []
            
            # Get all interfaces
            interfaces = self.api.path("interface")
            
            # We only want 'ether' and 'bridge' usually (filter out wlan, pptp, etc)
            results = []
            for iface in interfaces:
                iface_type = iface.get("type", "")
                # Only include physical ports and bridges
                if iface_type in ["ether", "bridge", "vlan"]:
                    results.append({
                        "name": iface.get("name"),
                        "type": iface_type,
                        "tx-byte": iface.get("tx-byte", 0),
                        "rx-byte": iface.get("rx-byte", 0),
                        "running": iface.get("running", False),
                        "disabled": iface.get("disabled", False),
                        "mac-address": iface.get("mac-address", ""),
                        "comment": iface.get("comment", "")
                    })
            return results
        except Exception as e:
            logger.error(f"Error fetching ports: {e}")
            return []
        finally:
            self.disconnect()
    
    # ────────────────────────────────────────────────────────────────
    # COMMAND EXECUTION (Reboot, Ping, Backup)
    # ────────────────────────────────────────────────────────────────

    def reboot_device(self) -> bool:
        """Reboot Mikrotik device"""
        try:
            if not self.connect(): return False
            
            # FIXED: Execute directly, don't list/print
            try:
                self.api.path('system')('reboot')
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
            self.api.path('system', 'backup').call('save', name='yourisp-backup')
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
            result = list(self.api.path('/')('ping', address=target, count=str(count)))
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
            result = list(self.api.path('tool')('traceroute', address=target, count="1"))
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            self.disconnect()

    # ────────────────────────────────────────────────────────────────
    # LIVE STATUS & HEALTH (Optimized for speed)
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
    # DIAGNOSTICS & LOGS (Optimized with router-side filtering)
    # ────────────────────────────────────────────────────────────────

    def get_system_logs(self, lines: int = 50) -> List[Dict]:
        try:
            if not self.connect(): return []
            
            # OPTIMIZED: Use limit parameter to fetch only what we need
            # This prevents downloading thousands of logs
            try:
                # Some RouterOS versions support limit, others don't
                # Try with limit first
                logs = list(self.api.path('/log')(limit=lines))
            except:
                # Fallback to fetching all and slicing in Python
                logs = list(self.api.path('/log'))
                logs = logs[-lines:] if lines and logs else logs
                
            return logs
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
    # STANDARD GETTERS (Optimized with router-side filtering where possible)
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
            # OPTIMIZED: Only get active/bound leases
            leases = list(self.api.path('/ip/dhcp-server/lease'))
            # Filter in Python for now
            return [l for l in leases if l.get('status') == 'bound']
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
                    # Remove the active session - pass ID directly
                    self.api.path('/ip/hotspot/active').remove(user['.id'])
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
                    # Remove the active session - pass ID directly
                    self.api.path('/ppp/active').remove(session['.id'])
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
    # PORT SCANNING (Pre-configuration health check)
    # ────────────────────────────────────────────────────────────────

    def scan_ports(self, ports: Optional[List[int]] = None, timeout: float = 1.5) -> Dict:
        """
        TCP port scan against the router's IP. Used to verify reachability
        of common MikroTik services before attempting configuration.

        Args:
            ports: List of TCP ports to probe. Defaults to standard MikroTik ports.
            timeout: Socket timeout per port in seconds.

        Returns:
            Dict with 'target_ip', 'results' list, 'api_reachable' bool,
            'winbox_reachable' bool, 'web_reachable' bool.
        """
        DEFAULT_PORTS = [
            (8728, "API",       "MikroTik API (plain)"),
            (8729, "API-SSL",   "MikroTik API (SSL)"),
            (80,   "HTTP",      "Web management (HTTP)"),
            (443,  "HTTPS",     "Web management (HTTPS)"),
            (22,   "SSH",       "Secure Shell"),
            (8291, "Winbox",    "Winbox management"),
            (21,   "FTP",       "File Transfer"),
            (23,   "Telnet",    "Telnet access"),
            (8080, "HTTP-Alt",  "HTTP proxy / alt web"),
            (53,   "DNS",       "DNS service"),
        ]

        target_ip = (
            self.device.vpn_ip_address
            if (self.device.vpn_provisioned and self.device.vpn_ip_address)
            else self.device.ip_address
        )

        if not target_ip:
            return {
                'target_ip': None,
                'results': [],
                'error': 'No IP address configured on this router',
                'api_reachable': False,
                'winbox_reachable': False,
                'web_reachable': False,
            }

        scan_list = []
        if ports:
            # Build scan list from custom ports
            port_map = {p[0]: p for p in DEFAULT_PORTS}
            for port_num in ports:
                if port_num in port_map:
                    scan_list.append(port_map[port_num])
                else:
                    scan_list.append((port_num, f"Port-{port_num}", f"Custom port {port_num}"))
        else:
            scan_list = DEFAULT_PORTS

        results = []
        for port_num, service_name, description in scan_list:
            entry = {
                'port': port_num,
                'service': service_name,
                'description': description,
                'status': 'closed',
                'latency_ms': None,
            }
            try:
                start = time.time()
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                result = sock.connect_ex((target_ip, port_num))
                elapsed = (time.time() - start) * 1000
                sock.close()
                if result == 0:
                    entry['status'] = 'open'
                    entry['latency_ms'] = round(elapsed, 1)
                else:
                    entry['status'] = 'closed'
            except socket.timeout:
                entry['status'] = 'filtered'
            except OSError:
                entry['status'] = 'error'

            results.append(entry)

        api_open = any(r['port'] in (8728, 8729) and r['status'] == 'open' for r in results)
        winbox_open = any(r['port'] == 8291 and r['status'] == 'open' for r in results)
        web_open = any(r['port'] in (80, 443) and r['status'] == 'open' for r in results)

        return {
            'target_ip': target_ip,
            'results': results,
            'api_reachable': api_open,
            'winbox_reachable': winbox_open,
            'web_reachable': web_open,
            'open_count': sum(1 for r in results if r['status'] == 'open'),
            'total_scanned': len(results),
        }

    def get_full_interface_detail(self) -> List[Dict]:
        """
        Retrieve ALL interfaces (ethernet, wireless, bridge, vlan) with enriched
        information about current usage — which bridge each port belongs to, whether
        an IP address is assigned, and traffic stats.  Used by the Hotspot Setup
        Wizard to let users pick the right interface.
        """
        try:
            if not self.connect():
                return []

            # 1. All interfaces
            interfaces = list(self.api.path('interface'))

            # 2. Bridge port membership
            try:
                bridge_ports = {
                    bp.get('interface'): bp.get('bridge')
                    for bp in self.api.path('interface', 'bridge', 'port')
                }
            except Exception:
                bridge_ports = {}

            # 3. IP addresses → map interface → address
            try:
                ip_addrs = {}
                for addr in self.api.path('ip', 'address'):
                    ip_addrs[addr.get('interface', '')] = addr.get('address', '')
            except Exception:
                ip_addrs = {}

            # 4. Hotspot servers → which interface is already a hotspot
            try:
                hotspot_ifaces = {
                    srv.get('interface'): srv.get('name')
                    for srv in self.api.path('ip', 'hotspot')
                }
            except Exception:
                hotspot_ifaces = {}

            enriched = []
            for iface in interfaces:
                name = iface.get('name', '')
                itype = iface.get('type', '')

                # Determine current use
                current_use = 'unused'
                if name in hotspot_ifaces:
                    current_use = 'hotspot'
                elif name in ip_addrs:
                    addr = ip_addrs[name]
                    # WAN heuristic: if it has a public-ish IP or is ether1
                    if name == 'ether1' or (addr and not addr.startswith(('10.', '172.', '192.168.'))):
                        current_use = 'wan'
                    else:
                        current_use = 'lan'
                elif name in bridge_ports:
                    current_use = 'bridge-member'

                enriched.append({
                    'name': name,
                    'type': itype,
                    'mac_address': iface.get('mac-address', ''),
                    'running': iface.get('running', 'false') == 'true' if isinstance(iface.get('running'), str) else bool(iface.get('running', False)),
                    'disabled': iface.get('disabled', 'false') == 'true' if isinstance(iface.get('disabled'), str) else bool(iface.get('disabled', False)),
                    'default_name': iface.get('default-name', ''),
                    'comment': iface.get('comment', ''),
                    'speed': iface.get('link-speed', '') if itype == 'ether' else None,
                    'bridge': bridge_ports.get(name),
                    'ip_address': ip_addrs.get(name),
                    'current_use': current_use,
                    'hotspot_server': hotspot_ifaces.get(name),
                    'tx_bytes': int(iface.get('tx-byte', 0)),
                    'rx_bytes': int(iface.get('rx-byte', 0)),
                })

            return enriched
        except Exception as e:
            logger.error(f"Error fetching full interface detail: {e}")
            return []
        finally:
            self.disconnect()

    def full_hotspot_setup(self, config: dict) -> dict:
        """
        Complete hotspot setup on the router from scratch.  Creates:
          1. IP pool
          2. DHCP server
          3. Hotspot profile
          4. Hotspot server
          5. IP address on interface

        IMPORTANT:
        Do not use short defaults (like 5m/2m) here.
        They can terminate active hotspot users within a few minutes,
        even when their paid subscription is still valid.

        Args:
            config: {
                'interface':       'ether2',
                'gateway':         '10.5.50.1',
                'network_mask':    '24',
                'pool_name':       'hs-pool-1',
                'pool_range':      '10.5.50.10-10.5.50.254',
                'dns_server':      '8.8.8.8',
                'server_name':     'hotspot1',
                'profile_name':    'hs-profile-1',
                'idle_timeout':    'none',      # FIXED: Default to 'none' instead of '5m'
                'keepalive_timeout': '10m',     # FIXED: Use longer keepalive (10m) instead of '2m'
                'login_by':        'mac,http-chap',
                'dns_name':        'hotspot.local',
            }

        Returns:
            { 'success': bool, 'steps': [...], 'error': str|None }
        """
        steps = []
        try:
            if not self.connect():
                return {'success': False, 'steps': steps, 'error': 'Connection failed'}

            iface       = config['interface']
            gateway     = config.get('gateway', '10.5.50.1')
            mask        = config.get('network_mask', '24')
            pool_name   = config.get('pool_name', 'hs-pool-1')
            pool_range  = config.get('pool_range', '10.5.50.10-10.5.50.254')
            dns         = config.get('dns_server', '8.8.8.8')
            srv_name    = config.get('server_name', 'hotspot1')
            prof_name   = config.get('profile_name', f'{srv_name}-profile')
            
            # FIX: Use 'none' for idle timeout by default (no idle disconnect)
            # This prevents users from being kicked when their phone screen locks
            # or traffic briefly stops while their subscription is still valid.
            idle        = config.get('idle_timeout', 'none')
            
            # FIX: Use longer keepalive timeout (10 minutes) to prevent premature
            # session termination during temporary network hiccups.
            keepalive   = config.get('keepalive_timeout', '10m')
            
            login_by    = config.get('login_by', 'mac,http-chap')
            dns_name    = config.get('dns_name', '')

            # ── Step 1: Assign IP to interface ──
            try:
                # Remove existing IP on this interface first
                for addr in self.api.path('ip', 'address'):
                    if addr.get('interface') == iface:
                        self.api.path('ip', 'address').remove(addr['.id'])
                self.api.path('ip', 'address').add(
                    address=f'{gateway}/{mask}',
                    interface=iface,
                )
                steps.append({'step': 'ip_address', 'status': 'ok', 'detail': f'{gateway}/{mask} on {iface}'})
            except Exception as e:
                steps.append({'step': 'ip_address', 'status': 'error', 'detail': str(e)})
                return {'success': False, 'steps': steps, 'error': f'IP assignment failed: {e}'}

            # ── Step 2: Create IP Pool ──
            try:
                # Remove existing pool if same name
                for pool in self.api.path('ip', 'pool'):
                    if pool.get('name') == pool_name:
                        self.api.path('ip', 'pool').remove(pool['.id'])
                self.api.path('ip', 'pool').add(
                    name=pool_name,
                    ranges=pool_range,
                )
                steps.append({'step': 'ip_pool', 'status': 'ok', 'detail': f'{pool_name}: {pool_range}'})
            except Exception as e:
                steps.append({'step': 'ip_pool', 'status': 'error', 'detail': str(e)})
                return {'success': False, 'steps': steps, 'error': f'Pool creation failed: {e}'}

            # ── Step 3: Create Hotspot Profile ──
            try:
                for p in self.api.path('ip', 'hotspot', 'profile'):
                    if p.get('name') == prof_name:
                        self.api.path('ip', 'hotspot', 'profile').remove(p['.id'])
                add_args = {
                    'name': prof_name,
                    'hotspot-address': gateway,
                    'rate-limit': '',
                }
                if dns_name:
                    add_args['dns-name'] = dns_name
                if login_by:
                    add_args['login-by'] = login_by
                self.api.path('ip', 'hotspot', 'profile').add(**add_args)
                steps.append({'step': 'hotspot_profile', 'status': 'ok', 'detail': prof_name})
            except Exception as e:
                steps.append({'step': 'hotspot_profile', 'status': 'error', 'detail': str(e)})
                return {'success': False, 'steps': steps, 'error': f'Profile creation failed: {e}'}

            # ── Step 4: Create Hotspot Server ──
            try:
                for srv in self.api.path('ip', 'hotspot'):
                    if srv.get('name') == srv_name:
                        self.api.path('ip', 'hotspot').remove(srv['.id'])
                self.api.path('ip', 'hotspot').add(
                    name=srv_name,
                    interface=iface,
                    **{'address-pool': pool_name},
                    profile=prof_name,
                    **{'idle-timeout': idle},
                    **{'keepalive-timeout': keepalive},
                    disabled='no',
                )
                steps.append({'step': 'hotspot_server', 'status': 'ok', 'detail': srv_name})
            except Exception as e:
                steps.append({'step': 'hotspot_server', 'status': 'error', 'detail': str(e)})
                return {'success': False, 'steps': steps, 'error': f'Server creation failed: {e}'}

            # ── Step 5: Set DNS ──
            try:
                self.api.path('ip', 'dns').set(servers=dns)
                steps.append({'step': 'dns', 'status': 'ok', 'detail': dns})
            except Exception as e:
                steps.append({'step': 'dns', 'status': 'warning', 'detail': f'DNS set failed (non-fatal): {e}'})

            return {
                'success': True,
                'steps': steps,
                'server_name': srv_name,
                'profile_name': prof_name,
                'pool_name': pool_name,
            }

        except Exception as e:
            logger.error(f"Full hotspot setup failed: {e}")
            return {'success': False, 'steps': steps, 'error': str(e)}
        finally:
            self.disconnect()

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
                    self.api.path('/interface/bridge/port').remove(port['.id'])
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
                    self.api.path('/interface/bridge/port').remove(port['.id'])
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
            # OPTIMIZED: Fetch in parallel? No, but we can limit what we fetch
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