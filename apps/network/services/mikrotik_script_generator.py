<<<<<<< HEAD
# apps/network/services/mikrotik_script_generator.py
"""
Lipanet-Style Cloud Controller Script Generator (v4.0)

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
    • PPPoE server + profiles
    • Anti-sharing mangle rules
    • Cloud portal redirector (login.html)

Design Principles (from Lipanet reverse-engineering):
─────────────────────────────────────────────────────
1. VPN uses username/password — no certificate management headaches
2. SSL certs are separate /tool fetch calls — no syntax escaping issues
3. login.html is fetched — not embedded in script text
4. Version detection is client-side — one magic link works for all routers
5. Every section is idempotent — re-running the script is safe
"""

=======
>>>>>>> 9fb26f9b9e1561c3cadb44471a2dfdfa8d44d90a
from django.conf import settings
from apps.network.models.router_models import Router
from django.utils import timezone

class MikrotikScriptGenerator:
<<<<<<< HEAD
    """
    Generates Lipanet-style two-stage provisioning scripts.

    Usage:
        gen = MikrotikScriptGenerator(router)
        magic_link = gen.get_magic_link()              # One-liner for admin
        base_script = gen.generate_base_script()       # Stage 1
        config_rsc  = gen.generate_config_script("7")  # Stage 2 (v6 or v7)
        login_html  = gen.generate_login_html()        # Hotspot redirector
        status_html = gen.generate_status_html()       # Post-auth status page
    """

    def __init__(self, router: Router):
        self.router = router

        # ── URLs ──────────────────────────────────────────────────
        self.base_url = getattr(settings, 'BASE_URL', '').rstrip('/')
        self.portal_url = getattr(
            settings, 'CAPTIVE_PORTAL_URL',
            self.base_url
        ).rstrip('/')
        self.vpn_server_ip = getattr(settings, 'VPN_SERVER_IP', '10.8.0.1')
        self.vpn_api_url = getattr(settings, 'VPN_API_URL', f'http://{self.vpn_server_ip}:8000')

        # ── Provisioning download base ────────────────────────────
        # Public endpoints for MikroTik /tool fetch
        self.provision_base = f"{self.base_url}/api/v1/network/provision"

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # MAGIC LINK — The one-liner the admin pastes into MikroTik
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def get_magic_link(self) -> str:
        """
        Single command that downloads + executes the base script.
        This is what the admin copies from the dashboard and pastes
        into MikroTik Terminal.

        Example output:
          /tool fetch url="https://app.netily.co.ke/api/v1/network/provision/RTR_ABC12345_AUTH/a3b9c1d2/script.rsc" dst-path="netily.rsc"; :delay 2s; /import netily.rsc
        """
        r = self.router
        url = (
            f"{self.provision_base}/{r.auth_key}/{r.provision_slug}/script.rsc"
        )
        return (
            f'/tool fetch url="{url}" dst-path="netily.rsc" mode=http; '
            f':delay 2s; /import netily.rsc'
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # STAGE 1 — Base Script (Version Detection + Stage 2 Fetch)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def generate_base_script(self) -> str:
        """
        Lightweight script that:
        1. Checks internet connectivity (DNS ping)
        2. Detects RouterOS major version (6 or 7)
        3. Fetches the version-specific conf.rsc (Stage 2)
        4. Imports and executes it

        Modeled after Lipanet's approach — universally works on v6 & v7.
        """
        r = self.router
        config_url = f"{self.provision_base}/{r.auth_key}/config"

        return f"""# ═══════════════════════════════════════════════════════════════
# Netily Cloud Controller — Base Script (Stage 1)
# Router: {r.name}
# Generated: {timezone.now().strftime("%Y-%m-%d %H:%M:%S")}
# ═══════════════════════════════════════════════════════════════
:put ">>> NETILY CLOUD CONTROLLER v4.0 <<<"
:put ">>> Stage 1: Detecting RouterOS version..."

# ─── Check Internet Connectivity ────────────────────────────
:local hasInternet false
:do {{
    /tool dns-query name="dns.google" type=A
    :set hasInternet true
}} on-error={{
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
:local configUrl ("{config_url}\\?version=" . $rosVersion . "&router={r.id}&subdomain={r.tenant_subdomain or 'public'}")
:put ("Downloading config from: " . $configUrl)

:do {{
    /tool fetch url=$configUrl dst-path="netily_conf.rsc" mode=http
    :delay 2s
    :put "Config downloaded. Importing..."
    /import netily_conf.rsc
    :put ">>> Stage 2 complete. Router configured."
}} on-error={{
    :put "ERROR: Could not download configuration!"
    :put "URL: $configUrl"
    :error "Config download failed"
}}
"""

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # STAGE 2 — Full Config Script (v6/v7 Aware)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def generate_config_script(self, version: str = "7") -> str:
        """
        The full RouterOS configuration script. Called by the base script
        after version detection. Handles v6 vs v7 cipher/syntax differences.

        Sections:
        1. Identity & Cleanup
        2. API User & Management Access
        3. OpenVPN Tunnel (username/password auth)
        4. Bridge & Ports
        5. IP Pool & DHCP
        6. RADIUS Configuration
        7. Hotspot Profile & Server
        8. Walled Garden
        9. SSL Certificate Downloads
        10. Hotspot HTML Downloads (login.html, status.html)
        11. PPPoE Server
        12. Anti-Sharing Mangle Rules
        13. Masquerade & NAT
        14. Schedulers
        """
        r = self.router
        v = str(version).strip()
        is_v6 = v.startswith("6")

        # Derived values
        portal_domain = self.portal_url.split('://')[-1]
        gateway_ip = r.gateway_ip
        pool_range = r.pool_range
        # For DHCP network — use the /16 or whatever CIDR the gateway has
        gateway_parts = gateway_ip.split('.')
        dhcp_network = f"{gateway_parts[0]}.{gateway_parts[1]}.0.0/16"
        pppoe_local = getattr(r, 'pppoe_local_address', None) or r.get_pppoe_local_ip()

        # v6 vs v7 cipher syntax
        ovpn_cipher = "aes256" if is_v6 else "aes-256-cbc"
        ovpn_auth = "sha1"

        # Build the script
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

    # ──────────────────────────────────────────────────────────────
    # SCRIPT SECTIONS
    # ──────────────────────────────────────────────────────────────

    def _section_header(self, r: Router, version: str) -> str:
        return f"""# ═══════════════════════════════════════════════════════════════
# Netily Cloud Controller — Configuration Script v4.0
# Router: {r.name}
# Tenant: {r.tenant_subdomain or 'public'}
# VPN IP: {r.vpn_ip_address or 'auto-assigned'}
# RouterOS: v{version}
# Generated: {timezone.now().strftime("%Y-%m-%d %H:%M:%S")}
# ═══════════════════════════════════════════════════════════════
:put ">>> Netily v4.0 — Configuring {r.name} (RouterOS v{version})..."
:delay 1s
"""

    def _section_identity_cleanup(self, r: Router) -> str:
        return f"""# ─────────────────────────────────────────────────────────────
# 1. SYSTEM IDENTITY & CLEANUP
# ─────────────────────────────────────────────────────────────
/system identity set name="{r.name}"
:put "Cleaning up old Netily configurations..."

# Remove old configurations (idempotent)
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

:if ([:len [/user find name="{r.api_username}"]] = 0) do={{
    /user add name="{r.api_username}" group=full password="{r.api_password}" comment="Netily Cloud API"
}} else={{
    /user set [find name="{r.api_username}"] password="{r.api_password}" group=full
}}

# Enable API service (restricted to VPN + localhost)
/ip service set api disabled=no port={r.api_port} address=10.0.0.0/8,127.0.0.0/8
/ip service set api-ssl disabled=yes
"""

    def _section_openvpn(self, r: Router, cipher: str, auth: str, is_v6: bool) -> str:
        """
        OpenVPN tunnel using USERNAME/PASSWORD authentication.
        This is the Lipanet approach — no certificates needed for the tunnel itself.
        Certificates are for the SSL transport, managed by the OpenVPN server CA.
        """
        # v6 uses 'cipher' parameter; v7 uses 'cipher' but with different values
        if is_v6:
            ovpn_cmd = f"""/interface ovpn-client add name="Netily-VPN" \\
    connect-to="{r.openvpn_server}" \\
    port={r.openvpn_port} \\
    user="{r.openvpn_username}" \\
    password="{r.openvpn_password}" \\
    cipher={cipher} \\
    auth={auth} \\
    add-default-route=no \\
    comment="Netily Cloud Controller Tunnel" """
        else:
            ovpn_cmd = f"""/interface ovpn-client add name="Netily-VPN" \\
    connect-to="{r.openvpn_server}" \\
    port={r.openvpn_port} \\
    user="{r.openvpn_username}" \\
    password="{r.openvpn_password}" \\
    cipher={cipher} \\
    auth={auth} \\
    add-default-route=no \\
    tls-version=any \\
    verify-server-certificate=no \\
    comment="Netily Cloud Controller Tunnel" """

        # If the server requires a CA cert for TLS verification,
        # we fetch it separately BEFORE creating the OVPN client
        ca_fetch = ""
        if r.ca_certificate:
            ca_url = f"{self.provision_base}/{r.auth_key}/certs/ca.crt"
            ca_fetch = f"""
# Download OpenVPN CA certificate (for TLS verification)
:do {{
    /tool fetch url="{ca_url}" dst-path="netily-vpn-ca.crt" mode=http
    :delay 1s
    /certificate import file-name="netily-vpn-ca.crt" passphrase=""
    :put "VPN CA certificate imported."
}} on-error={{
    :put "Note: VPN CA cert not available. Using unverified TLS."
}}
"""

        return f"""# ─────────────────────────────────────────────────────────────
# 3. OPENVPN TUNNEL (Username/Password Authentication)
# ─────────────────────────────────────────────────────────────
:put "Establishing Cloud VPN tunnel..."
{ca_fetch}
{ovpn_cmd}

# Wait for tunnel to establish
:put "Waiting for VPN tunnel..."
:delay 8s

# Verify tunnel is running
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

# Allow VPN traffic (bypass hotspot auth)
/ip firewall filter add chain=input action=accept src-address=10.8.0.0/24 comment="Netily-VPN-Input-Allow" place-before=0
/ip firewall filter add chain=forward action=accept src-address=10.8.0.0/24 comment="Netily-VPN-Forward-Allow" place-before=0
/ip firewall filter add chain=forward action=accept dst-address=10.8.0.0/24 comment="Netily-VPN-Forward-Return" place-before=0

# Allow established/related connections
/ip firewall filter add chain=input action=accept connection-state=established,related comment="Netily-Established" place-before=0
"""

    def _section_bridge_ports(self, r: Router) -> str:
        """Generate bridge + port assignment commands."""
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

# Add interfaces to bridge
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
        return f"""# ─────────────────────────────────────────────────────────────
# 7. RADIUS (Cloud RADIUS via VPN Tunnel)
# ─────────────────────────────────────────────────────────────
:put "Configuring Cloud RADIUS..."

# Remove old RADIUS entries
:do {{ :foreach i in=[/radius find comment~"Netily"] do={{ /radius remove $i }} }} on-error={{}}

# RADIUS client → cloud server VPN IP
/radius add address={self.vpn_server_ip} secret="{r.shared_secret}" \\
    service=hotspot,ppp timeout=3000ms comment="Netily-Cloud-RADIUS"

# Enable RADIUS incoming (for CoA / Disconnect messages)
/radius incoming set accept=yes port=3799
"""

    def _section_hotspot(self, r: Router, gateway_ip: str) -> str:
        return f"""# ─────────────────────────────────────────────────────────────
# 8. HOTSPOT PROFILE & SERVER
# ─────────────────────────────────────────────────────────────
:put "Configuring Hotspot..."

/ip hotspot profile add name="netily-profile" \\
    hotspot-address="{gateway_ip}" \\
    dns-name="{r.dns_name}" \\
    html-directory="hotspot" \\
    login-by=http-pap,mac-cookie \\
    use-radius=yes \\
    radius-accounting=yes \\
    http-cookie-lifetime=1d \\
    rate-limit=""

/ip hotspot add name="netily-hotspot" interface="netily-bridge" \\
    address-pool="netily-pool" profile="netily-profile" disabled=no
"""

    def _section_walled_garden(self, r: Router, portal_domain: str) -> str:
        return f"""# ─────────────────────────────────────────────────────────────
# 9. WALLED GARDEN (Pre-Auth Access)
# ─────────────────────────────────────────────────────────────
:put "Configuring Walled Garden..."

# Remove old entries
:do {{ :foreach i in=[/ip hotspot walled-garden find comment~"Netily"] do={{ /ip hotspot walled-garden remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/ip hotspot walled-garden ip find comment~"Netily"] do={{ /ip hotspot walled-garden ip remove $i }} }} on-error={{}}

# ── Cloud Portal (Next.js captive portal) ──
/ip hotspot walled-garden add dst-host="*{portal_domain}*" comment="Netily-Portal"
/ip hotspot walled-garden add dst-host="*netily.co.ke*" comment="Netily-Backend"
/ip hotspot walled-garden add dst-host="*netily.io*" comment="Netily-Alt"

# ── Payment Gateways (M-Pesa / PayHero) ──
/ip hotspot walled-garden add dst-host="*.safaricom.co.ke" comment="Netily-MPesa"
/ip hotspot walled-garden add dst-host="*.safaricom.com" comment="Netily-Safaricom"
/ip hotspot walled-garden add dst-host="*.payhero.co.ke" comment="Netily-PayHero"

# ── VPN server (direct API access via IP) ──
/ip hotspot walled-garden ip add dst-address={self.vpn_server_ip}/32 action=accept comment="Netily-VPN-API"
/ip hotspot walled-garden ip add dst-address=10.8.0.0/24 action=accept comment="Netily-VPN-Network"
"""

    def _section_ssl_certs(self, r: Router) -> str:
        """
        Download SSL certificates via separate /tool fetch calls.
        This is the Lipanet approach — avoids PEM escaping issues in scripts.
        SSL certs are for the hotspot HTTPS portal, not for VPN.
        """
        if not r.ssl_certificate:
            return f"""# ─────────────────────────────────────────────────────────────
# 10. SSL CERTIFICATES (Hotspot HTTPS)
# ─────────────────────────────────────────────────────────────
:put "No SSL certificates configured — hotspot will use HTTP only."
"""

        ssl_cert_url = f"{self.provision_base}/{r.auth_key}/certs/ssl.crt"
        ssl_key_url = f"{self.provision_base}/{r.auth_key}/certs/ssl.key"
        passphrase = r.ssl_passphrase or ''

        return f"""# ─────────────────────────────────────────────────────────────
# 10. SSL CERTIFICATES (Hotspot HTTPS)
# ─────────────────────────────────────────────────────────────
:put "Downloading SSL certificates..."

# Remove old certs
:do {{ /certificate remove [find name~"netily-ssl"] }} on-error={{}}

# Download SSL Certificate
:do {{
    /tool fetch url="{ssl_cert_url}" dst-path="netily-ssl.crt" mode=http
    :delay 1s
    /certificate import file-name="netily-ssl.crt" passphrase="{passphrase}"
    :put "SSL certificate imported."
}} on-error={{
    :put "WARNING: Could not download SSL certificate."
}}

# Download SSL Private Key
:do {{
    /tool fetch url="{ssl_key_url}" dst-path="netily-ssl.key" mode=http
    :delay 1s
    /certificate import file-name="netily-ssl.key" passphrase="{passphrase}"
    :put "SSL key imported."
}} on-error={{
    :put "WARNING: Could not download SSL key."
}}

# Apply SSL to hotspot profile
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
        """
        Download login.html and status.html via /tool fetch.
        Lipanet approach: HTML files are served by the cloud, not embedded.
        """
        login_url = f"{self.provision_base}/{r.auth_key}/hotspot/login.html"
        status_url = f"{self.provision_base}/{r.auth_key}/hotspot/status.html"

        return f"""# ─────────────────────────────────────────────────────────────
# 11. HOTSPOT HTML PAGES (Cloud Portal Redirectors)
# ─────────────────────────────────────────────────────────────
:put "Downloading hotspot pages..."

# Ensure hotspot directory exists
:do {{ /file print file="hotspot/." }} on-error={{}}

# Download login.html (Cloud Redirector)
:do {{
    /tool fetch url="{login_url}" dst-path="hotspot/login.html" mode=http
    :put "login.html installed."
}} on-error={{
    :put "WARNING: Could not download login.html"
}}

# Download status.html (Post-Auth Page)
:do {{
    /tool fetch url="{status_url}" dst-path="hotspot/status.html" mode=http
    :put "status.html installed."
}} on-error={{
    :put "WARNING: Could not download status.html"
}}
"""

    def _section_pppoe(self, r: Router, pppoe_local: str) -> str:
        """
        PPPoE Server configuration. Both Hotspot and PPPoE share the
        same bridge but use separate pools. RADIUS handles auth for both.
        """
        return f"""# ─────────────────────────────────────────────────────────────
# 12. PPPoE SERVER
# ─────────────────────────────────────────────────────────────
:put "Configuring PPPoE server..."

# PPPoE IP Pool
/ip pool add name="netily-pppoe-pool" ranges="{r.pppoe_pool}"

# PPPoE Profile (RADIUS-driven, no local rate limit)
/ppp profile add name="netily-pppoe-profile" \\
    local-address={pppoe_local} \\
    remote-address=netily-pppoe-pool \\
    dns-server=8.8.8.8,1.1.1.1 \\
    use-encryption=no \\
    comment="Netily PPPoE Profile"

# PPPoE Server on bridge
/interface pppoe-server server add name="netily-pppoe" \\
    interface="netily-bridge" \\
    service-name="netily-pppoe" \\
    default-profile="netily-pppoe-profile" \\
    authentication=pap,chap \\
    disabled=no \\
    comment="Netily PPPoE Server"
"""

    def _section_anti_sharing(self, r: Router, is_v6: bool) -> str:
        """
        Anti-sharing mangle rules (TTL manipulation).
        Detects devices sharing via hotspot/tethering by checking TTL.
        Lipanet uses TTL 1 change on forward chain.
        """
        if is_v6:
            ttl_action = "change-ttl=1"
        else:
            ttl_action = "change-ttl=set:1"

        return f"""# ─────────────────────────────────────────────────────────────
# 13. ANTI-SHARING (TTL Mangle Rules)
# ─────────────────────────────────────────────────────────────
:put "Configuring anti-sharing rules..."

# Detect forwarded traffic (TTL-1 means router behind hotspot client)
/ip firewall mangle add chain=forward action={ttl_action} \\
    passthrough=yes comment="Netily-AntiShare-TTL"
"""

    def _section_nat(self, r: Router) -> str:
        return f"""# ─────────────────────────────────────────────────────────────
# 14. MASQUERADE & NAT
# ─────────────────────────────────────────────────────────────
:put "Configuring NAT..."

# Masquerade for hotspot/PPPoE clients to reach internet
:do {{
    :if ([:len [/ip firewall nat find comment="Netily-Masquerade"]] = 0) do={{
        /ip firewall nat add chain=srcnat action=masquerade \\
            out-interface="{r.wan_interface}" comment="Netily-Masquerade"
    }}
}} on-error={{}}
"""

    def _section_schedulers(self, r: Router) -> str:
        """
        Periodic tasks: heartbeat reporting, config refresh check.
        """
        heartbeat_url = f"{self.vpn_api_url}/api/v1/network/routers/heartbeat/"

        return f"""# ─────────────────────────────────────────────────────────────
# 15. SCHEDULERS (Heartbeat & Auto-Update)
# ─────────────────────────────────────────────────────────────
:put "Setting up scheduled tasks..."

# Remove old schedulers
:do {{ /system scheduler remove [find name~"netily"] }} on-error={{}}

# Heartbeat: Report status to cloud every 5 minutes
/system scheduler add name="netily-heartbeat" interval=5m on-event="/tool fetch url=\\"{heartbeat_url}?auth_key={r.auth_key}\\" keep-result=no mode=http" start-time=startup comment="Netily Cloud Heartbeat"
"""

    def _section_footer(self, r: Router) -> str:
        return f"""# ═══════════════════════════════════════════════════════════════
# PROVISIONING COMPLETE
# ═══════════════════════════════════════════════════════════════
:delay 1s
:log info "Netily Cloud Controller v4.0 provisioning complete for {r.name}"
:put ""
:put "════════════════════════════════════════════════════"
:put " NETILY CLOUD CONTROLLER — SETUP COMPLETE"
:put " Router:  {r.name}"
:put " VPN:     {r.openvpn_server}:{r.openvpn_port}"
:put " RADIUS:  {self.vpn_server_ip}"
:put " Portal:  {self.portal_url}"
:put "════════════════════════════════════════════════════"
"""

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # HOTSPOT HTML GENERATORS
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def generate_login_html(self) -> str:
        """
        Cloud Portal Redirector — The login.html that MikroTik serves.
        Redirects to the Next.js captive portal with MikroTik variables.

        MikroTik Template Variables:
        $(mac)             — client MAC address
        $(ip)              — client IP address
        $(identity)        — router name
        $(link-login-only) — MikroTik login callback URL
        $(link-orig)       — originally requested URL
        $(error)           — any error message

        Smart TV Detection:
        Checks User-Agent for smart TV / IoT patterns and auto-authenticates
        via MAC-cookie if possible.
        """
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
    <!-- Normal browser users see spinner + redirect -->
    <div class="container" id="main">
        <div class="spinner"></div>
        <h2>Connecting to WiFi...</h2>
        <p>You'll be redirected to the login portal shortly.</p>
        <p style="margin-top: 16px; font-size: 12px;">
            Not redirected? <a id="manual-link" href="#">Click here</a>
        </p>
    </div>

    <!-- Smart TV / IoT auto-auth (hidden) -->
    <div class="container hidden" id="tv-auth">
        <h2>Smart TV Detected</h2>
        <p>Attempting automatic connection...</p>
    </div>

    <script>
        // ── MikroTik Template Variables ──
        var mac      = '$(mac)';
        var ip       = '$(ip)';
        var identity = '$(identity)';
        var loginUrl = '$(link-login-only)';
        var origUrl  = '$(link-orig)';
        var error    = '$(error)';

        // ── Build portal URL ──
        var portalBase = '{portal}/portal/login';
        var params = new URLSearchParams({{
            mac: mac,
            ip: ip,
            router: identity,
            router_id: '{r.id}',
            login_url: loginUrl,
            orig_url: origUrl,
            error: error,
            tenant: '{r.tenant_subdomain or ""}'
        }});
        var portalUrl = portalBase + '?' + params.toString();

        // ── Smart TV / IoT Detection ──
        var ua = navigator.userAgent.toLowerCase();
        var isTv = /smart-tv|smarttv|googletv|appletv|hbbtv|pov_tv|netcast|viera|nettv|roku|dlnadoc|ce-html|lg-|samsung|tizen|webos|bravia|philips|panasonic|vestel/.test(ua);
        var isIot = /cros|playstation|xbox|nintendo|kindle|fire/.test(ua);

        if (isTv || isIot) {{
            // Show TV auth UI and attempt MAC login
            document.getElementById('main').classList.add('hidden');
            document.getElementById('tv-auth').classList.remove('hidden');
            // Try MAC cookie auto-auth
            window.location.href = loginUrl + '?username=T-' + mac + '&password=' + mac;
        }} else {{
            // Normal redirect to cloud portal
            document.getElementById('manual-link').href = portalUrl;
            setTimeout(function() {{
                window.location.href = portalUrl;
            }}, 1500);
        }}
    </script>
</body>
</html>"""

    def generate_status_html(self) -> str:
        """
        Post-authentication status page. Shown after successful RADIUS auth.
        Provides connection info, remaining time, and logout button.
        """
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
        <a class="portal-link" href="{portal}/portal/status?mac=$(mac)&ip=$(ip)&router_id={r.id}&tenant={r.tenant_subdomain or ''}">
            Manage Account &rarr;
        </a>
    </div>
</body>
</html>"""

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # LEGACY COMPAT — Old method names
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def generate_full_script(self) -> str:
        """Legacy compat: returns the v7 config by default."""
        return self.generate_config_script("7")

    def generate_one_liner(self) -> str:
        """Legacy compat: alias for get_magic_link."""
        return self.get_magic_link()
=======
    def __init__(self, router: Router):
        self.router = router
        self.portal_url = getattr(settings, 'CAPTIVE_PORTAL_URL', settings.BASE_URL).rstrip('/')
        self.api_base = f"{settings.BASE_URL}/api/v1/network"
        self.vpn_server_ip = getattr(settings, 'VPN_SERVER_IP', '10.8.0.1')

    def generate_full_script(self) -> str:
        r = self.router
        
        # LipaNet Style: User/Pass for VPN (Matches their OVPN command)
        # Note: We use the router's auth_key or a generated vpn_password
        vpn_user = f"router_{r.id}"
        vpn_pass = r.auth_key 

        # 1. DOWNLOAD LINKS (Matches your setup in views.py)
        url_ca = f"{self.api_base}/routers/{r.id}/cert/ca.crt/"
        url_crt = f"{self.api_base}/routers/{r.id}/cert/client.crt/"
        url_key = f"{self.api_base}/routers/{r.id}/cert/client.key/"
        
        # HTML Redirect (Simple version)
        login_html = f"<html><head><meta http-equiv='refresh' content='0;url={self.portal_url}/hotspot/{r.id}?mac=\\$(mac)'></head></html>"

        script = f"""# Netily Config - LipaNet Blueprint Mode
/system identity set name="{r.name}"

# CLEANUP
:do {{ /ip hotspot remove [find name="netily-hotspot"] }} on-error={{}}
:do {{ /ip hotspot profile remove [find name="netily-profile"] }} on-error={{}}
:do {{ /interface ovpn-client remove [find name="Netily-VPN"] }} on-error={{}}
:do {{ /certificate remove [find name~"Netily"] }} on-error={{}}

# VPN (LipaNet Blueprint: User/Pass + Cipher + Auth)
/interface ovpn-client add name="Netily-VPN" connect-to="{r.openvpn_server}" user="{vpn_user}" password="{vpn_pass}" cipher=aes256 auth=sha1 comment="netily_vpn"

# FIREWALL (Simple accept)
/ip firewall filter add chain=input action=accept src-address={self.vpn_server_ip} comment="netily" place-before=0

# LAN & DHCP
/interface bridge add name="netily-bridge"
/ip address add address="{r.gateway_cidr}" interface="netily-bridge"
{self._generate_interface_script()}
/ip pool add name="netily-pool" ranges="{r.pool_range}"
/ip dhcp-server add name="netily-dhcp" interface="netily-bridge" address-pool="netily-pool" lease-time=1h
/ip dhcp-server network add address="{r.gateway_ip.rsplit('.', 1)[0]}.0/24" gateway="{r.gateway_ip}" dns-server=8.8.8.8

# HOTSPOT
/ip hotspot profile add name="netily-profile" hotspot-address="{r.gateway_ip}" dns-name="{r.dns_name}" login-by=http-pap,mac-cookie use-radius=yes
/ip hotspot add name="netily-hotspot" interface="netily-bridge" address-pool="netily-pool" profile="netily-profile"

# RADIUS
/radius add service=ppp,hotspot address={self.vpn_server_ip} secret="{r.shared_secret}" timeout=3000ms comment="netily_radius"

# SSL & UI (LipaNet style fetch)
/tool fetch url="{url_ca}" dst-path=netily.crt mode=http
/tool fetch url="{url_key}" dst-path=netily.key mode=http
/certificate import file-name=netily.crt passphrase="" name="Netily-SSL"
/certificate import file-name=netily.key passphrase="" name="Netily-SSL"
/ip hotspot profile set [find name="netily-profile"] ssl-certificate=Netily-SSL_0

# UI REDIRECT
/file set [find name="hotspot/login.html"] contents="{login_html}"

:put "Setup complete."
"""
        return script

    def _generate_interface_script(self) -> str:
        cmds = []
        for port in (self.router.hotspot_interfaces or []):
            if port.strip():
                cmds.append(f'/interface bridge port add bridge="netily-bridge" interface="{port.strip()}"')
        return "\n".join(cmds)

    def generate_one_liner(self) -> str:
        base = self.portal_url or settings.BASE_URL
        url = f"{base}/api/v1/network/routers/config/?auth_key={self.router.auth_key}"
        return f'/file remove [find name="netily_setup.rsc"]; /tool fetch url="{url}" dst-path=netily_setup.rsc mode=http; :delay 2s; /import netily_setup.rsc;'
>>>>>>> 9fb26f9b9e1561c3cadb44471a2dfdfa8d44d90a
