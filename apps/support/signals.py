from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone
from .models import Ticket, TicketMessage

@receiver(post_save, sender=Ticket)
def update_ticket_metrics(sender, instance, created, **kwargs):
    """Update technician metrics when ticket is resolved"""
    if not created and instance.status and instance.status.is_closed:
        if instance.assigned_to and instance.resolved_at:
            # Update technician's total tickets resolved
            instance.assigned_to.total_tickets_resolved += 1
            
            # Update total resolution time
            if instance.created_at and instance.resolved_at:
                resolution_time = instance.resolved_at - instance.created_at
                instance.assigned_to.total_resolution_time += resolution_time
            
            # Update average rating
            if instance.customer_rating:
                total_rated = Ticket.objects.filter(
                    assigned_to=instance.assigned_to,
                    customer_rating__isnull=False
                ).count()
                
                if total_rated > 0:
                    avg_rating = Ticket.objects.filter(
                        assigned_to=instance.assigned_to
                    ).aggregate(avg=models.Avg('customer_rating'))['avg']
                    instance.assigned_to.average_rating = avg_rating or 0
            
            instance.assigned_to.save()

@receiver(pre_save, sender=TicketMessage)
def update_ticket_on_new_message(sender, instance, **kwargs):
    """Update ticket when new message is added"""
    if instance.pk is None:  # New message
        # Update ticket's updated timestamp
        instance.ticket.updated_at = timezone.now()
        instance.ticket.save()