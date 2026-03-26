from django.contrib import admin
from django.urls import include, path

from common.views import health_view, prometheus_metrics_view

urlpatterns = [
    path("health/", health_view, name="health"),
    path("metrics/", prometheus_metrics_view, name="prometheus-metrics"),
    path("admin/", admin.site.urls),
    path("", include("blog.urls")),
    path("api/", include("api.urls")),
    path("orders/", include("orders.urls")),
    path("drivers/", include("drivers.urls")),
    path("bot/", include("bot.urls")),
    path("analytics/", include("analytics.urls")),
]
