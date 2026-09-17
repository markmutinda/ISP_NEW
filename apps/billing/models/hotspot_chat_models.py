# apps/billing/models/hotspot_chat_models.py
from django.db import models
from django.utils import timezone


class HotspotChatThread(models.Model):
    """
    One thread per phone number per tenant schema. Phone number is the
    source of truth — anonymous hotspot users re-enter it to resume
    a conversation instead of authenticating.
    """
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('pending', 'Pending Reply'),
        ('resolved', 'Resolved'),
    ]

    phone_number = models.CharField(max_length=20, unique=True, db_index=True)
    router = models.ForeignKey(
        'network.Router', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='hotspot_chat_threads'
    )
    hotspot_client = models.ForeignKey(
        'billing.HotspotClient', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='chat_threads'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    subject = models.CharField(max_length=200, blank=True, default='Hotspot Support')

    assigned_to = models.ForeignKey(
        'core.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='assigned_hotspot_chats'
    )

    last_message_preview = models.CharField(max_length=260, blank=True, default='')
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)
    unread_by_admin = models.BooleanField(default=True)
    unread_by_customer = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_message_at', '-created_at']
        indexes = [
            models.Index(fields=['status', 'last_message_at']),
            models.Index(fields=['unread_by_admin', 'status']),
        ]

    def __str__(self):
        return f"Chat<{self.phone_number}:{self.status}>"

    def touch(self, message, *, status_value=None):
        self.last_message_preview = message.body[:260]
        self.last_message_at = message.created_at
        if status_value:
            self.status = status_value
        self.save(update_fields=['last_message_preview', 'last_message_at', 'status', 'updated_at'])


class HotspotChatMessage(models.Model):
    SENDER_CHOICES = [
        ('customer', 'Customer'),
        ('agent', 'Agent'),
        ('system', 'System'),
    ]

    thread = models.ForeignKey(HotspotChatThread, on_delete=models.CASCADE, related_name='messages')
    sender_type = models.CharField(max_length=20, choices=SENDER_CHOICES)
    agent = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, blank=True)
    sender_name = models.CharField(max_length=160, blank=True, default='')
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['created_at']