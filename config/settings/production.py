"""
Production settings.
"""
from .base import *

DEBUG = False
ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost').split(',')
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
