"""Yuk hajmini bir xil o‘lchovga (metrik tonna) keltirish — yo‘ldagi yo‘qotishni ko‘rish uchun."""

from decimal import Decimal

from orders.models import QuantityUnit


def quantity_to_metric_tonnes(
    quantity: Decimal,
    uom: str,
    *,
    density_kg_per_liter: Decimal | None = None,
) -> Decimal | None:
    """
    Metrik tonnaga keltiradi.
    Litr uchun `density_kg_per_liter` (kg/L) majburiy; bo‘lmasa None.
    """
    q = Decimal(quantity)
    if uom == QuantityUnit.TON:
        return q
    if uom == QuantityUnit.KG:
        return (q / Decimal("1000")).quantize(Decimal("0.0001"))
    if uom == QuantityUnit.LITER:
        d = density_kg_per_liter
        if d is None or d <= 0:
            return None
        kg = q * Decimal(d)
        return (kg / Decimal("1000")).quantize(Decimal("0.0001"))
    return None


def shortage_tonnes(loaded_t: Decimal | None, delivered_t: Decimal | None) -> Decimal | None:
    """Ijobiy = yo‘lda kamaytirish (yuklanganidan ko‘ra kam topshirilgan)."""
    if loaded_t is None or delivered_t is None:
        return None
    return (loaded_t - delivered_t).quantize(Decimal("0.0001"))
