from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .cache import bump_catalog_version
from .models import Category, Product


@receiver([post_save, post_delete], sender=Product)
@receiver([post_save, post_delete], sender=Category)
def _invalidate_catalog(sender, **kwargs):
    try:
        bump_catalog_version()
    except Exception:
        pass  # cache entries expire on their own (60s TTL)