# apps/network/services/ipam_calculator.py

import ipaddress
import logging

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────
# VALID CHOICES (mirrors the frontend dropdowns)
# ────────────────────────────────────────────────────────────────

VALID_BASE_IPS = [
    "172.12.0.1",    # Recommended for Hotspot
    "192.168.88.1",  # MikroTik Default
    "192.168.0.1",   # Common Home Router
    "10.0.0.1",      # Enterprise Network
    "172.16.0.1",    # Private Network
    "192.168.100.1", # Alternative
]

VALID_CIDRS = [8, 12, 16, 20, 24, 28]

CIDR_HOST_COUNTS = {
    8:  16_777_214,
    12: 1_048_574,
    16: 65_534,
    20: 4_094,
    24: 254,
    28: 14,
}


def validate_hotspot_ipam_input(base_ip: str, cidr: int) -> dict:
    """
    Validates that the user-selected base_ip and CIDR are within
    the allowed dropdown values. Returns an error dict or None.
    """
    errors = {}

    if base_ip not in VALID_BASE_IPS:
        errors['base_ip'] = (
            f"Invalid IP address '{base_ip}'. "
            f"Must be one of: {', '.join(VALID_BASE_IPS)}"
        )

    if cidr not in VALID_CIDRS:
        errors['subnet_cidr'] = (
            f"Invalid CIDR '{cidr}'. "
            f"Must be one of: {', '.join(str(c) for c in VALID_CIDRS)}"
        )

    return errors if errors else None


def calculate_mikrotik_hotspot_network(base_ip: str, cidr: int) -> dict:
    """
    Takes an IP and CIDR (e.g., 172.12.0.1 and 16) and generates the exact
    strings needed for the MikroTik RouterOS API.

    Returns:
        {
            "interface_address": "172.12.0.1/16",
            "network": "172.12.0.0",
            "gateway": "172.12.0.1",
            "pool_range": "172.12.0.10-172.12.255.254",
            "broadcast": "172.12.255.255",
            "total_hosts": 65534,
            "usable_hosts": 65524,   # minus 10 reserved IPs at start
            "subnet_mask": "255.255.0.0",
        }
    """
    # Create the network object (strict=False ignores host bits)
    network_string = f"{base_ip}/{cidr}"
    net = ipaddress.ip_network(network_string, strict=False)

    # 1. Gateway IP (Always the base_ip the user selected)
    gateway_ip = base_ip

    # 2. Network Address (e.g., 172.12.0.0)
    network_address = str(net.network_address)

    # 3. Broadcast Address (e.g., 172.12.255.255)
    broadcast_address = str(net.broadcast_address)

    # 4. IP Pool Range
    #    Start the pool 10 IPs after the network address to leave room for
    #    the gateway + static devices (e.g., 172.12.0.10)
    pool_start = str(net.network_address + 10)

    #    End the pool right before the broadcast address (e.g., 172.12.255.254)
    pool_end = str(net.broadcast_address - 1)
    pool_range = f"{pool_start}-{pool_end}"

    # 5. Total hosts
    total_hosts = net.num_addresses - 2  # exclude network + broadcast
    usable_hosts = total_hosts - 10      # minus the 10 reserved IPs

    # 6. Subnet mask in dotted notation
    subnet_mask = str(net.netmask)

    result = {
        "interface_address": f"{gateway_ip}/{cidr}",  # e.g. "172.12.0.1/16"
        "network": network_address,                     # e.g. "172.12.0.0"
        "gateway": gateway_ip,                          # e.g. "172.12.0.1"
        "pool_range": pool_range,                       # e.g. "172.12.0.10-172.12.255.254"
        "broadcast": broadcast_address,                 # e.g. "172.12.255.255"
        "total_hosts": total_hosts,
        "usable_hosts": max(usable_hosts, 0),
        "subnet_mask": subnet_mask,                     # e.g. "255.255.0.0"
    }

    logger.info(f"IPAM calculated for {network_string}: gateway={gateway_ip}, pool={pool_range}")
    return result
