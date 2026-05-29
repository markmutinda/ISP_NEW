import django.db.models.deletion
import django.core.validators
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('staff', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('customers', '0003_serviceconnection_billing_account_number_and_more'),
        ('support', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Technician',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('employee_id', models.CharField(max_length=50, unique=True)),
                ('phone', models.CharField(max_length=20)),
                ('skills', models.JSONField(blank=True, default=list)),
                ('status', models.CharField(
                    choices=[
                        ('available', 'Available'),
                        ('busy', 'Busy'),
                        ('offline', 'Offline'),
                        ('on_leave', 'On Leave'),
                    ],
                    default='available',
                    max_length=20,
                )),
                ('current_location', models.CharField(blank=True, max_length=255, null=True)),
                ('latitude', models.DecimalField(blank=True, decimal_places=7, max_digits=10, null=True)),
                ('longitude', models.DecimalField(blank=True, decimal_places=7, max_digits=10, null=True)),
                ('total_jobs_completed', models.PositiveIntegerField(default=0)),
                ('average_rating', models.DecimalField(decimal_places=2, default=0.0, max_digits=3)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='technician_profile',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'verbose_name': 'Technician',
                'verbose_name_plural': 'Technicians',
                'ordering': ['employee_id'],
            },
        ),
        migrations.CreateModel(
            name='DispatchJob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('job_number', models.CharField(editable=False, max_length=20, unique=True)),
                ('job_type', models.CharField(
                    choices=[
                        ('installation', 'Installation'),
                        ('repair', 'Repair'),
                        ('maintenance', 'Maintenance'),
                        ('relocation', 'Relocation'),
                        ('disconnection', 'Disconnection'),
                        ('survey', 'Survey'),
                    ],
                    max_length=20,
                )),
                ('description', models.TextField(blank=True, default='')),
                ('priority', models.CharField(
                    choices=[
                        ('low', 'Low'),
                        ('medium', 'Medium'),
                        ('high', 'High'),
                        ('urgent', 'Urgent'),
                    ],
                    default='medium',
                    max_length=10,
                )),
                ('status', models.CharField(
                    choices=[
                        ('pending', 'Pending'),
                        ('assigned', 'Assigned'),
                        ('in_progress', 'In Progress'),
                        ('completed', 'Completed'),
                        ('cancelled', 'Cancelled'),
                    ],
                    default='pending',
                    max_length=20,
                )),
                ('scheduled_date', models.DateField()),
                ('scheduled_time', models.TimeField(blank=True, null=True)),
                ('estimated_duration', models.PositiveIntegerField(default=60)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('notes', models.TextField(blank=True, default='')),
                ('customer_rating', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('customer_feedback', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('assigned_to', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='jobs',
                    to='staff.technician',
                )),
                ('customer', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='dispatch_jobs',
                    to='customers.customer',
                )),
                ('ticket', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='dispatch_jobs',
                    to='support.supportticket',
                )),
            ],
            options={
                'verbose_name': 'Dispatch Job',
                'verbose_name_plural': 'Dispatch Jobs',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='dispatchjob',
            index=models.Index(fields=['status'], name='staff_dispatchjob_status_idx'),
        ),
        migrations.AddIndex(
            model_name='dispatchjob',
            index=models.Index(fields=['assigned_to'], name='staff_dispatchjob_assigned_idx'),
        ),
        migrations.AddIndex(
            model_name='dispatchjob',
            index=models.Index(fields=['scheduled_date'], name='staff_dispatchjob_sched_idx'),
        ),
    ]
