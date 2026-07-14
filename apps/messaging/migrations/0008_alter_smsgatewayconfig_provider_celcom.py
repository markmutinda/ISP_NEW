from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('messaging', '0007_alter_smsgatewayconfig_provider'),
    ]

    operations = [
        migrations.AlterField(
            model_name='smsgatewayconfig',
            name='provider',
            field=models.CharField(
                choices=[
                    ('africastalking', "Africa's Talking"),
                    ('twilio', 'Twilio'),
                    ('vonage', 'Vonage (Nexmo)'),
                    ('infobip', 'Infobip'),
                    ('beem', 'Beem Africa'),
                    ('advanta', 'Advanta SMS'),
                    ('hubtel', 'Hubtel'),
                    ('bytewave', 'Bytewave'),
                    ('blessedtexts', 'BlessedTexts'),
                    ('texin', 'Texin'),
                    ('celcom', 'Celcom Africa'),
                ],
                max_length=30,
            ),
        ),
    ]