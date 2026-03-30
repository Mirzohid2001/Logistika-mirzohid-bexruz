import logging

from django.db.models.signals import post_delete
from django.dispatch import receiver

from drivers.models import DriverDeliveryReview

logger = logging.getLogger(__name__)


@receiver(post_delete, sender=DriverDeliveryReview)
def recompute_driver_rating_on_review_delete(sender, instance, **kwargs):
    try:
        from drivers.services import recompute_driver_rating_score

        recompute_driver_rating_score(instance.driver)
    except Exception:
        logger.exception("Sharh o‘chirilgach reytingni qayta hisoblash muvaffaqiyatsiz (driver_id=%s)", instance.driver_id)
