from django.urls import path

from .views import (
    accounting_pnl_export_csv,
    accounting_pnl_report,
    client_360_report,
    clients_monthly_yearly_report,
    clients_rating_report,
    export_clients_report_pdf,
    export_clients_report_csv,
    export_clients_yearly_report_csv,
    export_drivers_report_csv,
    export_drivers_report_xlsx,
    generate_monthly_report,
    live_fleet_data,
    live_fleet_map,
    ops_dashboard,
)

urlpatterns = [
    path("accounting/pnl/", accounting_pnl_report, name="accounting-pnl-report"),
    path("accounting/pnl/export.csv", accounting_pnl_export_csv, name="accounting-pnl-export-csv"),
    path("dashboard/", ops_dashboard, name="ops-dashboard"),
    path("live-fleet/", live_fleet_map, name="live-fleet-map"),
    path("live-fleet/data/", live_fleet_data, name="live-fleet-data"),
    path("generate-monthly/", generate_monthly_report, name="generate-monthly-report"),
    path("export/clients.csv", export_clients_report_csv, name="export-clients-report-csv"),
    path("export/clients.pdf", export_clients_report_pdf, name="export-clients-report-pdf"),
    path("export/clients-yearly.csv", export_clients_yearly_report_csv, name="export-clients-yearly-report-csv"),
    path("export/drivers.csv", export_drivers_report_csv, name="export-drivers-report-csv"),
    path("export/drivers.xlsx", export_drivers_report_xlsx, name="export-drivers-report-xlsx"),
    path("clients-rating/", clients_rating_report, name="clients-rating-report"),
    path("clients-reports/", clients_monthly_yearly_report, name="clients-monthly-yearly-report"),
    path("clients/<int:client_id>/360/", client_360_report, name="client-360-report"),
]
