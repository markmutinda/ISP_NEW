from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0013_lead_lead_source'),
    ]

    operations = [
        migrations.AddField(
            model_name='lead',
            name='is_contacted',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='lead',
            name='contacted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
