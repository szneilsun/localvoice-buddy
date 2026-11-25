import http.client
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import voice_web
from voice_lan import GATEWAY_HEALTH, Gateway, gateway_address
from voice_smoke import LOCAL_HTTP


class LanAccessTest(unittest.TestCase):
    def test_gateway_rejects_nonlocal_networks(self):
        config = {"lanIp": "192.168.1.30", "clientNetwork": "0.0.0.0/0", "port": 8443}
        with self.assertRaises(ValueError):
            gateway_address(config)

    def test_gateway_allows_voice_requests_without_a_login(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with (
            patch("voice_lan.ssl.SSLContext") as tls,
            voice_web.VoiceServer(("127.0.0.1", 0)) as backend,
        ):
            tls.return_value.wrap_socket.side_effect = lambda sock, **_: sock
            config = {"lanIp": "127.0.0.1", "clientNetwork": "127.0.0.1/32",
                      "port": port, "backendPort": backend.server_port}
            with Gateway(config, Path(".")) as gateway:
                servers = (backend, gateway)
                threads = [threading.Thread(target=server.serve_forever) for server in servers]
                for thread in threads:
                    thread.start()

                def request(method, path, headers=None, expected=200):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                    try:
                        conn.request(method, path, headers=headers or {})
                        with conn.getresponse() as response:
                            self.assertEqual(response.status, expected)
                            self.assertIsNone(response.getheader("WWW-Authenticate"))
                            return response.read()
                    finally:
                        conn.close()

                try:
                    self.assertEqual(json.loads(request("GET", "/api/health")), GATEWAY_HEALTH)
                    self.assertIn(b"<html", request("GET", "/"))
                    headers = {"Origin": gateway.origin, "X-Conversation-Id": "a" * 32}
                    request("POST", "/api/warm-quality", headers, expected=202)
                    request("POST", "/api/warm-quality", expected=403)
                    request("GET", "/", {"Sec-Fetch-Site": "cross-site"}, expected=403)
                    request("GET", "/api/chat-settings", expected=403)
                    request("POST", "/api/chat-settings", headers, expected=403)
                    self.assertFalse(gateway.verify_request(None, ("127.0.0.2", 12345)))
                finally:
                    for server, thread in zip(servers, threads):
                        server.shutdown()
                        thread.join()

    def test_connection_guide_exposes_public_metadata_and_certificate_only(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(voice_web, "LAN", Path(temp)),
            patch.object(voice_web, "gateway_ready", return_value=True),
            voice_web.VoiceServer(("127.0.0.1", 0)) as server,
        ):
            lan = Path(temp)
            password = "test-password-not-for-the-guide"
            config = {"lanIp": "192.168.1.30", "clientNetwork": "192.168.1.0/24", "port": 8443,
                      "password": password, "backendPort": server.server_port}
            (lan / "access.json").write_text(json.dumps(config), encoding="utf-8")
            (lan / "localvoice-ca.cer").write_bytes(b"public-test-certificate")
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"
            try:
                with LOCAL_HTTP.open(origin + "/api/access", timeout=3) as response:
                    body = response.read()
                self.assertNotIn(password.encode(), body)
                self.assertEqual(json.loads(body), {
                    "local_url": origin + "/", "model_port": 8001,
                    "lan": {"url": "https://192.168.1.30:8443/", "port": 8443,
                            "ready": True, "certificate": True},
                })
                with LOCAL_HTTP.open(origin + "/localvoice-ca.cer", timeout=3) as response:
                    self.assertEqual(response.headers.get_content_type(), "application/pkix-cert")
                    self.assertEqual(response.read(), b"public-test-certificate")
            finally:
                server.shutdown()
                thread.join()
