import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import voice_web
from launcher.start_service import server_command, service_ready


class VoiceBuddyLauncherTest(unittest.TestCase):
    def test_startup_probe_does_not_check_the_local_model(self):
        with (
            voice_web.VoiceServer(("127.0.0.1", 0)) as server,
            patch.object(voice_web, "llama_ready", return_value=False) as llama,
        ):
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                self.assertTrue(service_ready(server.server_port))
                llama.assert_not_called()

                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                try:
                    connection.request("GET", "/api/status")
                    with connection.getresponse() as response:
                        self.assertEqual(response.status, 200)
                        self.assertIs(json.load(response)["llama"], False)
                finally:
                    connection.close()
                llama.assert_called_once_with()
            finally:
                server.shutdown()
                thread.join()

    def test_quality_tts_uses_graph_when_installed_and_stays_warm_otherwise(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "app"
            assets = Path(temp) / "assets"
            environment = assets / ".venv-qwen-graph"
            python = environment / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.touch()

            stock = server_command(8011, root, assets)
            self.assertNotIn("--cold-tts", stock)
            self.assertNotIn("--qwen-graph-python", stock)

            package = environment / "Lib" / "site-packages" / "faster_qwen3_tts" / "__init__.py"
            package.parent.mkdir(parents=True)
            package.touch()
            graph = server_command(8011, root, assets)
            self.assertEqual(graph, stock + ["--qwen-graph-python", str(python)])
