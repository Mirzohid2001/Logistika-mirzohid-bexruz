import django.utils.timezone
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0008_order_delivered_density_kg_per_liter"),
    ]

    operations = [
        migrations.CreateModel(
            name="OrderSeal",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("compartment", models.CharField(blank=True, help_text="Masalan: 1, Old, O‘ng", max_length=80, verbose_name="Bo‘lim")),
                (
                    "seal_number_loading",
                    models.CharField(
                        help_text="Zavod / yuklash paytidagi muhr raqami",
                        max_length=160,
                        verbose_name="Muhr (yuklash)",
                    ),
                ),
                (
                    "seal_number_unloading",
                    models.CharField(
                        blank=True,
                        help_text="Klientda ko‘rilgan muhr (almashtirilgan bo‘lsa — yangi raqam)",
                        max_length=160,
                        verbose_name="Muhr (tushirish)",
                    ),
                ),
                ("loading_recorded_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("loading_recorded_by", models.CharField(blank=True, max_length=120)),
                ("unloading_recorded_at", models.DateTimeField(blank=True, null=True)),
                ("unloading_recorded_by", models.CharField(blank=True, max_length=120)),
                ("is_broken", models.BooleanField(default=False, verbose_name="Muhr buzilgan")),
                ("broken_at", models.DateTimeField(blank=True, null=True)),
                ("broken_note", models.TextField(blank=True, verbose_name="Buzilish izohi")),
                ("broken_recorded_by", models.CharField(blank=True, max_length=120)),
                ("sort_order", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="seals",
                        to="orders.order",
                    ),
                ),
            ],
            options={
                "verbose_name": "Buyurtma muhr",
                "verbose_name_plural": "Buyurtma muhrlari",
                "ordering": ["sort_order", "pk"],
            },
        ),
    ]
