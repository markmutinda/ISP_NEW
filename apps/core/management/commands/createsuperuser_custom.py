"""
Custom command to create superuser with additional fields
"""
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.utils import timezone
import getpass

User = get_user_model()


class Command(BaseCommand):
    help = 'Create a superuser with custom fields'

    def handle(self, *args, **options):
        self.stdout.write(self.style.HTTP_INFO('Creating superuser...'))
        
        # Get user input
        email = input('Email: ')
        
        # Check if user already exists
        if User.objects.filter(email=email).exists():
            self.stdout.write(self.style.ERROR(f'User with email {email} already exists.'))
            return
        
        first_name = input('First Name: ')
        last_name = input('Last Name: ')
        phone_number = input('Phone Number: ')
        
        while True:
            password = getpass.getpass('Password: ')
            confirm_password = getpass.getpass('Confirm Password: ')
            
            if password != confirm_password:
                self.stdout.write(self.style.ERROR('Passwords do not match. Try again.'))
                continue
            
            if len(password) < 8:
                self.stdout.write(self.style.ERROR('Password must be at least 8 characters long.'))
                continue
            
            break
        
        # Create superuser
        try:
            user = User.objects.create_superuser(
                email=email,
                password=password,
                first_name=first_name,
                last_name=last_name,
                phone_number=phone_number,
                role='admin',
                is_verified=True,
            )
            
            self.stdout.write(self.style.SUCCESS(
                f'Superuser {user.email} created successfully!'
            ))
            
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error creating superuser: {str(e)}'))
