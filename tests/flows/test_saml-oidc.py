import time
from urllib.parse import urlparse, parse_qsl, urlencode

import pytest
from oic.oic.message import IdToken
from saml2 import BINDING_HTTP_REDIRECT
from saml2.config import SPConfig
from werkzeug.test import Client
from werkzeug.wrappers import BaseResponse

from satosa.metadata_creation.saml_metadata import create_entity_descriptors
from satosa.proxy_server import WsgiApplication
from satosa.satosa_config import SATOSAConfig
from tests.users import USERS
from tests.util import FakeSP


@pytest.fixture
def oidc_backend_config():
    data = {
        "module": "satosa.backends.openid_connect.OpenIDConnectBackend",
        "name": "OIDCBackend",
        "config": {
            "provider_metadata": {
                "issuer": "https://op.example.com",
                "authorization_endpoint": "https://example.com/authorization"
            },
            "client": {
                "auth_req_params": {
                    "response_type": "code",
                    "scope": "openid, profile, email, address, phone"
                },
                "client_metadata": {
                    "client_id": "backend_client",
                    "application_name": "SATOSA",
                    "application_type": "web",
                    "contacts": ["suppert@example.com"],
                    "redirect_uris": ["http://example.com/OIDCBackend"],
                    "subject_type": "public",
                }
            },
            "entity_info": {
                "contact_person": [{
                    "contact_type": "technical",
                    "email_address": ["technical_test@example.com", "support_test@example.com"],
                    "given_name": "Test",
                    "sur_name": "OP"
                }, {
                    "contact_type": "support",
                    "email_address": ["support_test@example.com"],
                    "given_name": "Support_test"
                }],
                "organization": {
                    "display_name": ["OP Identities", "en"],
                    "name": [["En test-OP", "se"], ["A test OP", "en"]],
                    "url": [["http://www.example.com", "en"], ["http://www.example.se", "se"]],
                    "ui_info": {
                        "description": [["This is a test OP", "en"]],
                        "display_name": [["OP - TEST", "en"]]
                    }
                }
            }
        }
    }

    return data


class TestSAMLToOIDC:
    def run_test(self, satosa_config_dict, sp_conf, oidc_backend_config, frontend_config):
        user_id = "testuser1"
        # proxy config
        satosa_config_dict["FRONTEND_MODULES"] = [frontend_config]
        satosa_config_dict["BACKEND_MODULES"] = [oidc_backend_config]
        satosa_config_dict["INTERNAL_ATTRIBUTES"]["attributes"] = {attr_name: {"openid": [attr_name],
                                                                               "saml": [attr_name]}
                                                                   for attr_name in USERS[user_id]}
        frontend_metadata, backend_metadata = create_entity_descriptors(SATOSAConfig(satosa_config_dict))

        # application
        app = WsgiApplication(config=SATOSAConfig(satosa_config_dict))
        test_client = Client(app, BaseResponse)

        # config test SP
        frontend_metadata_str = str(frontend_metadata[frontend_config["name"]][0])
        sp_conf["metadata"]["inline"].append(frontend_metadata_str)
        fakesp = FakeSP(SPConfig().load(sp_conf, metadata_construction=False))

        # create auth req
        req = urlparse(fakesp.make_auth_req(frontend_metadata[frontend_config["name"]][0].entity_id))
        auth_req = req.path + "?" + req.query

        # make auth req to proxy
        proxied_auth_req = test_client.get(auth_req)
        assert proxied_auth_req.status == "302 Found"
        parsed_auth_req = dict(parse_qsl(urlparse(proxied_auth_req.data.decode("utf-8")).query))

        # create auth resp
        id_token_claims = {k: v[0] for k, v in USERS[user_id].items()}
        id_token_claims["sub"] = user_id
        id_token_claims["iat"] = time.time()
        id_token_claims["exp"] = time.time() + 3600
        id_token_claims["iss"] = "http://op.example.com"
        id_token_claims["aud"] = oidc_backend_config["config"]["client"]["client_metadata"]["client_id"]
        id_token_claims["nonce"] = parsed_auth_req["nonce"]
        id_token = IdToken(**id_token_claims).to_jwt()
        authn_resp = {"state": parsed_auth_req["state"], "id_token": id_token}

        # make auth resp to proxy
        redirect_uri_path = urlparse(
            oidc_backend_config["config"]["client"]["client_metadata"]["redirect_uris"][0]).path
        authn_resp_req = redirect_uri_path + "?" + urlencode(authn_resp)
        authn_resp = test_client.get(authn_resp_req)
        assert authn_resp.status == "303 See Other"

        # verify auth resp from proxy
        resp_dict = dict(parse_qsl(urlparse(authn_resp.data.decode("utf-8")).query))
        auth_resp = fakesp.parse_authn_request_response(resp_dict["SAMLResponse"], BINDING_HTTP_REDIRECT)
        assert auth_resp.ava == USERS[user_id]

    def test_full_flow(self, satosa_config_dict, sp_conf, oidc_backend_config,
                       saml_frontend_config, saml_mirror_frontend_config):
        for conf in [saml_frontend_config, saml_mirror_frontend_config]:
            self.run_test(satosa_config_dict, sp_conf, oidc_backend_config, conf)