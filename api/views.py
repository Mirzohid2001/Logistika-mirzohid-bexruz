from rest_framework import generics, permissions, status
from rest_framework.authtoken.models import Token
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.response import Response
from rest_framework.views import APIView

from analytics.models import ClientAnalyticsSnapshot, DriverPerformanceSnapshot
from drivers.models import Driver
from orders.models import Client, Order

from .serializers import (
    ClientAnalyticsSerializer,
    ClientSerializer,
    DriverPerformanceSerializer,
    DriverSerializer,
    OrderSerializer,
)


class StaffObtainAuthToken(ObtainAuthToken):
    """
    POST /api/auth/token/ — faqat staff foydalanuvchilar token oladi (list API bilan mos).
    """

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        if not user.is_staff:
            return Response(
                {"detail": "Faqat xodim (staff) akkauntlariga API token beriladi."},
                status=status.HTTP_403_FORBIDDEN,
            )
        token, _created = Token.objects.get_or_create(user=user)
        return Response({"token": token.key})


class StaffReadOnlyPermission(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.is_staff
            and request.method in permissions.SAFE_METHODS
        )


class ClientListApi(generics.ListAPIView):
    permission_classes = [StaffReadOnlyPermission]
    serializer_class = ClientSerializer
    queryset = Client.objects.order_by("name")


class DriverListApi(generics.ListAPIView):
    permission_classes = [StaffReadOnlyPermission]
    serializer_class = DriverSerializer
    queryset = Driver.objects.order_by("full_name")


class OrderListApi(generics.ListAPIView):
    permission_classes = [StaffReadOnlyPermission]
    serializer_class = OrderSerializer
    queryset = Order.objects.select_related("client").order_by("-created_at")


class DriverAnalyticsListApi(generics.ListAPIView):
    permission_classes = [StaffReadOnlyPermission]
    serializer_class = DriverPerformanceSerializer
    queryset = DriverPerformanceSnapshot.objects.order_by("-period_year", "-period_month")


class ClientAnalyticsListApi(generics.ListAPIView):
    permission_classes = [StaffReadOnlyPermission]
    serializer_class = ClientAnalyticsSerializer
    queryset = ClientAnalyticsSnapshot.objects.order_by("-period_year", "-period_month")


class StaffWritePermission(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.is_staff
            and request.method == "POST"
        )


class RotateOwnTokenApi(APIView):
    permission_classes = [StaffWritePermission]

    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        token = Token.objects.create(user=request.user)
        return Response({"token": token.key}, status=status.HTTP_201_CREATED)
