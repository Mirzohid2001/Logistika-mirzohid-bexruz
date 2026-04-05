from decimal import Decimal, InvalidOperation

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError

from .models import Client, Order, OrderExtraExpense, OrderSeal


def normalize_uz_phone_digits(raw: str) -> str:
    """Bo'sh yoki 9 ta raqam (998 prefiks yoki boshidagi 0 hisobga olinadi)."""
    import re

    if raw is None or not str(raw).strip():
        return ""
    s = re.sub(r"\D", "", str(raw))
    if s.startswith("998") and len(s) >= 12:
        s = s[3:]
    elif len(s) == 10 and s.startswith("0"):
        s = s[1:]
    if len(s) != 9 or not s.isdigit():
        raise ValidationError(
            "Telefon 9 ta raqam bo‘lishi kerak (masalan: 901234567 yoki +998 90 123 45 67)."
        )
    return s


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
            "penalty_amount",
        ]
        labels = {
            "client": "Klient",
            "from_location": "Qayerdan *",
            "to_location": "Qayerga *",
            "cargo_type": "Yuk turi *",
            "weight_ton": "Yuk og'irligi (kg) *",
            "pickup_time": "Yuk olish vaqti *",
            "contact_name": "Mas'ul shaxs (ixtiyoriy)",
            "contact_phone": "Telefon (ixtiyoriy)",
            "comment": "Izoh (ixtiyoriy)",
            "route_polyline": "Marshrut chizig'i (JSON)",
            "geofence_polygon": "Geofence hududi (JSON)",
            "route_deviation_threshold_km": "Marshrutdan og'ish limiti (km)",
            "driver_fee": "Shofyor xizmat haqqi",
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
        self.fields["contact_name"].required = False
        self.fields["contact_phone"].required = False
        self.fields["comment"].required = False
        self.fields["client"].required = False
        self.fields["from_location"].widget.attrs.update({"placeholder": "Masalan: Toshkent, Sergeli"})
        self.fields["to_location"].widget.attrs.update({"placeholder": "Masalan: Farg'ona, Quvasoy"})
        self.fields["route_polyline"].widget.attrs.update({"rows": 4})
        self.fields["geofence_polygon"].widget.attrs.update({"rows": 4})
        self.fields["route_deviation_threshold_km"].widget.attrs.update({"min": "0.1", "step": "0.1"})
        for money_field in ["driver_fee", "penalty_amount"]:
            self.fields[money_field].widget.attrs.update({"min": "0", "step": "1000", "placeholder": "0"})
        self.fields["penalty_amount"].required = False
        max_kg = int(getattr(settings, "ORDER_MAX_WEIGHT_KG", 200_000) or 200_000)
        self.fields["weight_ton"].help_text = (
            f"Kilogramm. Maksimal {max_kg} kg. Tizim ichki hisobda tonnaga aylantiradi (÷1000)."
        )
        self.fields["weight_ton"].widget.attrs.update(
            {
                "min": "0.001",
                "step": "any",
                "placeholder": "Masalan: 12500",
                "inputmode": "decimal",
                "title": f"Maks. {max_kg} kg",
            }
        )

    def clean_weight_ton(self):
        """Formada kg kiritiladi; model maydoni tonna sifatida saqlanadi."""
        kg = self.cleaned_data.get("weight_ton")
        if kg is None:
            return kg
        try:
            kg_dec = Decimal(str(kg))
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError("Og‘irlikni raqam bilan kiriting (nuqta yoki vergul).") from None
        if kg_dec <= 0:
            raise ValidationError("Og‘irlik 0 dan katta bo‘lishi kerak.")
        max_kg = Decimal(str(int(getattr(settings, "ORDER_MAX_WEIGHT_KG", 200_000) or 200_000)))
        if kg_dec > max_kg:
            raise ValidationError(
                f"Yuk og‘irligi juda katta ({kg_dec} kg). Maksimal ruxsat: {max_kg} kg — raqamni tekshiring."
            )
        tons = (kg_dec / Decimal("1000")).quantize(Decimal("0.01"))
        return tons

    def clean_contact_name(self):
        v = (self.cleaned_data.get("contact_name") or "").strip()
        return v

    def clean_contact_phone(self):
        raw = (self.cleaned_data.get("contact_phone") or "").strip()
        if not raw:
            return ""
        try:
            return normalize_uz_phone_digits(raw)
        except ValidationError:
            raise
        except Exception:
            raise ValidationError("Telefon formatini tekshiring.") from None

    def clean_penalty_amount(self):
        v = self.cleaned_data.get("penalty_amount")
        if v is None:
            return Decimal("0")
        return v


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
            "delivered_density_kg_per_liter",
        ]
        labels = {
            "loaded_quantity": "Yuklangan (fakt)",
            "loaded_quantity_uom": "Yuklangan o‘lchov",
            "delivered_quantity": "Klientga topshirilgan",
            "delivered_quantity_uom": "Topshirish o‘lchov",
            "density_kg_per_liter": "Zichlik kg/L (yuklangan, litr)",
            "delivered_density_kg_per_liter": "Zichlik kg/L (topshirish, litr)",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in (
            "loaded_quantity",
            "delivered_quantity",
            "density_kg_per_liter",
            "delivered_density_kg_per_liter",
        ):
            self.fields[name].widget.attrs.setdefault("class", "form-control")
        self.fields["loaded_quantity"].widget.attrs.update({"step": "0.001", "min": "0"})
        self.fields["delivered_quantity"].widget.attrs.update({"step": "0.001", "min": "0"})
        for dname in ("density_kg_per_liter", "delivered_density_kg_per_liter"):
            self.fields[dname].widget.attrs.update(
                {"step": "0.0001", "min": "0", "placeholder": "masalan 0.84"}
            )
        self.fields["loaded_quantity_uom"].widget.attrs.setdefault("class", "form-select")
        self.fields["delivered_quantity_uom"].widget.attrs.setdefault("class", "form-select")


class OrderSealAddForm(forms.ModelForm):
    class Meta:
        model = OrderSeal
        fields = ["compartment", "seal_number_loading"]
        labels = {
            "compartment": "Bo‘lim (ixtiyoriy)",
            "seal_number_loading": "Muhr raqami (yuklash)",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["compartment"].widget.attrs.setdefault("class", "form-control")
        self.fields["compartment"].required = False
        self.fields["seal_number_loading"].widget.attrs.setdefault("class", "form-control")


class OrderSealUpdateForm(forms.ModelForm):
    class Meta:
        model = OrderSeal
        fields = ["seal_number_unloading", "is_broken", "broken_note"]
        labels = {
            "seal_number_unloading": "Muhr (tushirishda)",
            "is_broken": "Muhr buzilgan",
            "broken_note": "Buzilish / farq izohi",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["seal_number_unloading"].widget.attrs.setdefault("class", "form-control")
        self.fields["broken_note"].widget.attrs.setdefault("class", "form-control")
        self.fields["broken_note"].widget.attrs.setdefault("rows", "2")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("is_broken"):
            new_note = (cleaned.get("broken_note") or "").strip()
            old_note = (self.instance.broken_note or "").strip()
            if not new_note and not old_note:
                raise ValidationError({"broken_note": "Buzilish belgilanganda izoh kiriting."})
        return cleaned


class OrderExtraExpenseForm(forms.ModelForm):
    class Meta:
        model = OrderExtraExpense
        fields = ["category", "amount", "note", "incurred_at"]
        labels = {
            "category": "Tur",
            "amount": "Summa",
            "note": "Izoh",
            "incurred_at": "Sana/vaqt",
        }
        widgets = {
            "incurred_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].widget.attrs.setdefault("class", "form-select")
        self.fields["amount"].widget.attrs.setdefault("class", "form-control")
        self.fields["amount"].widget.attrs.update({"min": "0", "step": "1000"})
        self.fields["note"].widget.attrs.setdefault("class", "form-control")
        self.fields["incurred_at"].widget.attrs.setdefault("class", "form-control")

    def clean_amount(self):
        amt = self.cleaned_data.get("amount")
        if amt is None or amt <= 0:
            raise ValidationError("Xarajat summasi 0 dan katta bo‘lishi kerak.")
        return amt


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
