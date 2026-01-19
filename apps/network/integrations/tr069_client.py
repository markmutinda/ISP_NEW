# apps/network/integrations/tr069_client.py
import requests
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Any
import logging
from datetime import datetime
import uuid

logger = logging.getLogger(__name__)


class TR069Client:
    """TR-069 ACS Client"""
    
    def __init__(self, acs_config):
        self.acs_config = acs_config
        self.base_url = acs_config.acs_url
        self.auth = (acs_config.acs_username, acs_config.acs_password) if acs_config.acs_username else None
        self.headers = {
            'Content-Type': 'application/xml',
            'SOAPAction': '',
        }
    
    def _build_soap_envelope(self, body_content: str) -> str:
        """Build SOAP envelope for TR-069 requests"""
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
    <soapenv:Header/>
    <soapenv:Body>
        {body_content}
    </soapenv:Body>
</soapenv:Envelope>"""
    
    def _make_request(self, method: str, body: str) -> Dict[str, Any]:
        """Make SOAP request to ACS"""
        try:
            envelope = self._build_soap_envelope(body)
            
            response = requests.post(
                self.base_url,
                data=envelope,
                headers=self.headers,
                auth=self.auth,
                timeout=30
            )
            
            response.raise_for_status()
            
            # Parse XML response
            root = ET.fromstring(response.text)
            namespaces = {
                'soapenv': 'http://schemas.xmlsoap.org/soap/envelope/',
                'cwmp': 'urn:dslforum-org:cwmp-1-0'
            }
            
            # Extract response data
            result = {}
            for elem in root.findall('.//cwmp:*', namespaces):
                tag = elem.tag.split('}')[-1]  # Remove namespace
                result[tag] = elem.text
            
            return result
            
        except requests.exceptions.RequestException as e:
            logger.error(f"TR-069 request failed: {str(e)}")
            raise
        except ET.ParseError as e:
            logger.error(f"Failed to parse TR-069 response: {str(e)}")
            raise
    
    def inform(self, cpe_device) -> Dict[str, Any]:
        """Handle Inform message from CPE (typically called by ACS, not client)"""
        # This would typically be implemented on the ACS server side
        # For client-side operations, we focus on RPC methods
        pass
    
    def get_parameter_values(self, cpe_device, parameter_names: List[str] = None) -> Dict[str, Any]:
        """Get parameter values from CPE"""
        if parameter_names is None:
            # Get all parameters
            parameter_names = ["InternetGatewayDevice."]
        
        param_list = '\n'.join([f'        <string>{param}</string>' for param in parameter_names])
        
        body = f"""<cwmp:GetParameterValues>
    <ParameterNames soapenc:arrayType="xsd:string[{len(parameter_names)}]">
{param_list}
    </ParameterNames>
</cwmp:GetParameterValues>"""
        
        return self._make_request('GetParameterValues', body)
    
    def set_parameter_values(self, cpe_device, parameters: Dict[str, str]) -> Dict[str, Any]:
        """Set parameter values on CPE"""
        param_list = ''
        for name, value in parameters.items():
            param_list += f"""
        <SetParameterValuesStruct>
            <Name>{name}</Name>
            <Value xsi:type="xsd:string">{value}</Value>
        </SetParameterValuesStruct>"""
        
        body = f"""<cwmp:SetParameterValues>
    <ParameterList soapenc:arrayType="cwmp:SetParameterValuesStruct[{len(parameters)}]">
{param_list}
    </ParameterList>
    <ParameterKey>{str(uuid.uuid4())}</ParameterKey>
</cwmp:SetParameterValues>"""
        
        return self._make_request('SetParameterValues', body)
    
    def reboot_device(self, cpe_device) -> Dict[str, Any]:
        """Reboot CPE device"""
        body = """<cwmp:Reboot>
    <CommandKey>RebootCommand</CommandKey>
</cwmp:Reboot>"""
        
        return self._make_request('Reboot', body)
    
    def factory_reset(self, cpe_device) -> Dict[str, Any]:
        """Factory reset CPE device"""
        body = """<cwmp:FactoryReset/>"""
        
        return self._make_request('FactoryReset', body)
    
    def download(self, cpe_device, url: str, file_type: str = '1 Firmware Upgrade Image') -> Dict[str, Any]:
        """Initiate firmware/download"""
        body = f"""<cwmp:Download>
    <CommandKey>DownloadCommand</CommandKey>
    <FileType>{file_type}</FileType>
    <URL>{url}</URL>
    <Username>{cpe_device.acs_config.cpe_username}</Username>
    <Password>{cpe_device.acs_config.cpe_password}</Password>
    <FileSize>0</FileSize>
    <TargetFileName>firmware.bin</TargetFileName>
    <DelaySeconds>0</DelaySeconds>
    <SuccessURL/>
    <FailureURL/>
</cwmp:Download>"""
        
        return self._make_request('Download', body)
    
    def upload(self, cpe_device, file_type: str = '1 Vendor Configuration File') -> Dict[str, Any]:
        """Initiate file upload from CPE"""
        body = f"""<cwmp:Upload>
    <CommandKey>UploadCommand</CommandKey>
    <FileType>{file_type}</FileType>
    <URL>{cpe_device.acs_config.connection_request_url}</URL>
    <Username>{cpe_device.acs_config.cpe_username}</Username>
    <Password>{cpe_device.acs_config.cpe_password}</Password>
    <DelaySeconds>0</DelaySeconds>
</cwmp:Upload>"""
        
        return self._make_request('Upload', body)
    
    def get_rpc_methods(self, cpe_device) -> Dict[str, Any]:
        """Get supported RPC methods from CPE"""
        body = """<cwmp:GetRPCMethods/>"""
        
        return self._make_request('GetRPCMethods', body)
    
    def provision_device(self, cpe_device) -> Dict[str, Any]:
        """Provision CPE device with configuration"""
        try:
            # Build provisioning parameters based on device type and service
            parameters = {
                'InternetGatewayDevice.DeviceInfo.ProvisioningCode': 'ISP-PROV-001',
                'InternetGatewayDevice.ManagementServer.ConnectionRequestURL': 
                    cpe_device.acs_config.connection_request_url,
                'InternetGatewayDevice.ManagementServer.PeriodicInformInterval': 
                    str(cpe_device.acs_config.periodic_interval),
            }
            
            # Add service-specific parameters
            if cpe_device.service_connection:
                # Add bandwidth parameters
                parameters.update({
                    'InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.MaxBitRateDown': 
                        str(cpe_device.service_connection.plan.speed_down * 1000),  # Convert to kbps
                    'InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.MaxBitRateUp': 
                        str(cpe_device.service_connection.plan.speed_up * 1000),
                })
            
            # Set parameters
            result = self.set_parameter_values(cpe_device, parameters)
            
            # Reboot to apply changes
            reboot_result = self.reboot_device(cpe_device)
            
            return {
                'provisioning': result,
                'reboot': reboot_result,
                'parameters_set': len(parameters)
            }
            
        except Exception as e:
            logger.error(f"Failed to provision device {cpe_device.serial_number}: {str(e)}")
            raise
    
    def get_device_status(self, cpe_device) -> Dict[str, Any]:
        """Get comprehensive device status"""
        try:
            # Get basic device info parameters
            status_params = [
                'InternetGatewayDevice.DeviceInfo.SoftwareVersion',
                'InternetGatewayDevice.DeviceInfo.HardwareVersion',
                'InternetGatewayDevice.DeviceInfo.UpTime',
                'InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.ExternalIPAddress',
                'InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceIPAddress',
            ]
            
            status = self.get_parameter_values(cpe_device, status_params)
            
            return {
                'software_version': status.get('SoftwareVersion', 'Unknown'),
                'hardware_version': status.get('HardwareVersion', 'Unknown'),
                'uptime': status.get('UpTime', 'Unknown'),
                'wan_ip': status.get('ExternalIPAddress', 'Unknown'),
                'lan_ip': status.get('IPInterfaceIPAddress', 'Unknown'),
                'last_check': datetime.now().isoformat(),
            }
            
        except Exception as e:
            logger.error(f"Failed to get device status for {cpe_device.serial_number}: {str(e)}")
            raise
    
    def sync_parameters(self, cpe_device) -> Dict[str, str]:
        """Sync all parameters from CPE device"""
        try:
            # Get all parameters (this is simplified - in reality you'd need to navigate the tree)
            # Start with common parameters
            common_params = [
                'InternetGatewayDevice.DeviceInfo.',
                'InternetGatewayDevice.ManagementServer.',
                'InternetGatewayDevice.WANDevice.1.',
                'InternetGatewayDevice.LANDevice.1.',
            ]
            
            all_parameters = {}
            
            for base_param in common_params:
                try:
                    params = self.get_parameter_values(cpe_device, [base_param])
                    # Parse and extract parameters (simplified)
                    for key, value in params.items():
                        if key.startswith(base_param):
                            all_parameters[key] = value
                except:
                    continue
            
            return all_parameters
            
        except Exception as e:
            logger.error(f"Failed to sync parameters for {cpe_device.serial_number}: {str(e)}")
            raise
