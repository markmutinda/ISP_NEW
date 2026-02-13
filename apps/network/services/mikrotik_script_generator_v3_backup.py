# apps/network/services/mikrotik_script_generator.py

from django.conf import settings
from apps.network.models.router_models import Router
from django.utils import timezone


class MikrotikScriptGenerator:
    """
    Generates a Zero-Touch Cloud Controller configuration script (v3.0).
    
    Philosophy: The Router is dumb. The Cloud is smart.
    
    v3.0 Changes:
    - Certificate-based VPN authentication (instead of user/pass)
    - RADIUS points to VPN gateway (10.8.0.1) for in-tunnel auth
    - Cloud Redirector sends users to Next.js captive portal
    - Smart TV detection in login.html
    - Walled garden for M-Pesa, PayHero, portal domain
    """
    
    def __init__(self, router: Router):
        self.router = router
        # Public portal URL (Next.js captive portal)
        self.portal_url = getattr(settings, 'CAPTIVE_PORTAL_URL', settings.BASE_URL).rstrip('/')
        # Backend API reachable via VPN tunnel
        self.vpn_api_url = getattr(settings, 'VPN_API_URL', 'http://10.8.0.1:8000')
        # VPN server IP (for RADIUS and firewall rules)
        self.vpn_server_ip = getattr(settings, 'VPN_SERVER_IP', '10.8.0.1')

    def generate_full_script(self) -> str:
        """
        Constructs the full RouterOS .rsc configuration script.
        Sequence matches the Cloud Controller architecture spec:
        1. Identity & Cleanup
        2. Certificate injection (CA + client cert + key)
        3. OpenVPN tunnel (certificate-based auth)
        4. API user + Firewall (allow VPN traffic)
        5. Bridge & Ports (hotspot network)
        6. IP Pool & DHCP
        7. RADIUS (pointing to cloud via VPN)
        8. Hotspot profile & server
        9. Walled Garden (portal + M-Pesa + PayHero)
        10. Cloud Redirector (login.html → Next.js)
        """
        r = self.router
        
        # Prepare cert content (escape for RouterOS file set)
        ca_cert = self._escape_cert(r.ca_certificate or '')
        client_cert = self._escape_cert(r.client_certificate or '')
        client_key = self._escape_cert(r.client_key or '')
        
        # Portal domain (strip https://)
        portal_domain = self.portal_url.split('://')[-1]

        script = f"""# ============================================================
# Netily Cloud Controller — Router Configuration Script v3.0
# Generated for: {r.name}
# Tenant: {r.tenant_subdomain or 'public'}
# VPN IP: {r.vpn_ip_address or 'pending'}
# Date: {timezone.now().strftime("%Y-%m-%d %H:%M:%S")}
# ============================================================

:delay 2s
:put ">>> NETILY CLOUD CONTROLLER v3.0 <<<"
:put ">>> Configuring {r.name}..."

# ─────────────────────────────────────────────────────────────
# 1. SYSTEM IDENTITY & CLEANUP
# ─────────────────────────────────────────────────────────────
/system identity set name="{r.name}"

:put "Cleaning up old Netily configurations..."
:do {{ :foreach i in=[/ip hotspot find name="netily-hotspot"] do={{ /ip hotspot remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/ip hotspot profile find name="netily-profile"] do={{ /ip hotspot profile remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/ip pool find name="netily-pool"] do={{ /ip pool remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/ip dhcp-server find name="netily-dhcp"] do={{ /ip dhcp-server remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/ip dhcp-server network find comment="Netily DHCP Network"] do={{ /ip dhcp-server network remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/interface ovpn-client find name="Netily-VPN"] do={{ /interface ovpn-client remove $i }} }} on-error={{}}
:do {{ :foreach i in=[/interface bridge find name="netily-bridge"] do={{ 
    /interface bridge port remove [find bridge="netily-bridge"]
    /ip address remove [find interface="netily-bridge"]
    /interface bridge remove $i 
}} }} on-error={{}}

# ─────────────────────────────────────────────────────────────
# 2. INJECT VPN CERTIFICATES (Cloud Controller PKI)
# ─────────────────────────────────────────────────────────────
:put "Importing VPN certificates..."

# Remove old certs
:do {{ /certificate remove [find name~"netily"] }} on-error={{}}

# CA Certificate
:do {{
    /file print file="netily-ca.crt"
    :delay 1s
    /file set "netily-ca.crt" contents="{ca_cert}"
    /certificate import file-name="netily-ca.crt" passphrase=""
}} on-error={{ :put "Warning: CA cert import issue" }}

# Client Certificate
:do {{
    /file print file="netily-client.crt"
    :delay 1s
    /file set "netily-client.crt" contents="{client_cert}"
    /certificate import file-name="netily-client.crt" passphrase=""
}} on-error={{ :put "Warning: Client cert import issue" }}

# Client Key
:do {{
    /file print file="netily-client.key"
    :delay 1s
    /file set "netily-client.key" contents="{client_key}"
    /certificate import file-name="netily-client.key" passphrase=""
}} on-error={{ :put "Warning: Client key import issue" }}

:delay 3s
:put "Certificates imported."

# ─────────────────────────────────────────────────────────────
# 3. OPENVPN TUNNEL (Certificate-Based Authentication)
# ─────────────────────────────────────────────────────────────
:put "Establishing Cloud VPN tunnel..."
/interface ovpn-client add name="Netily-VPN" \\
    connect-to="{r.openvpn_server}" \\
    port={r.openvpn_port} \\
    user="{r.openvpn_username or r.name}" \\
    certificate="netily-client.crt_0" \\
    cipher=aes256-cbc \\
    auth=sha1 \\
    add-default-route=no \\
    comment="Netily Cloud Controller Tunnel"

# Wait for VPN to connect
:delay 5s

# ─────────────────────────────────────────────────────────────
# 4. API USER & FIREWALL (Management Access)
# ─────────────────────────────────────────────────────────────
:put "Configuring management access..."

# Create API user (restricted to VPN & localhost)
:if ([:len [/user find name="{r.api_username}"]] = 0) do={{
    /user add name="{r.api_username}" group=full password="{r.api_password}" comment="Netily Cloud API User"
}} else={{
    /user set [find name="{r.api_username}"] password="{r.api_password}" group=full
}}

# Enable API service (only from VPN range)
/ip service set api disabled=no port={r.api_port} address=10.0.0.0/8,127.0.0.0/8

# Firewall: Allow VPN traffic to bypass hotspot
:do {{ /ip firewall filter remove [find comment="Netily-VPN-Allow"] }} on-error={{}}
/ip firewall filter add chain=input action=accept src-address=10.8.0.0/24 comment="Netily-VPN-Allow" place-before=0
/ip firewall filter add chain=forward action=accept src-address=10.8.0.0/24 comment="Netily-VPN-Allow" place-before=0

# ─────────────────────────────────────────────────────────────
# 5. BRIDGE & PORTS (Hotspot Network)
# ─────────────────────────────────────────────────────────────
:put "Creating hotspot network bridge..."
/interface bridge add name="netily-bridge" comment="Netily Hotspot Bridge"
/ip address add address="{r.gateway_cidr}" interface="netily-bridge" comment="Netily Gateway"

# Add interfaces to bridge
{self._generate_interface_script()}

# ─────────────────────────────────────────────────────────────
# 6. IP POOL & DHCP
# ─────────────────────────────────────────────────────────────
:put "Configuring DHCP..."
/ip pool add name="netily-pool" ranges="{r.pool_range}"
/ip dhcp-server add name="netily-dhcp" interface="netily-bridge" address-pool="netily-pool" lease-time=1h
/ip dhcp-server network add address="{r.gateway_ip.rsplit('.', 1)[0]}.0/16" gateway="{r.gateway_ip}" dns-server=8.8.8.8,1.1.1.1 comment="Netily DHCP Network"

# ─────────────────────────────────────────────────────────────
# 7. RADIUS (Cloud RADIUS via VPN Tunnel)
# ─────────────────────────────────────────────────────────────
:put "Configuring Cloud RADIUS..."

# RADIUS client pointing to cloud server VPN IP
:do {{ /radius remove [find comment="Netily-Cloud-RADIUS"] }} on-error={{}}
/radius add address={self.vpn_server_ip} secret="{r.shared_secret}" \\
    service=hotspot,ppp timeout=3000ms comment="Netily-Cloud-RADIUS"

# ─────────────────────────────────────────────────────────────
# 8. HOTSPOT PROFILE & SERVER
# ─────────────────────────────────────────────────────────────
:put "Configuring Hotspot..."

/ip hotspot profile add name="netily-profile" \\
    hotspot-address="{r.gateway_ip}" \\
    dns-name="{r.dns_name}" \\
    html-directory="hotspot" \\
    login-by=http-pap,mac-cookie \\
    use-radius=yes \\
    radius-accounting=yes \\
    http-cookie-lifetime=1d

/ip hotspot add name="netily-hotspot" interface="netily-bridge" \\
    address-pool="netily-pool" profile="netily-profile"

# ─────────────────────────────────────────────────────────────
# 9. WALLED GARDEN (Allow Portal & Payments Pre-Auth)
# ─────────────────────────────────────────────────────────────
:put "Configuring Walled Garden..."

# Remove old walled garden entries
:do {{ :foreach i in=[/ip hotspot walled-garden find comment~"Netily"] do={{ /ip hotspot walled-garden remove $i }} }} on-error={{}}

# Cloud Portal (Next.js captive portal)
/ip hotspot walled-garden add dst-host="*{portal_domain}" comment="Netily Portal"
# M-Pesa (Safaricom STK Push)
/ip hotspot walled-garden add dst-host="*.safaricom.co.ke" comment="Netily M-Pesa"
# PayHero Payment Gateway
/ip hotspot walled-garden add dst-host="*.payhero.co.ke" comment="Netily PayHero"
# VPN gateway (Django API access)
/ip hotspot walled-garden ip add dst-address={self.vpn_server_ip}/32 action=accept comment="Netily VPN API"
# Backend API (direct access if not via VPN)
/ip hotspot walled-garden add dst-host="*netily.co.ke" comment="Netily Backend"
/ip hotspot walled-garden add dst-host="*netily.io" comment="Netily Backend Alt"

# ─────────────────────────────────────────────────────────────
# 10. CLOUD REDIRECTOR (login.html → Next.js Portal)
# ─────────────────────────────────────────────────────────────
:put "Installing Cloud Portal Redirector..."

# Overwrite login.html with a redirect to the Next.js captive portal
# MikroTik variables: $(mac), $(ip), $(identity), $(link-login-only), $(error)
:global redirectPage "<html><head><meta charset='utf-8'><meta http-equiv='pragma' content='no-cache'><meta name='viewport' content='width=device-width,initial-scale=1'><style>body{{background:#f0f2f5;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}}.spin{{border:4px solid #f3f3f3;border-top:4px solid #3498db;border-radius:50%;width:40px;height:40px;animation:s 1s linear infinite}}@keyframes s{{to{{transform:rotate(360deg)}}}}</style></head><body><div style='text-align:center'><div class='spin' style='margin:0 auto 20px'></div><p>Connecting to WiFi...</p><p style='font-size:12px;color:#666'>If not redirected <a id='lnk' href='#'>click here</a></p></div><script>var u='{self.portal_url}/hotspot/{r.id}';var p='\\$(mac)='+encodeURIComponent('\\$(mac)')+'&ip='+encodeURIComponent('\\$(ip)')+'&router='+encodeURIComponent('\\$(identity)')+'&login_url='+encodeURIComponent('\\$(link-login-only)')+'&error='+encodeURIComponent('\\$(error)');var f=u+'?'+p;document.getElementById('lnk').href=f;window.location.href=f</script></body></html>"

:do {{
    /file print file="hotspot/login.html"
    :delay 1s
    /file set [find name="hotspot/login.html"] contents=$redirectPage
}} on-error={{
    :put "Note: Could not create hotspot directory. Trying fetch method..."
    :do {{
        /tool fetch url="{self.vpn_api_url}/api/v1/hotspot/login-page/{r.id}/" dst-path="hotspot/login.html" mode=http
    }} on-error={{ :put "Warning: Could not install login.html redirector" }}
}}

# ─────────────────────────────────────────────────────────────
# DONE
# ─────────────────────────────────────────────────────────────
:delay 1s
:log info "Netily Cloud Controller provisioning complete for {r.name}"
:put ""
:put "============================================"
:put " NETILY CLOUD CONTROLLER — SETUP COMPLETE"
:put " Router: {r.name}"
:put " VPN IP: {r.vpn_ip_address or 'pending'}"
:put " Portal: {self.portal_url}/hotspot/{r.id}"
:put "============================================"
"""
        return script

    def _generate_interface_script(self) -> str:
        """
        Generate RouterOS commands to add ports to the hotspot bridge.
        Reads from the router's hotspot_interfaces JSON field.
        """
        cmds = []
        ports = self.router.hotspot_interfaces or []
        
        for port in ports:
            safe_port = port.strip()
            if not safe_port:
                continue
            cmds.append(f""":do {{
    /interface bridge port remove [find interface="{safe_port}"]
}} on-error={{}}
:do {{
    /interface bridge port add bridge="netily-bridge" interface="{safe_port}"
}} on-error={{ :put "Could not add {safe_port} to bridge (may not exist)" }}""")
        
        return "\n".join(cmds) if cmds else ':put "No hotspot interfaces configured"'

    def _escape_cert(self, pem_content: str) -> str:
        """
        Escape PEM certificate content for RouterOS /file set contents.
        RouterOS needs special handling for multi-line certificate data.
        """
        if not pem_content:
            return ''
        # RouterOS /file set contents expects the PEM as a single-line value
        # Replace newlines with \\n for RouterOS interpretation
        # Also escape any double quotes
        escaped = pem_content.strip()
        escaped = escaped.replace('"', '\\"')
        escaped = escaped.replace('\r\n', '\\n')
        escaped = escaped.replace('\n', '\\n')
        return escaped

    def generate_one_liner(self) -> str:
        """
        The 'Magic Link' — a single command an admin pastes into MikroTik Terminal.
        Downloads and executes the full configuration script.
        """
        base = self.portal_url or settings.BASE_URL
        url = f"{base}/api/v1/network/routers/config/?auth_key={self.router.auth_key}"
        return (
            f'/tool fetch url="{url}" dst-path=netily_setup.rsc mode=http; '
            f':delay 2s; /import netily_setup.rsc;'
        )

    def generate_vpn_only_script(self) -> str:
        """
        Lighter script that ONLY sets up the VPN tunnel (no hotspot).
        Useful for PPPoE-only routers or re-establishing the management tunnel.
        """
        r = self.router
        ca_cert = self._escape_cert(r.ca_certificate or '')
        client_cert = self._escape_cert(r.client_certificate or '')
        client_key = self._escape_cert(r.client_key or '')

        return f"""# Netily VPN-Only Script for {r.name}
# Date: {timezone.now().strftime("%Y-%m-%d %H:%M:%S")}

:do {{ /interface ovpn-client remove [find name="Netily-VPN"] }} on-error={{}}
:do {{ /certificate remove [find name~"netily"] }} on-error={{}}

/file print file="netily-ca.crt"
:delay 1s
/file set "netily-ca.crt" contents="{ca_cert}"
/certificate import file-name="netily-ca.crt" passphrase=""

/file print file="netily-client.crt"
:delay 1s
/file set "netily-client.crt" contents="{client_cert}"
/certificate import file-name="netily-client.crt" passphrase=""

/file print file="netily-client.key"
:delay 1s
/file set "netily-client.key" contents="{client_key}"
/certificate import file-name="netily-client.key" passphrase=""

:delay 3s
/interface ovpn-client add name="Netily-VPN" \\
    connect-to="{r.openvpn_server}" \\
    port={r.openvpn_port} \\
    user="{r.openvpn_username or r.name}" \\
    certificate="netily-client.crt_0" \\
    cipher=aes256-cbc auth=sha1 \\
    add-default-route=no \\
    comment="Netily Cloud Controller Tunnel"

:put "VPN tunnel configured for {r.name}"
"""