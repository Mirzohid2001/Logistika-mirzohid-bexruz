from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect

# --- Rollar (veb) ---
# Alohicha dispetcher lavozimi yo‘q: barcha operatsiyani admin (Owner) boshqaradi.
# «Dispatcher» guruhi tarixiy moslik uchun qolgan; setup_roles da Owner bilan bir xil ruxsat beriladi.
WEB_OPERATION_GROUPS = ("Owner", "Dispatcher")
WEB_PANEL_GROUPS = ("Owner", "Dispatcher", "Finance", "Analyst")


def groups_required(*group_names):
    required = set(group_names)

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)
            user_groups = set(request.user.groups.values_list("name", flat=True))
            if required.intersection(user_groups):
                return view_func(request, *args, **kwargs)
            messages.error(request, "Bu sahifaga kirish uchun ruxsat yo‘q. Admin (Owner yoki mos guruh) ga murojaat qiling.")
            return redirect("admin:index")

        return _wrapped

    return decorator
