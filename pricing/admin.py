from django.contrib import admin

from .models import PriceQuote, TenderBid, TenderSession


@admin.register(PriceQuote)
class PriceQuoteAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "order",
        "distance_km",
        "base_rate",
        "weight_ton",
        "wait_minutes",
        "empty_return_km",
        "peak_coef",
        "distance_cost",
        "weight_cost",
        "wait_cost",
        "empty_return_cost",
        "cargo_coef",
        "suggested_price",
        "final_price",
        "is_approved",
        "created_at",
    )
    list_filter = ("is_approved",)


@admin.register(TenderSession)
class TenderSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "opened_by", "duration_minutes", "opened_at", "closed_at", "auto_selected_bid")
    list_filter = ("duration_minutes", "closed_at")


@admin.register(TenderBid)
class TenderBidAdmin(admin.ModelAdmin):
    list_display = ("id", "session", "bidder_name", "bid_price", "eta_minutes", "quality_score", "score", "created_at")
    list_filter = ("quality_score",)
