from django import forms

from .models import Driver, Vehicle


class DriverForm(forms.ModelForm):
    class Meta:
        model = Driver
        fields = ["full_name", "phone", "telegram_user_id", "status"]


class VehicleForm(forms.ModelForm):
    class Meta:
        model = Vehicle
        fields = ["plate_number", "vehicle_type", "capacity_ton"]
