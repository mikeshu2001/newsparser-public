from __future__ import annotations

import json
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from scripts import oauth_ai_login


def test_build_authorization_url_uses_pkce_and_state() -> None:
    url = oauth_ai_login.build_authorization_url(
        authorization_url="https://auth.example/authorize",
        client_id="client-id",
        redirect_uri="http://127.0.0.1:8765/callback",
        scopes="chat generate",
        state="state-123",
        code_challenge="challenge-123",
    )

    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.example"
    assert parsed.path == "/authorize"
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == ["http://127.0.0.1:8765/callback"]
    assert query["scope"] == ["chat generate"]
    assert query["state"] == ["state-123"]
    assert query["code_challenge"] == ["challenge-123"]
    assert query["code_challenge_method"] == ["S256"]


def test_write_token_file_adds_expires_at_and_restrictive_permissions(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "nested" / "oauth-token.json"

    oauth_ai_login.write_token_file(
        token_file,
        {"access_token": "token", "expires_in": 3600},
    )

    payload = json.loads(token_file.read_text(encoding="utf-8"))
    mode = stat.S_IMODE(token_file.stat().st_mode)

    assert payload["access_token"] == "token"
    assert payload["expires_at"] > 0
    assert mode == 0o600
