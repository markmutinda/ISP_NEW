from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("subscriptions", "0018_netilyplan_selfie_activation_plan")]

    operations = [
        migrations.AlterField(
            model_name="netilyplan",
            name="code",
            field=models.CharField(
                choices=[
                    ("starter", "Starter"),
                    ("enterprise", "Enterprise"),
                    ("test", "Test"),
                    ("metered", "Metered (legacy)"),
                    ("professional", "Professional (legacy)"),
                ],
                max_length=50,
                unique=True,
            ),
        ),
    ]
