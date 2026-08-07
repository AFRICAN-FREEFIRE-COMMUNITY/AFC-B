from django.contrib.auth.backends import BaseBackend
from django.contrib.auth import get_user_model

from .identifiers import resolve_login_identifier

User = get_user_model()

class EmailOrUsernameModelBackend(BaseBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        # Guard empty/missing credentials. Without this, an empty login body passes username=None
        # and password=None, and the resolution below would filter on None values - which on the
        # NULLABLE uid column used to match every UID-less user (hundreds of rows) and raise
        # MultipleObjectsReturned, surfacing in the login view as an unhandled 500 instead of a
        # clean 401. Short-circuit to None so authenticate fails normally and the view returns
        # "invalid credentials". KEEP THIS: tests_login_identifiers pins it, because a regression
        # here 500s the login endpoint rather than merely misbehaving.
        if not username or not password:
            return None

        # Resolve the typed identifier to at most ONE user, trying email, then in-game name, then
        # UID (afc_auth/identifiers.py LOGIN_IDENTIFIER_PRECEDENCE - the order and the reasoning
        # behind it live there). This replaces a single Q(username) | Q(uid) | Q(email) .get(),
        # which matched two rows whenever one string was one account's name and another's UID and
        # then refused the login for BOTH of them; 10 such pairs existed in the live table.
        #
        # Resolution is settled HERE, before the password is looked at, and the password is then
        # checked ONCE against that one row. It deliberately does not retry the next column on a
        # password mismatch: that would turn one typed string into a password probe across up to
        # three accounts, on a path with no rate limiting at all. See resolve_login_identifier.
        user = resolve_login_identifier(username)
        if user is None:
            print("user not found")
            return None
        print("user found")

        if user.check_password(password):
            print("password correct")
            return user
        print("password not correct")
        return None

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
