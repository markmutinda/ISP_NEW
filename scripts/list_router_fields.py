import os, sys, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.production')
sys.path.insert(0, '/app')
django.setup()
from apps.network.models.router_models import Router
for f in Router._meta.get_fields():
    if hasattr(f, 'column'):
        print("{:30s} {:20s} null={}".format(f.name, type(f).__name__, f.null))
