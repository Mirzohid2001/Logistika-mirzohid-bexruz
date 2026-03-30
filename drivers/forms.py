from django import forms
from django.core.exceptions import ValidationError

from .models import Driver, DriverDeliveryReview, Vehicle


class DriverForm(forms.ModelForm):
    class Meta:
        model = Driver
        fields = ["full_name", "phone", "telegram_user_id", "status"]


class VehicleForm(forms.ModelForm):
    class Meta:
        model = Vehicle
        fields = ["plate_number", "vehicle_type", "capacity_ton"]


MAX_REVIEW_COMMENT_LEN = 2000


class DriverDeliveryReviewForm(forms.ModelForm):
    class Meta:
        model = DriverDeliveryReview
        fields = ["stars", "comment"]
        labels = {
            "stars": "Baho (1–5 yulduz)",
            "comment": "Izoh (ixtiyoriy)",
        }
        help_texts = {
            "stars": "1 yulduz — juda yomon, 5 — a’lo. Reyting: o‘rtacha yulduz × 20 ball (0–100).",
            "comment": f"Eng ko‘pi bilan {MAX_REVIEW_COMMENT_LEN} belgi.",
        }

    def __init__(self, *args, **kwargs):
        instance = kwargs.get("instance")
        super().__init__(*args, **kwargs)
        star_choices = [(i, f"{i} yulduz") for i in range(1, 6)]
        if not instance or not getattr(instance, "pk", None):
            star_choices = [("", "— Tanlang —")] + star_choices
        self.fields["stars"].widget = forms.Select(
            choices=star_choices,
            attrs={"class": "form-select"},
        )
        self.fields["stars"].required = True
        self.fields["comment"].widget.attrs.setdefault("class", "form-control")
        self.fields["comment"].widget.attrs.setdefault("rows", "3")
        self.fields["comment"].widget.attrs.setdefault("maxlength", str(MAX_REVIEW_COMMENT_LEN))

    def clean_stars(self):
        s = self.cleaned_data.get("stars")
        if s is None or s == "":
            raise ValidationError("Yulduzlarni tanlang.")
        try:
            n = int(s)
        except (TypeError, ValueError):
            raise ValidationError("Noto‘g‘ri baho.")
        if n < 1 or n > 5:
            raise ValidationError("1 dan 5 gacha yulduz tanlang.")
        return n

    def clean_comment(self):
        text = (self.cleaned_data.get("comment") or "").strip()
        if len(text) > MAX_REVIEW_COMMENT_LEN:
            raise ValidationError(f"Izoh {MAX_REVIEW_COMMENT_LEN} belgidan oshmasin.")
        return text
