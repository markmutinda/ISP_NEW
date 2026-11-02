from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0032_rename_tenant_activation_payment_index'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='onboarding_tour_started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='user',
            name='onboarding_tour_dismissed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='user',
            name='onboarding_tour_completed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='user',
            name='onboarding_video_viewed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
