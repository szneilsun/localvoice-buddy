import tempfile
import unittest
from pathlib import Path

from launcher.start_service import server_command


class VoiceBuddyLauncherTest(unittest.TestCase):
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
