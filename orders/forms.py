from decimal import Decimal, InvalidOperation

from django import forms
from django.core.exceptions import ValidationError

from .models import Client, Order


class OrderCreateForm(forms.ModelForm):
    pickup_time = forms.DateTimeField(
        input_formats=["%Y-%m-%d %H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )

    client = forms.ModelChoiceField(queryset=Client.objects.filter(is_active=True), required=False)
    route_polyline = forms.JSONField(required=False, initial=list)
    geofence_polygon = forms.JSONField(required=False, initial=list)

    class Meta:
        model = Order
        fields = [
            "client",
            "from_location",
            "to_location",
            "cargo_type",
            "weight_ton",
            "pickup_time",
            "contact_name",
            "contact_phone",
            "comment",
            "route_polyline",
            "geofence_polygon",
            "route_deviation_threshold_km",
            "driver_fee",
            "fuel_cost",
            "extra_cost",
            "penalty_amount",
        ]
        labels = {
            "client": "Klient",
            "from_location": "Qayerdan",
            "to_location": "Qayerga",
            "cargo_type": "Yuk turi",
            "weight_ton": "Yuk og'irligi (kg)",
            "pickup_time": "Yuk olish vaqti",
            "contact_name": "Mas'ul shaxs",
            "contact_phone": "Telefon",
            "comment": "Izoh",
            "route_polyline": "Marshrut chizig'i (JSON)",
            "geofence_polygon": "Geofence hududi (JSON)",
            "route_deviation_threshold_km": "Marshrutdan og'ish limiti (km)",
            "driver_fee": "Shofyor to'lovi",
            "fuel_cost": "Yoqilg'i xarajati",
            "extra_cost": "Qo'shimcha xarajat",
            "penalty_amount": "Jarima summasi",
        }
        help_texts = {
            "weight_ton": "Kilogramm. Tizim ichki hisobda tonnaga aylantiradi (÷1000).",
            "from_location": "Yandex xarita orqali tanlasangiz koordinata avtomatik yoziladi.",
            "to_location": "Yandex xarita orqali tanlasangiz koordinata avtomatik yoziladi.",
            "route_polyline": "Xaritada 'Marshrut nuqtasi qo'shish' tugmasi bilan to'ldiriladi.",
            "geofence_polygon": "Xaritada 'Geofence nuqtasi qo'shish' tugmasi bilan to'ldiriladi.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["from_location"].widget.attrs.update({"placeholder": "Masalan: Toshkent, Sergeli"})
        self.fields["to_location"].widget.attrs.update({"placeholder": "Masalan: Farg'ona, Quvasoy"})
        self.fields["route_polyline"].widget.attrs.update({"rows": 4})
        self.fields["geofence_polygon"].widget.attrs.update({"rows": 4})
        self.fields["route_deviation_threshold_km"].widget.attrs.update({"min": "0.1", "step": "0.1"})
        for money_field in ["driver_fee", "fuel_cost", "extra_cost", "penalty_amount"]:
            self.fields[money_field].widget.attrs.update({"min": "0", "step": "1000", "placeholder": "0"})
        self.fields["weight_ton"].widget.attrs.update(
            {"min": "0.001", "step": "any", "placeholder": "Masalan: 12500", "inputmode": "decimal"}
        )

    def clean_weight_ton(self):
        """Formada kg kiritiladi; model maydoni tonna sifatida saqlanadi."""
        kg = self.cleaned_data.get("weight_ton")
        if kg is None:
            return kg
        try:
            kg_dec = Decimal(str(kg))
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError("Og‘irlikni raqam bilan kiriting.") from None
        if kg_dec <= 0:
            raise ValidationError("Og‘irlik 0 dan katta bo‘lishi kerak.")
        tons = (kg_dec / Decimal("1000")).quantize(Decimal("0.01"))
        return tons


class OrderCustodyForm(forms.ModelForm):
    """Zavoddan chiqqan va klientga berilgan hajm (o‘g‘irlash nazorati)."""

    class Meta:
        model = Order
        fields = [
            "loaded_quantity",
            "loaded_quantity_uom",
            "delivered_quantity",
            "delivered_quantity_uom",
            "density_kg_per_liter",
        ]
        labels = {
            "loaded_quantity": "Yuklangan (fakt)",
            "loaded_quantity_uom": "Yuklangan o‘lchov",
            "delivered_quantity": "Klientga topshirilgan",
            "delivered_quantity_uom": "Topshirish o‘lchov",
            "density_kg_per_liter": "Zichlik kg/L (litr ishlatilsa)",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("loaded_quantity", "delivered_quantity", "density_kg_per_liter"):
            self.fields[name].widget.attrs.setdefault("class", "form-control")
        self.fields["loaded_quantity"].widget.attrs.update({"step": "0.001", "min": "0"})
        self.fields["delivered_quantity"].widget.attrs.update({"step": "0.001", "min": "0"})
        self.fields["density_kg_per_liter"].widget.attrs.update({"step": "0.0001", "min": "0", "placeholder": "masalan 0.84"})
        self.fields["loaded_quantity_uom"].widget.attrs.setdefault("class", "form-select")
        self.fields["delivered_quantity_uom"].widget.attrs.setdefault("class", "form-select")


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = [
            "name",
            "contact_name",
            "phone",
            "sla_minutes",
            "contract_base_rate_per_ton",
            "contract_min_fee",
            "payment_terms",
            "is_active",
        ]
