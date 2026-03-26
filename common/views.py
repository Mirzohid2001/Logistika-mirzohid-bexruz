"""
Healthcheck va minimal Prometheus matn (infratuzilma monitoring).
"""
import time

from django.conf import settings
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def health_view(_request):
    checks: dict = {"database": "ok"}
    try:
        connection.ensure_connection()
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        return JsonResponse({"status": "unhealthy", "checks": checks}, status=503)
    return JsonResponse({"status": "ok", "checks": checks})


_prometheus_start_time = time.time()


@require_GET
def prometheus_metrics_view(_request):
    if not getattr(settings, "PROMETHEUS_METRICS_ENABLED", False):
        return HttpResponse("Metrics disabled", status=404, content_type="text/plain")
    uptime = time.time() - _prometheus_start_time
    lines = [
        "# HELP django_process_uptime_seconds Process uptime in seconds",
        "# TYPE django_process_uptime_seconds gauge",
        f"django_process_uptime_seconds {uptime:.3f}",
        "# HELP django_up Django app reports ready",
        "# TYPE django_up gauge",
        "django_up 1",
        "",
    ]
    return HttpResponse("".join(lines), content_type="text/plain; version=0.0.4")
