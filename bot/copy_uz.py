"""
Telegram foydalanuvchilariga chiqadigan matnlar (o‘zbekcha, qisqa va aniq).
"""

DRIVER_HELP = (
    "<b>🚚 Haydovchi buyruqlari</b>\n\n"
    "<b>Pastdagi tugmalar</b> (reply-klaviatura) xuddi shu buyruqlarning o‘rnini bosadi: "
    "«Safarni boshlash» → <code>/start_trip</code>, «Tugatish so‘rovi» → <code>/finish_trip</code> va hokazo.\n\n"
    "<b>Hajm (yo‘qotish nazorati)</b>\n"
    "• <code>/yuklandi 10.5 tonna</code> — zavoddan chiqqan fakt\n"
    "• <code>/yuklandi 12500 kg</code>\n"
    "• <code>/topshirildi 10000 litr 0.84</code> — litr + zichlik kg/L\n"
    "• <code>/zichlik 0.84</code> — keyingi litr kiritishlar uchun (bir marta)\n\n"
    "• <code>/trip_map</code> yoki «🗺 Reys xaritasi» — marshrutni Telegram <b>ichida</b> ko‘rish uchun "
    "mini-ilova (<code>TELEGRAM_WEBAPP_BASE_URL</code>); pinlar faqat Web App yo‘q bo‘lsa yuboriladi "
    "(pin bosilganda ko‘pincha tashqi xarita ochiladi). Yandex havolalari — ixtiyoriy "
    "(<code>TRIP_MAP_SHOW_YANDEX_LINKS</code>)\n"
    "• <code>/wizard</code> — tezkor qadamlar (yo‘lda emas bo‘lsa)\n"
    "• <code>/add_vehicle</code> — yangi mashina qo‘shish (davlat raqami + sig‘im)\n"
    "• <code>/start_trip</code> [buyurtma_id] — safarni boshlash (xabar ichida "
    "<b>xarita havolalari</b> chiqadi — bosganda Yandex ochiladi; koordinata "
    "<code>lat, lon</code> buyurtmada bo‘lsa marshrut ham bo‘ladi)\n"
    "• <code>/finish_trip</code> [buyurtma_id] — tugatish so‘rovi (keyin <b>admin web</b>da tasdiq)\n"
    "• <code>/checkpoint</code> [id] [matn] — oraliq eslatma\n"
    "• <code>/trip_summary</code> [id] — qisqa hisobot\n\n"
    "<b>📍 Lokatsiya</b>\n"
    "📎 → Location → <b>Share Live Location</b> (muddat — reys oxirigacha). "
    "Yoki «📍 GPS (bir marta)». Admin webda Live flot va buyurtma xaritasida ko‘radi."
)

ORDER_NOT_FOUND = "Buyurtma topilmadi."

DRIVER_NOT_FOUND = "Haydovchi topilmadi."

UNKNOWN_COMMAND_DRIVER = "Buyruq aniqlanmadi. Yordam: <code>/help</code>"

# Yo‘lda (IN_TRANSIT) — qisqa: to‘liq DRIVER_HELP o‘rniga
DRIVER_HELP_IN_TRANSIT = (
    "<b>🚛 Siz hozir yo‘ldasiz</b>\n\n"
    "• «🗺 Reys xaritasi» — Telegram <b>ichida</b> marshrut (Web App; <code>.env</code> da "
    "<code>TELEGRAM_WEBAPP_BASE_URL=https://…</code> majburiy)\n"
    "• <code>/trip_map</code> — matn + (Web App yo‘q bo‘lsa) pinlar\n"
    "• 📎 → Joylashuv → <b>Jonli joylashuv</b> (admin kuzatadi)\n"
    "• «📝 Tugatish so‘rovi» / <code>/finish_trip</code> — admin webda tasdiqlagach yakunlanadi\n\n"
    "Boshqa buyurtmani guruhda qabul qila olmaysiz — bu normal."
)

REGISTER_FIRST = "Avval <code>/start</code> ni bosing va telefon raqamingizni ulang."

WEB_ONLY_ACTION = (
    "🔒 Bu amal faqat <b>web-panel</b> orqali: admin akkaunti bilan kiring."
)

# CallbackQuery.show_alert uchun HTML ishlatilmaydi — qisqa matn.
WEB_ONLY_CALLBACK_ANSWER = "Bu amal faqat web-panel orqali (admin)."
