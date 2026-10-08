from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0005_platform_expenditure_ledger")]

    operations = [
        migrations.AddField(
            model_name="platformexpenditure",
            name="reverses",
            field=models.OneToOneField(
                to="superadmin.platformexpenditure", on_delete=django.db.models.deletion.PROTECT,
                null=True, blank=True, related_name="reversal",
                help_text="Original entry offset by this correction.",
            ),
        ),
    ]
