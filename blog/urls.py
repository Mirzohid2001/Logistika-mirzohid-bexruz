from django.urls import path

from .views import home, home_kpis

urlpatterns = [
    path("", home, name="home"),
    path("kpis/", home_kpis, name="home-kpis"),
]
