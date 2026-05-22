from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0012_merge_0009_changelog_notification_fields_0011_alter_routertenantindex_router_id_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="lead_source",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
    ]
