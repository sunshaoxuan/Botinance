import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from binance_ai.config import load_settings
from binance_ai.secrets import (
    PASSPHRASE_ENV_VAR,
    decrypt_secret_values,
    encrypt_secret_values,
    is_sensitive_key,
    migrate_plaintext_env,
    resolve_passphrase,
)


class SecretsTests(unittest.TestCase):
    def test_is_sensitive_key_detects_common_secret_fields(self) -> None:
        self.assertTrue(is_sensitive_key("BINANCE_API_KEY"))
        self.assertTrue(is_sensitive_key("BINANCE_API_SECRET"))
        self.assertTrue(is_sensitive_key("GITHUB_TOKEN"))
        self.assertFalse(is_sensitive_key("BINANCE_BASE_URL"))
        self.assertFalse(is_sensitive_key("NEWS_REFRESH_SECONDS"))

    def test_encrypt_and_decrypt_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            encrypted_path = Path(tmpdir) / ".secrets.enc"
            payload = {
                "BINANCE_API_KEY": "demo-key",
                "BINANCE_API_SECRET": "demo-secret",
                "LLM_API_KEY": "demo-llm",
            }
            encrypt_secret_values(payload, encrypted_path, passphrase="round-trip-passphrase")
            decrypted = decrypt_secret_values(encrypted_path, passphrase="round-trip-passphrase")
        self.assertEqual(decrypted, payload)

    def test_load_settings_reads_secret_values_from_encrypted_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            env_path = workdir / ".env"
            secrets_path = workdir / ".secrets.enc"
            env_path.write_text(
                "\n".join(
                    [
                        "BINANCE_API_KEY=plain-key",
                        "BINANCE_API_SECRET=plain-secret",
                        "BINANCE_BASE_URL=https://api.binance.com",
                        "TRADING_SYMBOLS=XRPJPY",
                        "QUOTE_ASSET=JPY",
                        "DRY_RUN=true",
                        "LLM_API_KEY=plain-llm",
                        "LLM_BASE_URL=http://localhost:49530/v1",
                        "LLM_MODEL=gpt-5.5",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            migrate_plaintext_env(
                env_path=env_path,
                secrets_path=secrets_path,
                keychain_service="test-service",
                keychain_account="test-account",
                passphrase="settings-passphrase",
                write_keychain=False,
            )

            original_cwd = Path.cwd()
            try:
                os.chdir(workdir)
                with patch.dict(os.environ, {PASSPHRASE_ENV_VAR: "settings-passphrase"}, clear=True):
                    settings = load_settings()
            finally:
                os.chdir(original_cwd)

        self.assertEqual(settings.api_key, "plain-key")
        self.assertEqual(settings.api_secret, "plain-secret")
        self.assertEqual(settings.trading_symbols, ["XRPJPY"])
        self.assertEqual(settings.quote_asset, "JPY")
        self.assertEqual(settings.llm_api_key, "plain-llm")
        self.assertEqual(settings.llm_base_url, "http://localhost:49530/v1")

    def test_resolve_passphrase_uses_windows_dpapi_file_before_keychain(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dpapi_path = Path(tmpdir) / "secrets_passphrase.dpapi"
            dpapi_path.write_text("encrypted", encoding="utf-8")

            with (
                patch("binance_ai.secrets._is_windows", return_value=True),
                patch("binance_ai.secrets.read_passphrase_from_windows_dpapi_file", return_value="dpapi-passphrase") as read_dpapi,
                patch("binance_ai.secrets.read_passphrase_from_keychain") as read_keychain,
                patch.dict(os.environ, {"SECRETS_DPAPI_FILE": str(dpapi_path)}, clear=True),
            ):
                passphrase = resolve_passphrase("service", "account")

        self.assertEqual(passphrase, "dpapi-passphrase")
        read_dpapi.assert_called_once_with(dpapi_path)
        read_keychain.assert_not_called()


if __name__ == "__main__":
    unittest.main()
