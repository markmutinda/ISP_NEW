from django.db import migrations, models
from django.db.models import F


def backfill_completed_at(apps, schema_editor):
    Topup = apps.get_model("messaging", "SMSUnitTopup")
    Topup.objects.filter(status="completed", completed_at__isnull=True).update(completed_at=F("updated_at"))


class Migration(migrations.Migration):
    dependencies = [("messaging", "0010_smstemplate_unique_active_event_type_per_tenant")]

    operations = [
        migrations.AddField(
            model_name="smsunittopup",
            name="completed_at",
            field=models.DateTimeField(null=True, blank=True, db_index=True),
        ),
        migrations.RunPython(backfill_completed_at, migrations.RunPython.noop),
    ]
