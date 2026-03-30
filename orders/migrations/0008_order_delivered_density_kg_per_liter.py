from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0007_order_shortage_flagged_at_order_shortage_kg_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="delivered_density_kg_per_liter",
            field=models.DecimalField(
                blank=True,
                decimal_places=4,
                max_digits=8,
                null=True,
            ),
        ),
    ]
