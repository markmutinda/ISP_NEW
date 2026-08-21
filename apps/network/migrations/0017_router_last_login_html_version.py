from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('network', '0016_ipbinding'),
    ]

    operations = [
        migrations.AddField(
            model_name='router',
            name='last_login_html_version',
            field=models.CharField(blank=True, max_length=10, null=True),
        ),
    ]