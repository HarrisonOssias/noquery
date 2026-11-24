"""URL configuration for the Django integration tests. The engine is swapped in per test."""

from django.core.exceptions import PermissionDenied
from django.urls import include, path

from noquery.integrations.django import noquery_urls

state = {"nq": None, "errors": []}


class _Proxy:
    def __getattr__(self, name):
        return getattr(state["nq"], name)


def get_auth(request):
    org = request.headers.get("X-Test-Org")
    if not org:
        raise PermissionDenied("no organization")
    return {"userId": "u1", "orgId": org, "roles": ["analyst"]}


urlpatterns = [path("analytics/", include(noquery_urls(_Proxy(), get_auth, on_error=state["errors"].append)))]
