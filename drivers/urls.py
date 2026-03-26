from django.urls import path

from .views import (
    driver_archive,
    driver_create,
    driver_detail,
    telegram_file_preview,
    driver_verify_approve,
    driver_verify_reject,
    driver_edit,
    driver_list,
    driver_restore,
    vehicle_create,
    vehicle_delete,
    vehicle_edit,
)

urlpatterns = [
    path("", driver_list, name="driver-list"),
    path("new/", driver_create, name="driver-create"),
    path("<int:driver_id>/edit/", driver_edit, name="driver-edit"),
    path("<int:driver_id>/archive/", driver_archive, name="driver-archive"),
    path("<int:driver_id>/restore/", driver_restore, name="driver-restore"),
    path("<int:driver_id>/", driver_detail, name="driver-detail"),
    path("telegram-file/<str:file_id>/", telegram_file_preview, name="driver-telegram-file"),
    path("<int:driver_id>/verify/approve/", driver_verify_approve, name="driver-verify-approve"),
    path("<int:driver_id>/verify/reject/", driver_verify_reject, name="driver-verify-reject"),
    path("<int:driver_id>/vehicles/new/", vehicle_create, name="vehicle-create"),
    path("<int:driver_id>/vehicles/<int:vehicle_id>/edit/", vehicle_edit, name="vehicle-edit"),
    path("<int:driver_id>/vehicles/<int:vehicle_id>/delete/", vehicle_delete, name="vehicle-delete"),
]
