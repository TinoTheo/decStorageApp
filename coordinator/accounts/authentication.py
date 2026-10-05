from rest_framework import authentication, exceptions

from .models import AuthToken


class HashedTokenAuthentication(authentication.BaseAuthentication):
    """`Authorization: Token <token>` checked against hashed, expiring tokens."""

    keyword = "Token"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword.lower().encode():
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Invalid token header.")
        try:
            raw = header[1].decode("ascii")
        except UnicodeDecodeError as exc:
            raise exceptions.AuthenticationFailed("Invalid token header.") from exc

        token = AuthToken.lookup(raw)
        if token is None or not token.user.is_active:
            raise exceptions.AuthenticationFailed("Invalid or expired token.")
        token.touch()
        return token.user, token

    def authenticate_header(self, request):
        return self.keyword
