"""Empty URLConf for test settings.

The legacy `afc.urls` triggers import of every app's views.py on URL resolution
which pulls in pandas/easyocr/sympy/etc. Running tests does not require URL
routing for the wallet/wager apps (we test services, models, settlement —
not HTTP). An empty urlpatterns list keeps Django happy.
"""

urlpatterns = []
