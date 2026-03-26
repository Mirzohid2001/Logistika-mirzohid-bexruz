from django.urls import path

from api.views import (
    ClientAnalyticsListApi,
    ClientListApi,
    DriverAnalyticsListApi,
    DriverListApi,
    OrderListApi,
    RotateOwnTokenApi,
    StaffObtainAuthToken,
)

urlpatterns = [
    path("auth/token/", StaffObtainAuthToken.as_view(), name="api-auth-token"),
    path("auth/token/rotate/", RotateOwnTokenApi.as_view(), name="api-auth-token-rotate"),
    path("clients/", ClientListApi.as_view(), name="api-clients"),
    path("drivers/", DriverListApi.as_view(), name="api-drivers"),
    path("orders/", OrderListApi.as_view(), name="api-orders"),
    path("analytics/drivers/", DriverAnalyticsListApi.as_view(), name="api-analytics-drivers"),
    path("analytics/clients/", ClientAnalyticsListApi.as_view(), name="api-analytics-clients"),
]
