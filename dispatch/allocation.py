from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from django.conf import settings

from drivers.models import Driver
from orders.models import Order


@dataclass
class DriverCapacity:
  driver: Driver
  capacity_kg: Decimal


@dataclass
class AllocationResult:
  order: Order
  allocations: list[tuple[Driver, Decimal]]
  remaining_kg: Decimal


def calculate_big_order_allocation(
  order: Order,
  *,
  drivers: Iterable[DriverCapacity],
) -> AllocationResult:
  """
  Katta zakazni haydovchilar o‘rtasida taqsimlash (tavsiya).
  """
  total_kg = (Decimal(order.weight_ton or 0) * Decimal("1000")).quantize(Decimal("1"))
  remaining = total_kg
  allocations: list[tuple[Driver, Decimal]] = []

  util_raw = getattr(settings, "BIG_ORDER_UTILIZATION", "0.90") or "0.90"
  try:
    utilization = Decimal(str(util_raw))
  except Exception:
    utilization = Decimal("0.90")
  if utilization <= 0 or utilization > 1:
    utilization = Decimal("0.90")

  sorted_drivers = sorted(
    drivers,
    key=lambda dc: (
      -(dc.driver.rating_score or Decimal("0")),
      -dc.capacity_kg,
    ),
  )

  for dc in sorted_drivers:
    if remaining <= 0:
      break
    if dc.capacity_kg <= 0:
      continue
    safe_cap = (dc.capacity_kg * utilization).quantize(Decimal("1"))
    if safe_cap <= 0:
      continue
    take = min(safe_cap, remaining)
    if take <= 0:
      continue
    allocations.append((dc.driver, take))
    remaining -= take

  if remaining < 0:
    remaining = Decimal("0")

  return AllocationResult(order=order, allocations=allocations, remaining_kg=remaining)

