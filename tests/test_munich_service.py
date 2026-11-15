import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from munich_service import _dashboard_command, _supervise, main


class MunichServiceTests(unittest.TestCase):
    def test_dashboard_requires_password_before_starting_either_child(self):
        with patch.dict(os.environ, {"MUNICH_DASHBOARD_ENABLED": "true"}, clear=True):
            with patch("munich_service._supervise") as supervise, patch("sys.stderr"):
                self.assertEqual(main(), 1)
            supervise.assert_not_called()

    def test_railway_settings_select_the_target_port_and_public_host(self):
        with patch.dict(os.environ, {
            "MUNICH_DASHBOARD_PASSWORD": "temporary-test-password",
            "RAILWAY_PUBLIC_DOMAIN": "mietradar.example.com",
            "PORT": "9042",
        }, clear=True):
            command = _dashboard_command()
        self.assertIn("--server.port=9042", command)
        self.assertIn("--browser.serverAddress=mietradar.example.com", command)
        self.assertIn("--browser.serverPort=443", command)
        self.assertNotIn("temporary-test-password", " ".join(command))

    @unittest.skipUnless(os.name == "posix", "Deployment uses POSIX process groups")
    def test_worker_failure_stops_dashboard(self):
        worker = """
from pathlib import Path
import sys
import time

ready = Path(sys.argv[1])
deadline = time.monotonic() + 5
while not ready.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
sys.exit(9)
"""
        dashboard = """
from pathlib import Path
import signal
import sys
import time

def stop(*_):
    Path(sys.argv[2]).touch()
    sys.exit(0)

signal.signal(signal.SIGTERM, stop)
Path(sys.argv[1]).touch()
time.sleep(6)
"""
        with TemporaryDirectory() as temp:
            ready = Path(temp) / "started"
            stopped = Path(temp) / "stopped"
            services = (
                ("Radar worker", [sys.executable, "-c", worker, str(ready)]),
                ("Dashboard", [sys.executable, "-c", dashboard, str(ready), str(stopped)]),
            )
            with patch("sys.stderr"):
                self.assertEqual(_supervise(services), 1)
            self.assertTrue(stopped.exists())
