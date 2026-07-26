# apps/billing/migrations/0015_currency_default_blank.py
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0014_alter_hotspotclient_canonical_phone'),
    ]

    operations = [
        migrations.AlterField(
            model_name='payment',
            name='currency',
            field=models.CharField(blank=True, default='', max_length=3),
        ),
        migrations.AlterField(
            model_name='receipt',
            name='currency',
            field=models.CharField(blank=True, default='', max_length=3),
        ),
        migrations.AlterField(
            model_name='hotspotplan',
            name='currency',
            field=models.CharField(blank=True, default='', max_length=3),
        ),
    ]