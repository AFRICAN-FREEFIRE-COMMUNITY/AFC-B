from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('afc_team', '0001_initial'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='teammembers',
            constraint=models.UniqueConstraint(
                fields=['member'],
                name='unique_member_one_team',
            ),
        ),
    ]
