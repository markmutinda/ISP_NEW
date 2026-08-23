"""
NEW VERSION OF NETILY SCRIPT GENERATOR

"""

import logging
from django.conf import settings
from apps.network.models.router_models import Router
from django.utils import timezone
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────
# 🔥 VERSION STAMP — bump this every time generate_login_html()
# changes meaningfully. Used by the diagnostic engine to detect
# stale login.html on routers.
# ────────────────────────────────────────────────────────────────
LOGIN_HTML_VERSION = "4"  # bump this every time generate_login_html() changes meaningfully


class MikrotikScriptGenerator:
    def __init__(self, router: Router, request=None):
        self.router = router
        self.request = request

        # ── VPN Gateway Logic ─────────────────────────────────────────
        self.vpn_gateway = getattr(settings, 'VPN_GATEWAY_IP', '10.8.0.1')
        self.vpn_network_cidr = getattr(settings, 'VPN_NETWORK_CIDR', '10.8.0.0/16')
        
        # ── Production URLs Logic ─────────────────────────────────────
        self.base_url = getattr(settings, 'BASE_URL', 'https://api.netily.co.ke').rstrip('/')
        self.portal_url = getattr(settings, 'FRONTEND_URL', 'https://netily.co.ke').rstrip('/')

        _api_url = getattr(settings, 'API_URL', '').rstrip('/')
        if not _api_url:
            _portal_host = self.portal_url.split('://')[-1]
            _base_host = self.base_url.split('://')[-1]
            if _base_host == _portal_host:
                _api_url = f"https://api.{_portal_host}"
            else:
                _api_url = self.base_url
        
        self.api_url = _api_url
        self.active_url = self.api_url  
        
        self.provision_base = f"{self.api_url}/api/v1/network/provision"
        
        self.vpn_server_ip = getattr(settings, 'VPN_SERVER_IP', '10.8.0.1')
        self.vpn_api_url = getattr(settings, 'VPN_API_URL', f'http://{self.vpn_server_ip}:8000')

    def _escape_ros_string(self, s: str) -> str:
        if s is None:
            return ""
        s = s.replace('\\', '\\\\')
        s = s.replace('"', '\\"')
        s = s.replace('$', '\\$')
        return s

    def _get_vpn_host(self, r: Router) -> str:
        vpn_host = r.openvpn_server
        if not vpn_host or vpn_host == "vpn.yourisp.com":
            from urllib.parse import urlparse
            parsed_url = urlparse(self.api_url)
            root_domain = parsed_url.netloc.replace('api.', '')
            return f"vpn.{root_domain}"
        return vpn_host

    def get_tenant_portal_url(self) -> str:
        r = self.router
        base_frontend = self.portal_url
        
        if not r.tenant_subdomain or r.tenant_subdomain == 'public':
            return base_frontend
            
        parsed = urlparse(base_frontend)
        netloc = parsed.netloc
        
        if netloc.startswith(f"{r.tenant_subdomain}."):
            return base_frontend
            
        return f"{parsed.scheme}://{r.tenant_subdomain}.{netloc}"

    def get_magic_link(self) -> str:
        r = self.router
        url = f"{self.provision_base}/{r.auth_key}/{r.provision_slug}/script.rsc"
        return (
            f'/tool fetch url="{url}" dst-path="netily.rsc"; '
            f':delay 2s; /import netily.rsc'
        )

    def generate_base_script(self) -> str:
        r = self.router
        subdomain = r.tenant_subdomain or 'public'
        api_path = f"/api/v1/network/provision/{r.auth_key}/{r.provision_slug}/config"
        
        absolute_api_path = f"{self.api_url}{api_path}"
        config_fetch_url = absolute_api_path.split('?')[0]

        return f""":put ">>> NETILY CLOUD CONTROLLER v4.6 <<<"
:local configUrl ("{config_fetch_url}?version=7&router={r.id}&subdomain={subdomain}")
:put ("Downloading config from: " . $configUrl)
/tool fetch url=$configUrl dst-path="netily_conf.rsc" check-certificate=no
:delay 2s
/import netily_conf.rsc
:put ">>> Stage 2 complete."
"""

    def generate_config_script(self, version: str = "7") -> str:
        r = self.router
        v = str(version).strip()
        is_v6 = v.startswith("6")

        portal_domain = self.portal_url.split('://')[-1]
        
        from apps.network.services.ipam_calculator import calculate_mikrotik_hotspot_network
        
        base_ip = getattr(r, 'hotspot_base_ip', None) or '172.12.0.1'
        cidr = getattr(r, 'hotspot_subnet_cidr', None) or 16
        
        math = calculate_mikrotik_hotspot_network(base_ip, cidr)
        
        gateway_ip = math['gateway']
        pool_range = math['pool_range']
        dhcp_network = f"{math['network']}/{cidr}"
        r_gateway_cidr = math['interface_address']

        pppoe_local = getattr(r, 'pppoe_local_address', None) or r.get_pppoe_local_ip()

        ovpn_cipher = "aes256"
        ovpn_auth = "sha1"

        sections = [
            self._section_header(r, v),
            self._section_identity_cleanup(r),
            self._section_api_user(r),
            self._section_openvpn(r, ovpn_cipher, ovpn_auth, is_v6),
            self._section_firewall(r),
            self._section_bridge_ports(r, r_gateway_cidr),
            self._section_dhcp(r, gateway_ip, pool_range, dhcp_network),
            self._section_radius(r),
            self._section_hotspot(r, gateway_ip),
            self._section_walled_garden(r, portal_domain),
            self._section_ssl_certs(r),
            self._section_hotspot_html(r),
            self._section_pppoe(r, pppoe_local) if r.enable_pppoe else "",
            self._section_anti_sharing(r, is_v6),
            self._section_nat(r),
            self._section_schedulers(r),
            self._section_footer(r),
        ]
        
        full_script = "\n".join(s for s in sections if s)
        
        lines = []
        for line in full_script.splitlines():
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            lines.append(line)
            
        return "\n".join(lines)

    def _section_header(self, r: Router, version: str) -> str:
        return f"""# ═══════════════════════════════════════════════════════════════
# Netily Cloud Controller — Configuration Script v4.6
# Router: {self._escape_ros_string(r.name)}
# Tenant: {self._escape_ros_string(r.tenant_subdomain or 'public')}
# VPN IP: {r.vpn_ip_address or 'auto-assigned'}
# VPN Network: {self.vpn_network_cidr}
# RouterOS: v{version}
# Generated: {timezone.now().strftime("%Y-%m-%d %H:%M:%S")}
# ═══════════════════════════════════════════════════════════════
:put ">>> Netily v4.6 — Configuring {self._escape_ros_string(r.name)} (RouterOS v{version})..."
:delay 1s
"""

    def _section_identity_cleanup(self, r: Router) -> str:
        return f"""# ─────────────────────────────────────────────────────────────
# 1. SYSTEM IDENTITY, CLOCK & CLEANUP
# ─────────────────────────────────────────────────────────────
/system identity set name="{self._escape_ros_string(r.name)}"
/system clock set time-zone-name=Africa/Nairobi
:put "Cleaning up old Netily configurations..."

:do {{ :foreach i in=[/ip hotspot find name="netily-hotspot"] do={{ /ip hotspot remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/ip hotspot profile find name="netily-profile"] do={{ /ip hotspot profile remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/ip pool find name="netily-pool"] do={{ /ip pool remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/ip pool find name="netily-pppoe-pool"] do={{ /ip pool remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/ip dhcp-server find name="netily-dhcp"] do={{ /ip dhcp-server remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/ip dhcp-server network find comment="Netily DHCP Network"] do={{ /ip dhcp-server network remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/interface ovpn-client find name="Netily-VPN"] do={{ /interface ovpn-client remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/interface wireguard find name="Netily-VPN"] do={{ /interface wireguard remove $i }} }} on-error={{}}
:do {{ /interface wireguard peers remove [find where interface="Netily-VPN"] }} on-error={{}}
:do {{ :foreach i in=[/ip address find comment="Netily-WG-IP"] do={{ /ip address remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/ppp profile find name="netily-pppoe-profile"] do={{ /ppp profile remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/interface pppoe-server server find name="netily-pppoe"] do={{ /interface pppoe-server server remove $i }} }} on-error={{}}
:do {{ /interface bridge port remove [find bridge="netily-bridge"] }} on-error={{}}
:do {{ /ip address remove [find interface="netily-bridge"] }} on-error={{}}
:do {{ /interface bridge remove [find name="netily-bridge"] }} on-error={{}}
:do {{ /ip firewall filter remove [find comment~"Netily"] }} on-error={{}}
:do {{ /ip firewall nat remove [find comment~"Netily"] }} on-error={{}}
:do {{ /ip firewall mangle remove [find comment~"Netily"] }} on-error={{}}

:put "Cleanup complete."
"""

    def _section_api_user(self, r: Router) -> str:
        password = self._escape_ros_string(r.api_password)
        username = self._escape_ros_string(r.api_username)
        return f""":put "Configuring API user..."
:do {{ /user remove [find name="{username}"] }} on-error={{}}
/user add name="{username}" group=full password="{password}" comment="Netily Cloud API"
/ip service set api disabled=no port={r.api_port} address={self.vpn_gateway}/32,127.0.0.0/8,172.18.0.0/16
/ip service set api-ssl disabled=yes
"""

    def _section_openvpn(self, r: Router, cipher: str, auth: str, is_v6: bool) -> str:
        if is_v6:
            vpn_host = self._get_vpn_host(r)
            return f"""# ─────────────────────────────────────────────────────────────
# 3. OPENVPN TUNNEL (RouterOS v6 fallback)
# ─────────────────────────────────────────────────────────────
:put "RouterOS v6: establishing OpenVPN tunnel..."
/interface ovpn-client add name="Netily-VPN" \\
    connect-to="{self._escape_ros_string(vpn_host)}" \\
    port={r.openvpn_port} \\
    user="{self._escape_ros_string(r.openvpn_username)}" \\
    password="{self._escape_ros_string(r.openvpn_password)}" \\
    cipher=aes256-cbc auth=sha1 protocol=udp \\
    add-default-route=no \\
    comment="Netily Cloud Controller Tunnel"
:delay 8s
:put "OpenVPN tunnel configured (v6 mode)."
"""

        wg_private_key = r.wireguard_private_key or ''
        
        import os as _os
        wg_server_pubkey = (
            getattr(settings, 'WG_SERVER_PUBLIC_KEY', '')
            or _os.environ.get('WG_SERVER_PUBLIC_KEY', '')
        )
        _wg_host = (
            getattr(settings, 'WG_SERVER_HOST', '')
            or _os.environ.get('WG_SERVER_HOST', '')
            or getattr(settings, 'VPN_SERVER_IP', '')
            or _os.environ.get('VPN_SERVER_IP', self.vpn_server_ip)
        )
        _wg_port = str(
            getattr(settings, 'WG_SERVER_PORT', '')
            or _os.environ.get('WG_SERVER_PORT', '51820')
        )
        wg_endpoint = f"{_wg_host}:{_wg_port}" if _wg_host else ''
        wg_endpoint_host = wg_endpoint.split(':')[0] if ':' in wg_endpoint else wg_endpoint
        wg_endpoint_port = wg_endpoint.split(':')[1] if ':' in wg_endpoint else '51820'

        vpn_ip = r.vpn_ip_address or ''
        vpn_network_cidr = self.vpn_network_cidr

        if not wg_private_key or not wg_server_pubkey:
            vpn_host = self._get_vpn_host(r)
            return f"""# ─────────────────────────────────────────────────────────────
# 3. OPENVPN TUNNEL (WireGuard keys not provisioned — falling back)
# ─────────────────────────────────────────────────────────────
:put "WireGuard keys not ready, using OpenVPN fallback..."
/interface ovpn-client add name="Netily-VPN" \\
    connect-to="{self._escape_ros_string(vpn_host)}" \\
    port={r.openvpn_port} \\
    user="{self._escape_ros_string(r.openvpn_username)}" \\
    password="{self._escape_ros_string(r.openvpn_password)}" \\
    cipher=aes256-cbc auth=sha1 protocol=udp \\
    add-default-route=no \\
    comment="Netily Cloud Controller Tunnel (fallback)"
:delay 8s
:put "OpenVPN fallback tunnel configured."
"""

        return f"""# ─────────────────────────────────────────────────────────────
# 3. WIREGUARD TUNNEL (Compatible with ALL v7 versions)
# ─────────────────────────────────────────────────────────────
:put "Configuring WireGuard VPN tunnel..."

:do {{ /interface wireguard peers remove [find where interface="Netily-VPN"] }} on-error={{}}
:do {{ /interface wireguard remove [find name="Netily-VPN"] }} on-error={{}}
:do {{ /ip address remove [find comment="Netily-WG-IP"] }} on-error={{}}

:do {{ /interface wireguard add name="Netily-VPN" listen-port=51820 mtu=1320 comment="Netily Cloud Controller WireGuard" }} on-error={{ :put "Error: Could not add WireGuard interface" }}
:do {{ /interface wireguard set [find name="Netily-VPN"] private-key="{self._escape_ros_string(wg_private_key)}" }} on-error={{ :put "Error: Could not set WireGuard private key" }}
:do {{ /ip address add address="{vpn_ip}/{vpn_network_cidr.split('/')[1]}" interface="Netily-VPN" comment="Netily-WG-IP" }} on-error={{ :put "Warning: WireGuard IP may already exist" }}

:do {{ /interface wireguard peers add interface="Netily-VPN" public-key="{self._escape_ros_string(wg_server_pubkey)}" endpoint-address="{wg_endpoint_host}" endpoint-port={wg_endpoint_port} allowed-address=0.0.0.0/0 persistent-keepalive=15s comment="Netily Cloud Server" }} on-error={{ :put "Error: Could not add WireGuard peer" }}

:do {{ /ip firewall filter add chain=output action=accept protocol=udp dst-port=1812,1813,3799 out-interface="Netily-VPN" comment="Netily-RADIUS-Output" }} on-error={{}}
:do {{ /ip firewall filter remove [find comment="Netily-WG-Input"] }} on-error={{}}
:do {{ /ip firewall filter add chain=input action=accept protocol=udp dst-port=51820 in-interface=all-ethernet comment="Netily-WG-Input" }} on-error={{}}
:do {{ /ip firewall filter add chain=input action=accept connection-state=established,related comment="Netily-Established" }} on-error={{}}

:delay 5s
:put "WireGuard VPN tunnel configured — IP: {vpn_ip} (MTU: 1320)"
:put "RADIUS accounting traffic allowed via tunnel (stable + long-term compatible)"
"""

    def _section_firewall(self, r: Router) -> str:
        return f"""# ─────────────────────────────────────────────────────────────
# 4. FIREWALL (VPN & Management)
# ─────────────────────────────────────────────────────────────
:put "Configuring firewall rules..."

/ip firewall filter add chain=input action=accept src-address={self.vpn_network_cidr} comment="Netily-VPN-Input-Allow"
/ip firewall filter add chain=forward action=accept src-address={self.vpn_network_cidr} comment="Netily-VPN-Forward-Allow"
/ip firewall filter add chain=forward action=accept dst-address={self.vpn_network_cidr} comment="Netily-VPN-Forward-Return"
"""

    def _section_bridge_ports(self, r: Router, gateway_cidr: str) -> str:
        port_cmds = []
        ports = r.hotspot_interfaces or []
        
        for port in ports:
            safe_port = port.strip()
            if not safe_port: continue
            
            port_cmds.append(f"""
:do {{ /interface bridge port remove [find interface="{safe_port}"] }} on-error={{}}
:do {{ /interface bridge port add bridge="netily-bridge" interface="{safe_port}" }} on-error={{ :put "Error adding {safe_port}" }}
""")

        ports_script = "\n".join(port_cmds)

        return f"""# ─────────────────────────────────────────────────────────────
# 5. SUPER BRIDGE & PORTS
# ─────────────────────────────────────────────────────────────
:put "Configuring Bridge Topology..."

:do {{ /interface bridge remove [find name="netily-bridge"] }} on-error={{}}
/interface bridge add name="netily-bridge" comment="Netily Hotspot & PPPoE"

:do {{ /ip address remove [find interface="netily-bridge"] }} on-error={{}}
/ip address add address="{gateway_cidr}" interface="netily-bridge" comment="Netily Gateway"

{ports_script}
"""

    def _section_dhcp(self, r: Router, gateway_ip: str, pool_range: str, dhcp_network: str) -> str:
        return f"""# ─────────────────────────────────────────────────────────────
# 6. IP POOL & DHCP (Bridge Mode)
# ─────────────────────────────────────────────────────────────
:put "Configuring DHCP..."

:do {{ /ip dhcp-server network remove [find address="{dhcp_network}"] }} on-error={{}}

/ip pool add name="netily-pool" ranges="{pool_range}"
/ip dhcp-server add name="netily-dhcp" interface="netily-bridge" address-pool="netily-pool" lease-time=1h disabled=no
/ip dhcp-server network add address="{dhcp_network}" gateway="{gateway_ip}" dns-server=8.8.8.8,1.1.1.1 comment="Netily DHCP Network"
"""

    def _section_radius(self, r: Router) -> str:
        # Removed authentication-port and accounting-port (deprecated on v7)
        radius_cmd = (
            f'/radius add address={self.vpn_gateway} secret="{self._escape_ros_string(r.shared_secret)}" '
            f'service=hotspot,ppp timeout=3000ms comment="Netily-Cloud-RADIUS"'
        )
        
        return f"""# ─────────────────────────────────────────────────────────────
# 7. RADIUS (Cloud RADIUS via VPN Tunnel)
# ─────────────────────────────────────────────────────────────
:put "Configuring Cloud RADIUS..."
:do {{ /radius remove [find comment~"Netily"] }} on-error={{}}
{radius_cmd}
/radius incoming set accept=yes port=3799
"""

    def _section_hotspot(self, r: Router, gateway_ip: str) -> str:
        # Removed rate-limit="" (crashes v7 parser)
        profile_cmd = f'/ip hotspot profile add name="netily-profile" hotspot-address="{gateway_ip}" dns-name="{self._escape_ros_string(r.dns_name)}" login-by=http-pap,mac-cookie use-radius=yes radius-accounting=yes http-cookie-lifetime=1d'
        server_cmd = f'/ip hotspot add name="netily-hotspot" interface="netily-bridge" address-pool="netily-pool" profile="netily-profile" disabled=no'
        
        return f"""# ─────────────────────────────────────────────────────────────
# 8. HOTSPOT PROFILE & SERVER (Bridge Mode)
# ─────────────────────────────────────────────────────────────
:put "Configuring Hotspot..."
{profile_cmd}
{server_cmd}

/ip hotspot user profile set [find name="default"] shared-users=1 keepalive-timeout=10m

/ip hotspot profile set [find name="netily-profile"] use-radius=yes radius-accounting=yes radius-interim-update=00:03:00
:put " + RADIUS accounting enabled with 3-minute interim updates"
"""

    def _section_walled_garden(self, r: Router, portal_domain: str) -> str:
        """
        WALLED GARDEN — Centipid-style (domain → address-list)

        Uses an address-list containing the real domain names.
        RouterOS resolves them and keeps the IPs updated automatically.
        This stops SNI/Host spoofing while surviving Droplet IP changes.
        """
        tenant_domain = urlparse(self.get_tenant_portal_url()).netloc
        api_domain = urlparse(self.api_url).netloc

        # Domains we own and want to IP-pin
        own_domains = []
        for d in (tenant_domain, api_domain, portal_domain):
            if d and d not in own_domains:
                own_domains.append(d)

        # Build the address-list entries
        addr_list_lines = []
        for domain in own_domains:
            addr_list_lines.append(
                f'/ip firewall address-list add list=netily-portal-ips '
                f'address="{domain}" comment="Netily-Portal-Domain"'
            )
        addr_list_script = "\n".join(addr_list_lines) if addr_list_lines else ""

        return f"""# ─────────────────────────────────────────────────────────────
# 9. WALLED GARDEN (domain → address-list, anti-tunnel)
# ─────────────────────────────────────────────────────────────
:put "Configuring hardened Walled Garden (address-list style)..."

# Clean previous Netily rules
:do {{ :foreach i in=[/ip hotspot walled-garden find comment~"Netily"] do={{ /ip hotspot walled-garden remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/ip hotspot walled-garden ip find comment~"Netily"] do={{ /ip hotspot walled-garden ip remove $i }} }} on-error={{}}
:do {{ /ip firewall address-list remove [find list="netily-portal-ips"] }} on-error={{}}

# 1. Put our domains into an address-list (RouterOS will resolve & keep IPs fresh)
{addr_list_script}

# 2. Allow only real destination IPs belonging to those domains (ports 80 + 443)
/ip hotspot walled-garden ip add action=accept protocol=tcp dst-port=80  dst-address-list=netily-portal-ips comment="Netily-Portal-80"
/ip hotspot walled-garden ip add action=accept protocol=tcp dst-port=443 dst-address-list=netily-portal-ips comment="Netily-Portal-443"

# 3. Third-party payment gateways (hostname only — we cannot pin their IPs)
/ip hotspot walled-garden add dst-host="*.safaricom.co.ke" comment="Netily-MPesa"
/ip hotspot walled-garden add dst-host="*.safaricom.com" comment="Netily-Safaricom"

# 4. VPN / RADIUS (already IP-based)
/ip hotspot walled-garden ip add dst-address={self.vpn_gateway}/32 action=accept comment="Netily-VPN-API"
/ip hotspot walled-garden ip add dst-address={self.vpn_network_cidr} action=accept comment="Netily-VPN-Network"

:put "Walled Garden hardened (address-list style)."
"""

    def _section_ssl_certs(self, r: Router) -> str:
        if not r.ssl_certificate:
            return f"""# ─────────────────────────────────────────────────────────────
# 10. SSL CERTIFICATES (Hotspot HTTPS)
# ─────────────────────────────────────────────────────────────
:put "No SSL certificates configured — hotspot will use HTTP only."
"""
        ssl_cert_url = f"{self.api_url}/api/v1/network/provision/{r.auth_key}/certs/ssl.crt"
        ssl_key_url = f"{self.api_url}/api/v1/network/provision/{r.auth_key}/certs/ssl.key"
        passphrase = self._escape_ros_string(r.ssl_passphrase or '')

        return f"""# ─────────────────────────────────────────────────────────────
# 10. SSL CERTIFICATES (Hotspot HTTPS)
# ─────────────────────────────────────────────────────────────
:put "Downloading SSL certificates..."

:do {{ /certificate remove [find name~"netily-ssl"] }} on-error={{}}

/tool fetch url="{ssl_cert_url}" dst-path="netily-ssl.crt" check-certificate=no
:delay 1s
:do {{ /certificate import file-name="netily-ssl.crt" passphrase="{passphrase}" }} on-error={{ :put "SSL cert import warning" }}
/tool fetch url="{ssl_key_url}" dst-path="netily-ssl.key" check-certificate=no
:delay 1s
:do {{ /certificate import file-name="netily-ssl.key" passphrase="{passphrase}" }} on-error={{ :put "SSL key import warning" }}
:delay 2s
"""

    def _section_hotspot_html(self, r: Router) -> str:
        login_url = f"{self.active_url}/api/v1/network/provision/{r.auth_key}/hotspot/login.html"
        status_url = f"{self.active_url}/api/v1/network/provision/{r.auth_key}/hotspot/status.html"

        return f"""# ─────────────────────────────────────────────────────────────
# 11. HOTSPOT HTML PAGES (Cloud Portal Redirectors)
# ─────────────────────────────────────────────────────────────
:put "Downloading hotspot pages..."

:local dir "hotspot"
:do {{ :set dir [/ip hotspot profile get [find name="netily-profile"] html-directory] }} on-error={{}}
:if ($dir = "") do={{ :set dir "hotspot" }}

:do {{ /tool fetch url="{login_url}" dst-path=($dir . "/login.html") check-certificate=no }} on-error={{ :put "ERROR: login.html failed" }}
:do {{ /tool fetch url="{status_url}" dst-path=($dir . "/status.html") check-certificate=no }} on-error={{ :put "ERROR: status.html failed" }}
"""

    def _section_pppoe(self, r: Router, pppoe_local: str) -> str:
        return f"""# ─────────────────────────────────────────────────────────────
# 12. PPPoE SERVER (Bridge Mode)
# ─────────────────────────────────────────────────────────────
:put "Configuring PPPoE Server..."

:do {{ /ip pool remove [find name="netily-pppoe-pool"] }} on-error={{}}
/ip pool add name="netily-pppoe-pool" ranges="{r.pppoe_pool}"

:do {{ /ppp profile remove [find name="netily-pppoe-profile"] }} on-error={{}}
/ppp profile add name="netily-pppoe-profile" local-address={pppoe_local} remote-address=netily-pppoe-pool dns-server=8.8.8.8,1.1.1.1 use-encryption=no change-tcp-mss=yes only-one=default

:do {{ /interface pppoe-server server remove [find service-name="netily-pppoe"] }} on-error={{}}
/interface pppoe-server server add service-name="netily-pppoe" interface="netily-bridge" default-profile="netily-pppoe-profile" authentication=pap,chap disabled=no

/ppp aaa set use-radius=yes accounting=yes interim-update=00:03:00
:put " + PPPoE RADIUS enabled with 3-minute interim updates"
"""

    def _section_anti_sharing(self, r: Router, is_v6: bool) -> str:
        # CRITICAL FIX: Always use "set:1" for new-ttl (works on both v6 and v7)
        ttl_action = "set:1"
        return f"""# ─────────────────────────────────────────────────────────────
# 13. SMART ANTI-SHARING
# ─────────────────────────────────────────────────────────────
:put "Configuring Anti-Sharing Rules..."

:do {{ /ip firewall mangle remove [find comment~"Netily"] }} on-error={{}}
:do {{ /ip firewall address-list remove [find list="allowed-ips"] }} on-error={{}}

/ip firewall address-list add list="allowed-ips" address={r.pppoe_pool} comment="Netily-PPPoE-VIPs"
/ip firewall mangle add chain=prerouting src-address-list=allowed-ips action=mark-connection new-connection-mark=allowed-con passthrough=yes comment="Netily-VIP-Mark"
/ip firewall mangle add chain=postrouting connection-mark=!allowed-con out-interface="netily-bridge" action=change-ttl new-ttl={ttl_action} passthrough=no comment="Netily-AntiShare-Enforce"
"""

    def _section_nat(self, r: Router) -> str:
        return f"""# ─────────────────────────────────────────────────────────────
# 14. MASQUERADE & NAT
# ─────────────────────────────────────────────────────────────
:put "Configuring NAT..."

:do {{ /ip firewall nat remove [find comment="Netily-Masquerade"] }} on-error={{}}
/ip firewall nat add chain=srcnat action=masquerade comment="Netily-Masquerade"
"""

    def _section_schedulers(self, r: Router) -> str:
        return ""

    def _section_footer(self, r: Router) -> str:
        return f"""# ═══════════════════════════════════════════════════════════════
# PROVISIONING COMPLETE
# ═══════════════════════════════════════════════════════════════
:delay 1s
:log info "Netily Cloud Controller v4.6 provisioning complete for {self._escape_ros_string(r.name)}"
:put ""
:put "════════════════════════════════════════════════════════"
:put " NETILY CLOUD CONTROLLER — SETUP COMPLETE"
:put " Router:  {self._escape_ros_string(r.name)}"
:put " VPN:     WireGuard (v7) / OpenVPN v6 fallback"
:put " RADIUS:  {self.vpn_gateway}:1812/1813"
:put " Portal:  {self.get_tenant_portal_url()}"
:put "════════════════════════════════════════════════════════"
"""

    def generate_login_html(self) -> str:
        r = self.router
        portal_base = self.get_tenant_portal_url().rstrip('/')
        tenant_name = self._escape_ros_string(r.tenant_subdomain or 'public')

        return f"""<!DOCTYPE html>
<!-- NETILY_LOGIN_HTML_VERSION={LOGIN_HTML_VERSION} -->
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta http-equiv="pragma" content="no-cache">
    <meta http-equiv="cache-control" content="no-cache, no-store, must-revalidate">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Connecting...</title>
    <link rel="dns-prefetch" href="{portal_base}">
    <link rel="preconnect" href="{portal_base}" crossorigin>
    <script>
    // Runs the instant this line is parsed — BEFORE any CSS/body parsing
    // and before DOMContentLoaded. This is the single biggest lever on
    // perceived speed: nothing below this tag blocks the redirect.
    (function() {{
        var mac = '$(mac)', ip = '$(ip)', identity = '$(identity)';
        var loginUrl = '$(link-login-only)', error = '$(error)';
        var params = new URLSearchParams(window.location.search);
        var inboundUser = params.get('username'), inboundPass = params.get('password');

        // Return trip after payment: skip the portal hop entirely.
        if (inboundUser && inboundPass) {{
            document.write(
                '<form id="lf" method="post" action="' + loginUrl + '">' +
                '<input type="hidden" name="username" value="' + inboundUser + '">' +
                '<input type="hidden" name="password" value="' + inboundPass + '">' +
                '<input type="hidden" name="dst" value="$(link-orig)"></form>' +
                '<script>document.getElementById("lf").submit()<' + '/script>'
            );
            return;
        }}

        var ua = navigator.userAgent.toLowerCase();
        var smartTV = /smart-?tv|webos|tizen|vidaa|hbbtv|roku|firetv|apple\\s?tv/i.test(ua) ? '1' : '0';
        var portalBase = '{portal_base}/hotspot/{r.id}';
        var qs = [
            'mac=' + encodeURIComponent(mac),
            'ip=' + encodeURIComponent(ip),
            'router=' + encodeURIComponent(identity),
            'login_url=' + encodeURIComponent(loginUrl),
            'error=' + encodeURIComponent(error),
            'tenant={tenant_name}',
            'smart_tv=' + smartTV,
        ].join('&');
        var redirectUrl = portalBase + '?' + qs;

        // Speculative same-origin fetch — warms the captive-portal payload
        // in sessionStorage while the browser is still navigating, so the
        // Next.js page renders from cache instead of fetching after hydration.
        try {{
            fetch('{portal_base}/api/v1/hotspot/captive-portal/?router={r.id}&tenant={tenant_name}', {{ credentials: 'omit' }})
                .then(function(res) {{ return res.ok ? res.json() : null; }})
                .then(function(data) {{
                    if (!data) return;
                    data._cachedAt = Date.now();
                    try {{ sessionStorage.setItem('portal_cache:{r.id}', JSON.stringify(data)); }} catch (e) {{}}
                }})
                .catch(function() {{}});
        }} catch (e) {{}}

        window.location.replace(redirectUrl);
    }})();
    </script>
</head>
<body>
    <p>Connecting to WiFi&hellip; <a href="#" onclick="window.location.reload();return false;">Click here</a> if not redirected.</p>
</body>
</html>"""

    def generate_status_html(self) -> str:
        r = self.router
        portal_base = self.get_tenant_portal_url().rstrip('/')
        tenant_name = self._escape_ros_string(r.tenant_subdomain or 'public')
        primary_color = "#2563eb"

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Connected — {self._escape_ros_string(r.name or 'WiFi')}</title>
    <style>
        *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif; background: linear-gradient(135deg, #ecfdf5 0%, #d1fae5 50%, #a7f3d0 100%); min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 1rem; }}
        .card {{ background: white; border-radius: 1.5rem; box-shadow: 0 25px 50px -12px rgba(0,0,0,0.15), 0 0 0 1px rgba(0,0,0,0.05); padding: 2.5rem 2rem; width: 100%; max-width: 380px; text-align: center; }}
        .check-circle {{ width: 72px; height: 72px; background: linear-gradient(135deg, #10b981, #059669); border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto 1.25rem; box-shadow: 0 8px 24px rgba(16,185,129,0.35); }}
        .check-circle svg {{ width: 36px; height: 36px; color: white; }}
        h1 {{ font-size: 1.5rem; font-weight: 700; color: #065f46; margin-bottom: 0.375rem; }}
        .network-name {{ font-size: 0.875rem; color: #6b7280; margin-bottom: 1.75rem; }}
        .stats-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; margin-bottom: 1.5rem; }}
        .stat {{ background: #f9fafb; border-radius: 0.875rem; padding: 0.875rem 0.75rem; text-align: center; }}
        .stat-label {{ font-size: 0.6875rem; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.25rem; font-weight: 600; }}
        .stat-value {{ font-size: 1rem; font-weight: 700; color: #111827; }}
        .btn-disconnect {{ width: 100%; padding: 0.875rem; background: #fee2e2; color: #b91c1c; border: none; border-radius: 0.875rem; font-size: 0.9375rem; font-weight: 600; cursor: pointer; text-decoration: none; display: block; margin-bottom: 0.75rem; transition: background 0.15s; }}
        .btn-disconnect:hover {{ background: #fecaca; }}
        .btn-account {{ width: 100%; padding: 0.875rem; background: {primary_color}; color: white; border: none; border-radius: 0.875rem; font-size: 0.9375rem; font-weight: 600; cursor: pointer; text-decoration: none; display: block; transition: opacity 0.15s; }}
        .btn-account:hover {{ opacity: 0.9; }}
        .live-dot {{ display: inline-block; width: 8px; height: 8px; background: #10b981; border-radius: 50%; margin-right: 6px; animation: blink 1.8s ease-in-out infinite; }}
        @keyframes blink {{ 0%,100%{{opacity:1}} 50%{{opacity:0.3}} }}
    </style>
</head>
<body>
    <div class="card">
        <div class="check-circle">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
        </div>
        <h1>You're Online!</h1>
        <p class="network-name"><span class="live-dot"></span>Connected to <strong>$(identity)</strong></p>
        <div class="stats-grid">
            <div class="stat"><div class="stat-label">Session Time</div><div class="stat-value">$(uptime)</div></div>
            <div class="stat"><div class="stat-label">Your IP</div><div class="stat-value" style="font-size:0.8125rem">$(ip)</div></div>
            <div class="stat"><div class="stat-label">Downloaded</div><div class="stat-value">$(bytes-in-nice)</div></div>
            <div class="stat"><div class="stat-label">Uploaded</div><div class="stat-value">$(bytes-out-nice)</div></div>
        </div>
        <a class="btn-disconnect" href="$(link-logout)">Disconnect</a>
        <a class="btn-account" href="{portal_base}/hotspot/{r.id}?mac=$(mac)&ip=$(ip)&tenant={tenant_name}">Manage My Account →</a>
    </div>
</body>
</html>"""

    def generate_full_script(self) -> str:
        return self.generate_config_script("7")

    def generate_one_liner(self) -> str:
        return self.get_magic_link()