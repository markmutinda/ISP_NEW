from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("superadmin", "0004_platform_expenditure"),
    ]

    operations = [
        migrations.AddField(
            model_name="platformexpenditure",
            name="ledger",
            field=models.CharField(
                choices=[
                    ("primary", "Original Business Account"),
                    ("new_business", "New Business Account"),
                ],
                db_index=True,
                default="primary",
                max_length=32,
            ),
        ),
        migrations.AddIndex(
            model_name="platformexpenditure",
            index=models.Index(fields=["ledger", "incurred_on"], name="sadm_exp_ledger_date_idx"),
        ),
    ]
