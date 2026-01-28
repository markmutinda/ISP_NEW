# apps/network/services/mikrotik_script_generator.py

from django.conf import settings
from apps.network.models.router_models import Router
from django.utils import timezone

class MikrotikScriptGenerator:
    """
    Generates a Zero-Touch configuration script.
    Philosophy: The Router is dumb. The Cloud is smart.
    """
    
    def __init__(self, router: Router):
        self.router = router
        # Use NGROK or Public Domain for the captive portal URL
        self.portal_url = settings.BASE_URL 
        
    def generate_full_script(self) -> str:
        """
        Returns the full .rsc script content.
        """
        return f"""# Netily Router Configuration Script v2.0
# Generated for: {self.router.name}
# Tenant: {self.router.tenant_subdomain}
# Date: {timezone.now().strftime("%Y-%m-%d %H:%M:%S")}

:put ">>> INITIALIZING NETILY CONFIGURATION <<<"

# =========================================================
# 1. CLEANUP (The "Nuke" Phase)
# Remove conflicting configs to ensure a clean slate.
# =========================================================
:put "Cleaning up old configurations..."
:foreach i in=[/ip hotspot find name="netily-hotspot"] do={{ /ip hotspot remove $i }}
:foreach i in=[/ip hotspot profile find name="netily-profile"] do={{ /ip hotspot profile remove $i }}
:foreach i in=[/ip pool find name="netily-pool"] do={{ /ip pool remove $i }}
:foreach i in=[/ip dhcp-server find name="netily-dhcp"] do={{ /ip dhcp-server remove $i }}
:foreach i in=[/interface bridge find name="netily-bridge"] do={{ /interface bridge remove $i }}
# Remove old addresses on the bridge
:foreach i in=[/ip address find interface="netily-bridge"] do={{ /ip address remove $i }}

# =========================================================
# 2. MANAGEMENT ACCESS (VPN & API)
# =========================================================
:put "Configuring Management Tunnel..."

# Create API User (Restricted to Localhost & VPN)
:if ([:len [/user find name="{self.router.api_username}"]] = 0) do={{
    /user add name="{self.router.api_username}" group=full password="{self.router.api_password}" comment="Netily System User"
}} else={{
    /user set [find name="{self.router.api_username}"] password="{self.router.api_password}" group=full
}}

# Enable API Service
/ip service set api disabled=no port={self.router.api_port} address=10.0.0.0/8

# Configure OpenVPN Client
:if ([:len [/interface ovpn-client find name="Netily_VPN"]] != 0) do={{
    /interface ovpn-client remove [find name="Netily_VPN"]
}}
/interface ovpn-client add name="Netily_VPN" \\
    connect-to="{self.router.vpn_server}" \\
    user="{self.router.vpn_username}" \\
    password="{self.router.vpn_password}" \\
    cipher=aes256-cbc auth=sha1 \\
    add-default-route=no \\
    comment="Netily Management Tunnel"

# Firewall: Allow Cloud to talk to Router
:do {{
    /ip firewall filter remove [find comment="Allow_Netily_VPN"]
    /ip firewall filter add chain=input action=accept src-address=10.0.0.0/8 comment="Allow_Netily_VPN" place-before=0
}} on-error={{}}

# =========================================================
# 3. NETWORK TOPOLOGY (Bridge & IP)
# =========================================================
:put "Configuring LAN Network..."

# Create Bridge
/interface bridge add name="netily-bridge" comment="Netily Hotspot Bridge"

# Assign Gateway IP (Dynamic from DB)
/ip address add address="{self.router.gateway_cidr}" interface="netily-bridge" comment="Netily Gateway"

# Create IP Pool
/ip pool add name="netily-pool" ranges="{self.router.pool_range}"

# DHCP Server
/ip dhcp-server add name="netily-dhcp" interface="netily-bridge" address-pool="netily-pool" lease-time=1h
/ip dhcp-server network add address="{self.router.gateway_ip.rsplit('.', 1)[0]}.0/16" gateway="{self.router.gateway_ip}" dns-server=8.8.8.8,1.1.1.1

# =========================================================
# 4. PORT ASSIGNMENT (Dynamic from DB)
# =========================================================
:put "Assigning Interfaces..."
{self._generate_interface_script()}

# =========================================================
# 5. HOTSPOT & RADIUS
# =========================================================
:put "Configuring Hotspot Service..."

# RADIUS Client
:if ([:len [/radius find comment="Netily_RADIUS"]] = 0) do={{
    /radius add address="{self.router.radius_server}" secret="{self.router.radius_secret}" service=hotspot,ppp timeout=3000ms comment="Netily_RADIUS"
}} else={{
    /radius set [find comment="Netily_RADIUS"] address="{self.router.radius_server}" secret="{self.router.radius_secret}"
}}

# Hotspot Profile (The "Cloud Redirect" Magic)
/ip hotspot profile add name="netily-profile" \\
    hotspot-address="{self.router.gateway_ip}" \\
    dns-name="{self.router.dns_name}" \\
    html-directory="hotspot" \\
    login-by=http-pap,mac-cookie \\
    use-radius=yes radius-accounting=yes

# Hotspot Server
/ip hotspot add name="netily-hotspot" interface="netily-bridge" address-pool="netily-pool" profile="netily-profile"

# Walled Garden (Allow Payment & Portal Access)
/ip hotspot walled-garden add dst-host="*{settings.BASE_URL.split('://')[-1]}" comment="Allow Portal"
/ip hotspot walled-garden add dst-host="*.safaricom.co.ke" comment="M-Pesa"
/ip hotspot walled-garden add dst-host="*.payhero.co.ke" comment="Payment Gateway"

# =========================================================
# 6. INSTALL CLOUD REDIRECTOR (The Frontend Link)
# We overwrite login.html to immediately bounce to React
# =========================================================
:put "Installing Cloud Redirector..."

:global redirectHtml "<html><head><meta http-equiv=\\"refresh\\" content=\\"0;url={self.portal_url}/login?router_id={self.router.id}&mac=\$(mac)&ip=\$(ip)&u=\$(username)\\" /></head><body><p>Loading Portal...</p></body></html>"

# Ensure directory exists (fallback)
:do {{ /file print file="hotspot/login.html" }} on-error={{}}
:delay 1s
/file set [find name="hotspot/login.html"] contents=$redirectHtml

:log info "Netily Configuration Complete for {self.router.name}"
:put ">>> SUCCESS: ROUTER PROVISIONED <<<"
"""

    def _generate_interface_script(self) -> str:
        """
        Loops through the list of ports saved in the DB (e.g. ['ether2', 'ether3'])
        and generates the script to add them to the bridge.
        """
        cmds = []
        # Fallback if list is empty
        ports = self.router.hotspot_interfaces or []
        
        for port in ports:
            # Sanitize port name to prevent script injection
            safe_port = port.strip()
            if not safe_port: continue
            
            cmd = f"""
:do {{
    /interface bridge port remove [find interface="{safe_port}"]
}} on-error={{}}
:do {{
    /interface bridge port add bridge="netily-bridge" interface="{safe_port}"
}} on-error={{ :put "Could not add {safe_port} (might not exist)" }}
"""
            cmds.append(cmd)
        
        return "\n".join(cmds)

    def generate_one_liner(self) -> str:
        """The command user pastes into terminal"""
        url = f"{self.portal_url}/api/v1/network/routers/config/?auth_key={self.router.auth_key}"
        return f'/tool fetch url="{url}" dst-path=netily_setup.rsc mode=http; :delay 2s; /import netily_setup.rsc;'