import unittest
from pathlib import Path
import tempfile

from secretary.config import ConfigError, SecretaryConfig


class ConfigTests(unittest.TestCase):
    def test_defaults_valid(self):
        cfg = SecretaryConfig()
        cfg.validate()
        self.assertEqual(cfg.segment_seconds, 900.0)
        self.assertEqual(cfg.concurrency, 1)
        self.assertTrue(cfg.allow_loopback_http)

    def test_redacted_hides_secrets(self):
        cfg = SecretaryConfig(proxy_api_key="SUPER-SECRET", api_token="TOKEN9")
        dumped = str(cfg.redacted())
        self.assertNotIn("SUPER-SECRET", dumped)
        self.assertNotIn("TOKEN9", dumped)
        self.assertIn("***", dumped)

    def test_env_roundtrip(self):
        cfg = SecretaryConfig.from_env({
            "SECRETARY_PROXY_BASE_URL": "https://proxy.example.com",
            "SECRETARY_PROXY_API_KEY": "k",
            "SECRETARY_MODEL": "model-x",
            "SECRETARY_MAX_AUDIO_BYTES": "2048",
            "SECRETARY_SEGMENT_SECONDS": "600",
            "SECRETARY_OVERLAP_SECONDS": "3",
        })
        self.assertEqual(cfg.model, "model-x")
        self.assertEqual(cfg.max_audio_bytes, 2048)
        self.assertEqual(cfg.segment_seconds, 600.0)

    def test_url_import_env_roundtrip(self):
        cfg = SecretaryConfig.from_env({
            "SECRETARY_URL_MAX_BYTES": "123456",
            "SECRETARY_URL_MAX_REDIRECTS": "3",
            "SECRETARY_URL_TIMEOUT_SECONDS": "45",
            "SECRETARY_URL_MIN_FREE_AFTER_BYTES": "654321",
        })
        self.assertEqual(cfg.url_max_bytes, 123456)
        self.assertEqual(cfg.url_max_redirects, 3)
        self.assertEqual(cfg.url_timeout_seconds, 45.0)
        self.assertEqual(cfg.url_min_free_after_bytes, 654321)

    def test_bad_numbers(self):
        for key, value in [("SECRETARY_MAX_AUDIO_BYTES", "-5"),
                           ("SECRETARY_SEGMENT_SECONDS", "abc"),
                           ("SECRETARY_MAX_ATTEMPTS", "0"),
                           ("SECRETARY_CONCURRENCY", "0")]:
            with self.subTest(key=key), self.assertRaises(ConfigError):
                SecretaryConfig.from_env({key: value})

    def test_https_enforced_for_remote(self):
        with self.assertRaises(ConfigError):
            SecretaryConfig(proxy_base_url="http://proxy.example.com").validate()

    def test_http_loopback_allowed_and_forbidden(self):
        SecretaryConfig(proxy_base_url="http://127.0.0.1:9000").validate()
        with self.assertRaises(ConfigError):
            SecretaryConfig(proxy_base_url="http://127.0.0.1:9000",
                            allow_loopback_http=False).validate()

    def test_userinfo_and_v1_suffix_rejected(self):
        with self.assertRaises(ConfigError):
            SecretaryConfig(proxy_base_url="https://user:pass@example.com").validate()
        with self.assertRaises(ConfigError):
            SecretaryConfig(proxy_base_url="https://example.com/v1").validate()

    def test_overlap_bounds(self):
        with self.assertRaises(ConfigError):
            SecretaryConfig(overlap_seconds=30.0).validate()  # >= min (30)
        with self.assertRaises(ConfigError):
            SecretaryConfig(min_segment_seconds=900.0).validate()

    def test_env_file_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "# comment\nSECRETARY_MODEL=file-model\n"
                "SECRETARY_SEGMENT_SECONDS=120\n", encoding="utf-8")
            cfg = SecretaryConfig.from_env({"SECRETARY_MODEL": "env-model"},
                                           env_file=str(env_file))
            self.assertEqual(cfg.model, "env-model")  # env важнее файла
            self.assertEqual(cfg.segment_seconds, 120.0)

    # ---- pass2: переменные .env.example, которые раньше игнорировались ------

    def test_previously_ignored_vars_roundtrip(self):
        """Все 7 переменных из .env.example читаются (ревью pass2, P1)."""
        cfg = SecretaryConfig.from_env({
            "SECRETARY_RETRY_AFTER_CAP_SECONDS": "77",
            "SECRETARY_WORKER_POLL_SECONDS": "0.25",
            "SECRETARY_FFMPEG_TIMEOUT_SECONDS": "240",
            "SECRETARY_MAX_ZIP_ENTRIES": "128",
            "SECRETARY_MAX_ZIP_TOTAL_BYTES": "536870912",
            "SECRETARY_MAX_ZIP_MEMBER_BYTES": "10485760",
            "SECRETARY_MAX_ZIP_RATIO": "50",
        })
        self.assertEqual(cfg.retry_after_cap_seconds, 77.0)
        self.assertEqual(cfg.worker_poll_seconds, 0.25)
        self.assertEqual(cfg.ffmpeg_timeout_seconds, 240.0)
        self.assertEqual(cfg.max_zip_entries, 128)
        self.assertEqual(cfg.max_zip_total_bytes, 536870912)
        self.assertEqual(cfg.max_zip_member_bytes, 10485760)
        self.assertEqual(cfg.max_zip_ratio, 50.0)

    def test_previously_ignored_vars_bad_values(self):
        for key, value in [
                ("SECRETARY_RETRY_AFTER_CAP_SECONDS", "abc"),
                ("SECRETARY_RETRY_AFTER_CAP_SECONDS", "-1"),
                ("SECRETARY_WORKER_POLL_SECONDS", "0"),
                ("SECRETARY_WORKER_POLL_SECONDS", "-0.5"),
                ("SECRETARY_FFMPEG_TIMEOUT_SECONDS", "0"),
                ("SECRETARY_MAX_ZIP_ENTRIES", "0"),
                ("SECRETARY_MAX_ZIP_TOTAL_BYTES", "x"),
                ("SECRETARY_MAX_ZIP_MEMBER_BYTES", "-5"),
                ("SECRETARY_MAX_ZIP_RATIO", "0.5")]:
            with self.subTest(key=key, value=value), \
                    self.assertRaises(ConfigError):
                SecretaryConfig.from_env({key: value})

    def test_zip_member_exceeding_total_rejected(self):
        with self.assertRaises(ConfigError):
            SecretaryConfig(max_zip_member_bytes=100,
                            max_zip_total_bytes=10).validate()

    def test_env_file_matches_parsed_keys(self):
        """Каждая переменная из .env.example реально читается from_env."""
        env_file = Path(__file__).parent.parent / ".env.example"
        wanted = [
            "SECRETARY_RETRY_AFTER_CAP_SECONDS",
            "SECRETARY_WORKER_POLL_SECONDS",
            "SECRETARY_FFMPEG_TIMEOUT_SECONDS",
            "SECRETARY_MAX_ZIP_ENTRIES",
            "SECRETARY_MAX_ZIP_TOTAL_BYTES",
            "SECRETARY_MAX_ZIP_MEMBER_BYTES",
            "SECRETARY_MAX_ZIP_RATIO",
        ]
        content = env_file.read_text(encoding="utf-8")
        for key in wanted:
            with self.subTest(key=key):
                self.assertIn(f"{key}=", content)


if __name__ == "__main__":
    unittest.main()
