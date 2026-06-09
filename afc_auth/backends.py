from django.contrib.auth.backends import BaseBackend
from django.contrib.auth import get_user_model
from django.db.models import Q

User = get_user_model()

class EmailOrUsernameModelBackend(BaseBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        # Guard empty/missing credentials. Without this, an empty login body
        # passes username=None and password=None. The lookup below then becomes
        # Q(username=None) | Q(uid=None), which matches EVERY row whose uid is
        # NULL (uid is a nullable field, so most accounts match). User.objects.get
        # would then raise MultipleObjectsReturned, which bubbled up to the login
        # view as an unhandled 500 instead of a clean 401. Short-circuit to None
        # so authenticate fails normally and the view returns "invalid credentials".
        if not username or not password:
            return None

        try:
            # Match the typed identifier against in-game name (username), game UID, OR
            # email (case-insensitive). This lets users log in with any of the three
            # (owner request 2026-06-09: email login was missing despite the class name,
            # the lookup previously only checked username + uid). The non-empty guard
            # above keeps Q(email__iexact=username) from matching rows with a NULL email.
            user = User.objects.get(
                Q(username=username) | Q(uid=username) | Q(email__iexact=username)
            )
            print("user found")
        except User.DoesNotExist:
            print("user not found")
            return None
        except User.MultipleObjectsReturned:
            # prevents MultipleObjectsReturned: an ambiguous identifier must not
            # authenticate anyone. Treat it as a failed login (returns None -> 401).
            print("ambiguous identifier, refusing login")
            return None

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
