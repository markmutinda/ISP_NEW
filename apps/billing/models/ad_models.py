# apps/billing/models/ad_models.py
from decimal import Decimal
from django.db import models
from django.db.models import Sum, F
from django.utils import timezone


class HotspotAd(models.Model):
    MEDIA_TYPE_CHOICES = [('VIDEO', 'Video'), ('IMAGE', 'Image')]
    STORAGE_LIMIT_MB = Decimal('10')

    schema_name = models.SlugField(max_length=63, editable=False, default="default_schema")
    name = models.CharField(max_length=200)

    # Media — either uploaded file OR external URL
    media_file = models.FileField(upload_to='hotspot/ads/', null=True, blank=True)
    media_url = models.URLField(blank=True, help_text="External video URL (YouTube, etc.)")
    media_type = models.CharField(max_length=10, choices=MEDIA_TYPE_CHOICES, default='VIDEO')
    file_size_mb = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0'))

    target_url = models.URLField(blank=True, help_text="Where to redirect on click (optional)")

    # Reward config (per-ad)
    reward_enabled = models.BooleanField(default=True)
    reward_minutes = models.PositiveIntegerField(
        default=30,
        help_text="Minutes of free internet after watching (0–60)"
    )

    # Router targeting — null means all routers for this tenant
    router = models.ForeignKey(
        'network.Router', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='hotspot_ads'
    )

    # Schedule & status
    is_active = models.BooleanField(default=True)
    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)
    priority = models.PositiveIntegerField(default=1, help_text="Lower = shown first")

    # Stats (atomic updates only — never direct .save())
    impressions = models.PositiveIntegerField(default=0)
    completions = models.PositiveIntegerField(default=0)

    created_by = models.ForeignKey(
        'core.User', on_delete=models.SET_NULL, null=True, related_name='created_hotspot_ads'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['priority', '-created_at']
        indexes = [
            models.Index(fields=['schema_name', 'is_active', 'priority']),
        ]

    def __str__(self):
        return f"{self.name} ({self.schema_name})"

    @classmethod
    def get_used_storage_mb(cls, schema_name: str) -> Decimal:
        result = cls.objects.filter(
            schema_name=schema_name, media_file__isnull=False
        ).exclude(media_file='').aggregate(total=Sum('file_size_mb'))
        return result['total'] or Decimal('0')

    @classmethod
    def get_available_storage_mb(cls, schema_name: str) -> Decimal:
        return cls.STORAGE_LIMIT_MB - cls.get_used_storage_mb(schema_name)

    def get_media_url(self, request=None) -> str:
        if self.media_file:
            # Use the streaming endpoint for uploaded files (supports range requests + caching)
            base = request.build_absolute_uri('/').rstrip('/') if request else ''
            # Include tenant so the media view can find the ad
            schema = getattr(self, 'schema_name', 'public')
            return f"{base}/api/v1/hotspot/ads/media/{self.pk}/?tenant={schema}"
        return self.media_url  # External URLs served as-is


class HotspotAdGrant(models.Model):
    """Tracks ad-sponsored internet grants per MAC address to prevent abuse."""
    schema_name = models.SlugField(max_length=63, editable=False, default="default_schema")
    ad = models.ForeignKey(HotspotAd, on_delete=models.CASCADE, related_name='grants')
    mac_address = models.CharField(max_length=17, db_index=True)
    router = models.ForeignKey('network.Router', on_delete=models.CASCADE)
    access_code = models.CharField(max_length=20, unique=True)
    granted_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=['schema_name', 'mac_address', 'expires_at']),
        ]

    def __str__(self):
        return f"{self.mac_address} → {self.ad.name} until {self.expires_at}"