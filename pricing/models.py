from django.db import models

from orders.models import Order


class PriceQuote(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="quotes")
    distance_km = models.DecimalField(max_digits=10, decimal_places=2)
    base_rate = models.DecimalField(max_digits=12, decimal_places=2)
    weight_ton = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    wait_minutes = models.PositiveIntegerField(default=0)
    empty_return_km = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    peak_coef = models.DecimalField(max_digits=6, decimal_places=2, default=1)
    distance_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    weight_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    wait_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    empty_return_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cargo_coef = models.DecimalField(max_digits=6, decimal_places=2, default=1)
    suggested_price = models.DecimalField(max_digits=12, decimal_places=2)
    final_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    is_approved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class TenderSession(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="tender_sessions")
    opened_by = models.CharField(max_length=120, blank=True)
    duration_minutes = models.PositiveIntegerField(default=5)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    auto_selected_bid = models.ForeignKey(
        "TenderBid", on_delete=models.SET_NULL, null=True, blank=True, related_name="selected_sessions"
    )

    class Meta:
        ordering = ["-opened_at"]


class TenderBid(models.Model):
    session = models.ForeignKey(TenderSession, on_delete=models.CASCADE, related_name="bids")
    bidder_name = models.CharField(max_length=120)
    bid_price = models.DecimalField(max_digits=12, decimal_places=2)
    eta_minutes = models.PositiveIntegerField(default=0)
    quality_score = models.DecimalField(max_digits=6, decimal_places=2, default=100)
    score = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["score", "eta_minutes", "bid_price"]
