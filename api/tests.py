from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token

from orders.models import Client


class ApiSecurityTests(TestCase):
    def setUp(self):
        Client.objects.create(name="API Client")
        self.staff = User.objects.create_user(username="api_staff", password="x", is_staff=True)
        self.non_staff = User.objects.create_user(username="api_user", password="x", is_staff=False)
        self.staff_token = Token.objects.create(user=self.staff)
        self.non_staff_token = Token.objects.create(user=self.non_staff)

    def test_api_requires_authentication(self):
        response = self.client.get(reverse("api-clients"))
        self.assertEqual(response.status_code, 401)

    def test_api_rejects_non_staff_token(self):
        response = self.client.get(
            reverse("api-clients"),
            HTTP_AUTHORIZATION=f"Token {self.non_staff_token.key}",
        )
        self.assertEqual(response.status_code, 403)

    def test_api_accepts_staff_token(self):
        response = self.client.get(
            reverse("api-clients"),
            HTTP_AUTHORIZATION=f"Token {self.staff_token.key}",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("results", body)
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["count"], 1)

    def test_api_clients_pagination_page_size(self):
        Client.objects.create(name="Second")
        response = self.client.get(
            reverse("api-clients"),
            {"page_size": 1},
            HTTP_AUTHORIZATION=f"Token {self.staff_token.key}",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["count"], 2)
        self.assertIsNotNone(body.get("next"))


class StaffAuthTokenTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username="tok_staff", password="secret", is_staff=True)
        self.regular = User.objects.create_user(username="tok_user", password="secret", is_staff=False)

    def test_auth_token_requires_staff(self):
        r = self.client.post(
            reverse("api-auth-token"),
            {"username": "tok_user", "password": "secret"},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403)

    def test_auth_token_issued_for_staff(self):
        r = self.client.post(
            reverse("api-auth-token"),
            {"username": "tok_staff", "password": "secret"},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("token", r.json())


class HealthMetricsTests(TestCase):
    def test_health_returns_ok(self):
        r = self.client.get(reverse("health"))
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data.get("status"), "ok")
        self.assertEqual(data.get("checks", {}).get("database"), "ok")

    def test_metrics_disabled_returns_404(self):
        with override_settings(PROMETHEUS_METRICS_ENABLED=False):
            r = self.client.get(reverse("prometheus-metrics"))
        self.assertEqual(r.status_code, 404)

    def test_metrics_enabled_returns_text(self):
        with override_settings(PROMETHEUS_METRICS_ENABLED=True):
            r = self.client.get(reverse("prometheus-metrics"))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"django_up", r.content)
