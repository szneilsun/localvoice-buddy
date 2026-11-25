import contextlib
import io
import subprocess
import sys
import unittest
from unittest.mock import patch

from run_voice_turn import run_step


class WorkerLaunchTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows console behavior")
    def test_one_shot_tts_launches_without_a_console_and_keeps_logs(self):
        with patch("run_voice_turn.subprocess.run") as launch, contextlib.redirect_stdout(io.StringIO()):
            run_step(sys.executable, "tts", "--text=Test")
        args, kwargs = launch.call_args
        self.assertIn("tts", args[0])
        self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertNotIn("stdout", kwargs)
        self.assertNotIn("stderr", kwargs)
