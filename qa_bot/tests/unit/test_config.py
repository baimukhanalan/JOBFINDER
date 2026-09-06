import unittest
from pathlib import Path
from qa_bot.config import AppConfig, load_config, parse_config


class ConfigTests(unittest.TestCase):
    def test_defaults_disabled(self):
        config = AppConfig()
        self.assertEqual(config.mode, "dry-run")
        self.assertFalse(config.external_services)
        self.assertFalse(config.browser_enabled)

    def test_checked_in_config_loads(self):
        root = Path(__file__).resolve().parents[2]
        self.assertEqual(load_config(root / "configs" / "offline.toml"), AppConfig())

    def test_unsafe_settings_rejected(self):
        for data in ({"mode": "staging"}, {"external_services": True},
                     {"browser_enabled": True}, {"browser_enabled": "false"}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_config(data)

    def test_unknown_fields_rejected(self):
        with self.assertRaises(ValueError):
            parse_config({"unsupported_field": "value"})

    def test_invalid_limits_rejected(self):
        for data in ({"operation_timeout_seconds": 0},
                     {"operation_timeout_seconds": float("inf")},
                     {"max_retries": True}, {"max_retries": -1}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_config(data)

    def test_missing_file_not_silently_ignored(self):
        with self.assertRaises(FileNotFoundError):
            load_config(Path(__file__).parent / "missing.toml")
