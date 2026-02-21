import logging
from apps.network.integrations.mikrotik_api import MikrotikAPI

logger = logging.getLogger(__name__)

def sync_bridge_ports_to_router(router, desired_ports: list) -> dict:
    """
    Safely adds or removes physical ports from the 'netily-bridge'.
    It calculates the difference between what is already on the bridge
    and what the admin selected on the dashboard.
    """
    api_wrapper = MikrotikAPI(router)
    if not api_wrapper.connect():
        return {'success': False, 'message': 'Cannot connect to router. Ensure it is online.'}

    try:
        # 1. Get all current ports attached to any bridge
        bridge_ports = list(api_wrapper._execute('/interface/bridge/port'))
        
        # Track which ports are currently on our specific bridge
        current_netily_ports = {}
        for p in bridge_ports:
            if p.get('bridge') == 'netily-bridge':
                current_netily_ports[p.get('interface')] = p.get('.id')
        
        current_set = set(current_netily_ports.keys())
        desired_set = set(desired_ports)
        
        # Calculate what needs to be added and removed
        to_remove = current_set - desired_set
        to_add = desired_set - current_set
        
        steps = []
        
        # 2. Remove ports that the admin unchecked
        for iface in to_remove:
            port_id = current_netily_ports[iface]
            api_wrapper._execute('/interface/bridge/port', remove={'.id': port_id})
            steps.append(f"Removed {iface} from bridge")
            
        # 3. Add new ports that the admin checked
        for iface in to_add:
            # Failsafe: If the port is attached to a default MikroTik bridge, remove it first
            for p in bridge_ports:
                if p.get('interface') == iface and p.get('bridge') != 'netily-bridge':
                    api_wrapper._execute('/interface/bridge/port', remove={'.id': p.get('.id')})
            
            # Add to our bridge
            api_wrapper._execute('/interface/bridge/port', add={
                'bridge': 'netily-bridge',
                'interface': iface
            })
            steps.append(f"Added {iface} to bridge")

        return {
            'success': True,
            'message': 'Ports synchronized successfully',
            'added': list(to_add),
            'removed': list(to_remove),
            'steps': steps
        }
    except Exception as e:
        logger.error(f"Bridge Sync Error on router {router.name}: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        api_wrapper.disconnect()