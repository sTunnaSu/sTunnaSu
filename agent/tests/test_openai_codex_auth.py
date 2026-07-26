"""Regression tests for Codex OAuth storage, refresh, and CLI resolution."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest
from rich.console import Console

from src.providers import openai_codex as codex
from src.providers.codex_credentials import CodexCredentialStore


def _token(access: str, refresh: str, account_id: str = "acct"):
    from oauth_cli_kit.models import OAuthToken

    return OAuthToken(
        access=access,
        refresh=refresh,
        expires=int(time.time() * 1000) + 3_600_000,
        account_id=account_id,
    )


def test_login_always_replaces_old_invalidated_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = CodexCredentialStore(tmp_path)
    store.save(_token("old-access", "old-refresh"))
    monkeypatch.setattr(codex, "CodexCredentialStore", lambda: store)

    import oauth_cli_kit

    new_token = _token("new-access", "new-refresh")

    def fake_login(**kwargs):
        kwargs["storage"].save(new_token)
        return new_token

    monkeypatch.setattr(oauth_cli_kit, "login_oauth_interactive", fake_login)
    result = codex.login_openai_codex()

    assert result.access == "new-access"
    assert store.load().refresh == "new-refresh"


def test_runtime_reads_credentials_written_by_login(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = CodexCredentialStore(tmp_path)
    store.save(_token("written-access", "written-refresh"))
    monkeypatch.setattr(codex, "CodexCredentialStore", lambda: store)

    import oauth_cli_kit

    seen = {}

    def fake_get_token(*, storage):
        seen["path"] = storage.get_token_path()
        return storage.load()

    monkeypatch.setattr(oauth_cli_kit, "get_token", fake_get_token)
    result = codex._get_codex_token()

    assert result.access == "written-access"
    assert seen["path"] == store.path


class _Response:
    def __init__(self, status_code: int, payload: bytes = b"") -> None:
        self.status_code = status_code
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload

    def iter_lines(self):
        return ['data: {"type":"response.completed","response":{"status":"completed"}}', ""]


class _Client:
    responses: list[_Response] = []
    requests: list[dict] = []

    def __init__(self, **kwargs: object) -> None:
        pass

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def stream(self, method: str, url: str, *, headers: dict, json: dict) -> _Response:
        self.requests.append({"method": method, "url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


def test_invalidated_access_token_refreshes_and_retries_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = CodexCredentialStore(tmp_path)
    old = _token("invalid-access", "refresh-one")
    new = _token("fresh-access", "refresh-two")
    store.save(old)
    monkeypatch.setattr(codex, "CodexCredentialStore", lambda: store)

    import oauth_cli_kit
    from oauth_cli_kit import flow

    monkeypatch.setattr(oauth_cli_kit, "get_token", lambda *, storage: storage.load())
    monkeypatch.setattr(flow, "_refresh_token", lambda refresh, provider: new)
    _Client.responses = [
        _Response(401, b'{"error":{"code":"token_invalidated","message":"invalid"}}'),
        _Response(200),
    ]
    _Client.requests = []
    monkeypatch.setattr(codex.httpx, "Client", _Client)

    list(codex.OpenAICodexLLM(model="gpt-5.6-sol").stream([{"role": "user", "content": "hi"}]))

    assert len(_Client.requests) == 2
    assert _Client.requests[0]["headers"]["Authorization"].endswith("invalid-access")
    assert _Client.requests[1]["headers"]["Authorization"].endswith("fresh-access")
    assert store.load().access == "fresh-access"


def test_invalidated_request_never_retries_more_than_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = CodexCredentialStore(tmp_path)
    store.save(_token("old", "refresh"))
    monkeypatch.setattr(codex, "CodexCredentialStore", lambda: store)

    import oauth_cli_kit
    from oauth_cli_kit import flow

    monkeypatch.setattr(oauth_cli_kit, "get_token", lambda *, storage: storage.load())
    monkeypatch.setattr(flow, "_refresh_token", lambda refresh, provider: _token("new", "refresh2"))
    _Client.responses = [
        _Response(401, b'{"error":{"code":"token_invalidated","message":"invalid"}}'),
        _Response(401, b'{"error":{"code":"token_invalidated","message":"still invalid"}}'),
    ]
    _Client.requests = []
    monkeypatch.setattr(codex.httpx, "Client", _Client)

    with pytest.raises(RuntimeError, match="OpenAI Codex HTTP 401"):
        list(codex.OpenAICodexLLM(model="gpt-5.6-sol").stream([{"role": "user", "content": "hi"}]))
    assert len(_Client.requests) == 2


def test_failed_refresh_is_actionable_and_redacts_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = CodexCredentialStore(tmp_path)
    store.save(_token("access-secret", "refresh-secret"))
    monkeypatch.setattr(codex, "CodexCredentialStore", lambda: store)

    import oauth_cli_kit
    from oauth_cli_kit import flow

    monkeypatch.setattr(oauth_cli_kit, "get_token", lambda *, storage: storage.load())
    monkeypatch.setattr(
        flow,
        "_refresh_token",
        lambda refresh, provider: (_ for _ in ()).throw(RuntimeError("refresh_token=refresh-secret")),
    )
    _Client.responses = [_Response(401, b'{"error":{"code":"token_invalidated","message":"bad"}}')]
    _Client.requests = []
    monkeypatch.setattr(codex.httpx, "Client", _Client)

    with pytest.raises(RuntimeError, match="vibe-trading provider login openai-codex") as error:
        list(codex.OpenAICodexLLM(model="gpt-5.6-sol").stream([{"role": "user", "content": "hi"}]))
    assert "access-secret" not in str(error.value)
    assert "refresh-secret" not in str(error.value)
    assert not store.path.exists()


def test_logout_removes_codex_credentials_but_preserves_other_codex_cli_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = CodexCredentialStore(tmp_path)
    store.save(_token("access", "refresh"))
    cli_auth = tmp_path / ".codex" / "auth.json"
    cli_auth.parent.mkdir(parents=True)
    cli_auth.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": "access", "refresh_token": "refresh", "account_id": "acct"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(codex, "CodexCredentialStore", lambda: store)

    codex.logout_openai_codex()

    assert not store.path.exists()
    remaining = json.loads(cli_auth.read_text(encoding="utf-8"))
    assert remaining["auth_mode"] == "chatgpt"
    assert "tokens" not in remaining


def test_model_command_uses_provider_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands import chat

    output = io.StringIO()
    monkeypatch.setattr(chat, "_resolve_console", lambda: Console(file=output, force_terminal=False))
    monkeypatch.setattr(chat, "provider_diagnostics", None, raising=False)

    import src.providers.llm as llm

    monkeypatch.setattr(
        llm,
        "provider_diagnostics",
        lambda: {
            "provider": "openai-codex",
            "model": "openai-codex/gpt-5.6-sol",
            "base_url": "https://chatgpt.com",
        },
    )
    chat.cmd_model()

    text = output.getvalue()
    assert "Provider: openai-codex" in text
    assert "Model:    openai-codex/gpt-5.6-sol" in text
