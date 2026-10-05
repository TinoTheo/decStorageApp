CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        # No inline scripts: an injected script on this page could read keys.
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
    ]
)


class ContentSecurityPolicyMiddleware:
    """Sets a strict CSP on every response that doesn't already carry one."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        return response
