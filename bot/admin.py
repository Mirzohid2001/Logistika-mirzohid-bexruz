from django.contrib import admin

from .models import TelegramMessageLog


@admin.register(TelegramMessageLog)
class TelegramMessageLogAdmin(admin.ModelAdmin):
    list_display = ("order", "chat_id", "message_id", "event", "created_at")
    search_fields = ("order__id", "chat_id", "message_id")
