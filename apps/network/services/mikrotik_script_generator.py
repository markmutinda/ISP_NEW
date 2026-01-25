# apps/network/services/mikrotik_script_generator.py

from django.conf import settings
from apps.network.models.router_models import Router
import secrets
from django.utils import timezone

class MikrotikScriptGenerator:
    """Generate customized Mikrotik configuration scripts"""
    
    def __init__(self, router: Router):
        self.router = router
        self.company = router.company_name
        self.tenant_subdomain = router.tenant_subdomain
        self.base_url = "http://192.168.88.2:8000"
        
    def generate_full_script(self) -> str:
        # 1. AUTO-GENERATE CREDENTIALS
        # If the router doesn't have an API password yet, generate one and save it to the DB.
        # This ensures Django knows the password before the router even runs the command.
        if not self.router.api_password or self.router.api_username != 'yourisp_api':
            # Generate a secure password
            new_password = secrets.token_urlsafe(12)
            
            # Save to Database immediately
            self.router.api_username = 'yourisp_api'
            self.router.api_password = new_password
            self.router.save(update_fields=['api_username', 'api_password'])

        script = """# YourISP Auto-Configuration Script v7 (Zero-Touch)
:put ">>> STARTING CONFIGURATION <<<"

# ============================================
# STEP 0: CREATE SYSTEM API USER
# ============================================
{}

# ============================================
# STEP 1: OPENVPN SETUP
# ============================================
{}

# ============================================
# STEP 2: BRIDGE & NETWORK SETUP
# ============================================
{}

# ============================================
# STEP 3: WAN INTERFACE CONFIGURATION
# ============================================
{}

# ============================================
# STEP 4: HOTSPOT CONFIGURATION
# ============================================
{}

# ============================================
# STEP 5: PPPOE SERVER SETUP
# ============================================
{}

# ============================================
# STEP 6: RADIUS AUTHENTICATION
# ============================================
{}

# ============================================
# STEP 7: FIREWALL & SECURITY
# ============================================
{}

# ============================================
# STEP 8: WALLED GARDEN
# ============================================
{}

# ============================================
# STEP 9: DHCP & DNS CONFIGURATION
# ============================================
{}

# ============================================
# STEP 10: REGISTRATION HEARTBEAT
# ============================================
{}

:put ">>> CONFIGURATION COMPLETE SUCCESS <<<"
:log info "YourISP configuration completed successfully!"
""".format(
            self._generate_api_user_config(),  # <--- NEW STEP
            self._generate_openvpn_config(),
            self._generate_bridge_config(),
            self._generate_wan_config(),
            self._generate_hotspot_config(),
            self._generate_pppoe_config(),
            self._generate_radius_config(),
            self._generate_firewall_config(),
            self._generate_walled_garden(),
            self._generate_dhcp_dns_config(),
            self._generate_heartbeat_scheduler()
        )
        return script

    def _generate_api_user_config(self) -> str:
        """Create the API user that Django will use to connect"""
        username = self.router.api_username
        password = self.router.api_password
        
        return f"""
:put "Step 0: Creating System API User..."
# Enable API service just in case
:do {{ /ip service set [find name=api] disabled=no port=8728 }} on-error={{}}

# Create User
:do {{
    /user add name="{username}" group=full password="{password}" comment="Managed by YourISP System"
}} on-error={{
    # If user exists, update the password
    /user set [find name="{username}"] password="{password}" group=full
}}
"""
    
    def _generate_openvpn_config(self) -> str:
        openvpn_url = f"{self.base_url}/api/v1/network/routers/{self.router.id}/openvpn-config/?auth_key={self.router.auth_key}"
        
        return f"""
:put "Step 1: Configuring OpenVPN..."
# Download OpenVPN config
/tool fetch url="{openvpn_url}" dst-path=yourisp-vpn.ovpn mode=http

:put "Step 1a: Pausing for filesystem..."
:delay 5s

:put "Step 1b: Skipping Auto-Import (Manual Step Required)..."

:put "Step 1c: Configuring Interface..."
# Try to remove old interface, ignore error if not found
:do {{
    /interface ovpn-client remove [find name="YourISP_VPN"]
}} on-error={{}}

# Add interface
:do {{
    /interface ovpn-client add name=YourISP_VPN connect-to=192.168.88.2 port=1194 mode=ip user=mikrotik_client password="lab-password" cipher=aes256-cbc auth=sha1 add-default-route=no comment="YourISP VPN Tunnel"
}} on-error={{ :put "VPN Interface likely exists." }}

:put "Step 1: OpenVPN Configured (Pending Certs)."
"""
    
    def _generate_bridge_config(self) -> str:
        return """
:put "Step 2: Configuring Bridge..."
:do {
    /interface bridge add name=yourisp-bridge comment="YourISP Customer Bridge"
} on-error={ :put "Bridge already exists." }

# Assign interfaces to bridge (ether3,4,5)
:foreach i in=[:toarray "3,4,5"] do={
    :local ifaceName ("ether" . $i)
    :do {
        /interface bridge port add bridge=yourisp-bridge interface=$ifaceName
    } on-error={ :put ("Port " . $ifaceName . " already in bridge or invalid.") }
}

:do {
    /ip address add address=172.20.0.1/16 interface=yourisp-bridge network=172.20.0.0
} on-error={ :put "IP Address already exists." }

:do {
    /ip pool add name=yourisp-pool ranges=172.20.2.1-172.20.255.254
} on-error={ :put "IP Pool already exists." }
"""
    
    def _generate_wan_config(self) -> str:
        return """
:put "Step 3: Configuring WAN..."
:do { /interface list add name=WAN comment="YourISP WAN Interfaces" } on-error={}
:do { /interface list add name=LAN comment="YourISP LAN Interfaces" } on-error={}

# CRITICAL: Using ether6 as WAN
:do {
    /interface list member add interface=ether6 list=WAN comment="Lab WAN (Laptop Connection)"
} on-error={ :put "WAN member already exists." }

:do {
    /interface list member add interface=yourisp-bridge list=LAN comment="YourISP Customer Bridge"
} on-error={ :put "LAN member already exists." }

:do {
    /ip route add gateway=192.168.88.2 distance=1 comment="Lab Default Route"
} on-error={ :put "Route likely exists." }
"""
    
    def _generate_hotspot_config(self) -> str:
        return f"""
:put "Step 4: Configuring Hotspot..."
:do {{
    /ip hotspot profile add name=yourisp-profile hotspot-address=172.20.0.1 dns-name=portal.yourisp.local login-by=http-pap,cookie,mac-cookie http-cookie-lifetime=1w use-radius=yes radius-accounting=yes nas-port-type=wireless-802.11
}} on-error={{ :put "Hotspot profile already exists." }}

:do {{
    /ip hotspot add name=yourisp-hotspot interface=yourisp-bridge address-pool=yourisp-pool profile=yourisp-profile disabled=no
}} on-error={{ :put "Hotspot server already exists." }}
"""
    
    def _generate_pppoe_config(self) -> str:
        return """
:put "Step 5: Configuring PPPoE..."
:do {
    /ppp profile add name=yourisp-pppoe-profile local-address=192.40.1.1 use-compression=no use-encryption=no change-tcp-mss=yes only-one=yes
} on-error={ :put "PPPoE profile already exists." }

:do {
    /interface pppoe-server server add interface=yourisp-bridge service-name=yourisp-pppoe default-profile=yourisp-pppoe-profile authentication=pap disabled=no
} on-error={ :put "PPPoE server already exists." }
"""
    
    def _generate_radius_config(self) -> str:
        return f"""
:put "Step 6: Configuring RADIUS..."
:do {{
    /radius add address=192.168.88.2 secret="{self.router.shared_secret}" service=ppp,hotspot,login timeout=3000ms
}} on-error={{ :put "RADIUS server already exists." }}

:do {{
    /ppp aaa set use-radius=yes
}} on-error={{}}
"""
    
    def _generate_firewall_config(self) -> str:
        return """
:put "Step 7: Configuring Firewall..."
:do {
    /ip firewall nat add chain=srcnat out-interface-list=WAN action=masquerade comment="masquerade"
} on-error={ :put "NAT rule already exists." }
"""
    
    def _generate_walled_garden(self) -> str:
        return """
:put "Step 8: Configuring Walled Garden..."
:do {
    /ip hotspot walled-garden add dst-host=192.168.88.2 action=allow comment="Allow access to Laptop Server"
} on-error={ :put "Walled garden entry already exists." }
"""
    
    def _generate_dhcp_dns_config(self) -> str:
        return """
:put "Step 9: Configuring DHCP/DNS..."
:do {
    /ip dhcp-server add name=yourisp-dhcp interface=yourisp-bridge address-pool=yourisp-pool lease-time=1h disabled=no
} on-error={ :put "DHCP server already exists." }

:do {
    /ip dhcp-server network add address=172.20.0.0/16 gateway=172.20.0.1 dns-server=8.8.8.8
} on-error={ :put "DHCP network already exists." }

:do {
    /ip dns set allow-remote-requests=yes servers=8.8.8.8
} on-error={}
"""
    
    def _generate_heartbeat_scheduler(self) -> str:
        return """
:put "Step 10: Heartbeat Skipped."
"""

    def generate_debug_script(self) -> tuple:
        script = self.generate_full_script()
        return script, 0, "Debug mode"

    def generate_one_liner(self) -> str:
        url = f"{self.base_url}/api/v1/network/routers/{self.router.id}/config/?auth_key={self.router.auth_key}&version=7"
        return f'/tool fetch url="{url}" dst-path=yourisp-config.rsc mode=http; :delay 2s; /import yourisp-config.rsc;'

    def _get_timestamp(self) -> str:
        return timezone.now().strftime("%Y-%m-%d %H:%M:%S")