"""
Service to generate Mikrotik auto-configuration scripts
Similar to Lipanet's approach
"""

from django.conf import settings
from apps.network.models import Router
import secrets


class MikrotikScriptGenerator:
    """Generate customized Mikrotik configuration scripts"""
    
    def __init__(self, router: Router):
        self.router = router
        self.company = router.company_name
        self.tenant_subdomain = router.tenant_subdomain
        self.base_url = settings.BASE_URL or "https://camden-convocative-oversorrowfully.ngrok-free.dev"
        
    def generate_full_script(self) -> str:
        """Generate complete configuration script"""
        
        script = f"""# YourISP Auto-Configuration Script
# Router: {self.router.name}
# Company: {self.company}
# Generated: {self._get_timestamp()}

:log info "Starting YourISP configuration..."

# ============================================
# STEP 1: OPENVPN SETUP (for non-public IP ISPs)
# ============================================
{self._generate_openvpn_config()}

# ============================================
# STEP 2: BRIDGE & NETWORK SETUP
# ============================================
{self._generate_bridge_config()}

# ============================================
# STEP 3: WAN INTERFACE CONFIGURATION
# ============================================
{self._generate_wan_config()}

# ============================================
# STEP 4: HOTSPOT CONFIGURATION
# ============================================
{self._generate_hotspot_config()}

# ============================================
# STEP 5: PPPOE SERVER SETUP
# ============================================
{self._generate_pppoe_config()}

# ============================================
# STEP 6: RADIUS AUTHENTICATION
# ============================================
{self._generate_radius_config()}

# ============================================
# STEP 7: FIREWALL & SECURITY
# ============================================
{self._generate_firewall_config()}

# ============================================
# STEP 8: WALLED GARDEN (Portal Access)
# ============================================
{self._generate_walled_garden()}

# ============================================
# STEP 9: DHCP & DNS CONFIGURATION
# ============================================
{self._generate_dhcp_dns_config()}

# ============================================
# STEP 10: REGISTRATION HEARTBEAT
# ============================================
{self._generate_heartbeat_scheduler()}

:log info "YourISP configuration completed successfully!"
"""
        return script
    
    def _generate_openvpn_config(self) -> str:
        """Generate OpenVPN configuration"""
        
        # Generate unique VPN credentials for this router
        vpn_username = f"{self.tenant_subdomain}_{self.router.id}_vpn"
        
        return f"""
# OpenVPN Configuration
:log info "Configuring OpenVPN tunnel..."

# Download OpenVPN config
/tool fetch url="{self.base_url}/api/v1/network/routers/{self.router.id}/openvpn-config/?auth_key={self.router.auth_key}" dst-path=yourisp-vpn.ovpn mode=https

# Create OpenVPN client interface
/interface ovpn-client
:if ([/interface ovpn-client find name="YourISP_VPN"] != "") do={{
    /interface ovpn-client remove [find name="YourISP_VPN"]
}}

add name=YourISP_VPN \\
    connect-to=vpn.yourisp.com \\
    port=1194 \\
    mode=ip \\
    user={vpn_username} \\
    password="{self._generate_vpn_password()}" \\
    cipher=aes256-cbc \\
    auth=sha1 \\
    add-default-route=no \\
    comment="YourISP VPN Tunnel"

:delay 3s
:log info "OpenVPN configured"
"""
    
    def _generate_bridge_config(self) -> str:
        """Generate bridge and IP configuration"""
        
        return """
# Bridge Configuration
:log info "Setting up network bridge..."

# Create bridge for hotspot
/interface bridge
:if ([/interface bridge find name="yourisp-bridge"] = "") do={{
    add name=yourisp-bridge comment="YourISP Customer Bridge"
}}

# Assign interfaces to bridge (ether7-10 by default, customize as needed)
/interface bridge port
:foreach i in=[7,8,9,10] do={{
    :local ifaceName;
    :set ifaceName ("ether" . $i);
    :if ([/interface find name=$ifaceName] != "") do={{
        :if ([/interface bridge port find interface=$ifaceName] = "") do={{
            add bridge=yourisp-bridge interface=$ifaceName
        }}
    }}
}}

# IP Address for bridge (172.20.0.0/16 range)
/ip address
:if ([/ip address find address~"172.20.0.1/16"] = "") do={{
    add address=172.20.0.1/16 interface=yourisp-bridge network=172.20.0.0
}}

# Create IP pool for DHCP
/ip pool
:if ([/ip pool find name="yourisp-pool"] = "") do={{
    add name=yourisp-pool ranges=172.20.2.1-172.20.255.254
}} else={{
    /ip pool set [find name="yourisp-pool"] ranges=172.20.2.1-172.20.255.254
}}

:log info "Bridge configuration completed"
"""
    
    def _generate_wan_config(self) -> str:
        """Generate WAN interface configuration"""
        
        # Get configured WAN interfaces or use defaults
        wan_interfaces = self._get_wan_interfaces()
        
        config = """
# WAN Interface Configuration
:log info "Configuring WAN interfaces..."

# Create WAN interface list if it doesn't exist
/interface list
:if ([/interface list find name="WAN"] = "") do={{
    add name=WAN comment="YourISP WAN Interfaces"
}}

# Create LAN interface list
:if ([/interface list find name="LAN"] = "") do={{
    add name=LAN comment="YourISP LAN Interfaces"
}}
"""
        
        # Add interfaces to WAN list
        if wan_interfaces:
            for iface in wan_interfaces:
                config += f"""
# Add {iface} to WAN list
/interface list member
:if ([/interface list member find interface={iface}] = "") do={{
    add interface={iface} list=WAN comment="YourISP WAN Interface"
}}
"""
        else:
            # Auto-detect potential WAN interfaces (usually first few ethernet ports)
            config += """
# Auto-detecting WAN interfaces (defaulting to ether1-ether3)
:foreach i in=[1,2,3] do={{
    :local ifaceName;
    :set ifaceName ("ether" . $i);
    :if ([/interface find name=$ifaceName] != "") do={{
        /interface list member
        :if ([/interface list member find interface=$ifaceName] = "") do={{
            add interface=$ifaceName list=WAN comment="Auto-detected WAN"
        }}
    }}
}}
"""
        
        # Add bridge to LAN list
        config += """
# Add bridge to LAN list
/interface list member
:if ([/interface list member find interface=yourisp-bridge] = "") do={{
    add interface=yourisp-bridge list=LAN comment="YourISP Customer Bridge"
}}
"""
        
        # Configure default route if WAN interfaces exist
        config += """
# Configure default route
/ip route
:if ([/ip route find dst-address=0.0.0.0/0] = "") do={{
    add gateway=dynamic distance=1 comment="YourISP Default Route"
}}
"""
        
        config += """
:log info "WAN configuration completed"
"""
        return config
    
    def _generate_hotspot_config(self) -> str:
        """Generate Mikrotik Hotspot configuration"""
        
        portal_url = f"{self.base_url}/hotspot/login/{self.tenant_subdomain}"
        
        return f"""
# Hotspot Configuration
:log info "Configuring Hotspot..."

# Download hotspot files
/tool fetch url="{self.base_url}/static/hotspot/login.html" dst-path=hotspot/login.html mode=https


# Create hotspot profile
/ip hotspot profile
:if ([/ip hotspot profile find name="yourisp-profile"] = "") do={{
    add name=yourisp-profile \\
        hotspot-address=172.20.0.1 \\
        dns-name=portal.yourisp.com \\
        login-by=http-pap,cookie,mac-cookie \\
        http-cookie-lifetime=1w \\
        use-radius=yes \\
        radius-accounting=yes \\
        nas-port-type=wireless-802.11
}} else={{
    /ip hotspot profile set [find name="yourisp-profile"] \\
        hotspot-address=172.20.0.1 \\
        use-radius=yes \\
        radius-accounting=yes
}}

# Create hotspot server
/ip hotspot
:if ([/ip hotspot find name="yourisp-hotspot"] = "") do={{
    add name=yourisp-hotspot \\
        interface=yourisp-bridge \\
        address-pool=yourisp-pool \\
        profile=yourisp-profile \\
        disabled=no
}}

:log info "Hotspot configured"
"""
    
    def _generate_pppoe_config(self) -> str:
        """Generate PPPoE server configuration"""
        
        return """
# PPPoE Server Configuration
:log info "Configuring PPPoE server..."

# PPP Profile
/ppp profile
:if ([/ppp profile find name="yourisp-pppoe-profile"] = "") do={{
    add name=yourisp-pppoe-profile \\
        local-address=192.40.1.1 \\
        use-compression=no \\
        use-encryption=no \\
        change-tcp-mss=yes \\
        only-one=yes \\
        comment="YourISP PPPoE Profile"
}} else={{
    /ppp profile set [find name="yourisp-pppoe-profile"] \\
        local-address=192.40.1.1
}}

# PPPoE Server
/interface pppoe-server server
:if ([/interface pppoe-server server find interface="yourisp-bridge"] = "") do={{
    add interface=yourisp-bridge \\
        service-name=yourisp-pppoe \\
        default-profile=yourisp-pppoe-profile \\
        authentication=pap \\
        disabled=no
}}

:log info "PPPoE server configured"
"""
    
    def _generate_radius_config(self) -> str:
        """Generate RADIUS configuration"""
        
        # In production, RADIUS server would be accessible via OpenVPN (10.10.0.1)
        # For local testing, use your local server IP
        radius_server = "10.10.0.1"  # Change to your local IP for testing
        
        return f"""
# RADIUS Configuration
:log info "Configuring RADIUS authentication..."

# Add RADIUS server
/radius
:if ([/radius find address={radius_server}] = "") do={{
    add address={radius_server} \\
        secret={self.router.shared_secret} \\
        service=ppp,hotspot,login \\
        timeout=3s
}} else={{
    /radius set [find address={radius_server}] \\
        secret={self.router.shared_secret} \\
        service=ppp,hotspot,login
}}

# Enable RADIUS for PPP
/ppp aaa
set use-radius=yes

:log info "RADIUS configured"
"""
    
    def _generate_firewall_config(self) -> str:
        """Generate firewall rules"""
        
        return """
# Firewall Configuration
:log info "Configuring firewall..."

# Accept traffic from YourISP management
/ip firewall filter
:if ([/ip firewall filter find comment="yourisp-management"] = "") do={{
    add chain=input \\
        src-address=10.10.0.0/24 \\
        action=accept \\
        comment="yourisp-management" \\
        place-before=0
}}

# Accept traffic from LAN
:if ([/ip firewall filter find comment="yourisp-lan-input"] = "") do={{
    add chain=input \\
        in-interface-list=LAN \\
        action=accept \\
        comment="yourisp-lan-input"
}}

# Allow established connections
:if ([/ip firewall filter find comment="accept-established"] = "") do={{
    add chain=input \\
        connection-state=established,related \\
        action=accept \\
        comment="accept-established"
}}

# Drop invalid
:if ([/ip firewall filter find comment="drop-invalid"] = "") do={{
    add chain=input \\
        connection-state=invalid \\
        action=drop \\
        comment="drop-invalid"
}}

# Allow forwarding from LAN to WAN
:if ([/ip firewall filter find comment="lan-to-wan"] = "") do={{
    add chain=forward \\
        in-interface-list=LAN \\
        out-interface-list=WAN \\
        action=accept \\
        comment="lan-to-wan"
}}

# Allow established/related back to LAN
:if ([/ip firewall filter find comment="wan-to-lan-established"] = "") do={{
    add chain=forward \\
        in-interface-list=WAN \\
        out-interface-list=LAN \\
        connection-state=established,related \\
        action=accept \\
        comment="wan-to-lan-established"
}}

# Drop all other WAN to LAN traffic
:if ([/ip firewall filter find comment="drop-wan-to-lan"] = "") do={{
    add chain=forward \\
        in-interface-list=WAN \\
        out-interface-list=LAN \\
        action=drop \\
        comment="drop-wan-to-lan"
}}

# NAT for internet sharing (masquerade)
/ip firewall nat
:if ([/ip firewall nat find comment="yourisp-masquerade"] = "") do={{
    add chain=srcnat \\
        out-interface-list=WAN \\
        action=masquerade \\
        comment="yourisp-masquerade"
}}

# Destination NAT for hotspot redirect (captive portal)
:if ([/ip firewall nat find comment="hotspot-redirect"] = "") do={{
    add chain=dstnat \\
        protocol=tcp \\
        dst-port=80 \\
        in-interface=yourisp-bridge \\
        action=redirect \\
        to-ports=80 \\
        comment="hotspot-redirect"
}}

:log info "Firewall configured"
"""
    
    def _generate_walled_garden(self) -> str:
        """Generate walled garden for portal access"""
        
        portal_domain = f"{self.tenant_subdomain}.yourisp.com"
        
        return f"""
# Walled Garden Configuration
:log info "Configuring walled garden..."

/ip hotspot walled-garden
:if ([/ip hotspot walled-garden find dst-host="{portal_domain}"] = "") do={{
    add dst-host={portal_domain} \\
        action=allow \\
        comment="YourISP Portal"
}}

:if ([/ip hotspot walled-garden find dst-host="*.yourisp.com"] = "") do={{
    add dst-host=*.yourisp.com \\
        action=allow \\
        comment="YourISP Domain"
}}

# Allow API access
:if ([/ip hotspot walled-garden find dst-host="api.yourisp.com"] = "") do={{
    add dst-host=api.yourisp.com \\
        action=allow \\
        comment="YourISP API"
}}

# Allow DNS for hotspot users
:if ([/ip hotspot walled-garden find dst-host="8.8.8.8"] = "") do={{
    add dst-host=8.8.8.8 \\
        action=allow \\
        comment="Google DNS"
}}

:if ([/ip hotspot walled-garden find dst-host="8.8.4.4"] = "") do={{
    add dst-host=8.8.4.4 \\
        action=allow \\
        comment="Google DNS"
}}

:log info "Walled garden configured"
"""
    
    def _generate_dhcp_dns_config(self) -> str:
        """Generate DHCP and DNS configuration"""
        
        return """
# DHCP & DNS Configuration
:log info "Configuring DHCP and DNS..."

# DHCP Server
/ip dhcp-server
:if ([/ip dhcp-server find name="yourisp-dhcp"] = "") do={{
    add name=yourisp-dhcp \\
        interface=yourisp-bridge \\
        address-pool=yourisp-pool \\
        lease-time=1h \\
        disabled=no
}}

# DHCP Network
/ip dhcp-server network
:if ([/ip dhcp-server network find address~"172.20.0.0/16"] = "") do={{
    add address=172.20.0.0/16 \\
        gateway=172.20.0.1 \\
        dns-server=172.20.0.1,8.8.8.8,8.8.4.4 \\
        comment="YourISP DHCP Network"
}}

# DNS Server
/ip dns
set allow-remote-requests=yes
set servers=8.8.8.8,8.8.4.4
set cache-size=2048KiB
set cache-max-ttl=1w

# Static DNS entries
/ip dns static
:if ([/ip dns static find name="portal.yourisp.com"] = "") do={{
    add name=portal.yourisp.com address=172.20.0.1
}}

:if ([/ip dns static find name="api.yourisp.com"] = "") do={{
    add name=api.yourisp.com address=172.20.0.1
}}

:if ([/ip dns static find name="*.yourisp.com"] = "") do={{
    add name="*.yourisp.com" address=172.20.0.1
}}

:log info "DHCP and DNS configured"
"""
    
    def _generate_heartbeat_scheduler(self) -> str:
        """Generate scheduler for heartbeat/status updates"""
        
        return f"""
# Heartbeat Scheduler
:log info "Setting up heartbeat scheduler..."

# Create heartbeat script
/system script
:if ([/system script find name="yourisp-heartbeat"] != "") do={{
    /system script remove [find name="yourisp-heartbeat"]
}}

add name=yourisp-heartbeat source={{
    :local authKey "{self.router.auth_key}"
    :local apiUrl "{self.base_url}/api/v1/network/routers/heartbeat/"
    
    # Get router stats
    :local cpuLoad [/system resource get cpu-load]
    :local freeMemory [/system resource get free-memory]
    :local uptime [/system resource get uptime]
    
    # Get active users count
    :local hotspotActive [/ip hotspot active print count-only]
    :local pppoeActive [/ppp active print count-only where service="pppoe"]
    :local totalActive ($hotspotActive + $pppoeActive)
    
    # Get interface status
    :local wanStatus "unknown"
    :local lanStatus "unknown"
    
    :if ([/interface list member find list=WAN] != "") do={{
        :local wanCount 0
        :local wanUpCount 0
        
        :foreach wanMember in=[/interface list member find list=WAN] do={{
            :set wanCount ($wanCount + 1)
            :local ifaceName [/interface list member get $wanMember interface]
            :if ([/interface get [find name=$ifaceName] running] = "true") do={{
                :set wanUpCount ($wanUpCount + 1)
            }}
        }}
        
        :set wanStatus ($wanUpCount . "/" . $wanCount . " up")
    }}
    
    :if ([/interface list member find list=LAN] != "") do={{
        :local lanCount 0
        :local lanUpCount 0
        
        :foreach lanMember in=[/interface list member find list=LAN] do={{
            :set lanCount ($lanCount + 1)
            :local ifaceName [/interface list member get $lanMember interface]
            :if ([/interface get [find name=$ifaceName] running] = "true") do={{
                :set lanUpCount ($lanUpCount + 1)
            }}
        }}
        
        :set lanStatus ($lanUpCount . "/" . $lanCount . " up")
    }}
    
    # Send heartbeat
    /tool fetch url=$apiUrl \\
        http-method=post \\
        http-header-field="Content-Type: application/json" \\
        http-data="{{\\"auth_key\\":\\"$authKey\\",\\"status\\":\\"online\\",\\"cpu_load\\":\\"$cpuLoad\\",\\"free_memory\\":\\"$freeMemory\\",\\"uptime\\":\\"$uptime\\",\\"active_users\\":$totalActive,\\"wan_status\\":\\"$wanStatus\\",\\"lan_status\\":\\"$lanStatus\\"}}" \\
        mode=https \\
        keep-result=no
    
    :log info "Heartbeat sent: $totalActive active users, WAN: $wanStatus, LAN: $lanStatus"
}}

# Create scheduler
/system scheduler
:if ([/system scheduler find name="yourisp-heartbeat"] != "") do={{
    /system scheduler remove [find name="yourisp-heartbeat"]
}}

add name=yourisp-heartbeat \\
    on-event=yourisp-heartbeat \\
    interval=5m \\
    comment="YourISP Status Updates"

:log info "Heartbeat scheduler created"
"""
    
    def _generate_vpn_password(self) -> str:
        """Generate secure VPN password"""
        return secrets.token_urlsafe(16)
    
    def _get_timestamp(self) -> str:
        """Get current timestamp"""
        from django.utils import timezone
        return timezone.now().strftime("%Y-%m-%d %H:%M:%S")
    
    def _get_wan_interfaces(self) -> list:
        """Get list of WAN interfaces from router configuration"""
        if hasattr(self.router, 'wan_interface') and self.router.wan_interface:
            # Split comma-separated interfaces and clean up
            interfaces = [iface.strip() for iface in self.router.wan_interface.split(',')]
            return [iface for iface in interfaces if iface]
        return []
    
    def generate_download_url(self) -> str:
        """Generate URL for script download"""
        return f"{self.base_url}/api/v1/network/routers/{self.router.id}/config/?auth_key={self.router.auth_key}&version=7"
    
    def generate_one_liner(self) -> str:
        """Generate one-line installation command"""
        url = self.generate_download_url()
        return f'/tool fetch url="{url}" dst-path=yourisp-config.rsc mode=https; :delay 2s; /import yourisp-config.rsc;'