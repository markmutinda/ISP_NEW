from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.customers.models import (
    Customer, CustomerAddress, CustomerDocument, 
    NextOfKin, ServiceConnection,
    CUSTOMER_STATUS_CHOICES,
)

# Use validate_phone_number from utils.helpers instead
from utils.helpers import validate_phone_number

User = get_user_model()


# Create a local validation function for ID numbers
def validate_id_number_local(id_number, id_type='NATIONAL_ID'):
    """Local validation for ID numbers"""
    import re
    
    if not id_number:
        raise ValidationError("ID number is required")
    
    id_number = str(id_number).strip()
    
    if id_type == 'NATIONAL_ID':
        if not re.match(r'^\d{7,9}$', id_number):
            raise ValidationError("Invalid National ID. Must be 7-9 digits")
    elif id_type == 'PASSPORT':
        if not re.match(r'^[A-Z]\d{7,8}$', id_number.upper()):
            raise ValidationError("Invalid Passport number. Format: Letter followed by 7-8 digits")
    
    return id_number


class CustomerForm(forms.ModelForm):
    """Form for creating/updating customers"""
    email = forms.EmailField(required=True)
    first_name = forms.CharField(max_length=100, required=True)
    last_name = forms.CharField(max_length=100, required=True)
    phone_number = forms.CharField(
        max_length=20, 
        validators=[validate_phone_number]  # Changed from validate_kenyan_phone
    )
    password = forms.CharField(
        widget=forms.PasswordInput, 
        required=False,
        help_text="Leave blank to keep existing password"
    )
    
    class Meta:
        model = Customer
        fields = [
            'email', 'first_name', 'last_name', 'phone_number', 'password',
            'date_of_birth', 'gender', 'id_type', 'id_number',
            'marital_status', 'occupation', 'employer', 
            'customer_type', 'category', 'status',
            'billing_cycle', 'credit_limit', 'receive_sms',
            'receive_email', 'receive_promotions', 'notes'
        ]
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # If editing existing customer, populate user fields
        if self.instance and self.instance.pk:
            self.fields['email'].initial = self.instance.user.email
            self.fields['first_name'].initial = self.instance.user.first_name
            self.fields['last_name'].initial = self.instance.user.last_name
            self.fields['phone_number'].initial = self.instance.user.phone_number
            self.fields['password'].required = False
    
    def clean_id_number(self):
        id_number = self.cleaned_data.get('id_number')
        id_type = self.cleaned_data.get('id_type', 'NATIONAL_ID')
        
        # Use local validation function
        try:
            id_number = validate_id_number_local(id_number, id_type)
        except ValidationError as e:
            raise ValidationError(str(e))
        
        # Check for uniqueness
        qs = Customer.objects.filter(id_number=id_number)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        
        if qs.exists():
            raise ValidationError('A customer with this ID number already exists')
        
        return id_number
    
    def save(self, commit=True):
        customer = super().save(commit=False)
        
        user_data = {
            'email': self.cleaned_data['email'],
            'first_name': self.cleaned_data['first_name'],
            'last_name': self.cleaned_data['last_name'],
            'phone_number': self.cleaned_data['phone_number'],
        }
        
        if self.instance.pk:
            # Update existing user
            user = customer.user
            for attr, value in user_data.items():
                setattr(user, attr, value)
            
            # Update password if provided
            password = self.cleaned_data.get('password')
            if password:
                user.set_password(password)
            
            user.save()
        else:
            # Create new user
            password = self.cleaned_data.get('password', 'default_password')
            user = User.objects.create_user(
                **user_data,
                password=password,
                role='customer'  # Note: lowercase 'customer' to match your USER_ROLES
            )
            customer.user = user
        
        if commit:
            customer.save()
        
        return customer


class AddressForm(forms.ModelForm):
    """Form for customer addresses"""
    
    class Meta:
        model = CustomerAddress
        fields = [
            'address_type', 'is_primary',
            'building_name', 'floor', 'room', 'street_address', 'landmark',
            'county', 'sub_county', 'ward', 'estate',
            'contact_person', 'contact_phone',
            'latitude', 'longitude', 'installation_notes'
        ]
        widgets = {
            'street_address': forms.Textarea(attrs={'rows': 3}),
            'installation_notes': forms.Textarea(attrs={'rows': 3}),
        }
    
    def clean(self):
        cleaned_data = super().clean()
        address_type = cleaned_data.get('address_type')
        is_primary = cleaned_data.get('is_primary')
        
        if is_primary:
            # Check if another primary address of same type exists
            customer = self.instance.customer if self.instance.pk else None
            if customer and self.instance.pk:
                existing_primary = CustomerAddress.objects.filter(
                    customer=customer,
                    address_type=address_type,
                    is_primary=True
                ).exclude(pk=self.instance.pk)
                
                if existing_primary.exists():
                    raise ValidationError(
                        f'A primary {address_type.lower()} address already exists'
                    )
        
        return cleaned_data


class DocumentUploadForm(forms.ModelForm):
    """Form for uploading customer documents"""
    
    class Meta:
        model = CustomerDocument
        fields = [
            'document_type', 'title', 'description',
            'document_file', 'expiry_date'
        ]
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
            'expiry_date': forms.DateInput(attrs={'type': 'date'}),
        }
    
    def clean_document_file(self):
        document_file = self.cleaned_data.get('document_file')
        
        if document_file:
            # Validate file size (5MB max)
            max_size = 5 * 1024 * 1024  # 5MB
            if document_file.size > max_size:
                raise ValidationError(
                    f'File size must be less than 5MB. Current size: {document_file.size / 1024 / 1024:.1f}MB'
                )
            
            # Validate file type
            allowed_types = [
                'application/pdf',
                'image/jpeg',
                'image/png',
                'image/jpg',
            ]
            if document_file.content_type not in allowed_types:
                raise ValidationError(
                    'Only PDF, JPEG, and PNG files are allowed'
                )
        
        return document_file


class NextOfKinForm(forms.ModelForm):
    """Form for next of kin information"""
    
    class Meta:
        model = NextOfKin
        fields = [
            'full_name', 'relationship', 'phone_number', 'email',
            'id_type', 'id_number', 'address', 'county',
            'is_primary_contact'
        ]
        widgets = {
            'address': forms.Textarea(attrs={'rows': 3}),
        }
    
    def clean_phone_number(self):
        phone_number = self.cleaned_data.get('phone_number')
        if phone_number:
            # Use validate_phone_number from utils.helpers
            try:
                phone_number = validate_phone_number(phone_number)
            except ValidationError as e:
                raise ValidationError(str(e))
        return phone_number


class ServiceConnectionForm(forms.ModelForm):
    """Form for service connections"""
    
    class Meta:
        model = ServiceConnection
        fields = [
            'service_type', 'service_plan', 'connection_type', 'status',
            'ip_address', 'mac_address', 'vlan_id',
            'router_model', 'router_serial', 'ont_model', 'ont_serial',
            'download_speed', 'upload_speed', 'data_cap', 'qos_profile',
            'installation_address', 'installation_notes',
            'monthly_price', 'setup_fee', 'prorated_billing',
            'auto_renew', 'contract_period'
        ]
        widgets = {
            'installation_notes': forms.Textarea(attrs={'rows': 3}),
            'activation_date': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
            'suspension_date': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
            'termination_date': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        }
    
    def clean_mac_address(self):
        mac_address = self.cleaned_data.get('mac_address')
        if mac_address:
            # Validate MAC address format
            import re
            mac_pattern = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$')
            if not mac_pattern.match(mac_address):
                raise ValidationError('Invalid MAC address format')
        return mac_address
    
    def clean(self):
        cleaned_data = super().clean()
        download_speed = cleaned_data.get('download_speed')
        upload_speed = cleaned_data.get('upload_speed')
        
        if download_speed and upload_speed:
            if upload_speed > download_speed:
                raise ValidationError(
                    'Upload speed cannot be greater than download speed'
                )
        
        return cleaned_data


class CustomerSearchForm(forms.Form):
    """Form for searching customers"""
    search = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'placeholder': 'Search by name, email, phone, or ID...'
        })
    )
    status = forms.ChoiceField(
        choices=[('', 'All Statuses')] + list(CUSTOMER_STATUS_CHOICES),
        required=False,
        label="Status"
    )
    customer_type = forms.ChoiceField(
        choices=[('', 'All Types')] + [
            ('RESIDENTIAL', 'Residential'),
            ('BUSINESS', 'Business'),
            ('CORPORATE', 'Corporate'),
            ('INSTITUTION', 'Institution'),
        ],
        required=False,
        label="Customer Type"
    )
    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
        label="From Date"
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
        label="To Date"
    )