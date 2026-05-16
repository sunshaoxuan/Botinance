from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets as token_secrets
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Iterable, Tuple


PASSPHRASE_ENV_VAR = "BINANCE_AI_SECRETS_PASSPHRASE"
DEFAULT_CIPHER = "aes-256-cbc"
DEFAULT_ITERATIONS = 200000
DEFAULT_KEYCHAIN_SERVICE = "binance-ai-trader"
DEFAULT_KEYCHAIN_ACCOUNT = "workspace-default"
HMAC_SALT = b"binance-ai-trader-hmac-v1"

EXPLICIT_SENSITIVE_KEYS = {
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "LLM_API_KEY",
    "GITHUB_TOKEN",
}
SENSITIVE_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_PASSPHRASE")


def parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def is_sensitive_key(key: str) -> bool:
    normalized = key.strip().upper()
    if normalized in EXPLICIT_SENSITIVE_KEYS:
        return True
    return any(normalized.endswith(suffix) for suffix in SENSITIVE_SUFFIXES)


def split_sensitive_values(values: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    public_values: Dict[str, str] = {}
    sensitive_values: Dict[str, str] = {}
    for key, value in values.items():
        if is_sensitive_key(key):
            sensitive_values[key] = value
        else:
            public_values[key] = value
    return public_values, sensitive_values


def _format_env_lines(values: Dict[str, str], preferred_order: Iterable[str] | None = None) -> str:
    ordered_keys = []
    seen = set()

    if preferred_order:
        for key in preferred_order:
            if key in values and key not in seen:
                ordered_keys.append(key)
                seen.add(key)

    for key in values:
        if key not in seen:
            ordered_keys.append(key)
            seen.add(key)

    return "\n".join(f"{key}={values[key]}" for key in ordered_keys) + "\n"


def generate_passphrase() -> str:
    return token_secrets.token_urlsafe(48)


def _derive_hmac_key(passphrase: str, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        passphrase.encode("utf-8"),
        HMAC_SALT,
        iterations,
        dklen=32,
    )


def _build_secret_envelope(secret_values: Dict[str, str], passphrase: str, iterations: int) -> bytes:
    normalized_payload = json.dumps(
        secret_values,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    mac = hmac.new(_derive_hmac_key(passphrase, iterations), normalized_payload, hashlib.sha256).hexdigest()
    envelope = {
        "version": 1,
        "mac": mac,
        "secrets": secret_values,
    }
    return json.dumps(envelope, ensure_ascii=True, sort_keys=True).encode("utf-8")


def _parse_secret_envelope(payload: bytes, passphrase: str, iterations: int) -> Dict[str, str]:
    envelope = json.loads(payload.decode("utf-8"))
    secret_values = envelope.get("secrets", {})
    normalized_payload = json.dumps(
        secret_values,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    expected_mac = hmac.new(_derive_hmac_key(passphrase, iterations), normalized_payload, hashlib.sha256).hexdigest()
    provided_mac = envelope.get("mac", "")
    if not hmac.compare_digest(provided_mac, expected_mac):
        raise ValueError("Encrypted secrets integrity check failed.")
    return secret_values


def store_passphrase_in_keychain(service: str, account: str, passphrase: str) -> None:
    subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-s",
            service,
            "-a",
            account,
            "-w",
            passphrase,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def read_passphrase_from_keychain(service: str, account: str) -> str:
    result = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-w",
            "-s",
            service,
            "-a",
            account,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def resolve_passphrase(service: str, account: str) -> str:
    provided = os.getenv(PASSPHRASE_ENV_VAR, "").strip()
    if provided:
        return provided
    return read_passphrase_from_keychain(service, account)


def _run_openssl(command: list[str], input_bytes: bytes | None, passphrase: str) -> bytes:
    env = os.environ.copy()
    env[PASSPHRASE_ENV_VAR] = passphrase
    command = list(command)
    command[0] = _resolve_openssl_executable()
    result = subprocess.run(
        command,
        input=input_bytes,
        check=True,
        capture_output=True,
        env=env,
    )
    return result.stdout


def _resolve_openssl_executable() -> str:
    found = shutil.which("openssl")
    if found:
        return found
    candidates = [
        Path(r"C:\Program Files\Git\usr\bin\openssl.exe"),
        Path(r"C:\Program Files (x86)\Git\usr\bin\openssl.exe"),
        Path(r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe"),
        Path(r"C:\Program Files\OpenSSL-Win32\bin\openssl.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return "openssl"


def encrypt_secret_values(
    secret_values: Dict[str, str],
    output_path: Path,
    passphrase: str,
    cipher: str = DEFAULT_CIPHER,
    iterations: int = DEFAULT_ITERATIONS,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plaintext = _build_secret_envelope(secret_values, passphrase, iterations)
    command = [
        "openssl",
        "enc",
        f"-{cipher}",
        "-salt",
        "-pbkdf2",
        "-iter",
        str(iterations),
        "-md",
        "sha256",
        "-pass",
        f"env:{PASSPHRASE_ENV_VAR}",
        "-out",
        str(output_path),
    ]
    _run_openssl(command, plaintext, passphrase)


def decrypt_secret_values(
    input_path: Path,
    passphrase: str,
    cipher: str = DEFAULT_CIPHER,
    iterations: int = DEFAULT_ITERATIONS,
) -> Dict[str, str]:
    command = [
        "openssl",
        "enc",
        f"-{cipher}",
        "-d",
        "-pbkdf2",
        "-iter",
        str(iterations),
        "-md",
        "sha256",
        "-pass",
        f"env:{PASSPHRASE_ENV_VAR}",
        "-in",
        str(input_path),
    ]
    decrypted = _run_openssl(command, None, passphrase)
    return _parse_secret_envelope(decrypted, passphrase, iterations)


def load_encrypted_secrets(public_env: Dict[str, str], env_path: Path) -> Dict[str, str]:
    enabled = public_env.get("SECRETS_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return {}

    secrets_file = public_env.get("SECRETS_FILE", ".secrets.enc").strip()
    cipher = public_env.get("SECRETS_CIPHER", DEFAULT_CIPHER).strip()
    iterations = int(public_env.get("SECRETS_ITERATIONS", str(DEFAULT_ITERATIONS)).strip())
    service = public_env.get("SECRETS_KEYCHAIN_SERVICE", DEFAULT_KEYCHAIN_SERVICE).strip()
    account = public_env.get("SECRETS_KEYCHAIN_ACCOUNT", DEFAULT_KEYCHAIN_ACCOUNT).strip()
    encrypted_path = (env_path.parent / secrets_file).resolve()

    if not encrypted_path.exists():
        return {}

    passphrase = resolve_passphrase(service, account)
    return decrypt_secret_values(
        encrypted_path,
        passphrase=passphrase,
        cipher=cipher,
        iterations=iterations,
    )


def migrate_plaintext_env(
    env_path: Path,
    secrets_path: Path,
    *,
    keychain_service: str,
    keychain_account: str,
    passphrase: str,
    cipher: str = DEFAULT_CIPHER,
    iterations: int = DEFAULT_ITERATIONS,
    write_keychain: bool = True,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    original_values = parse_env_file(env_path)
    public_values, sensitive_values = split_sensitive_values(original_values)

    if write_keychain:
        store_passphrase_in_keychain(keychain_service, keychain_account, passphrase)

    encrypt_secret_values(
        sensitive_values,
        output_path=secrets_path,
        passphrase=passphrase,
        cipher=cipher,
        iterations=iterations,
    )

    public_values.update(
        {
            "SECRETS_ENABLED": "true",
            "SECRETS_FILE": secrets_path.name,
            "SECRETS_CIPHER": cipher,
            "SECRETS_ITERATIONS": str(iterations),
            "SECRETS_KEYCHAIN_SERVICE": keychain_service,
            "SECRETS_KEYCHAIN_ACCOUNT": keychain_account,
        }
    )
    env_path.write_text(
        _format_env_lines(public_values, preferred_order=original_values.keys()),
        encoding="utf-8",
    )
    return public_values, sensitive_values


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage encrypted secrets for Binance AI Trader.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate_parser = subparsers.add_parser("migrate-dotenv", help="Encrypt sensitive .env keys into a git-safe secrets file.")
    migrate_parser.add_argument("--env-file", default=".env")
    migrate_parser.add_argument("--secrets-file", default=".secrets.enc")
    migrate_parser.add_argument("--keychain-service", default=DEFAULT_KEYCHAIN_SERVICE)
    migrate_parser.add_argument("--keychain-account", default=DEFAULT_KEYCHAIN_ACCOUNT)
    migrate_parser.add_argument("--cipher", default=DEFAULT_CIPHER)
    migrate_parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    migrate_parser.add_argument("--skip-keychain", action="store_true")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command != "migrate-dotenv":
        raise SystemExit(f"Unsupported command: {args.command}")

    env_path = Path(args.env_file).resolve()
    secrets_path = Path(args.secrets_file)
    if not secrets_path.is_absolute():
        secrets_path = (env_path.parent / secrets_path).resolve()

    passphrase = os.getenv(PASSPHRASE_ENV_VAR, "").strip() or generate_passphrase()
    public_values, sensitive_values = migrate_plaintext_env(
        env_path=env_path,
        secrets_path=secrets_path,
        keychain_service=args.keychain_service,
        keychain_account=args.keychain_account,
        passphrase=passphrase,
        cipher=args.cipher,
        iterations=args.iterations,
        write_keychain=not args.skip_keychain,
    )

    print(
        json.dumps(
            {
                "env_file": str(env_path),
                "secrets_file": str(secrets_path),
                "public_key_count": len(public_values),
                "sensitive_key_count": len(sensitive_values),
                "keychain_service": args.keychain_service,
                "keychain_account": args.keychain_account,
                "stored_in_keychain": not args.skip_keychain,
            },
            ensure_ascii=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
