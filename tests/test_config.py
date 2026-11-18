"""設定 URL の検証。"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from src.config import ConfigurationError, load_config


class ConfigTests(unittest.TestCase):
    def config_path(self, top_url):
        config = {
            "login_url": "https://attendance.is.chibatech.ac.jp/attendance/login",
            "top_url": top_url,
            "student_id": "",
            "browser": {"headless": True, "timeout_ms": 15000},
            "dry_run": False,
        }
        return Mock(read_text=Mock(return_value=json.dumps(config)))

    def test_top_url_uses_login_host(self):
        path = self.config_path("https://attendance.is.chibatech.ac.jp/attendance/top")
        self.assertEqual(load_config(path)["student_id"], "")

    def test_rejects_different_top_host(self):
        path = self.config_path("https://attendance.is.it-chiba.ac.jp/attendance/top")
        with self.assertRaisesRegex(ConfigurationError, "ホスト名"):
            load_config(path)

    def test_missing_config_includes_setup_recovery_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "config.json"
            with self.assertRaises(ConfigurationError) as captured:
                load_config(missing)
        message = str(captured.exception)
        self.assertIn("config.json がありません", message)
        self.assertIn("bash install.sh", message)
        self.assertIn("cp config.example.json config.json && chmod 600 config.json", message)


if __name__ == "__main__":
    unittest.main()
