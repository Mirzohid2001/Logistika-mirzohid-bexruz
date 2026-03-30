import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("drivers", "0006_driverdeliveryreview"),
    ]

    operations = [
        migrations.AlterField(
            model_name="driverdeliveryreview",
            name="stars",
            field=models.PositiveSmallIntegerField(
                help_text="1–5 yulduz",
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(5),
                ],
            ),
        ),
    ]
