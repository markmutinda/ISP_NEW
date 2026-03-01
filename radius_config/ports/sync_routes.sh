#!/bin/sh
# ZERO-DOWNTIME IPTABLES SYNC
# 1. Create custom chains if they don't exist, or flush them to clear old rules
iptables -t nat -F NETILY_PRE 2>/dev/null || iptables -t nat -N NETILY_PRE
iptables -t nat -F NETILY_POST 2>/dev/null || iptables -t nat -N NETILY_POST

# 2. Link custom chains to main chains (fails silently if already linked)
iptables -t nat -C PREROUTING -i tun0 -j NETILY_PRE 2>/dev/null || iptables -t nat -I PREROUTING 1 -i tun0 -j NETILY_PRE
iptables -t nat -C POSTROUTING -j NETILY_POST 2>/dev/null || iptables -t nat -I POSTROUTING 1 -j NETILY_POST

# 3. Read map file and inject new rules live
MAP_FILE=/etc/openvpn/ports/tenant_ports.conf
if [ -f "$MAP_FILE" ]; then
    while read -r tenant auth acct; do
        case "$tenant" in ''|\#*) continue ;; esac
        
        # Get live IP of the container
        TENANT_IP=$(getent hosts netily_radius_$tenant | awk '{ print $1 }')
        
        if [ ! -z "$TENANT_IP" ]; then
            iptables -t nat -A NETILY_PRE -p udp --dport $auth -j DNAT --to-destination $TENANT_IP:1812
            iptables -t nat -A NETILY_PRE -p udp --dport $acct -j DNAT --to-destination $TENANT_IP:1813
            iptables -t nat -A NETILY_POST -d $TENANT_IP -p udp -j MASQUERADE
            echo "✅ Live-routed $tenant to $TENANT_IP"
        fi
    done < "$MAP_FILE"
fi
