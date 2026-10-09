"""
Business logic: enroll devices, push tasks, and pull full device state from
GenieACS's NBI. Vendor parameter paths vary — the TR098/TR181 paths below
cover the common cases; verify against your actual CPE fleet and extend
_PATHS as needed.
"""
import logging

from django.conf import settings
from django.db import connection
from django.utils import timezone

from ..models import CPEDevice, CPETaskLog
from . import device_credentials
from .genieacs_client import GenieACSClient, GenieACSError

logger = logging.getLogger(__name__)

_PATHS = {
    'tr181': {
        'root': 'Device.',
        'device_info': 'Device.DeviceInfo.',
        'uptime': 'Device.DeviceInfo.UpTime',
        'wan_ip': 'Device.IP.Interface.1.IPv4Address.1.IPAddress',
        'inform_interval': 'Device.ManagementServer.PeriodicInformInterval',
        'rx_power': 'Device.Optical.Interface.1.RXPower',
        'tx_power': 'Device.Optical.Interface.1.TXPower',
        'wifi_ssid_root': 'Device.WiFi.SSID.',
        'wifi_ap_root': 'Device.WiFi.AccessPoint.',
        'hosts_root': 'Device.Hosts.Host.',
    },
    'tr098': {
        'root': 'InternetGatewayDevice.',
        'device_info': 'InternetGatewayDevice.DeviceInfo.',
        'uptime': 'InternetGatewayDevice.DeviceInfo.UpTime',
        'wan_ip': 'InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.ExternalIPAddress',
        'inform_interval': 'InternetGatewayDevice.ManagementServer.PeriodicInformInterval',
        'rx_power': 'InternetGatewayDevice.WANDevice.1.X_GponInterafceConfig.RXPower',
        'tx_power': 'InternetGatewayDevice.WANDevice.1.X_GponInterafceConfig.TXPower',
        'wifi_ssid_root': 'InternetGatewayDevice.LANDevice.1.WLANConfiguration.',
        'hosts_root': 'InternetGatewayDevice.LANDevice.1.Hosts.Host.',
    },
}

OUI_VENDORS = {'00259E': 'Huawei', 'E48D8C': 'MikroTik'}


def _identity(raw):
    did = raw.get('_deviceId', {}) or {}
    oui = (did.get('_OUI') or '').upper()
    maker = (did.get('_Manufacturer') or '').strip()
    if not maker or maker.startswith('Technologies'):
        maker = OUI_VENDORS.get(oui, maker)
    return {
        'oui': oui,
        'product_class': did.get('_ProductClass') or '',
        'manufacturer': maker,
        'model_name': did.get('_ProductClass') or '',
    }


class ProvisioningError(Exception):
    def __init__(self, message, code='error'):
        super().__init__(message)
        self.code = code


def _val(node):
    return node.get('_value') if isinstance(node, dict) else None


def _flatten_tree(tree: dict) -> dict:
    """
    Convert GenieACS NBI's nested tree into {'A.B.C': {'_value': ...}} leaf entries,
    which is the shape every _extract_* helper and _PATHS lookup expects.
    Iterative (no recursion limit) and single pass, so it stays cheap on large Huawei trees.
    """
    flat = {}
    stack = [(k, v) for k, v in tree.items() if not k.startswith('_') and isinstance(v, dict)]
    while stack:
        path, node = stack.pop()
        if '_value' in node:
            flat[path] = node
        for k, v in node.items():
            if not k.startswith('_') and isinstance(v, dict):
                stack.append((f'{path}.{k}', v))
    return flat


def _find_wan_ip(remote):
    for key, node in remote.items():
        if 'WANDevice' in key and key.endswith('.ExternalIPAddress'):
            ip = _val(node)
            if ip and ip != '0.0.0.0' and not ip.startswith('169.254'):
                return ip
    return None


def _log_task(device, task_type, params, user=None):
    return CPETaskLog.objects.create(device=device, task_type=task_type, params=params, status='queued', created_by=user)


def _finish_task(log, delivered, error=None):
    log.status = 'completed' if delivered else 'queued'
    if error:
        log.status = 'failed'
        log.error_message = str(error)
    log.completed_at = timezone.now() if delivered or error else None
    log.save(update_fields=['status', 'error_message', 'completed_at', 'updated_at'])
    return log


def _require_device_id(device: CPEDevice) -> str:
    if not device.genieacs_device_id:
        raise ProvisioningError("This device hasn't checked in yet.", code='not_seen')
    return device.genieacs_device_id


def enroll_device(*, serial_number: str, label: str = '', customer=None, service_connection=None) -> CPEDevice:
    serial_number = serial_number.strip()
    if CPEDevice.objects.filter(serial_number__iexact=serial_number).exists():
        raise ProvisioningError("This serial is already in your list.", code='duplicate')

    from apps.core.models import Tr069DeviceIndex
    from django_tenants.utils import get_public_schema_name, schema_context

    with schema_context(get_public_schema_name()):
        claimed = Tr069DeviceIndex.objects.filter(
            serial_number__iexact=serial_number, is_active=True
        ).exclude(tenant_schema=connection.schema_name).exists()
    if claimed:
        raise ProvisioningError(
            "This serial is already registered on the platform.", code='claimed'
        )

    username, password = device_credentials.generate_credentials()
    device = CPEDevice.objects.create(
        serial_number=serial_number,
        label=label or '',
        customer=customer,
        service_connection=service_connection,
        acs_username=username,
        acs_password=password,
        status='never_seen',
    )
    device_credentials.sync_index_entry(device)
    return device


def rotate_credentials(device: CPEDevice) -> CPEDevice:
    username, password = device_credentials.generate_credentials()
    device.acs_username = username
    device.acs_password = password
    device.save(update_fields=['acs_username', 'acs_password', 'updated_at'])
    device_credentials.sync_index_entry(device)
    return device


def remove_device(device: CPEDevice):
    device_credentials.deactivate_index_entry(device.serial_number)
    if device.genieacs_device_id:
        try:
            GenieACSClient().delete_device(device.genieacs_device_id)
        except GenieACSError as exc:
            logger.warning("Could not delete %s from GenieACS: %s", device.genieacs_device_id, exc)
    device.delete()


def reboot_device(device: CPEDevice, user=None) -> CPETaskLog:
    device_id = _require_device_id(device)
    log = _log_task(device, 'reboot', {}, user)
    try:
        delivered, _ = GenieACSClient().reboot(device_id)
        return _finish_task(log, delivered)
    except GenieACSError as exc:
        _finish_task(log, False, error=exc)
        raise ProvisioningError(str(exc), code='acs_error') from exc


def factory_reset_device(device: CPEDevice, user=None) -> CPETaskLog:
    device_id = _require_device_id(device)
    log = _log_task(device, 'factory_reset', {}, user)
    try:
        delivered, _ = GenieACSClient().factory_reset(device_id)
        _finish_task(log, delivered)
        if delivered:
            device.status = 'never_seen'
            device.save(update_fields=['status', 'updated_at'])
        return log
    except GenieACSError as exc:
        _finish_task(log, False, error=exc)
        raise ProvisioningError(str(exc), code='acs_error') from exc


def refresh_device(device: CPEDevice, user=None) -> CPETaskLog:
    device_id = _require_device_id(device)
    paths = _PATHS.get(device.data_model or 'tr181')
    objects = [paths['device_info'], paths['wifi_ssid_root'].rstrip('.'), paths['hosts_root'].rstrip('.')]
    log = _log_task(device, 'refresh', {}, user)
    try:
        delivered, _ = GenieACSClient().refresh_object(device_id, objects)
        _finish_task(log, delivered)
        return log
    except GenieACSError as exc:
        _finish_task(log, False, error=exc)
        raise ProvisioningError(str(exc), code='acs_error') from exc


def set_wifi(device: CPEDevice, networks: list[dict], user=None) -> CPETaskLog:
    if not device.has_connected:
        raise ProvisioningError("This device hasn't checked in yet.", code='not_seen')
    if not device.wifi_networks:
        raise ProvisioningError("We don't have this device's WiFi details yet.", code='not_ready')

    known = {n['id']: n for n in device.wifi_networks}
    parameter_values = []
    for entry in networks:
        net_id = entry.get('id')
        known_net = known.get(net_id)
        if not known_net:
            raise ProvisioningError(f"Unknown network id {net_id}.", code='not_ready')
        paths = known_net.get('_paths', {})

        if 'ssid' in entry and entry['ssid']:
            if not paths.get('ssid'):
                raise ProvisioningError("This device doesn't expose a writable network name.", code='unsupported')
            parameter_values.append((paths['ssid'], entry['ssid'], 'xsd:string'))

        if 'password' in entry and entry['password']:
            if not paths.get('password'):
                raise ProvisioningError("This device doesn't expose a writable WiFi password.", code='unsupported')
            parameter_values.append((paths['password'], entry['password'], 'xsd:string'))

        if 'enabled' in entry and paths.get('enable'):
            parameter_values.append((paths['enable'], 'true' if entry['enabled'] else 'false', 'xsd:boolean'))

    if not parameter_values:
        raise ProvisioningError("Nothing to change.", code='not_ready')

    device_id = _require_device_id(device)
    log = _log_task(device, 'set_wifi', {'networks': networks}, user)
    try:
        delivered, _ = GenieACSClient().set_parameter_values(device_id, parameter_values)
        _finish_task(log, delivered)
        return log
    except GenieACSError as exc:
        _finish_task(log, False, error=exc)
        raise ProvisioningError(str(exc), code='acs_error') from exc


def _detect_data_model(remote: dict) -> str:
    return 'tr181' if any(k.startswith('Device.') for k in remote.keys()) else 'tr098'


def _extract_wifi_networks(remote: dict, data_model: str) -> list[dict]:
    """Best-effort TR098/TR181 WiFi extraction. Vendors vary — adjust for your fleet."""
    networks = []
    paths = _PATHS[data_model]
    prefix = paths['wifi_ssid_root']

    # Collect instance numbers present under the SSID/WLANConfiguration table.
    instance_nums = set()
    for key in remote.keys():
        if key.startswith(prefix):
            rest = key[len(prefix):]
            idx = rest.split('.')[0]
            if idx.isdigit():
                instance_nums.add(idx)

    for idx in sorted(instance_nums, key=int):
        base = f"{prefix}{idx}."
        ssid = _val(remote.get(f"{base}SSID"))
        if ssid is None:
            continue
        enabled = _val(remote.get(f"{base}Enable"))
        channel = _val(remote.get(f"{base}Channel"))
        standard = _val(remote.get(f"{base}Standard") or remote.get(f"{base}RadioEnabled"))

        if data_model == 'tr181':
            ap_idx = idx
            ap_base = f"{paths['wifi_ap_root']}{ap_idx}."
            password_path = f"{ap_base}Security.KeyPassphrase"
        else:
            password_path = f"{base}PreSharedKey.1.KeyPassphrase"

        networks.append({
            'id': f"1-{idx}",
            'ssid': ssid,
            'enabled': bool(enabled) if enabled is not None else True,
            'band': '5GHz' if channel and int(str(channel) or 0) > 14 else '2.4GHz',
            'channel': channel,
            'standard': standard or '',
            'clients': 0,
            '_paths': {
                'ssid': f"{base}SSID",
                'password': password_path,
                'enable': f"{base}Enable",
            },
        })
    return networks


def _extract_hosts(remote: dict, data_model: str) -> list[dict]:
    hosts = []
    prefix = _PATHS[data_model]['hosts_root']
    instance_nums = set()
    for key in remote.keys():
        if key.startswith(prefix):
            rest = key[len(prefix):]
            idx = rest.split('.')[0]
            if idx.isdigit():
                instance_nums.add(idx)

    for idx in sorted(instance_nums, key=int):
        base = f"{prefix}{idx}."
        ip = _val(remote.get(f"{base}IPAddress"))
        if not ip:
            continue
        iface_type = _val(remote.get(f"{base}InterfaceType")) or ''
        hosts.append({
            'name': _val(remote.get(f"{base}HostName")) or ip,
            'ip': ip,
            'mac': _val(remote.get(f"{base}MACAddress")) or '',
            'type': 'wifi' if 'wifi' in iface_type.lower() or '802.11' in iface_type else 'ethernet',
            'active': bool(_val(remote.get(f"{base}Active"))),
        })
    return hosts


def sync_device_from_genieacs(device: CPEDevice) -> CPEDevice:
    """Pulls the full parameter tree from GenieACS NBI and updates our local record."""
    client = GenieACSClient()

    genieacs_id = device.genieacs_device_id
    if not genieacs_id:
        genieacs_id = client.find_device_id_by_serial(device.serial_number)
        if not genieacs_id:
            return device
        device.genieacs_device_id = genieacs_id

    raw = client.get_device(genieacs_id)
    if not raw:
        return device
    remote = _flatten_tree(raw)

    for k, v in _identity(raw).items():
        if v:
            setattr(device, k, v)

    data_model = _detect_data_model(remote)
    paths = _PATHS[data_model]

    model = _val(remote.get(f"{paths['device_info']}ModelName"))
    if model:
        device.model_name = model

    device.data_model = data_model
    device.software_version = _val(remote.get(f"{paths['device_info']}SoftwareVersion")) or device.software_version
    device.hardware_version = _val(remote.get(f"{paths['device_info']}HardwareVersion")) or device.hardware_version

    uptime = _val(remote.get(paths['uptime']))
    device.uptime_seconds = int(uptime) if uptime is not None else device.uptime_seconds

    wan_ip = _find_wan_ip(remote)
    device.wan_ip = wan_ip or device.wan_ip

    interval = _val(remote.get(paths['inform_interval']))
    device.inform_interval = int(interval) if interval is not None else device.inform_interval

    rx = _val(remote.get(paths['rx_power']))
    tx = _val(remote.get(paths['tx_power']))
    if rx is not None:
        try:
            device.rx_power_dbm = float(rx) / 100 if abs(float(rx)) > 100 else float(rx)  # some vendors send centi-dBm
        except (TypeError, ValueError):
            pass
    if tx is not None:
        try:
            device.tx_power_dbm = float(tx) / 100 if abs(float(tx)) > 100 else float(tx)
        except (TypeError, ValueError):
            pass

    device.wifi_networks = _extract_wifi_networks(remote, data_model)
    device.hosts = _extract_hosts(remote, data_model)

    threshold = getattr(settings, 'TR069_WEAK_SIGNAL_DBM', -27)
    device.weak_signal = bool(device.rx_power_dbm is not None and float(device.rx_power_dbm) < threshold)

    device.status = 'online'
    device.last_sync_at = timezone.now()
    device.save()

    device_credentials.sync_index_entry(device)
    return device