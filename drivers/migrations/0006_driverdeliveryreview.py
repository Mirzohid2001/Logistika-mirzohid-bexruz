import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0009_orderseal"),
        ("drivers", "0005_driver_rating_score"),
    ]

    operations = [
        migrations.CreateModel(
            name="DriverDeliveryReview",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("stars", models.PositiveSmallIntegerField()),
                ("comment", models.TextField(blank=True)),
                ("recorded_by_username", models.CharField(blank=True, max_length=150)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "driver",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="delivery_reviews",
                        to="drivers.driver",
                    ),
                ),
                (
                    "order",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="driver_review",
                        to="orders.order",
                    ),
                ),
            ],
            options={
                "verbose_name": "Shofyor yetkazib berish sharhi",
                "verbose_name_plural": "Shofyor yetkazib berish sharhlari",
                "ordering": ["-created_at"],
            },
        ),
    ]
