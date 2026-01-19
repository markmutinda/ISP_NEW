# apps/network/views/ipam_views.py
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Q, Count, Sum
from netaddr import IPNetwork, IPAddress as NetIPAddress
from rest_framework import serializers
from apps.network.models.ipam_models import (
    Subnet, VLAN, IPPool, IPAddress, DHCPRange
)
from apps.network.serializers.ipam_serializers import (
    SubnetSerializer, VLANSerializer, IPPoolSerializer,
    IPAddressSerializer, DHCPRangeSerializer
)
from apps.core.permissions import HasCompanyAccess
import logging

logger = logging.getLogger(__name__)


class SubnetViewSet(viewsets.ModelViewSet):
    queryset = Subnet.objects.all().select_related('company')
    serializer_class = SubnetSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['company', 'version', 'is_public', 'vlan_id']
    search_fields = ['name', 'network_address', 'description']
    
    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        
        if user.is_superuser:
            company_id = self.request.query_params.get('company_id')
            if company_id:
                return qs.filter(company_id=company_id)
            return qs
        
        if hasattr(user, 'company') and user.company:
            return qs.filter(company=user.company)
        
        return qs.none()

    def perform_create(self, serializer):
        user = self.request.user
        if user.is_superuser:
            serializer.save()
        else:
            if hasattr(user, 'company') and user.company:
                serializer.save(company=user.company)
            else:
                raise serializers.ValidationError("No company assigned to user")
    
    @action(detail=True, methods=['get'])
    def utilization(self, request, pk=None):
        """Get subnet utilization statistics"""
        subnet = self.get_object()
        
        # Calculate utilization from IP addresses
        ip_stats = subnet.ip_addresses.aggregate(
            total=Count('id'),
            used=Count('id', filter=~Q(status='AVAILABLE')),
            active=Count('id', filter=Q(status='ACTIVE')),
            reserved=Count('id', filter=Q(status='RESERVED'))
        )
        
        stats = {
            'network': f"{subnet.network_address}/{subnet.cidr}",
            'total_ips': subnet.total_ips,
            'available_ips': subnet.available_ips,
            'used_ips': subnet.used_ips,
            'utilization_percentage': subnet.utilization_percentage,
            'ip_address_stats': ip_stats,
            'pools_count': subnet.pools.count(),
            'vlan': subnet.vlan_id,
            'public': subnet.is_public,
        }
        
        return Response(stats)
    
    @action(detail=True, methods=['get'])
    def available_ips(self, request, pk=None):
        """Get list of available IPs in subnet"""
        subnet = self.get_object()
        
        # Get all IPs in the subnet
        network = IPNetwork(f"{subnet.network_address}/{subnet.cidr}")
        
        # Get already assigned IPs
        assigned_ips = set(
            subnet.ip_addresses.values_list('ip_address', flat=True)
        )
        
        # Generate available IPs (excluding network and broadcast)
        available_ips = []
        for ip in network.iter_hosts():
            ip_str = str(ip)
            if ip_str not in assigned_ips:
                available_ips.append(ip_str)
                if len(available_ips) >= 100:  # Limit to 100 results
                    break
        
        return Response({
            'subnet': f"{subnet.network_address}/{subnet.cidr}",
            'available_ips': available_ips,
            'total_available': len(available_ips),
        })
    
    @action(detail=True, methods=['post'])
    def allocate_ip(self, request, pk=None):
        """Allocate an IP address from subnet"""
        subnet = self.get_object()
        ip_address = request.data.get('ip_address')
        assignment_type = request.data.get('assignment_type', 'STATIC')
        hostname = request.data.get('hostname', '')
        description = request.data.get('description', '')
        
        # Validate IP is in subnet
        try:
            ip = NetIPAddress(ip_address)
            network = IPNetwork(f"{subnet.network_address}/{subnet.cidr}")
            
            if ip not in network:
                return Response({
                    'status': 'error',
                    'message': f'IP {ip_address} is not in subnet {network}'
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'status': 'error',
                'message': f'Invalid IP address: {str(e)}'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Check if IP is already allocated
        if IPAddress.objects.filter(ip_address=ip_address).exists():
            return Response({
                'status': 'error',
                'message': f'IP {ip_address} is already allocated'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Create IP address
        ip_obj = IPAddress.objects.create(
            subnet=subnet,
            ip_address=ip_address,
            assignment_type=assignment_type,
            status='RESERVED',
            hostname=hostname,
            description=description
        )
        
        # Update subnet counts
        subnet.used_ips += 1
        subnet.available_ips = subnet.total_ips - subnet.used_ips
        if subnet.total_ips > 0:
            subnet.utilization_percentage = (subnet.used_ips / subnet.total_ips) * 100
        subnet.save()
        
        serializer = IPAddressSerializer(ip_obj)
        return Response({
            'status': 'success',
            'message': f'IP {ip_address} allocated successfully',
            'data': serializer.data
        })

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if user.is_superuser:
            company_id = self.request.query_params.get('company_id')
            if company_id:
                return queryset.filter(company_id=company_id)
            return queryset
        
        if hasattr(user, 'company') and user.company:
            return queryset.filter(company=user.company)
        
        return queryset.none()


class VLANViewSet(viewsets.ModelViewSet):
    queryset = VLAN.objects.all().select_related('company', 'subnet')
    serializer_class = VLANSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['company', 'vlan_id']
    search_fields = ['name', 'description']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return VLAN.objects.all()
        return VLAN.objects.filter(company__in=user.companies.all())
    
    @action(detail=False, methods=['get'])
    def available_vlans(self, request):
        """Get available VLAN IDs"""
        company_id = request.query_params.get('company')
        
        if not company_id:
            return Response({
                'status': 'error',
                'message': 'Company ID is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Get all used VLAN IDs for this company
        used_vlans = VLAN.objects.filter(company_id=company_id).values_list('vlan_id', flat=True)
        
        # Generate available VLAN IDs (1-4095)
        available_vlans = []
        for vlan_id in range(1, 4096):
            if vlan_id not in used_vlans:
                available_vlans.append(vlan_id)
            if len(available_vlans) >= 50:  # Limit results
                break
        
        return Response({
            'company_id': company_id,
            'available_vlans': available_vlans,
            'total_available': len(available_vlans),
        })

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if user.is_superuser:
            company_id = self.request.query_params.get('company_id')
            if company_id:
                return queryset.filter(company_id=company_id)
            return queryset
        
        if hasattr(user, 'company') and user.company:
            return queryset.filter(company=user.company)
        
        return queryset.none()

class IPPoolViewSet(viewsets.ModelViewSet):
    queryset = IPPool.objects.all().select_related('subnet', 'subnet__company')
    serializer_class = IPPoolSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['subnet', 'pool_type', 'is_active']
    search_fields = ['name', 'start_ip', 'end_ip']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return IPPool.objects.all()
        return IPPool.objects.filter(subnet__company__in=user.companies.all())
    
    @action(detail=True, methods=['get'])
    def allocate_ip(self, request, pk=None):
        """Allocate an IP from pool"""
        pool = self.get_object()
        
        if not pool.is_active:
            return Response({
                'status': 'error',
                'message': f'Pool {pool.name} is not active'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Find next available IP in pool
        start_ip = NetIPAddress(pool.start_ip)
        end_ip = NetIPAddress(pool.end_ip)
        
        # Get allocated IPs in this pool
        allocated_ips = set(
            pool.pool_addresses.values_list('ip_address', flat=True)
        )
        
        # Find first available IP
        allocated_ip = None
        for i in range((end_ip.value - start_ip.value) + 1):
            current_ip = str(start_ip + i)
            if current_ip not in allocated_ips:
                allocated_ip = current_ip
                break
        
        if not allocated_ip:
            return Response({
                'status': 'error',
                'message': f'No available IPs in pool {pool.name}'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        return Response({
            'status': 'success',
            'message': f'IP {allocated_ip} available from pool {pool.name}',
            'ip_address': allocated_ip,
            'pool': pool.name,
            'subnet': pool.subnet.network_cidr,
        })
    
    @action(detail=True, methods=['get'])
    def statistics(self, request, pk=None):
        """Get pool statistics"""
        pool = self.get_object()
        
        # Get IP address statistics
        ip_stats = pool.pool_addresses.aggregate(
            total=Count('id'),
            active=Count('id', filter=Q(status='ACTIVE')),
            reserved=Count('id', filter=Q(status='RESERVED')),
            available=Count('id', filter=Q(status='AVAILABLE'))
        )
        
        stats = {
            'pool': pool.name,
            'range': f"{pool.start_ip} - {pool.end_ip}",
            'total_ips': pool.total_ips,
            'used_ips': pool.used_ips,
            'available_ips': pool.total_ips - pool.used_ips,
            'utilization': (pool.used_ips / pool.total_ips * 100) if pool.total_ips > 0 else 0,
            'ip_address_stats': ip_stats,
            'gateway': pool.gateway,
            'dns_servers': pool.dns_servers,
            'lease_time': pool.lease_time,
            'active': pool.is_active,
        }
        
        return Response(stats)

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if user.is_superuser:
            company_id = self.request.query_params.get('company_id')
            if company_id:
                return queryset.filter(subnet__company_id=company_id)
            return queryset
        
        if hasattr(user, 'company') and user.company:
            return queryset.filter(subnet__company=user.company)
        
        return queryset.none()

class IPAddressViewSet(viewsets.ModelViewSet):
    queryset = IPAddress.objects.all().select_related(
        'subnet', 'subnet__company', 'ip_pool',
        'service_connection', 'service_connection__customer'
    )
    serializer_class = IPAddressSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['subnet', 'ip_pool', 'assignment_type', 'status']
    search_fields = ['ip_address', 'hostname', 'mac_address', 'description']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return IPAddress.objects.all()
        return IPAddress.objects.filter(subnet__company__in=user.companies.all())
    
    @action(detail=True, methods=['post'])
    def release(self, request, pk=None):
        """Release IP address"""
        ip_address = self.get_object()
        
        if ip_address.status == 'AVAILABLE':
            return Response({
                'status': 'error',
                'message': f'IP {ip_address.ip_address} is already available'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Update IP address
        old_status = ip_address.status
        ip_address.status = 'AVAILABLE'
        ip_address.mac_address = ''
        ip_address.hostname = ''
        ip_address.description = ''
        ip_address.service_connection = None
        ip_address.lease_start = None
        ip_address.lease_end = None
        ip_address.save()
        
        # Update subnet counts
        subnet = ip_address.subnet
        subnet.used_ips -= 1
        subnet.available_ips = subnet.total_ips - subnet.used_ips
        if subnet.total_ips > 0:
            subnet.utilization_percentage = (subnet.used_ips / subnet.total_ips) * 100
        subnet.save()
        
        return Response({
            'status': 'success',
            'message': f'IP {ip_address.ip_address} released from {old_status}',
            'ip_address': ip_address.ip_address,
            'subnet': subnet.network_cidr,
        })
    
    @action(detail=True, methods=['post'])
    def assign(self, request, pk=None):
        """Assign IP address to service"""
        ip_address = self.get_object()
        service_connection_id = request.data.get('service_connection_id')
        mac_address = request.data.get('mac_address', '')
        hostname = request.data.get('hostname', '')
        description = request.data.get('description', '')
        
        if not service_connection_id:
            return Response({
                'status': 'error',
                'message': 'Service connection ID is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Check if IP is available
        if ip_address.status != 'AVAILABLE':
            return Response({
                'status': 'error',
                'message': f'IP {ip_address.ip_address} is not available (status: {ip_address.status})'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Update IP address
        ip_address.status = 'ACTIVE'
        ip_address.service_connection_id = service_connection_id
        ip_address.mac_address = mac_address
        ip_address.hostname = hostname
        ip_address.description = description
        ip_address.save()
        
        return Response({
            'status': 'success',
            'message': f'IP {ip_address.ip_address} assigned successfully',
            'ip_address': ip_address.ip_address,
            'service_connection_id': service_connection_id,
        })
    
    @action(detail=False, methods=['get'])
    def search_by_mac(self, request):
        """Search IP address by MAC address"""
        mac_address = request.query_params.get('mac_address', '').strip().upper()
        
        if not mac_address:
            return Response({
                'status': 'error',
                'message': 'MAC address is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        ip_addresses = IPAddress.objects.filter(
            mac_address__iexact=mac_address
        ).select_related('subnet', 'service_connection')
        
        serializer = IPAddressSerializer(ip_addresses, many=True)
        return Response({
            'status': 'success',
            'count': len(ip_addresses),
            'results': serializer.data
        })

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if user.is_superuser:
            company_id = self.request.query_params.get('company_id')
            if company_id:
                return queryset.filter(subnet__company_id=company_id)
            return queryset
        
        if hasattr(user, 'company') and user.company:
            return queryset.filter(subnet__company=user.company)
        
        return queryset.none()

class DHCPRangeViewSet(viewsets.ModelViewSet):
    queryset = DHCPRange.objects.all().select_related('ip_pool', 'ip_pool__subnet')
    serializer_class = DHCPRangeSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['ip_pool', 'is_active']
    search_fields = ['name', 'start_ip', 'end_ip']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return DHCPRange.objects.all()
        return DHCPRange.objects.filter(ip_pool__subnet__company__in=user.companies.all())
    
    @action(detail=True, methods=['post'])
    def generate_dhcp_config(self, request, pk=None):
        """Generate DHCP configuration for this range"""
        dhcp_range = self.get_object()
        
        config = f"""
# DHCP Configuration for {dhcp_range.name}
subnet {dhcp_range.ip_pool.subnet.network_address} netmask {dhcp_range.ip_pool.subnet.subnet_mask} {{
    range {dhcp_range.start_ip} {dhcp_range.end_ip};
    option routers {dhcp_range.router or dhcp_range.ip_pool.gateway or ''};
    option domain-name-servers {dhcp_range.dns_server or dhcp_range.ip_pool.dns_servers or '8.8.8.8, 8.8.4.4'};
    option domain-name "{dhcp_range.domain_name or 'local'}";
    default-lease-time {dhcp_range.lease_time};
    max-lease-time {int(dhcp_range.lease_time) * 2};
}}
"""
        
        return Response({
            'status': 'success',
            'message': f'DHCP configuration for {dhcp_range.name}',
            'config': config.strip(),
            'format': 'isc-dhcpd',
        })
    
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if user.is_superuser:
            company_id = self.request.query_params.get('company_id')
            if company_id:
                return queryset.filter(ip_pool__subnet__company_id=company_id)
            return queryset
        
        if hasattr(user, 'company') and user.company:
            return queryset.filter(ip_pool__subnet__company=user.company)
        
        return queryset.none()
