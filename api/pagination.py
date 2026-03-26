from django.conf import settings
from rest_framework.pagination import PageNumberPagination


class StandardResultsSetPagination(PageNumberPagination):
    page_size_query_param = "page_size"
    max_page_size = int(getattr(settings, "API_MAX_PAGE_SIZE", 100) or 100)
