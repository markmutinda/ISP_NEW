"""
Production settings.
"""
from .base import *

DEBUG = False
ALLOWED_HOSTS = ['yourdomain.com']
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
