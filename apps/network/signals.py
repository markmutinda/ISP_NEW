from django.db.models.signals import post_save
from django.dispatch import receiver
from apps.network.models.router_models import Router
from apps.radius.models import Nas
from django.db import connection

@receiver(post_save, sender=Router)
def sync_router_to_radius_nas(sender, instance, created, **kwargs):
    """
    Automatically adds/updates the router in the RADIUS NAS whitelist 
    every time a router is saved in the dashboard.
    """
    # 1. Determine the IP FreeRADIUS will see. 
    # If it's a VPN router, use the VPN IP. Otherwise, fallback to 0.0.0.0/0
    nas_ip = instance.vpn_ip_address if instance.vpn_ip_address else '0.0.0.0/0'
    
    # 2. The secret must match what the MikroTik script generated
    secret = instance.shared_secret or f"netily_{connection.schema_name}_secret"

    # 3. Clean up any old entry if the IP changed, to prevent duplicates
    Nas.objects.filter(shortname=instance.name).delete()
    
    # 4. Create the fresh NAS entry in this tenant's schema
    Nas.objects.create(
        nasname=nas_ip,
        shortname=instance.name,
        type='mikrotik',
        secret=secret,
        server='Default'
    )
    print(f"📡 [RADIUS AUTO-SYNC] Added {instance.name} ({nas_ip}) to {connection.schema_name} NAS table.")