"""Filename safety and credential storage.

Credentials: on macOS the API key is stored in the login Keychain via the
`security` CLI; elsewhere (including Docker) keys are stored in files under
DATA_DIR/secrets with 0600 permissions. Only an opaque key reference is
stored in the database, and keys are redacted from logs and API responses.
"""
import os
import re
import subprocess
import unicodedata
import uuid
from pathlib import Path
from typing import Optional

from . import config

KEYCHAIN_SERVICE = "AudioTranscription"

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_filename(name: str, fallback: str = "file") -> str:
    """Return a display-safe filename with path separators and control
    characters removed. Never used to build paths from user input alone."""
    name = unicodedata.normalize("NFC", name or "")
    name = os.path.basename(name.replace("\\", "/"))
    name = _UNSAFE.sub("_", name).strip(". ")
    return name[:180] or fallback


def safe_child(base: Path, *parts: str) -> Path:
    """Join and verify the result stays inside base (path-traversal guard)."""
    p = (base.joinpath(*parts)).resolve()
    base = base.resolve()
    if base != p and base not in p.parents:
        raise ValueError("unsafe path")
    return p


def redact(text: str) -> str:
    return re.sub(r"(api[-_]?key|authorization|bearer)\s*[:=]?\s*\S+", r"\1=[REDACTED]",
                  text, flags=re.I)


class SecretStore:
    """Stores API keys by opaque reference."""

    def store(self, secret: str, existing_ref: Optional[str] = None) -> str:
        ref = existing_ref or ("k-" + uuid.uuid4().hex)
        if config.IS_MACOS and self._keychain_store(ref, secret):
            return ref
        path = config.SECRETS_DIR / ref
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secret)
        return ref

    def load(self, ref: str) -> Optional[str]:
        if not ref:
            return None
        if config.IS_MACOS:
            val = self._keychain_load(ref)
            if val is not None:
                return val
        try:
            return config.SECRETS_DIR.joinpath(ref).read_text().strip() or None
        except OSError:
            return None

    def delete(self, ref: str) -> None:
        if not ref:
            return
        if config.IS_MACOS:
            subprocess.run(
                ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", ref],
                capture_output=True,
            )
        try:
            config.SECRETS_DIR.joinpath(ref).unlink()
        except OSError:
            pass

    @staticmethod
    def _keychain_store(ref: str, secret: str) -> bool:
        r = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
             "-a", ref, "-w", secret],
            capture_output=True,
        )
        return r.returncode == 0

    @staticmethod
    def _keychain_load(ref: str) -> Optional[str]:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", ref, "-w"],
            capture_output=True, text=True,
        )
        return r.stdout.rstrip("\n") if r.returncode == 0 else None


secret_store = SecretStore()
