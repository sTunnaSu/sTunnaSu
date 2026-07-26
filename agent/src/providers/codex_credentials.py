"""Canonical, atomic storage for OpenAI Codex OAuth credentials."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from platformdirs import user_data_dir


def canonical_codex_token_path(home_dir: Path | None = None) -> Path:
    """Return Larry's single canonical Codex credential path."""
    home = home_dir or Path.home()
    return home / ".vibe-trading" / "auth" / "openai-codex.json"


def legacy_codex_token_paths(home_dir: Path | None = None) -> tuple[Path, ...]:
    """Return known legacy token locations without reading their contents."""
    home = home_dir or Path.home()
    paths = [
        Path(user_data_dir("oauth-cli-kit", appauthor=False)) / "auth" / "codex.json",
        home / ".codex" / "auth.json",
    ]
    override = os.environ.get("OAUTH_CLI_KIT_TOKEN_PATH")
    if override:
        paths.insert(0, Path(override).expanduser())
    canonical = canonical_codex_token_path(home)
    return tuple(dict.fromkeys(path for path in paths if path != canonical))


def _token_from_payload(payload: dict[str, Any]) -> Any | None:
    try:
        from oauth_cli_kit.models import OAuthToken  # type: ignore[import-untyped]

        access = str(payload["access"])
        refresh = str(payload["refresh"])
        expires = int(payload["expires"])
        if not access or not refresh or expires <= 0:
            return None
        return OAuthToken(
            access=access,
            refresh=refresh,
            expires=expires,
            account_id=str(payload["account_id"]) if payload.get("account_id") else None,
        )
    except (ImportError, KeyError, TypeError, ValueError):
        return None


def _read_token_file(path: Path) -> Any | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return _token_from_payload(payload) if isinstance(payload, dict) else None


def _read_codex_cli_token(path: Path) -> Any | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        tokens = payload.get("tokens") or {}
        if not isinstance(tokens, dict):
            return None
        access = tokens.get("access_token")
        refresh = tokens.get("refresh_token")
        account_id = tokens.get("account_id")
        if not access or not refresh or not account_id:
            return None
        return _token_from_payload(
            {
                "access": access,
                "refresh": refresh,
                "account_id": account_id,
                "expires": int(time.time() * 1000) + 3_600_000,
            }
        )
    except (OSError, ValueError, TypeError):
        return None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        os.chmod(temporary_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


class CodexCredentialStore:
    """TokenStorage-compatible store with legacy migration and safe writes."""

    def __init__(self, home_dir: Path | None = None) -> None:
        self.home_dir = home_dir or Path.home()
        self.path = canonical_codex_token_path(self.home_dir)

    def get_token_path(self) -> Path:
        return self.path

    def load(self) -> Any | None:
        token = _read_token_file(self.path)
        if token:
            return token
        for legacy_path in legacy_codex_token_paths(self.home_dir):
            if legacy_path == self.home_dir / ".codex" / "auth.json":
                token = _read_codex_cli_token(legacy_path)
            else:
                token = _read_token_file(legacy_path)
            if token:
                self.save(token)
                return token
        return None

    def save(self, token: Any) -> None:
        payload = {
            "access": str(token.access),
            "refresh": str(token.refresh),
            "expires": int(token.expires),
        }
        if token.account_id:
            payload["account_id"] = str(token.account_id)
        _atomic_write_json(self.path, payload)

    def remove_canonical(self) -> None:
        self.path.unlink(missing_ok=True)

    def clear(self) -> None:
        """Remove Codex credentials while preserving unrelated configuration."""
        self.remove_canonical()
        for legacy_path in legacy_codex_token_paths(self.home_dir):
            if legacy_path == self.home_dir / ".codex" / "auth.json":
                self._clear_codex_cli_tokens(legacy_path)
            else:
                legacy_path.unlink(missing_ok=True)

    @staticmethod
    def _clear_codex_cli_tokens(path: Path) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(payload, dict):
            return
        tokens = payload.get("tokens")
        if isinstance(tokens, dict):
            for key in ("access_token", "refresh_token", "id_token", "account_id"):
                tokens.pop(key, None)
            if not tokens:
                payload.pop("tokens", None)
        payload.pop("last_refresh", None)
        _atomic_write_json(path, payload)


__all__ = [
    "CodexCredentialStore",
    "canonical_codex_token_path",
    "legacy_codex_token_paths",
]
