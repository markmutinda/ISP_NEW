from django.db import models
from apps.core.models import User

class Customer(models.Model):
    ACCOUNT_TYPES = (
        ('prepaid', 'Prepaid'),
        ('postpaid', 'Postpaid'),
        ('corporate', 'Corporate'),
    )
    
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='customer')
    customer_id = models.CharField(max_length=20, unique=True)
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPES)
    registration_date = models.DateField(auto_now_add=True)
    balance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    def __str__(self):
        return f"{self.customer_id} - {self.user.username}"