# Shofir Platform

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## Test

```bash
source .venv/bin/activate
python manage.py test
python manage.py check
```

## Core Endpoints

- `/` home
- `/orders/new/` order form
- `/orders/<id>/` order detail
- `/bot/webhook/` telegram webhook
- `/admin/` admin panel
- `/analytics/dashboard/` ops dashboard

## Security

- Web sahifalar `staff` login bilan himoyalangan.
- Login URL: `/admin/login/`
- Rol guruhlarini yaratish:

```bash
python manage.py setup_roles
```

## Analytics and Finance Commands

```bash
python manage.py build_monthly_reports --year 2026 --month 3
python manage.py reconcile_finance
python manage.py bootstrap_pilot --year 2026 --month 3
```

## Pilot Import (CSV)

Clients CSV columns:

```text
name,contact_name,phone,is_active
```

Drivers CSV columns:

```text
full_name,phone,status,telegram_user_id,plate_number,vehicle_type,capacity_ton
```

Import commands:

```bash
python manage.py import_clients /path/to/clients.csv
python manage.py import_drivers /path/to/drivers.csv
```

## Rollar (veb)

Alohicha dispetcher yo‘q: operatsiyani **admin** (Django guruh **Owner**) boshqaradi. `python manage.py setup_roles` — **Dispatcher** guruhi tarixiy nom, **Owner bilan bir xil to‘liq ruxsat** (migratsiya uchun).

## Telegram bot

Bot **faqat haydovchilar** uchun: buyurtma biriktirish, admin buyruqlari va boshqalar **web-panel**da.

## Driver Commands

- `/help`
- `/start_trip [order_id]`
- `/finish_trip [order_id]`
