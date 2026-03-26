from django.urls import path

from .views import webhook
from .webapp_views import trip_map_ketdik, trip_map_live_ping, trip_map_webapp

urlpatterns = [
    path("webhook/", webhook, name="telegram-webhook"),
    path(
        "webapp/trip/<int:order_id>/<str:token>/ketdik/",
        trip_map_ketdik,
        name="telegram-trip-map-ketdik",
    ),
    path(
        "webapp/trip/<int:order_id>/<str:token>/live-ping/",
        trip_map_live_ping,
        name="telegram-trip-map-live-ping",
    ),
    path(
        "webapp/trip/<int:order_id>/<str:token>/",
        trip_map_webapp,
        name="telegram-trip-map-webapp",
    ),
]
