from django.conf import settings
from apps.network.models.router_models import Router
from django.utils import timezone

class MikrotikScriptGenerator:
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