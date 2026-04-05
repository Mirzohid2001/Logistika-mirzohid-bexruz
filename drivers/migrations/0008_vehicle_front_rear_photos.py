from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("drivers", "0007_alter_driverdeliveryreview_stars"),
    ]

    operations = [
        migrations.AddField(
            model_name="vehicle",
            name="front_photo_file_id",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="vehicle",
            name="rear_photo_file_id",
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
