from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0015_rename_core_emailot_user_id_fec6cf_idx_core_emailo_user_id_9360a5_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="referral_name",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
