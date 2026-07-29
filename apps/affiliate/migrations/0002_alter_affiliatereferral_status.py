from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("affiliate", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="affiliatereferral",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending review"),
                    ("approved", "Approved for commission"),
                    ("paid", "Commission paid"),
                    ("rejected", "Rejected"),
                    ("churned", "Rejected or churned"),
                ],
                db_index=True,
                default="pending",
                max_length=12,
            ),
        ),
    ]
