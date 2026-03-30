# Operatsiya va monitoring

## HTTP endpointlar

| Yo‘l | Maqsad |
|------|--------|
| `GET /health/` | JSON: `status`, `checks.database`. DB ulanishi yo‘q bo‘lsa **503**. |
| `GET /metrics/` | Prometheus matn (`text/plain`). **404** agar `PROMETHEUS_METRICS_ENABLED=False` (standart). |

Load balancer yoki Kubernetes probe sifatida `/health/` ishlatiladi. Prometheus scrape uchun `PROMETHEUS_METRICS_ENABLED=True` qiling va `/metrics/` ni target qiling.

## REST API autentifikatsiya

- `POST /api/auth/token/` — faqat **staff** Django foydalanuvchilariga token beriladi (read API bilan bir xil siyosat).
- `POST /api/auth/token/rotate/` — staff o‘z tokenini almashtiradi.

## Telegram webhook

- `update_id` bo‘yicha qisqa muddatli cache: xuddi shu yangilanish ikki marta kelganda handler ikkinchi marta ishlamaydi (Redis/LocMem umumiy bo‘lishi kerak).

## Logging

- `LOG_LEVEL` — masalan `INFO`, `DEBUG`.
- `LOG_JSON=True` — konsolga bir qatorli JSON (structured logging uchun log shipper).

## Redis va bir nechta worker

- `REDIS_CACHE_URL` — prod’da Redis; testlar `LocMemCache` ishlatadi.
- Bir nechta Gunicorn worker + Redis: cache kalitlari (`ymap:geo:…`, Telegram edit lock va hokazo) barcha protsesslar o‘rtasida umumiy bo‘lishi kerak.

## Veb rollar (admin)

Alohicha **dispetcher** lavozimi yo‘q: barcha operatsiyani **admin** (`Owner` guruhi) boshqaradi. `Dispatcher` guruhi tarixiy moslik uchun qolgan; `setup_roles` uni Owner bilan **bir xil** ruxsatlar bilan to‘ldiradi. Yangi foydalanuvchilarni **Owner** ga qo‘shish tavsiya etiladi.

**Telegram bot** faqat **haydovchilar** uchun; admin/dispatcher buyruqlari botda yo‘q — operatsiya **web-panel** orqali.

## Buxgalteriya / biznes switch’lar

**Shofir standart biznes modeli:** klientlar platformaga to‘lamaydi; `client_price` / `gross_revenue` maydonlari texnik (yangi buyurtmalar uchun 0). Asosiy moliyaviy nazorat — haydovchi to‘lovi va ichki xarajatlar (yoqilg‘i, qo‘shimcha, jarima).

Quyidagilar **sizning hisob siyosatingizga mos** ekanini alohida tasdiqlang:

- **`ANALYTICS_REVENUE_SUM_COMPLETED_ONLY`** (standart `True`): oylik/analitika `gross_revenue` (nomlangan, lekin `client_price` yig‘indisi) / haydovchi xarajatlari yig‘indisida faqat **COMPLETED** buyurtmalar hisoblanadi.
- **`ORDER_RESET_FINANCIALS_ON_ISSUE_OR_CANCELED`** (standart `True`): buyurtma **ISSUE** yoki **CANCELED** bo‘lganda narx/to‘lov maydonlari va tegishli ledgerlar nolga (yoki bo‘sh) qaytariladi. `False` bo‘lsa quote/moliya saqlanadi — bu audit va hisobotga ta’sir qiladi.

Boshqa muhim env’lar: `TENDER_DURATION_MIN_MINUTES`, `TENDER_DURATION_MAX_MINUTES`, `SPLIT_SHIPMENT_MAX_PARTS`, `ORDERS_LIST_PER_PAGE`, `API_PAGE_SIZE`, `API_MAX_PAGE_SIZE`.

## Backup va DR (kod tashqarisi)

- Ma’lumotlar bazasi: muntazam snapshot / dump (vaqt jadvali va saqlash joyi siyosatingizga bog‘liq).
- Redis: cache yo‘qolishi qayta so‘rovlar bilan tiklanadi; broker (`CELERY_BROKER_URL`) uchun alohida backup strategiyasi.
- Media fayllar: `MEDIA_ROOT` zaxira nusxasi.
- `.env` va maxfiy kalitlar: secret manager, repoda emas.

## Lokal yuk smoke

```bash
python manage.py smoke_health 200
```

Bu Django test client orqali `/health/` ni takrorlaydi (tashqi server shart emas).
