"""
Lipanet-Style Cloud Controller Script Generator (v4.6)
Hardcoded 3-minute interim updates for automatic accounting
"""

from django.conf import settings
from apps.network.models.router_models import Router
from django.utils import timezone
from django_tenants.utils import schema_context
from urllib.parse import urlparse


class MikrotikScriptGenerator:
    def __init__(self, router: Router, request=None):
        self.router = router
        self.request = request

        # ── VPN Gateway Logic ─────────────────────────────────────────
        self.vpn_gateway = getattr(settings, 'VPN_GATEWAY_IP', '10.8.0.1')
        
        # ── FIX 1A: Dedicated RADIUS container IP ─────────────────────
        self.radius_server_ip = getattr(settings, 'RADIUS_SERVER_IP', '10.10.0.3')
        
        # ── Production URLs Logic ─────────────────────────────────────
        self.base_url = getattr(settings, 'BASE_URL', 'https://api.netily.co.ke').rstrip('/')
        self.portal_url = getattr(settings, 'FRONTEND_URL', 'https://netily.co.ke').rstrip('/')
        self.active_url = self.base_url
        
        # ── Provisioning download base ────────────────────────────
        self.provision_base = f"{self.active_url}/api/v1/network/provision"
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
            parsed_url = urlparse(self.base_url)
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
        if getattr(self, 'request', None):
            absolute_api_path = self.request.build_absolute_uri(api_path)
        else:
            absolute_api_path = f"{self.base_url}{api_path}"
        base_api_url = absolute_api_path.split('?')[0]

        return f"""# ═══════════════════════════════════════════════════════════════
# Netily Cloud Controller — Base Script (Stage 1)
# Router: {self._escape_ros_string(r.name)}
# Generated: {timezone.now().strftime("%Y-%m-%d %H:%M:%S")}
# ═══════════════════════════════════════════════════════════════
:put ">>> NETILY CLOUD CONTROLLER v4.6 <<<"
:put ">>> Stage 1: Detecting RouterOS version..."

# ─── Check Internet Connectivity ────────────────────────────
:local hasInternet false
:do {{
    /tool dns-query name="dns.google" type=A
    :set hasInternet true
}} on-error={{}}
:if ($hasInternet = false) do={{
    :do {{
        /ping 8.8.8.8 count=2
        :set hasInternet true
    }} on-error={{}}
}}

:if ($hasInternet = false) do={{
    :put "ERROR: No internet detected! Connect WAN first."
    :error "No internet connectivity"
}}
:put "Internet: OK"

# ─── Detect RouterOS Version ────────────────────────────────
:local rosVersion "7"
:local verStr [/system resource get version]
if ([:pick $verStr 0 1] = "6") do={{
    :set rosVersion "6"
}}
:put ("RouterOS version detected: v" . $rosVersion)

# ─── Download Version-Specific Configuration ────────────────
:local configUrl ("{base_api_url}?version=" . $rosVersion . "&router={r.id}&subdomain={subdomain}")
:put ("Downloading config from: " . $configUrl)

:do {{
    /tool fetch url=$configUrl dst-path="netily_conf.rsc" http-header-field="ngrok-skip-browser-warning: true"
    :delay 2s
    :put "Config downloaded. Importing..."
    /import netily_conf.rsc
    :put ">>> Stage 2 complete. Router configured."
}} on-error={{
    :put "ERROR: Configuration download or import failed!"
    :put "URL: $configUrl"
    :put "Check that the config file is valid and the router has internet access."
    :put "To debug, run: /import netily_conf.rsc"
    :error "Provisioning failed"
}}
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
            self._section_radius(r),  # UPDATED with src-address and route
            self._section_hotspot(r, gateway_ip),
            self._section_walled_garden(r, portal_domain),
            self._section_ssl_certs(r),
            self._section_hotspot_html(r),
            self._section_pppoe(r, pppoe_local) if r.enable_pppoe else "",  # UPDATED with anchored local-address
            self._section_anti_sharing(r, is_v6),
            self._section_nat(r),  # UPDATED with RADIUS bypass
            self._section_schedulers(r),
            self._section_footer(r),
        ]
        return "\n".join(s for s in sections if s)

    def _section_header(self, r: Router, version: str) -> str:
        return f"""# ═══════════════════════════════════════════════════════════════
# Netily Cloud Controller — Configuration Script v4.6
# Router: {self._escape_ros_string(r.name)}
# Tenant: {self._escape_ros_string(r.tenant_subdomain or 'public')}
# VPN IP: {r.vpn_ip_address or 'auto-assigned'}
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
:do {{ :foreach i in=[/ppp profile find name="netily-pppoe-profile"] do={{ /ppp profile remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/interface pppoe-server server find name="netily-pppoe"] do={{ /interface pppoe-server server remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/interface bridge find name="netily-bridge"] do={{
    /interface bridge port remove [find bridge="netily-bridge"]
    /ip address remove [find interface="netily-bridge"]
    /interface bridge remove $i
}} }} on-error={{}}
:do {{ /ip firewall filter remove [find comment~"Netily"] }} on-error={{}}
:do {{ /ip firewall nat remove [find comment~"Netily"] }} on-error={{}}
:do {{ /ip firewall mangle remove [find comment~"Netily"] }} on-error={{}}

:put "Cleanup complete."
"""

    def _section_api_user(self, r: Router) -> str:
        password = self._escape_ros_string(r.api_password)
        return f"""# ─────────────────────────────────────────────────────────────
# 2. API USER (Cloud Management Access)
# ─────────────────────────────────────────────────────────────
:put "Configuring API user..."

:if ([:len [/user find name="{self._escape_ros_string(r.api_username)}"]] = 0) do={{
    /user add name="{self._escape_ros_string(r.api_username)}" group=full password="{password}" comment="Netily Cloud API"
}} else={{
    /user set [find name="{self._escape_ros_string(r.api_username)}"] password="{password}" group=full
}}

# Allow access from the VPN Tunnel and the local Docker network
/ip service set api disabled=no port={r.api_port} address={self.vpn_gateway}/32,127.0.0.0/8,172.18.0.0/16
/ip service set api-ssl disabled=yes
"""

    def _section_openvpn(self, r: Router, cipher: str, auth: str, is_v6: bool) -> str:
        vpn_host = self._get_vpn_host(r)

        ca_fetch = ""
        if r.ca_certificate:
            ca_url = f"{self.base_url}/api/v1/network/provision/{r.auth_key}/certs/ca.crt"
            ca_fetch = f"""
:do {{
    /tool fetch url="{ca_url}" dst-path="netily-vpn-ca.crt" http-header-field="ngrok-skip-browser-warning: true"
    :delay 1s
    /certificate import file-name="netily-vpn-ca.crt" passphrase=""
    :put "VPN CA certificate imported."
}} on-error={{
    :put "Note: VPN CA cert not available. Using unverified TLS."
}}
"""
        
        if is_v6:
            ovpn_cmd = f'/interface ovpn-client add name="Netily-VPN" connect-to="{self._escape_ros_string(vpn_host)}" port={r.openvpn_port} user="{self._escape_ros_string(r.openvpn_username)}" password="{self._escape_ros_string(r.openvpn_password)}" cipher={cipher} auth={auth} protocol=udp add-default-route=no comment="Netily Cloud Controller Tunnel"'
        else:
            ovpn_cmd = f'/interface ovpn-client add name="Netily-VPN" connect-to="{self._escape_ros_string(vpn_host)}" port={r.openvpn_port} user="{self._escape_ros_string(r.openvpn_username)}" password="{self._escape_ros_string(r.openvpn_password)}" cipher=aes256-cbc auth=sha1 protocol=udp add-default-route=no comment="Netily Cloud Controller Tunnel"'

        return f"""# ─────────────────────────────────────────────────────────────
# 3. OPENVPN TUNNEL (Username/Password Authentication)
# ─────────────────────────────────────────────────────────────
:put "Establishing Cloud VPN tunnel..."
{ca_fetch}
{ovpn_cmd}

:put "Waiting for VPN tunnel..."
:delay 8s

:local vpnRunning false
:do {{
    :if ([/interface ovpn-client get [find name="Netily-VPN"] running] = true) do={{
        :set vpnRunning true
    }}
}} on-error={{}}

:if ($vpnRunning = true) do={{
    :put "VPN tunnel established successfully!"
}} else={{
    :put "WARNING: VPN tunnel not yet connected. It may take a moment."
    :put "The router will keep trying to connect in the background."
}}
"""

    def _section_firewall(self, r: Router) -> str:
        return f"""# ─────────────────────────────────────────────────────────────
# 4. FIREWALL (VPN & Management)
# ─────────────────────────────────────────────────────────────
:put "Configuring firewall rules..."

/ip firewall filter add chain=input action=accept src-address=10.8.0.0/24 comment="Netily-VPN-Input-Allow"
/ip firewall filter add chain=forward action=accept src-address=10.8.0.0/24 comment="Netily-VPN-Forward-Allow"
/ip firewall filter add chain=forward action=accept dst-address=10.8.0.0/24 comment="Netily-VPN-Forward-Return"
/ip firewall filter add chain=input action=accept connection-state=established,related comment="Netily-Established"
"""

    def _section_bridge_ports(self, r: Router, gateway_cidr: str) -> str:
        port_cmds = []
        ports = r.hotspot_interfaces or []
        
        for port in ports:
            safe_port = port.strip()
            if not safe_port: continue
            
            port_cmds.append(f"""
:do {{ /interface bridge port remove [find interface="{safe_port}"] }} on-error={{}}
:do {{ 
    /interface bridge port add bridge="netily-bridge" interface="{safe_port}"
    :put " + Added {safe_port} to netily-bridge"
}} on-error={{ :put " ! Error adding {safe_port} (check hardware)" }}
""")

        ports_script = "\n".join(port_cmds)

        return f"""# ─────────────────────────────────────────────────────────────
# 5. SUPER BRIDGE & PORTS
# ─────────────────────────────────────────────────────────────
:put "Configuring Bridge Topology..."

# 1. Create the single master bridge
:if ([:len [/interface bridge find name="netily-bridge"]] = 0) do={{
    /interface bridge add name="netily-bridge" comment="Netily Hotspot & PPPoE"
}}

# 2. Assign Gateway IP to the bridge (using calculated CIDR)
:do {{ /ip address remove [find interface="netily-bridge"] }} on-error={{}}
/ip address add address="{gateway_cidr}" interface="netily-bridge" comment="Netily Gateway"

# 3. Add Ports
{ports_script}
"""

    def _section_dhcp(self, r: Router, gateway_ip: str, pool_range: str, dhcp_network: str) -> str:
        return f"""# ─────────────────────────────────────────────────────────────
# 6. IP POOL & DHCP (Bridge Mode)
# ─────────────────────────────────────────────────────────────
:put "Configuring DHCP..."

# --- FIXED: Remove conflicting network if it exists (even if not marked 'Netily') ---
:do {{ /ip dhcp-server network remove [find address="{dhcp_network}"] }} on-error={{}}
# -----------------------------------------------------------------------------------

/ip pool add name="netily-pool" ranges="{pool_range}"
/ip dhcp-server add name="netily-dhcp" interface="netily-bridge" address-pool="netily-pool" lease-time=1h disabled=no
/ip dhcp-server network add address="{dhcp_network}" gateway="{gateway_ip}" dns-server=8.8.8.8,1.1.1.1 comment="Netily DHCP Network"
"""

    # ============================================================
    # FIX 1B: UPDATED RADIUS with src-address and forced route
    # ============================================================
    def _section_radius(self, r: Router) -> str:
        """UPDATED: RADIUS with src-address from VPN IP and forced route via VPN tunnel"""
        radius_src = r.vpn_ip_address or "0.0.0.0"
        radius_cmd = (
            f'/radius add address={self.radius_server_ip} '
            f'src-address={radius_src} '
            f'secret="{self._escape_ros_string(r.shared_secret)}" '
            f'authentication-port=1812 accounting-port=1813 '
            f'service=hotspot,ppp timeout=3000ms comment="Netily-Cloud-RADIUS"'
        )

        return f"""# ─────────────────────────────────────────────────────────────
# 7. RADIUS (Cloud RADIUS via VPN Tunnel)
# ─────────────────────────────────────────────────────────────
:put "Configuring Cloud RADIUS..."
:do {{ /radius remove [find comment~"Netily"] }} on-error={{}}
{radius_cmd}

/radius incoming set accept=yes port=3799

# Force RADIUS host through VPN tunnel
:do {{ /ip route remove [find comment="Netily-RADIUS-Route"] }} on-error={{}}
/ip route add dst-address={self.radius_server_ip}/32 gateway="Netily-VPN" comment="Netily-RADIUS-Route"

# Allow return RADIUS packets from server
:do {{ /ip firewall filter remove [find comment="Netily-RADIUS-Input"] }} on-error={{}}
/ip firewall filter add chain=input src-address={self.radius_server_ip} action=accept place-before=0 comment="Netily-RADIUS-Input"
"""

    def _section_hotspot(self, r: Router, gateway_ip: str) -> str:
        profile_cmd = f'/ip hotspot profile add name="netily-profile" hotspot-address="{gateway_ip}" dns-name="{self._escape_ros_string(r.dns_name)}" login-by=http-pap,mac-cookie use-radius=yes radius-accounting=yes http-cookie-lifetime=1d rate-limit=""'
        server_cmd = f'/ip hotspot add name="netily-hotspot" interface="netily-bridge" address-pool="netily-pool" profile="netily-profile" disabled=no'
        
        return f"""# ─────────────────────────────────────────────────────────────
# 8. HOTSPOT PROFILE & SERVER (Bridge Mode)
# ─────────────────────────────────────────────────────────────
:put "Configuring Hotspot..."
{profile_cmd}
{server_cmd}

# Anti-sharing: one device per account, quick disconnect on idle
/ip hotspot user profile set [find name="default"] shared-users=1 keepalive-timeout=2m

# Enable RADIUS accounting with 3-minute interim updates
/ip hotspot profile set [find name="netily-profile"] use-radius=yes radius-accounting=yes radius-interim-update=00:03:00
:put " + RADIUS accounting enabled with 3-minute interim updates"
"""

    def _section_walled_garden(self, r: Router, portal_domain: str) -> str:
        tenant_domain = urlparse(self.get_tenant_portal_url()).netloc
        
        return f"""# ─────────────────────────────────────────────────────────────
# 9. WALLED GARDEN (Pre-Auth Access)
# ─────────────────────────────────────────────────────────────
:put "Configuring Walled Garden..."

:do {{ :foreach i in=[/ip hotspot walled-garden find comment~"Netily"] do={{ /ip hotspot walled-garden remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/ip hotspot walled-garden ip find comment~"Netily"] do={{ /ip hotspot walled-garden ip remove $i }} }} on-error={{}}

# Allow the specific tenant portal
/ip hotspot walled-garden add dst-host="*{tenant_domain}*" comment="Netily-Tenant-Portal"
/ip hotspot walled-garden add dst-host="*netily.co.ke*" comment="Netily-Backend-Core"

# Allow Payment Gateways & APIs
/ip hotspot walled-garden add dst-host="*.safaricom.co.ke" comment="Netily-MPesa"
/ip hotspot walled-garden add dst-host="*.safaricom.com" comment="Netily-Safaricom"
/ip hotspot walled-garden add dst-host="*.payhero.co.ke" comment="Netily-PayHero"

# Allow VPN / API ranges
/ip hotspot walled-garden ip add dst-address={self.vpn_gateway}/32 action=accept comment="Netily-VPN-API"
/ip hotspot walled-garden ip add dst-address=10.8.0.0/24 action=accept comment="Netily-VPN-Network"
"""

    def _section_ssl_certs(self, r: Router) -> str:
        if not r.ssl_certificate:
            return f"""# ─────────────────────────────────────────────────────────────
# 10. SSL CERTIFICATES (Hotspot HTTPS)
# ─────────────────────────────────────────────────────────────
:put "No SSL certificates configured — hotspot will use HTTP only."
"""
        ssl_cert_url = f"{self.base_url}/api/v1/network/provision/{r.auth_key}/certs/ssl.crt"
        ssl_key_url = f"{self.base_url}/api/v1/network/provision/{r.auth_key}/certs/ssl.key"
        passphrase = self._escape_ros_string(r.ssl_passphrase or '')

        return f"""# ─────────────────────────────────────────────────────────────
# 10. SSL CERTIFICATES (Hotspot HTTPS)
# ─────────────────────────────────────────────────────────────
:put "Downloading SSL certificates..."

:do {{ /certificate remove [find name~"netily-ssl"] }} on-error={{}}

:do {{
    /tool fetch url="{ssl_cert_url}" dst-path="netily-ssl.crt" http-header-field="ngrok-skip-browser-warning: true"
    :delay 1s
    /certificate import file-name="netily-ssl.crt" passphrase="{passphrase}"
    :put "SSL certificate imported."
}} on-error={{
    :put "WARNING: Could not download SSL certificate."
}}

:do {{
    /tool fetch url="{ssl_key_url}" dst-path="netily-ssl.key" http-header-field="ngrok-skip-browser-warning: true"
    :delay 1s
    /certificate import file-name="netily-ssl.key" passphrase="{passphrase}"
    :put "SSL key imported."
}} on-error={{
    :put "WARNING: Could not download SSL key."
}}

:delay 2s
:do {{
    :local certName [/certificate find where name~"netily-ssl"]
    :if ([:len $certName] > 0) do={{
        /ip hotspot profile set netily-profile ssl-certificate=[/certificate get $certName name]
        :put "SSL applied to hotspot profile."
    }}
}} on-error={{
    :put "Note: SSL cert not applied (might need manual assignment)."
}}
"""

    def _section_hotspot_html(self, r: Router) -> str:
        login_url = f"{self.active_url}/api/v1/network/provision/{r.auth_key}/hotspot/login.html"
        status_url = f"{self.active_url}/api/v1/network/provision/{r.auth_key}/hotspot/status.html"

        return f"""# ─────────────────────────────────────────────────────────────
# 11. HOTSPOT HTML PAGES (Cloud Portal Redirectors)
# ─────────────────────────────────────────────────────────────
:put "Downloading hotspot pages..."

# Detect the directory MikroTik just assigned
:local dir [/ip hotspot profile get [find name="netily-profile"] html-directory]
:if ($dir = "") do={{ :set dir "hotspot" }}

# Overwrite the default login and status pages with our Cloud Redirectors
:do {{
    /tool fetch url="{login_url}" dst-path=($dir . "/login.html")
    :put " -> login.html installed successfully!"
}} on-error={{ :put ">>> ERROR: Failed to download login.html" }}

:do {{
    /tool fetch url="{status_url}" dst-path=($dir . "/status.html")
    :put " -> status.html installed successfully!"
}} on-error={{ :put ">>> ERROR: Failed to download status.html" }}
"""

    # ============================================================
    # FIX 3: UPDATED PPPoE with anchored default profile local-address
    # ============================================================
    def _section_pppoe(self, r: Router, pppoe_local: str) -> str:
        return f"""# ─────────────────────────────────────────────────────────────
# 12. PPPoE SERVER (Bridge Mode)
# ─────────────────────────────────────────────────────────────
:put "Configuring PPPoE Server..."

# Create Pool (Fallback if RADIUS doesn't send IP)
:do {{ /ip pool remove [find name="netily-pppoe-pool"] }} on-error={{}}
/ip pool add name="netily-pppoe-pool" ranges="{r.pppoe_pool}"

# Create Profile (No Encryption, Change MSS)
:do {{ /ppp profile remove [find name="netily-pppoe-profile"] }} on-error={{}}
/ppp profile add name="netily-pppoe-profile" local-address={pppoe_local} remote-address=netily-pppoe-pool dns-server=8.8.8.8,1.1.1.1 use-encryption=no change-tcp-mss=yes only-one=default

# ANCHOR: Also set local-address on default profile (prevents PPP teardown/629 errors)
/ppp profile set [find name="default"] local-address={pppoe_local}

# Create Server on the BRIDGE
:do {{ /interface pppoe-server server remove [find service-name="netily-pppoe"] }} on-error={{}}
/interface pppoe-server server add service-name="netily-pppoe" interface="netily-bridge" default-profile="netily-pppoe-profile" authentication=pap,chap disabled=no

# Enforce RADIUS with 3-minute interim updates
/ppp aaa set use-radius=yes accounting=yes interim-update=00:03:00
:put " + PPPoE RADIUS enabled with 3-minute interim updates"
"""

    def _section_anti_sharing(self, r: Router, is_v6: bool) -> str:
        return f"""# ─────────────────────────────────────────────────────────────
# 13. SMART ANTI-SHARING
# ─────────────────────────────────────────────────────────────
:put "Configuring Anti-Sharing Rules..."

# Cleanup old rules
:do {{ /ip firewall mangle remove [find comment~"Netily"] }} on-error={{}}
:do {{ /ip firewall address-list remove [find list="allowed-ips"] }} on-error={{}}

# 1. Define VIP Network (The PPPoE Pool Range)
# (Any user with an IP in this range will NOT be restricted)
/ip firewall address-list add list=allowed-ips address={r.pppoe_pool} comment="Netily-PPPoE-VIPs"

# 2. Mark VIP Connections
/ip firewall mangle add chain=prerouting src-address-list=allowed-ips action=mark-connection new-connection-mark=allowed-con passthrough=yes comment="Netily-VIP-Mark"

# 3. Apply Anti-Share (TTL=1) ONLY to Non-VIPs (Hotspot Users)
/ip firewall mangle add chain=postrouting connection-mark=!allowed-con out-interface="netily-bridge" action=change-ttl new-ttl=set:1 passthrough=no comment="Netily-AntiShare-Enforce"
"""

    # ============================================================
    # FIX 2: UPDATED NAT with RADIUS bypass
    # ============================================================
    def _section_nat(self, r: Router) -> str:
        return f"""# ─────────────────────────────────────────────────────────────
# 14. MASQUERADE & NAT
# ─────────────────────────────────────────────────────────────
:put "Configuring NAT..."

# Do NOT NAT traffic to RADIUS server (important for reply tracking)
:do {{ /ip firewall nat remove [find comment="Netily-RADIUS-NoNAT"] }} on-error={{}}
/ip firewall nat add chain=srcnat dst-address={self.radius_server_ip} action=accept place-before=0 comment="Netily-RADIUS-NoNAT"

# General internet masquerade
:do {{
    :if ([:len [/ip firewall nat find comment="Netily-Masquerade"]] = 0) do={{
        /ip firewall nat add chain=srcnat action=masquerade comment="Netily-Masquerade"
    }}
}} on-error={{}}
"""

    def _section_schedulers(self, r: Router) -> str:
        return ""

    def _section_footer(self, r: Router) -> str:
        vpn_host = self._get_vpn_host(r)
        
        return f"""# ═══════════════════════════════════════════════════════════════
# PROVISIONING COMPLETE
# ═══════════════════════════════════════════════════════════════
:delay 1s
:log info "Netily Cloud Controller v4.6 provisioning complete for {self._escape_ros_string(r.name)}"
:put ""
:put "════════════════════════════════════════════════════"
:put " NETILY CLOUD CONTROLLER — SETUP COMPLETE"
:put " Router:  {self._escape_ros_string(r.name)}"
:put " VPN:     {self._escape_ros_string(vpn_host)}:{r.openvpn_port}"
:put " RADIUS:  {self.radius_server_ip}:1812/1813 (src-addr: {r.vpn_ip_address or 'auto'})"
:put " Portal:  {self.get_tenant_portal_url()}"
:put "════════════════════════════════════════════════════"
"""

    def generate_login_html(self) -> str:
        r = self.router
        portal_base = self.get_tenant_portal_url().rstrip('/')
        tenant_name = self._escape_ros_string(r.tenant_subdomain or 'public')
        
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta http-equiv="pragma" content="no-cache">
    <meta http-equiv="cache-control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="expires" content="0">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Connecting...</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f0f2f5;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .card {{
            background: white;
            padding: 2rem;
            border-radius: 1rem;
            text-align: center;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            width: 90%;
            max-width: 400px;
        }}
        .spinner {{
            border: 4px solid #f3f3f3;
            border-top: 4px solid #3498db;
            border-radius: 50%;
            width: 40px;
            height: 40px;
            animation: spin 1s linear infinite;
            margin: 0 auto 1rem;
        }}
        @keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
    </style>
</head>
<body>
    <div class="card">
        <div class="spinner"></div>
        <h2 id="status-text">Connecting to WiFi...</h2>
        <p id="sub-text">Please wait...</p>
        
        <form id="login-form" action="$(link-login-only)" method="post" style="display:none">
            <input type="hidden" name="username" id="usr">
            <input type="hidden" name="password" id="pwd">
            <input type="hidden" name="dst" value="$(link-orig)">
        </form>
    </div>

    <script>
    (function() {{
        // MikroTik variables
        var mac       = '$(mac)';
        var ip        = '$(ip)';
        var identity  = '$(identity)';
        var loginUrl  = '$(link-login-only)'; 
        var error     = '$(error)';
        
        // 1. CHECK FOR RETURN TRIP (Auto-Login Logic)
        // Check if the URL has ?username=... from the Payment Page
        var urlParams = new URLSearchParams(window.location.search);
        var inboundUser = urlParams.get('username');
        var inboundPass = urlParams.get('password');

        if (inboundUser && inboundPass) {{
            // == LOG IN MODE ==
            // The user just paid and was sent back here with credentials.
            // Submit the hidden form to MikroTik immediately.
            document.getElementById('status-text').innerText = "Authenticating...";
            document.getElementById('sub-text').innerText = "Finalizing your connection";
            
            document.getElementById('usr').value = inboundUser;
            document.getElementById('pwd').value = inboundPass;
            document.getElementById('login-form').submit();
            return; // Stop here, do not redirect to portal
        }}

        // 2. REDIRECT MODE (Standard)
        // No username found? This is a new user. Send them to the Cloud Portal.
        
        // Construct Portal URL: http://portal.netily.com/hotspot/ROUTER_ID
        var portalUrl = '{portal_base}/hotspot/{r.id}';
        
        var params = [
            'mac=' + encodeURIComponent(mac),
            'ip=' + encodeURIComponent(ip),
            'router=' + encodeURIComponent(identity),
            'login_url=' + encodeURIComponent(loginUrl),
            'error=' + encodeURIComponent(error),
            'tenant=' + '{tenant_name}'
        ];

        var redirectUrl = portalUrl + '?' + params.join('&');

        // Redirect after short delay
        setTimeout(function() {{
            window.location.href = redirectUrl;
        }}, 800);
    }})();
    </script>
</body>
</html>"""

    def generate_status_html(self) -> str:
        r = self.router
        portal_base = self.get_tenant_portal_url().rstrip('/')
        tenant_name = self._escape_ros_string(r.tenant_subdomain or 'public')
        
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Connected</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .container {{
            background: white;
            border-radius: 16px;
            padding: 40px 32px;
            text-align: center;
            max-width: 400px;
            width: 90%;
            box-shadow: 0 20px 60px rgba(0,0,0,0.2);
        }}
        .check {{ font-size: 48px; margin-bottom: 16px; }}
        h2 {{ color: #11998e; margin-bottom: 8px; }}
        .info {{ margin: 16px 0; font-size: 14px; color: #555; }}
        .info div {{ padding: 6px 0; border-bottom: 1px solid #eee; }}
        .info span {{ font-weight: 600; color: #333; }}
        .btn {{
            display: inline-block;
            margin-top: 20px;
            padding: 12px 32px;
            background: #e74c3c;
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 14px;
            cursor: pointer;
            text-decoration: none;
        }}
        .btn:hover {{ background: #c0392b; }}
        .portal-link {{
            display: block;
            margin-top: 12px;
            color: #11998e;
            text-decoration: none;
            font-size: 13px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="check">&#10004;</div>
        <h2>You're Connected!</h2>
        <p style="color: #666; font-size: 14px;">Welcome to $(identity)</p>
        <div class="info">
            <div>IP Address: <span>$(ip)</span></div>
            <div>Session Time: <span>$(uptime)</span></div>
            <div>Data Used: <span>$(bytes-in-nice) / $(bytes-out-nice)</span></div>
        </div>
        <a class="btn" href="$(link-logout)">Disconnect</a>
        <a class="portal-link" href="{portal_base}/hotspot/{r.id}/status?mac=$(mac)&ip=$(ip)&tenant={tenant_name}">
            Manage Account &rarr;
        </a>
    </div>
</body>
</html>"""

    def generate_full_script(self) -> str:
        return self.generate_config_script("7")

    def generate_one_liner(self) -> str:
        return self.get_magic_link()