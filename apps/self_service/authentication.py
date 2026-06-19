"""
Optional JWT authentication — used on endpoints that must stay public
(AllowAny) but want to personalize the response when a valid customer
token happens to be present (e.g. plans page showing current plan).
"""
from rest_framework_simplejwt.authentication import JWTAuthentication


class OptionalJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        try:
            return super().authenticate(request)
        except Exception:
            # Invalid/expired/missing token -> treat as anonymous, don't 401
            return None