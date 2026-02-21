# apps/network/services/mikrotik_ipam_sync.py

"""
MikroTik Hotspot IPAM Sync Service
===================================

Applies hotspot IP/subnet changes to a live MikroTik router via the RouterOS API.

CRITICAL EXECUTION ORDER:
  1. Update IP Pool         → so DHCP doesn't break
  2. Update Bridge IP       → netily-bridge gets new address
  3. Update DHCP Network    → DHCP server gets new gateway/subnet
  4. Update Hotspot Profile → captive portal uses new gateway
  5. Clear DHCP Leases      → kick connected devices to get new IPs

This sequence guarantees 0 seconds of backend downtime. The Walled Garden,
WPA2 passwords, and FreeRADIUS auth are completely unaffected because RADIUS
uses the router's VPN IP, not the Hotspot Subnet.
"""

import logging
from apps.network.services.ipam_calculator import calculate_mikrotik_hotspot_network

logger = logging.getLogger(__name__)


def sync_hotspot_ipam_to_router(router, new_base_ip: str, new_cidr: int) -> dict:
    """
    Applies hotspot IPAM changes to a live MikroTik router.

    Args:
        router: Router model instance (must be online with VPN)
        new_base_ip: New gateway IP (e.g., "172.12.0.1")
        new_cidr: New CIDR prefix (e.g., 16)

    Returns:
        dict with 'success', 'message', and optional 'details' or 'error'
    """
    import apps.network.integrations.mikrotik_api as mikrotik_api_module

    # 1. Calculate the new network math
    math = calculate_mikrotik_hotspot_network(new_base_ip, new_cidr)

    logger.info(
        f"[IPAM SYNC] Router '{router.name}' (ID={router.id}): "
        f"Applying {math['interface_address']} | Pool: {math['pool_range']}"
    )

    # 2. Connect to the MikroTik API via VPN
    mikrotik = mikrotik_api_module.MikrotikAPI(router)
    if not mikrotik.connect():
        return {
            'success': False,
            'error': 'connection_failed',
            'message': f"Cannot connect to router '{router.name}'. Ensure VPN tunnel is active.",
        }

    steps_completed = []
    try:
        # ══════════════════════════════════════════════════════════════
        # STEP 1: Update the IP Pool (Must be done FIRST so DHCP doesn't break)
        # ══════════════════════════════════════════════════════════════
        try:
            pools = list(mikrotik._execute('/ip/pool'))
            pool_id = None
            for pool in pools:
                if pool.get('name', '') in ('hotspot-pool', 'hs-pool-1', 'netily-pool', 'netily-hotspot-pool'):
                    pool_id = pool['.id']
                    break
            
            if pool_id:
                # FIX: Changed 'set=' to 'update='
                mikrotik._execute('/ip/pool', update={'.id': pool_id, 'ranges': math['pool_range']})
                steps_completed.append('ip_pool_updated')
                logger.info(f"  [STEP 1] IP Pool updated: {math['pool_range']}")
            else:
                # No existing pool found — create one
                mikrotik._execute('/ip/pool', add={
                    'name': 'netily-pool',
                    'ranges': math['pool_range'],
                })
                steps_completed.append('ip_pool_created')
                logger.info(f"  [STEP 1] IP Pool created: netily-pool = {math['pool_range']}")

        except Exception as e:
            logger.error(f"  [STEP 1] IP Pool update failed: {e}")
            raise Exception(f"Step 1 (IP Pool) failed: {e}")

        # ══════════════════════════════════════════════════════════════
        # STEP 2: Update the Bridge Interface IP
        # ══════════════════════════════════════════════════════════════
        try:
            addresses = list(mikrotik._execute('/ip/address'))
            addr_id = None
            for addr in addresses:
                iface = addr.get('interface', '')
                if iface in ('hotspot-bridge', 'netily-bridge', 'bridge1'):
                    addr_id = addr['.id']
                    break

            if addr_id:
                # FIX: Changed 'set=' to 'update='
                mikrotik._execute('/ip/address', update={
                    '.id': addr_id,
                    'address': math['interface_address'],
                    'network': math['network'],
                })
                steps_completed.append('bridge_ip_updated')
                logger.info(f"  [STEP 2] Bridge IP updated: {math['interface_address']}")
            else:
                # Add a new IP address on the bridge
                mikrotik._execute('/ip/address', add={
                    'address': math['interface_address'],
                    'interface': 'netily-bridge',
                    'network': math['network'],
                })
                steps_completed.append('bridge_ip_created')
                logger.info(f"  [STEP 2] Bridge IP created on netily-bridge: {math['interface_address']}")

        except Exception as e:
            logger.error(f"  [STEP 2] Bridge IP update failed: {e}")
            raise Exception(f"Step 2 (Bridge IP) failed: {e}")

        # ══════════════════════════════════════════════════════════════
        # STEP 3: Update the DHCP Server Network
        # ══════════════════════════════════════════════════════════════
        try:
            dhcp_networks = list(mikrotik._execute('/ip/dhcp-server/network'))
            if dhcp_networks:
                old_net_id = dhcp_networks[0]['.id']
                # FIX: Changed 'set=' to 'update='
                mikrotik._execute('/ip/dhcp-server/network', update={
                    '.id': old_net_id,
                    'address': f"{math['network']}/{new_cidr}",
                    'gateway': math['gateway'],
                    'dns-server': '8.8.8.8,8.8.4.4',
                })
                steps_completed.append('dhcp_network_updated')
                logger.info(f"  [STEP 3] DHCP Network updated: {math['network']}/{new_cidr}")
            else:
                mikrotik._execute('/ip/dhcp-server/network', add={
                    'address': f"{math['network']}/{new_cidr}",
                    'gateway': math['gateway'],
                    'dns-server': '8.8.8.8,8.8.4.4',
                })
                steps_completed.append('dhcp_network_created')
                logger.info(f"  [STEP 3] DHCP Network created: {math['network']}/{new_cidr}")

        except Exception as e:
            logger.error(f"  [STEP 3] DHCP Network update failed: {e}")
            raise Exception(f"Step 3 (DHCP Network) failed: {e}")

        # ══════════════════════════════════════════════════════════════
        # STEP 4: Update the Hotspot Profile Address
        # ══════════════════════════════════════════════════════════════
        try:
            profiles = list(mikrotik._execute('/ip/hotspot/profile'))
            prof_id = None
            for prof in profiles:
                if prof.get('name', '') in ('hotspot-profile', 'hsprof1', 'netily-profile', 'default'):
                    prof_id = prof['.id']
                    break

            if prof_id:
                # FIX: Changed 'set=' to 'update='
                mikrotik._execute('/ip/hotspot/profile', update={
                    '.id': prof_id,
                    'hotspot-address': math['gateway'],
                })
                steps_completed.append('hotspot_profile_updated')
                logger.info(f"  [STEP 4] Hotspot Profile updated: hotspot-address={math['gateway']}")
            else:
                logger.warning(f"  [STEP 4] No hotspot profile found — skipping (non-fatal)")
                steps_completed.append('hotspot_profile_skipped')

        except Exception as e:
            logger.error(f"  [STEP 4] Hotspot Profile update failed: {e}")
            raise Exception(f"Step 4 (Hotspot Profile) failed: {e}")

        # ══════════════════════════════════════════════════════════════
        # STEP 5: Clear old DHCP Leases (Kick all devices)
        # ══════════════════════════════════════════════════════════════
        try:
            leases = list(mikrotik._execute('/ip/dhcp-server/lease'))
            leases_cleared = 0
            for lease in leases:
                try:
                    mikrotik._execute('/ip/dhcp-server/lease', remove={'.id': lease['.id']})
                    leases_cleared += 1
                except Exception:
                    pass  # Non-fatal, some leases may be static

            steps_completed.append('leases_cleared')
            logger.info(f"  [STEP 5] Cleared {leases_cleared} DHCP leases")

        except Exception as e:
            logger.warning(f"  [STEP 5] Lease clearing failed (non-fatal): {e}")
            steps_completed.append('leases_clear_failed')

    except Exception as e:
        logger.error(f"[IPAM SYNC] FAILED at router '{router.name}': {e}")
        return {
            'success': False,
            'error': 'sync_failed',
            'message': str(e),
            'steps_completed': steps_completed,
        }
    finally:
        mikrotik.disconnect()

    logger.info(
        f"[IPAM SYNC] SUCCESS for router '{router.name}': "
        f"{len(steps_completed)} steps completed"
    )

    return {
        'success': True,
        'message': f"Hotspot IPAM updated to {math['interface_address']} successfully",
        'details': {
            'interface_address': math['interface_address'],
            'network': math['network'],
            'gateway': math['gateway'],
            'pool_range': math['pool_range'],
            'broadcast': math['broadcast'],
            'total_hosts': math['total_hosts'],
            'usable_hosts': math['usable_hosts'],
            'subnet_mask': math['subnet_mask'],
            'steps_completed': steps_completed,
        }
    }