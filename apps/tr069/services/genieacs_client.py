"""Thin wrapper around GenieACS's NBI REST API. Docs: https://genieacs.com/docs/api-reference/"""
import json
import logging
import urllib.parse

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class GenieACSError(Exception):
    pass


class GenieACSClient:
    def __init__(self):
        self.base_url = getattr(settings, 'GENIEACS_NBI_URL', 'http://genieacs:7557').rstrip('/')
        self.timeout = getattr(settings, 'TR069_CONNECTION_REQUEST_TIMEOUT', 15)

    def _url(self, path):
        return f"{self.base_url}{path}"

    def get_device(self, device_id: str):
        params = {'query': json.dumps({'_id': device_id})}
        resp = requests.get(self._url('/devices/'), params=params, timeout=self.timeout)
        resp.raise_for_status()
        devices = resp.json()
        return devices[0] if devices else None

    def find_device_id_by_serial(self, serial: str):
        params = {'query': json.dumps({'DeviceID.SerialNumber': serial}), 'projection': '_id'}
        resp = requests.get(self._url('/devices/'), params=params, timeout=self.timeout)
        resp.raise_for_status()
        devices = resp.json()
        return devices[0]['_id'] if devices else None

    def delete_device(self, device_id: str):
        resp = requests.delete(
            self._url(f'/devices/{urllib.parse.quote(device_id, safe="")}'), timeout=self.timeout
        )
        if resp.status_code not in (200, 202, 204, 404):
            raise GenieACSError(f"Failed to delete device {device_id}: {resp.text}")
        return True

    def push_task(self, device_id: str, task: dict):
        """
        Returns (delivered: bool, response_body: dict).
        delivered=True  -> GenieACS reached the device now (HTTP 200)
        delivered=False -> queued for the device's next Inform (HTTP 202)
        """
        resp = requests.post(
            self._url(f'/devices/{urllib.parse.quote(device_id, safe="")}/tasks'),
            params={'connection_request': ''},
            json=task,
            timeout=self.timeout,
        )
        if resp.status_code not in (200, 202):
            raise GenieACSError(f"Task push failed for {device_id}: {resp.status_code} {resp.text}")
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return resp.status_code == 200, body

    def reboot(self, device_id: str):
        return self.push_task(device_id, {'name': 'reboot'})

    def factory_reset(self, device_id: str):
        return self.push_task(device_id, {'name': 'factoryReset'})

    def set_parameter_values(self, device_id: str, parameter_values: list[tuple[str, str, str]]):
        return self.push_task(device_id, {
            'name': 'setParameterValues',
            'parameterValues': [list(pv) for pv in parameter_values],
        })

    def refresh_object(self, device_id: str, object_names: list[str]):
        delivered_any = False
        last_body = {}
        for obj in object_names:
            try:
                delivered, body = self.push_task(device_id, {'name': 'refreshObject', 'objectName': obj})
                delivered_any = delivered_any or delivered
                last_body = body
            except GenieACSError:
                continue
        return delivered_any, last_body