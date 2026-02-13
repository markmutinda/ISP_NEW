# apps/network/services/mikrotik_script_generator.py
"""
Lipanet-Style Cloud Controller Script Generator (v4.5)

Architecture: Two-Stage Download
─────────────────────────────────
Stage 1 — "Magic Link":
    Admin pastes ONE command into MikroTik terminal.
    Downloads a small base script that:
    • Detects RouterOS version (v6 vs v7)
    • Checks internet connectivity
    • Fetches the version-specific full config (Stage 2)

Stage 2 — Version-Specific Config (conf.rsc):
    The full RouterOS configuration with v6/v7-aware syntax.
    • OpenVPN tunnel (username/password, NOT certificates)
    • SSL certs downloaded separately via /tool fetch (NOT embedded)
    • Bridge + Ports + DHCP
    • RADIUS (pointing to VPN server IP)
    • Hotspot + Walled Garden
    • PPPoE server + profiles (set after creation, like LipaNet)
    • Anti-sharing mangle rules (v7: new-ttl=set:1)
    • Cloud portal redirector (login.html)
"""

from django.conf import settings
from apps.network.models.router_models import Router
from django.utils import timezone


class MikrotikScriptGenerator:
    def __init__(self, router: Router, request=None):
        self.router = router
        self.request = request

        # ── URLs ──────────────────────────────────────────────────
        self.base_url = getattr(settings, 'BASE_URL', '').rstrip('/')
        self.portal_url = getattr(
            settings, 'CAPTIVE_PORTAL_URL',
            self.base_url
        ).rstrip('/')
        self.vpn_server_ip = getattr(settings, 'VPN_SERVER_IP', '10.8.0.1')
        self.vpn_api_url = getattr(settings, 'VPN_API_URL', f'http://{self.vpn_server_ip}:8000')

        # ── Provisioning download base ────────────────────────────
        self.provision_base = f"{self.base_url}/api/v1/network/provision"

    def _escape_ros_string(self, s: str) -> str:
        """
        Escape backslash, double quote, and dollar sign for RouterOS double-quoted strings.
        Order matters: first escape backslash, then double quote, then dollar.
        """
        if s is None:
            return ""
        s = s.replace('\\', '\\\\')
        s = s.replace('"', '\\"')
        s = s.replace('$', '\\$')
        return s

    def get_magic_link(self) -> str:
        r = self.router
        url = (
            f"{self.provision_base}/{r.auth_key}/{r.provision_slug}/script.rsc"
        )
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
:put ">>> NETILY CLOUD CONTROLLER v4.5 <<<"
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
:if ([:pick $verStr 0 1] = "6") do={{
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
        gateway_ip = r.gateway_ip
        pool_range = r.pool_range
        gateway_parts = gateway_ip.split('.')
        dhcp_network = f"{gateway_parts[0]}.{gateway_parts[1]}.0.0/16"
        pppoe_local = getattr(r, 'pppoe_local_address', None) or r.get_pppoe_local_ip()

        ovpn_cipher = "aes256"
        ovpn_auth = "sha1"

        sections = [
            self._section_header(r, v),
            self._section_identity_cleanup(r),
            self._section_api_user(r),
            self._section_openvpn(r, ovpn_cipher, ovpn_auth, is_v6),
            self._section_firewall(r),
            self._section_bridge_ports(r),
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
        return "\n".join(s for s in sections if s)

    def _section_header(self, r: Router, version: str) -> str:
        return f"""# ═══════════════════════════════════════════════════════════════
# Netily Cloud Controller — Configuration Script v4.5
# Router: {self._escape_ros_string(r.name)}
# Tenant: {self._escape_ros_string(r.tenant_subdomain or 'public')}
# VPN IP: {r.vpn_ip_address or 'auto-assigned'}
# RouterOS: v{version}
# Generated: {timezone.now().strftime("%Y-%m-%d %H:%M:%S")}
# ═══════════════════════════════════════════════════════════════
:put ">>> Netily v4.5 — Configuring {self._escape_ros_string(r.name)} (RouterOS v{version})..."
:delay 1s
"""

    def _section_identity_cleanup(self, r: Router) -> str:
        return f"""# ─────────────────────────────────────────────────────────────
# 1. SYSTEM IDENTITY & CLEANUP
# ─────────────────────────────────────────────────────────────
/system identity set name="{self._escape_ros_string(r.name)}"
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
        return f"""# ─────────────────────────────────────────────────────────────
# 2. API USER (Cloud Management Access)
# ─────────────────────────────────────────────────────────────
:put "Configuring API user..."

:if ([:len [/user find name="{self._escape_ros_string(r.api_username)}"]] = 0) do={{
    /user add name="{self._escape_ros_string(r.api_username)}" group=full password="{self._escape_ros_string(r.api_password)}" comment="Netily Cloud API"
}} else={{
    /user set [find name="{self._escape_ros_string(r.api_username)}"] password="{self._escape_ros_string(r.api_password)}" group=full
}}

/ip service set api disabled=no port={r.api_port} address=10.0.0.0/8,127.0.0.0/8
/ip service set api-ssl disabled=yes
"""

    def _section_openvpn(self, r: Router, cipher: str, auth: str, is_v6: bool) -> str:
        ca_fetch = ""
        if r.ca_certificate:
            ca_url = f"{self.base_url}/api/v1/network/provision/{r.auth_key}/certs/ca.crt"
            ca_fetch = f"""
:do {{
    /tool fetch url="{ca_url}" dst-path="netily-vpn-ca.crt"
    :delay 1s
    /certificate import file-name="netily-vpn-ca.crt" passphrase=""
    :put "VPN CA certificate imported."
}} on-error={{
    :put "Note: VPN CA cert not available. Using unverified TLS."
}}
"""

        if is_v6:
            ovpn_cmd = f'/interface ovpn-client add name="Netily-VPN" connect-to="{self._escape_ros_string(r.openvpn_server)}" port={r.openvpn_port} user="{self._escape_ros_string(r.openvpn_username)}" password="{self._escape_ros_string(r.openvpn_password)}" cipher={cipher} auth={auth} add-default-route=no comment="Netily Cloud Controller Tunnel"'
        else:
            ovpn_cmd = f'/interface ovpn-client add name="Netily-VPN" connect-to="{self._escape_ros_string(r.openvpn_server)}" port={r.openvpn_port} user="{self._escape_ros_string(r.openvpn_username)}" password="{self._escape_ros_string(r.openvpn_password)}" add-default-route=no comment="Netily Cloud Controller Tunnel"'

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
        # Removed place-before – not needed and causes error in v7
        return f"""# ─────────────────────────────────────────────────────────────
# 4. FIREWALL (VPN & Management)
# ─────────────────────────────────────────────────────────────
:put "Configuring firewall rules..."

/ip firewall filter add chain=input action=accept src-address=10.8.0.0/24 comment="Netily-VPN-Input-Allow"
/ip firewall filter add chain=forward action=accept src-address=10.8.0.0/24 comment="Netily-VPN-Forward-Allow"
/ip firewall filter add chain=forward action=accept dst-address=10.8.0.0/24 comment="Netily-VPN-Forward-Return"
/ip firewall filter add chain=input action=accept connection-state=established,related comment="Netily-Established"
"""

    def _section_bridge_ports(self, r: Router) -> str:
        port_cmds = []
        ports = r.hotspot_interfaces or []
        for port in ports:
            safe = port.strip()
            if not safe:
                continue
            port_cmds.append(f""":do {{
    /interface bridge port remove [find interface="{safe}" bridge="netily-bridge"]
}} on-error={{}}
:do {{
    /interface bridge port add bridge="netily-bridge" interface="{safe}"
    :put "Added {safe} to bridge"
}} on-error={{
    :put "Could not add {safe} to bridge (may not exist on this hardware)"
}}""")
        ports_script = "\n".join(port_cmds) if port_cmds else ':put "No hotspot interfaces configured — add ports manually"'

        return f"""# ─────────────────────────────────────────────────────────────
# 5. BRIDGE & PORTS
# ─────────────────────────────────────────────────────────────
:put "Creating hotspot network bridge..."
/interface bridge add name="netily-bridge" comment="Netily Hotspot/PPPoE Bridge"
/ip address add address="{r.gateway_cidr}" interface="netily-bridge" comment="Netily Gateway"
{ports_script}
"""

    def _section_dhcp(self, r: Router, gateway_ip: str, pool_range: str, dhcp_network: str) -> str:
        return f"""# ─────────────────────────────────────────────────────────────
# 6. IP POOL & DHCP
# ─────────────────────────────────────────────────────────────
:put "Configuring DHCP..."
/ip pool add name="netily-pool" ranges="{pool_range}"
/ip dhcp-server add name="netily-dhcp" interface="netily-bridge" address-pool="netily-pool" lease-time=1h disabled=no
/ip dhcp-server network add address="{dhcp_network}" gateway="{gateway_ip}" dns-server=8.8.8.8,1.1.1.1 comment="Netily DHCP Network"
"""

    def _section_radius(self, r: Router) -> str:
        radius_cmd = f'/radius add address={self.vpn_server_ip} secret="{self._escape_ros_string(r.shared_secret)}" service=hotspot,ppp timeout=3000ms comment="Netily-Cloud-RADIUS"'
        return f"""# ─────────────────────────────────────────────────────────────
# 7. RADIUS (Cloud RADIUS via VPN Tunnel)
# ─────────────────────────────────────────────────────────────
:put "Configuring Cloud RADIUS..."

:do {{ :foreach i in=[/radius find comment~"Netily"] do={{ /radius remove $i }} }} on-error={{}}

{radius_cmd}

/radius incoming set accept=yes port=3799
"""

    def _section_hotspot(self, r: Router, gateway_ip: str) -> str:
        profile_cmd = f'/ip hotspot profile add name="netily-profile" hotspot-address="{gateway_ip}" dns-name="{self._escape_ros_string(r.dns_name)}" html-directory="hotspot" login-by=http-pap,mac-cookie use-radius=yes radius-accounting=yes http-cookie-lifetime=1d rate-limit=""'
        server_cmd = f'/ip hotspot add name="netily-hotspot" interface="netily-bridge" address-pool="netily-pool" profile="netily-profile" disabled=no'
        return f"""# ─────────────────────────────────────────────────────────────
# 8. HOTSPOT PROFILE & SERVER
# ─────────────────────────────────────────────────────────────
:put "Configuring Hotspot..."
{profile_cmd}
{server_cmd}
"""

    def _section_walled_garden(self, r: Router, portal_domain: str) -> str:
        return f"""# ─────────────────────────────────────────────────────────────
# 9. WALLED GARDEN (Pre-Auth Access)
# ─────────────────────────────────────────────────────────────
:put "Configuring Walled Garden..."

:do {{ :foreach i in=[/ip hotspot walled-garden find comment~"Netily"] do={{ /ip hotspot walled-garden remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/ip hotspot walled-garden ip find comment~"Netily"] do={{ /ip hotspot walled-garden ip remove $i }} }} on-error={{}}

/ip hotspot walled-garden add dst-host="*{portal_domain}*" comment="Netily-Portal"
/ip hotspot walled-garden add dst-host="*netily.co.ke*" comment="Netily-Backend"
/ip hotspot walled-garden add dst-host="*netily.io*" comment="Netily-Alt"
/ip hotspot walled-garden add dst-host="*.safaricom.co.ke" comment="Netily-MPesa"
/ip hotspot walled-garden add dst-host="*.safaricom.com" comment="Netily-Safaricom"
/ip hotspot walled-garden add dst-host="*.payhero.co.ke" comment="Netily-PayHero"
/ip hotspot walled-garden ip add dst-address={self.vpn_server_ip}/32 action=accept comment="Netily-VPN-API"
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
    /tool fetch url="{ssl_cert_url}" dst-path="netily-ssl.crt"
    :delay 1s
    /certificate import file-name="netily-ssl.crt" passphrase="{passphrase}"
    :put "SSL certificate imported."
}} on-error={{
    :put "WARNING: Could not download SSL certificate."
}}

:do {{
    /tool fetch url="{ssl_key_url}" dst-path="netily-ssl.key"
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
        login_url = f"{self.provision_base}/{r.auth_key}/hotspot/login.html"
        status_url = f"{self.provision_base}/{r.auth_key}/hotspot/status.html"

        return f"""# ─────────────────────────────────────────────────────────────
# 11. HOTSPOT HTML PAGES (Cloud Portal Redirectors)
# ─────────────────────────────────────────────────────────────
:put "Downloading hotspot pages..."

:do {{ /file print file="hotspot/." }} on-error={{}}

:do {{
    /tool fetch url="{login_url}" dst-path="hotspot/login.html"
    :put "login.html installed."
}} on-error={{
    :put "WARNING: Could not download login.html"
}}

:do {{
    /tool fetch url="{status_url}" dst-path="hotspot/status.html"
    :put "status.html installed."
}} on-error={{
    :put "WARNING: Could not download status.html"
}}
"""

    def _section_pppoe(self, r: Router, pppoe_local: str) -> str:
        # LipaNet style: create pool, profile, then minimal server, then set profile/auth
        pool_cmd = f'/ip pool add name="netily-pppoe-pool" ranges="{r.pppoe_pool}"'
        profile_cmd = f'/ppp profile add name="netily-pppoe-profile" local-address={pppoe_local} remote-address=netily-pppoe-pool dns-server=8.8.8.8,1.1.1.1 use-encryption=no comment="Netily PPPoE Profile"'
        # Minimal server add (no name, service-name only, as LipaNet does)
        server_add = f'/interface pppoe-server server add interface="netily-bridge" service-name="netily-pppoe" disabled=no comment="Netily PPPoE Server"'
        # After creation, set default-profile and authentication
        server_config = f'''
:do {{
    /interface pppoe-server server set [find service-name="netily-pppoe"] default-profile="netily-pppoe-profile" authentication=pap,chap
}} on-error={{}}
'''
        return f"""# ─────────────────────────────────────────────────────────────
# 12. PPPoE SERVER
# ─────────────────────────────────────────────────────────────
:put "Configuring PPPoE server..."

{pool_cmd}
{profile_cmd}
{server_add}
{server_config}
"""

    def _section_anti_sharing(self, r: Router, is_v6: bool) -> str:
        # LipaNet uses new-ttl=set:1 for both v6 and v7
        mangle_cmd = f'/ip firewall mangle add chain=forward action=change-ttl new-ttl=set:1 passthrough=yes comment="Netily-AntiShare-TTL"'
        return f"""# ─────────────────────────────────────────────────────────────
# 13. ANTI-SHARING (TTL Mangle Rules)
# ─────────────────────────────────────────────────────────────
:put "Configuring anti-sharing rules..."
{mangle_cmd}
"""

    def _section_nat(self, r: Router) -> str:
        return f"""# ─────────────────────────────────────────────────────────────
# 14. MASQUERADE & NAT
# ─────────────────────────────────────────────────────────────
:put "Configuring NAT..."

:do {{
    :if ([:len [/ip firewall nat find comment="Netily-Masquerade"]] = 0) do={{
        /ip firewall nat add chain=srcnat action=masquerade comment="Netily-Masquerade"
    }}
}} on-error={{}}
"""

    def _section_schedulers(self, r: Router) -> str:
        return ""

    def _section_footer(self, r: Router) -> str:
        return f"""# ═══════════════════════════════════════════════════════════════
# PROVISIONING COMPLETE
# ═══════════════════════════════════════════════════════════════
:delay 1s
:log info "Netily Cloud Controller v4.5 provisioning complete for {self._escape_ros_string(r.name)}"
:put ""
:put "════════════════════════════════════════════════════"
:put " NETILY CLOUD CONTROLLER — SETUP COMPLETE"
:put " Router:  {self._escape_ros_string(r.name)}"
:put " VPN:     {self._escape_ros_string(r.openvpn_server)}:{r.openvpn_port}"
:put " RADIUS:  {self.vpn_server_ip}"
:put " Portal:  {self.portal_url}"
:put "════════════════════════════════════════════════════"
"""

    def generate_login_html(self) -> str:
        r = self.router
        portal = self.portal_url
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta http-equiv="pragma" content="no-cache">
    <meta http-equiv="cache-control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="expires" content="0">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Connecting to WiFi...</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #333;
        }}
        .container {{
            background: white;
            border-radius: 16px;
            padding: 40px 32px;
            text-align: center;
            max-width: 400px;
            width: 90%;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        }}
        .spinner {{
            width: 48px;
            height: 48px;
            border: 4px solid #e0e0e0;
            border-top-color: #667eea;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            margin: 0 auto 24px;
        }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
        h2 {{ font-size: 20px; margin-bottom: 8px; color: #1a1a2e; }}
        p {{ font-size: 14px; color: #666; margin-top: 8px; }}
        a {{ color: #667eea; text-decoration: none; font-weight: 500; }}
        a:hover {{ text-decoration: underline; }}
        .hidden {{ display: none; }}
    </style>
</head>
<body>
    <div class="container" id="main">
        <div class="spinner"></div>
        <h2>Connecting to WiFi...</h2>
        <p>You'll be redirected to the login portal shortly.</p>
        <p style="margin-top: 16px; font-size: 12px;">
            Not redirected? <a id="manual-link" href="#">Click here</a>
        </p>
    </div>
    <div class="container hidden" id="tv-auth">
        <h2>Smart TV Detected</h2>
        <p>Attempting automatic connection...</p>
    </div>
    <script>
        var mac      = '$(mac)';
        var ip       = '$(ip)';
        var identity = '$(identity)';
        var loginUrl = '$(link-login-only)';
        var origUrl  = '$(link-orig)';
        var error    = '$(error)';
        var portalBase = '{portal}/portal/login';
        var params = new URLSearchParams({{
            mac: mac,
            ip: ip,
            router: identity,
            router_id: '{r.id}',
            login_url: loginUrl,
            orig_url: origUrl,
            error: error,
            tenant: '{self._escape_ros_string(r.tenant_subdomain or "")}'
        }});
        var portalUrl = portalBase + '?' + params.toString();
        var ua = navigator.userAgent.toLowerCase();
        var isTv = /smart-tv|smarttv|googletv|appletv|hbbtv|pov_tv|netcast|viera|nettv|roku|dlnadoc|ce-html|lg-|samsung|tizen|webos|bravia|philips|panasonic|vestel/.test(ua);
        var isIot = /cros|playstation|xbox|nintendo|kindle|fire/.test(ua);
        if (isTv || isIot) {{
            document.getElementById('main').classList.add('hidden');
            document.getElementById('tv-auth').classList.remove('hidden');
            window.location.href = loginUrl + '?username=T-' + mac + '&password=' + mac;
        }} else {{
            document.getElementById('manual-link').href = portalUrl;
            setTimeout(function() {{
                window.location.href = portalUrl;
            }}, 1500);
        }}
    </script>
</body>
</html>"""

    def generate_status_html(self) -> str:
        r = self.router
        portal = self.portal_url
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
        <a class="portal-link" href="{portal}/portal/status?mac=$(mac)&ip=$(ip)&router_id={r.id}&tenant={self._escape_ros_string(r.tenant_subdomain or '')}">
            Manage Account &rarr;
        </a>
    </div>
</body>
</html>"""

    def generate_full_script(self) -> str:
        return self.generate_config_script("7")

    def generate_one_liner(self) -> str:
        return self.get_magic_link()