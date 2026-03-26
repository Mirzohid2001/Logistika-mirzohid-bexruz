from decimal import Decimal
from typing import Any


def suggest_price(weight_ton: Decimal) -> Decimal:
    base = Decimal("350000")
    ton_coef = Decimal("45000")
    total = base + (weight_ton * ton_coef)
    return total.quantize(Decimal("1"))


def build_price_breakdown(
    *,
    distance_km: Decimal,
    weight_ton: Decimal,
    wait_minutes: int = 0,
    empty_return_km: Decimal = Decimal("0"),
    peak_coef: Decimal = Decimal("1.00"),
    cargo_coef: Decimal = Decimal("1.00"),
) -> dict[str, Any]:
    base_rate_per_km = Decimal("8200")
    weight_rate_per_ton = Decimal("42000")
    wait_rate_per_min = Decimal("1200")
    empty_return_rate_per_km = Decimal("3800")

    distance_cost = (distance_km * base_rate_per_km).quantize(Decimal("0.01"))
    weight_cost = (weight_ton * weight_rate_per_ton).quantize(Decimal("0.01"))
    wait_cost = (Decimal(wait_minutes) * wait_rate_per_min).quantize(Decimal("0.01"))
    empty_return_cost = (empty_return_km * empty_return_rate_per_km).quantize(Decimal("0.01"))
    subtotal = distance_cost + weight_cost + wait_cost + empty_return_cost
    suggested_price = (subtotal * peak_coef * cargo_coef).quantize(Decimal("0.01"))

    return {
        "base_rate": base_rate_per_km,
        "distance_cost": distance_cost,
        "weight_cost": weight_cost,
        "wait_cost": wait_cost,
        "empty_return_cost": empty_return_cost,
        "peak_coef": peak_coef,
        "cargo_coef": cargo_coef,
        "suggested_price": suggested_price,
    }


def evaluate_tender_bid(bid_price: Decimal, eta_minutes: int, quality_score: Decimal = Decimal("100")) -> Decimal:
    price_component = bid_price / Decimal("100000")
    eta_component = Decimal(eta_minutes) * Decimal("0.4")
    quality_component = (Decimal("100") - quality_score) * Decimal("0.3")
    return (price_component + eta_component + quality_component).quantize(Decimal("0.01"))
