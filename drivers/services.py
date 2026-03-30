"""Haydovchi reytingi: yetkazib berishdan keyingi sharhlardan va kamomad jarimalaridan."""

import logging
from decimal import Decimal

from django.conf import settings
from django.db.models import Avg, Sum

from orders.models import Order, OrderStatus

logger = logging.getLogger(__name__)


def get_driver_review_aggregates(driver) -> tuple[int, Decimal | None]:
    """Haydovchi bo‘yicha sharhlar soni va o‘rtacha yulduz (None agar sharh yo‘q)."""
    if driver is None or not getattr(driver, "pk", None):
        return 0, None
    from drivers.models import DriverDeliveryReview

    qs = DriverDeliveryReview.objects.filter(driver_id=driver.pk)
    n = qs.count()
    if n == 0:
        return 0, None
    raw = qs.aggregate(a=Avg("stars"))["a"]
    if raw is None:
        return n, None
    return n, Decimal(str(raw)).quantize(Decimal("0.01"))


def recompute_driver_rating_score(driver) -> None:
    """
    Reyting = (barcha sharhlarning o‘rtacha yulduzi) × 20 [0…100] −
    barcha yakunlangan buyurtmalardagi shortage_penalty_points yig‘indisi.
    Sharh bo‘lmasa asos 100.

    Faqat COMPLETED buyurtmalar jarimasi yig‘iladi (bir buyurtma — bir biriktirish).
    """
    if driver is None:
        return
    pk = getattr(driver, "pk", None)
    if not pk:
        return

    from drivers.models import Driver, DriverDeliveryReview

    agg = DriverDeliveryReview.objects.filter(driver_id=pk).aggregate(avg=Avg("stars"))
    avg = agg["avg"]
    if avg is None:
        base = Decimal("100")
    else:
        base = (Decimal(str(avg)) * Decimal("20")).quantize(Decimal("0.01"))

    pen_raw = (
        Order.objects.filter(
            status=OrderStatus.COMPLETED,
            assignment__driver_id=pk,
        ).aggregate(t=Sum("shortage_penalty_points"))["t"]
    )
    try:
        pen = Decimal(int(pen_raw or 0))
    except (TypeError, ValueError):
        pen = Decimal("0")

    minimum = Decimal(str(getattr(settings, "SHORTAGE_RATING_MIN", 0) or 0))
    if minimum > Decimal("100"):
        minimum = Decimal("100")
    eff = base - pen
    if eff < minimum:
        eff = minimum
    if eff > Decimal("100"):
        eff = Decimal("100")

    try:
        d = Driver.objects.get(pk=pk)
    except Driver.DoesNotExist:
        logger.warning("recompute_driver_rating_score: driver pk=%s topilmadi", pk)
        return
    d.rating_score = eff
    d.save(update_fields=["rating_score", "updated_at"])
