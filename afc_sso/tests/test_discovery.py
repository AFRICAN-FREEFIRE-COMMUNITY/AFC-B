# backend/afc_sso/tests/test_discovery.py
"""Discovery smoke test: partners bootstrap their whole integration from this document,
so if it 500s or omits an endpoint, nobody can integrate. Consumed by every OIDC client
library the partner might use."""
import json

from django.test import TestCase


class DiscoveryDocumentTests(TestCase):
    def test_discovery_document_advertises_the_endpoints_partners_need(self):
        resp = self.client.get("/sso/.well-known/openid-configuration/")
        self.assertEqual(resp.status_code, 200)
        doc = json.loads(resp.content)
        for key in (
            "issuer",
            "authorization_endpoint",
            "token_endpoint",
            "userinfo_endpoint",
            "jwks_uri",
        ):
            self.assertIn(key, doc)
        self.assertIn("code", doc["response_types_supported"])
        self.assertIn("RS256", doc["id_token_signing_alg_values_supported"])

    def test_jwks_endpoint_publishes_a_key(self):
        resp = self.client.get("/sso/.well-known/jwks.json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.content)["keys"])
