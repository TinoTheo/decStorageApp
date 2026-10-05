from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle

from .serializers import normalize_username


class AuthIPRateThrottle(ScopedRateThrottle):
    """
    Generous per-IP limit. Mobile networks often put thousands of people behind
    one address (carrier-grade NAT), so this only stops floods from one source.
    """

    scope_attr = "throttle_scope"


class AuthUsernameRateThrottle(SimpleRateThrottle):
    """
    Tight per-account limit, so guessing one account's passphrase stays slow no
    matter how many addresses the guesses come from.
    """

    scope = "auth_user"

    def get_cache_key(self, request, view):
        username = request.data.get("username") if hasattr(request.data, "get") else None
        if not isinstance(username, str) or not username.strip():
            return None
        return self.cache_format % {"scope": self.scope, "ident": normalize_username(username)}
