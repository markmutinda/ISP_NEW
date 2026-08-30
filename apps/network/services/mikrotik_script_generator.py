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
LOGIN_HTML_VERSION = "5"  # bump this every time generate_login_html() changes meaningfully


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

    def _get_own_domains(self) -> list:
        """Helper to get all domains owned by Netily for DNS pre-warm and walled garden."""
        tenant_domain = urlparse(self.get_tenant_portal_url()).netloc
        api_domain = urlparse(self.api_url).netloc
        portal_domain = self.portal_url.split('://')[-1]
        domains = []
        for d in (tenant_domain, api_domain, portal_domain):
            if d and d not in domains:
                domains.append(d)
        return domains

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
            self._section_dns(r),                       # ← FAST DNS for router
            self._section_dhcp(r, gateway_ip, pool_range, dhcp_network),
            self._section_radius(r),
            self._section_hotspot(r, gateway_ip),
            self._section_walled_garden(r, portal_domain),
            self._section_dns_prewarm(r),               # ← DNS pre-warm scheduler
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

    def _section_dns(self, r: Router) -> str:
        """
        The router's OWN resolver — used for every walled-garden domain lookup
        AND every OS captive-portal-detection probe before a client is online.
        Left unset, this defaults to whatever slow/flaky DNS the WAN link
        handed out, which is the #1 cause of "sometimes extremely slow"
        unauthenticated portal loads.

        🔥 FIX 1: allow-remote-requests=yes so client devices can use the router
        as their DNS resolver, preventing 9.5s timeouts from walled-garden
        blocking direct 8.8.8.8 queries.
        """
        return f"""# ─────────────────────────────────────────────────────────────
# 5b. ROUTER DNS RESOLVER (fast + cached)
# ─────────────────────────────────────────────────────────────
:put "Configuring fast DNS resolver..."
/ip dns set servers=1.1.1.1,8.8.8.8 cache-size=4096KiB cache-max-ttl=1d allow-remote-requests=yes
:put " + Fast DNS set to 1.1.1.1, 8.8.8.8 with 4MB cache"
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
        # 🔥 FIX: Added html-directory="netily-hotspot" so RouterOS looks in our custom folder
        profile_cmd = (
            f'/ip hotspot profile add name="netily-profile" '
            f'hotspot-address="{gateway_ip}" login-by=http-pap,mac-cookie '
            f'use-radius=yes radius-accounting=yes http-cookie-lifetime=1d '
            f'html-directory="netily-hotspot"'
        )
        server_cmd = f'/ip hotspot add name="netily-hotspot" interface="netily-bridge" address-pool="netily-pool" profile="netily-profile" disabled=no'

        return f"""# ─────────────────────────────────────────────────────────────
# 8. HOTSPOT PROFILE & SERVER (Bridge Mode)
# ─────────────────────────────────────────────────────────────
:put "Configuring Hotspot..."
:do {{ /file remove [find name="netily-hotspot" type="directory"] }} on-error={{}}
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
        own_domains = self._get_own_domains()

        # Build the address-list entries
        addr_list_lines = []
        for domain in own_domains:
            addr_list_lines.append(
                f'/ip firewall address-list add list="netily-portal-ips" '
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

    def _section_dns_prewarm(self, r: Router) -> str:
        """
        DNS PRE-WARM SCHEDULER — Keeps walled-garden portal/API IPs resolved
        and cached continuously so the FIRST unauthenticated device of the day
        (or after a CDN IP rotation) never pays a cold-DNS penalty.
        """
        domains = self._get_own_domains()
        resolve_lines = "\n".join(f':do {{ :resolve "{d}" }} on-error={{}}' for d in domains)
        
        return f"""# ─────────────────────────────────────────────────────────────
# 9b. DNS PRE-WARM SCHEDULER
# Keeps walled-garden portal/API IPs resolved & cached continuously so
# the FIRST unauthenticated device of the day (or after a CDN IP
# rotation) never pays a cold-DNS penalty inside the walled garden.
# ─────────────────────────────────────────────────────────────
:put "Installing DNS pre-warm scheduler..."
:do {{ /system script remove [find name="netily-dns-warm"] }} on-error={{}}
/system script add name="netily-dns-warm" comment="Netily-DNS-Prewarm" source={{
{resolve_lines}
}}
:do {{ /system scheduler remove [find name="netily-dns-warm"] }} on-error={{}}
/system scheduler add name="netily-dns-warm" interval=30s comment="Netily-DNS-Prewarm" on-event="/system script run netily-dns-warm"
:do {{ /system script run netily-dns-warm }} on-error={{ :put " + DNS pre-warm script ran successfully" }}
:put " + DNS pre-warm scheduler installed (runs every 30s)"
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
        # 🔥 FIX: Fetch ALL 5 required files into netily-hotspot/ folder
        base = f"{self.active_url}/api/v1/network/provision/{r.auth_key}/hotspot"
        pages = ["login.html", "rlogin.html", "alogin.html", "redirect.html", "error.html", "status.html"]

        fetch_lines = "\n".join(
            f':do {{ /tool fetch url="{base}/{p}" dst-path=("netily-hotspot/" . "{p}") check-certificate=no }} '
            f'on-error={{ :put "ERROR: {p} failed" }}'
            for p in pages
        )

        return f"""# ─────────────────────────────────────────────────────────────
# 11. HOTSPOT HTML PAGES (Cloud Portal Redirectors)
# ─────────────────────────────────────────────────────────────
:put "Downloading hotspot pages into netily-hotspot/..."
:do {{ /file add name="netily-hotspot" type="directory" }} on-error={{}}
{fetch_lines}
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
        """
        🔥 FIX 2: Add force-DNS redirect rules so client DNS queries (UDP/TCP 53)
        are intercepted and answered by the router, preventing the 9.5s timeout
        from walled-garden blocking direct 8.8.8.8 queries.
        """
        return f"""# ─────────────────────────────────────────────────────────────
# 14. MASQUERADE & NAT + FORCE DNS
# ─────────────────────────────────────────────────────────────
:put "Configuring NAT..."

:do {{ /ip firewall nat remove [find comment="Netily-Masquerade"] }} on-error={{}}
/ip firewall nat add chain=srcnat action=masquerade comment="Netily-Masquerade"

# 🔥 FIX 2: Force client DNS through the router
# Without these rules, clients try 8.8.8.8 directly, which the walled garden# blocks, causing ~9.5s timeouts before the OS falls back to the router.
:do {{ /ip firewall nat remove [find comment="Netily-Force-DNS"] }} on-error={{}}
:do {{ /ip firewall nat remove [find comment="Netily-Force-DNS-TCP"] }} on-error={{}}
/ip firewall nat add chain=dstnat action=redirect to-ports=53 protocol=udp dst-port=53 in-interface="netily-bridge" comment="Netily-Force-DNS"
/ip firewall nat add chain=dstnat action=redirect to-ports=53 protocol=tcp dst-port=53 in-interface="netily-bridge" comment="Netily-Force-DNS-TCP"
:put " + Force-DNS rules added (UDP/TCP port 53 → router)"
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
        primary_color = "#2563eb"

        # 🔥 VERSION STAMP — embedded as a comment so the diagnostic engine
        # can detect stale login.html without parsing the JS.
        return f"""<!DOCTYPE html>
<!-- NETILY_LOGIN_HTML_VERSION={LOGIN_HTML_VERSION} -->
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta http-equiv="pragma" content="no-cache">
    <meta http-equiv="cache-control" content="no-cache, no-store, must-revalidate">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Connecting...</title>
    <!-- 🔥 OPTIMIZATION: Preconnect to portal domain only — same-origin as redirect target -->
    <link rel="dns-prefetch" href="{portal_base}">
    <link rel="preconnect" href="{portal_base}" crossorigin>
    <!-- 🔥 FIX 4: rel=prefetch stores in the shared HTTP cache, which — unlike
         sessionStorage — is NOT partitioned to this page's own origin, so
         it is actually usable once we navigate to portal_base. This directly
         replaces the old sessionStorage-based "prewarm", which was silently
         non-functional: sessionStorage set on the router's hotspot origin
         is invisible to JS running on portal_base's origin. -->
    <link rel="prefetch" href="{portal_base}/hotspot/{r.id}" as="document">
    <style>
        *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif; background: linear-gradient(135deg, #eff6ff 0%, #dbeafe 50%, #e0e7ff 100%); min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 1rem; }}
        .card {{ background: white; border-radius: 1.5rem; box-shadow: 0 25px 50px -12px rgba(0,0,0,0.15), 0 0 0 1px rgba(0,0,0,0.05); padding: 2.5rem 2rem; width: 100%; max-width: 380px; text-align: center; }}
        .wifi-icon {{ width: 64px; height: 64px; background: linear-gradient(135deg, {primary_color}, #7c3aed); border-radius: 1rem; display: flex; align-items: center; justify-content: center; margin: 0 auto 1.5rem; }}
        .wifi-icon svg {{ width: 36px; height: 36px; color: white; }}
        h1 {{ font-size: 1.375rem; font-weight: 700; color: #111827; margin-bottom: 0.5rem; }}
        .subtitle {{ font-size: 0.875rem; color: #6b7280; margin-bottom: 2rem; line-height: 1.5; }}
        .progress-track {{ background: #f3f4f6; border-radius: 999px; height: 6px; overflow: hidden; margin-bottom: 1rem; }}
        .progress-bar {{ height: 100%; background: linear-gradient(90deg, {primary_color}, #7c3aed); border-radius: 999px; animation: progress 1.8s ease-in-out forwards; width: 0%; }}
        @keyframes progress {{ 0% {{ width: 0%; }} 30% {{ width: 45%; }} 70% {{ width: 78%; }} 100% {{ width: 95%; }} }}
        .status-row {{ display: flex; align-items: center; gap: 0.625rem; padding: 0.75rem 1rem; background: #f9fafb; border-radius: 0.75rem; margin-bottom: 0.5rem; }}
        .dot {{ width: 8px; height: 8px; border-radius: 50%; background: {primary_color}; animation: pulse 1.5s ease-in-out infinite; flex-shrink: 0; }}
        @keyframes pulse {{ 0%, 100% {{ opacity: 1; transform: scale(1); }} 50% {{ opacity: 0.5; transform: scale(0.85); }} }}
        .status-text {{ font-size: 0.8125rem; color: #374151; font-weight: 500; }}
        .link-row {{ margin-top: 1.5rem; font-size: 0.75rem; color: #9ca3af; }}
        .link-row a {{ color: {primary_color}; text-decoration: none; font-weight: 500; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="wifi-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M5 12.55a11 11 0 0 1 14.08 0"/>
                <path d="M1.42 9a16 16 0 0 1 21.16 0"/>
                <path d="M8.53 16.11a6 6 0 0 1 6.95 0"/>
                <circle cx="12" cy="20" r="1" fill="currentColor"/>
            </svg>
        </div>
        <h1 id="main-title">Connecting you...</h1>
        <p class="subtitle" id="sub-title">Redirecting to the WiFi portal</p>
        <div class="progress-track"><div class="progress-bar"></div></div>
        <div class="status-row"><div class="dot"></div><span class="status-text" id="status-msg">Detecting your device...</span></div>
        <div class="link-row">Not redirected? <a id="manual-link" href="#">Click here</a></div>
    </div>
    <form id="login-form" action="$(link-login-only)" method="post" style="display:none">
        <input type="hidden" name="username" id="usr">
        <input type="hidden" name="password" id="pwd">
        <input type="hidden" name="dst" value="$(link-orig)">
    </form>
    <script>
    (function() {{
        var mac = '$(mac)', ip = '$(ip)', identity = '$(identity)', loginUrl = '$(link-login-only)', error = '$(error)';
        var statusEl = document.getElementById('status-msg'), titleEl = document.getElementById('main-title'), subEl = document.getElementById('sub-title'), linkEl = document.getElementById('manual-link');
        var urlParams = new URLSearchParams(window.location.search);
        var inboundUser = urlParams.get('username'), inboundPass = urlParams.get('password');
        if (inboundUser && inboundPass) {{
            titleEl.textContent = 'Authenticating...'; subEl.textContent = 'Finalizing your connection'; statusEl.textContent = 'Logging you in now';
            document.getElementById('usr').value = inboundUser; document.getElementById('pwd').value = inboundPass;
            document.getElementById('login-form').submit(); return;
        }}
        var ua = navigator.userAgent.toLowerCase(), q = new URLSearchParams(window.location.search), forcedTV = null;
        if (q.get('force_tv') === '1' || q.get('smart_tv') === '1') forcedTV = true;
        else if (q.get('force_tv') === '0' || q.get('smart_tv') === '0') forcedTV = false;
        var tvByUA = /(smart-?tv|webos|tizen|vidaa|hbbtv|roku|firetv|appletv|apple\\s?tv|bravia|netcast|viera|aft[a-z]|crkey|tv safari)/i.test(ua);
        var isAndroidTV = /android/i.test(ua) && !/mobile/i.test(ua) && window.screen.width >= 1280;
        var isDesktopOS = /(windows nt|macintosh|\\bx11\\b|linux x86_64|cros)/i.test(ua);
        var geoTV = !isDesktopOS && window.screen.width >= 1280 && window.screen.height >= 720 && (window.screen.width / window.screen.height) >= 1.5 && !('ontouchstart' in window) && navigator.maxTouchPoints === 0;
        var finalIsTV = forcedTV !== null ? forcedTV : (tvByUA || isAndroidTV || geoTV);
        statusEl.textContent = finalIsTV ? 'Smart TV detected — opening pairing screen' : 'Redirecting to portal...';
        var portalBase = '{portal_base}/hotspot/{r.id}';
        var params = ['mac=' + encodeURIComponent(mac), 'ip=' + encodeURIComponent(ip), 'router=' + encodeURIComponent(identity), 'login_url=' + encodeURIComponent(loginUrl), 'error=' + encodeURIComponent(error), 'tenant=' + '{tenant_name}', 'smart_tv=' + (finalIsTV ? '1' : '0')];
        var redirectUrl = portalBase + '?' + params.join('&');
        linkEl.href = redirectUrl;

        // Navigate on the next frame — preconnect links in <head> warm DNS/TCP/TLS in parallel
        requestAnimationFrame(function() {{
            requestAnimationFrame(function() {{
                window.location.replace(redirectUrl);
            }});
        }});

        // 🔥 FIX 4: REMOVED the old speculative fetch() + sessionStorage.setItem(...) block.
        // It could never be read back — sessionStorage is same-origin only, and
        // this page's origin is never the same as portal_base's. It was a pure
        // cost: one extra HTTP request on an already bandwidth-limited captive
        // network, with zero payoff. The <link rel="prefetch"> in the <head> above
        // is the correct replacement.
    }})();
    </script>
</body>
</html>"""

    def generate_rlogin_html(self) -> str:
        """Shown on intercepted/redirected pre-auth requests — same portal
        redirect as login.html, but a lightweight loading page (no TV
        detection needed, RouterOS already knows this is a redirect)."""
        r = self.router
        portal_base = self.get_tenant_portal_url().rstrip('/')
        tenant_name = self._escape_ros_string(r.tenant_subdomain or 'public')
        primary_color = "#2563eb"

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta http-equiv="pragma" content="no-cache">
    <meta http-equiv="cache-control" content="no-cache, no-store, must-revalidate">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="2; url={portal_base}/hotspot/{r.id}">
    <title>Connecting...</title>
    <link rel="dns-prefetch" href="{portal_base}">
    <link rel="preconnect" href="{portal_base}" crossorigin>
    <style>
        *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: linear-gradient(135deg, #eff6ff 0%, #dbeafe 50%, #e0e7ff 100%); min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 1rem; }}
        .card {{ background: white; border-radius: 1.5rem; box-shadow: 0 25px 50px -12px rgba(0,0,0,0.15); padding: 2.25rem 2rem; width: 100%; max-width: 340px; text-align: center; }}
        .spinner {{ width: 40px; height: 40px; border: 3px solid #e5e7eb; border-top-color: {primary_color}; border-radius: 50%; margin: 0 auto 1.25rem; animation: spin 0.8s linear infinite; }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
        h1 {{ font-size: 1.125rem; font-weight: 700; color: #111827; margin-bottom: 0.375rem; }}
        p {{ font-size: 0.8125rem; color: #6b7280; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="spinner"></div>
        <h1>Connecting you...</h1>
        <p>Redirecting to the WiFi portal</p>
    </div>
    <script>
    (function() {{
        var mac = '$(mac)', ip = '$(ip)', identity = '$(identity)', loginUrl = '$(link-login-only)', error = '$(error)';
        var params = ['mac=' + encodeURIComponent(mac), 'ip=' + encodeURIComponent(ip), 'router=' + encodeURIComponent(identity), 'login_url=' + encodeURIComponent(loginUrl), 'error=' + encodeURIComponent(error), 'tenant=' + '{tenant_name}'];
        var redirectUrl = '{portal_base}/hotspot/{r.id}?' + params.join('&');
        try {{
            var inbound = new URLSearchParams(window.location.search);
            if (inbound.get('username') && inbound.get('password')) return; // let login.html handle it
        }} catch (e) {{}}
        window.location.replace(redirectUrl);
    }})();
    </script>
</body>
</html>"""

    def generate_alogin_html(self) -> str:
        """Shown briefly after a successful auto-login (MAC-cookie / trial)."""
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="pragma" content="no-cache">
    <meta http-equiv="expires" content="-1">
    <meta http-equiv="refresh" content="1; url=$(link-redirect)">
    <title>Connected</title>
    <style>
        body {{ margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center; background: #fafafa; color: #52525b; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 13.5px; }}
    </style>
</head>
<body>
<p>You're connected — taking you online…</p>
<script>
(function () {{
    var redirect = '$(link-redirect)';
    if (!redirect || !/^https?:\\/\\//i.test(redirect) || redirect.indexOf('hot.spot') !== -1) {{
        redirect = 'https://www.google.com';
    }}
    window.location.replace(redirect);
}})();
</script>
</body>
</html>"""

    def generate_redirect_html(self) -> str:
        """RouterOS-mandated redirect shim. The two $(if ...) header lines
        MUST stay exactly as-is and MUST be the first two lines — RouterOS
        parses them to build the raw HTTP 302 response before any HTML."""
        return """$(if http-status == 302)Hotspot redirect$(endif)
$(if http-header == "Location")$(link-redirect)$(endif)
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="0; url=$(link-redirect)">
    <meta http-equiv="pragma" content="no-cache">
    <meta http-equiv="expires" content="-1">
    <title>Redirecting</title>
</head>
<body></body>
</html>"""

    def generate_error_html(self) -> str:
        r = self.router
        portal_base = self.get_tenant_portal_url().rstrip('/')
        tenant_name = self._escape_ros_string(r.tenant_subdomain or 'public')

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="3; url={portal_base}/hotspot/{r.id}?tenant={tenant_name}&mikrotik_error=$(error)">
    <title>Sign-in problem</title>
    <style>
        *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #fafafa; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 1rem; }}
        .card {{ background: white; border: 1px solid #e4e4e7; border-radius: 1.125rem; padding: 2rem 1.5rem; max-width: 340px; width: 100%; text-align: center; }}
        .badge {{ width: 44px; height: 44px; margin: 0 auto 1.125rem; border-radius: 999px; background: #fef2f2; color: #dc2626; display: flex; align-items: center; justify-content: center; font-size: 22px; font-weight: 700; }}
        h1 {{ font-size: 1.1875rem; font-weight: 600; color: #18181b; margin-bottom: 0.375rem; }}
        p {{ font-size: 0.84375rem; color: #52525b; margin-bottom: 1.125rem; }}
        a {{ display: block; padding: 0.75rem 1.125rem; border-radius: 0.625rem; background: #18181b; color: #fff; font-weight: 600; font-size: 0.875rem; text-decoration: none; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="badge">!</div>
        <h1>We couldn't sign you in</h1>
        <p id="reason">$(error)</p>
        <a id="retry" href="{portal_base}/hotspot/{r.id}?tenant={tenant_name}&mikrotik_error=$(error)">Back to packages</a>
    </div>
    <script>
    (function() {{
        var el = document.getElementById('reason');
        if (el && (!el.textContent.trim() || el.textContent.indexOf('$(') === 0)) {{
            el.textContent = 'Your session could not be started.';
        }}
        var url = '{portal_base}/hotspot/{r.id}?mac=' + encodeURIComponent('$(mac)') + '&ip=' + encodeURIComponent('$(ip)') + '&tenant={tenant_name}';
        var retry = document.getElementById('retry');
        if (retry) retry.href = url;
        setTimeout(function() {{ window.location.replace(url); }}, 3000);
    }})();
    </script>
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