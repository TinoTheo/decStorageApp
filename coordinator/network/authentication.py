from rest_framework import authentication, exceptions, permissions

from .models import Node


class NodePrincipal:
    """Stands in for request.user on node endpoints. Nodes are not user accounts."""

    is_authenticated = True
    is_anonymous = False
    is_staff = False

    def __init__(self, node):
        self.node = node

    def __str__(self):
        return f"node:{self.node.pk}"


class NodeTokenAuthentication(authentication.BaseAuthentication):
    """
    `Authorization: Node <token>`. Kept separate from user tokens on purpose:
    a node token can't call user endpoints, and a user token can't call node ones.
    """

    keyword = "Node"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword.lower().encode():
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Invalid node token header.")
        try:
            raw = header[1].decode("ascii")
        except UnicodeDecodeError as exc:
            raise exceptions.AuthenticationFailed("Invalid node token header.") from exc
        node = Node.from_token(raw)
        if node is None:
            raise exceptions.AuthenticationFailed("Unknown node token.")
        return NodePrincipal(node), node

    def authenticate_header(self, request):
        return self.keyword


class IsNode(permissions.BasePermission):
    def has_permission(self, request, view):
        return isinstance(request.auth, Node)
