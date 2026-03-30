from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0009_orderseal"),
    ]

    operations = [
        migrations.CreateModel(
            name="OrderExtraExpense",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "category",
                    models.CharField(
                        choices=[
                            ("fuel", "Yoqilg‘i"),
                            ("toll", "Yo‘l to‘lovi"),
                            ("parking", "Parkovka"),
                            ("repair", "Ta’mir"),
                            ("loader", "Yuklash/tushirish xizmati"),
                            ("other", "Boshqa"),
                        ],
                        default="other",
                        max_length=20,
                    ),
                ),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("note", models.CharField(blank=True, max_length=255)),
                ("incurred_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("recorded_by", models.CharField(blank=True, max_length=120)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="additional_expenses",
                        to="orders.order",
                    ),
                ),
            ],
            options={
                "ordering": ["-incurred_at", "-id"],
            },
        ),
    ]

