#!/usr/bin/env python3
"""Generic OAuth login helper for the optional OAuth AI provider."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"
DEFAULT_TOKEN_FILE = ".oauth_ai_token.json"


def _load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _optional_env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _required_env(name: str) -> str:
    value = _optional_env(name)
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_code_verifier() -> str:
    return _base64url(secrets.token_bytes(48))


def build_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return _base64url(digest)


def build_authorization_url(
    *,
    authorization_url: str,
    client_id: str,
    redirect_uri: str,
    scopes: str | None,
    state: str,
    code_challenge: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if scopes:
        params["scope"] = scopes
    separator = "&" if "?" in authorization_url else "?"
    return f"{authorization_url}{separator}{urlencode(params)}"


def write_token_file(token_file: Path, token_payload: dict[str, Any]) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    if "expires_in" in token_payload and "expires_at" not in token_payload:
        try:
            token_payload["expires_at"] = int(time.time()) + int(token_payload["expires_in"])
        except (TypeError, ValueError):
            pass

    serialized = json.dumps(token_payload, indent=2, sort_keys=True)
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(serialized)
        handle.write("\n")


def _make_callback_handler(expected_state: str):
    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        code: str | None = None
        error: str | None = None

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if not query.get("state") and not query.get("code") and not query.get("error"):
                # Stray request (e.g. browser fetching /favicon.ico) — not the
                # OAuth callback; must not consume or fail the login flow.
                self._respond(404, "Not an OAuth callback.")
                return

            state = query.get("state", [""])[0]
            if state != expected_state:
                self.__class__.error = "OAuth state mismatch"
                self._respond(400, "OAuth state mismatch. You can close this tab.")
                return

            error = query.get("error", [None])[0]
            if error:
                self.__class__.error = error
                self._respond(400, f"OAuth error: {error}. You can close this tab.")
                return

            code = query.get("code", [None])[0]
            if not code:
                self.__class__.error = "Missing authorization code"
                self._respond(400, "Missing authorization code. You can close this tab.")
                return

            self.__class__.code = code
            self._respond(200, "Authorization complete. You can close this tab.")

        def _respond(self, status: int, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return OAuthCallbackHandler


def wait_for_authorization_code(redirect_uri: str, expected_state: str) -> str:
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    handler = _make_callback_handler(expected_state)

    with HTTPServer((host, port), handler) as server:
        print(f"Waiting for OAuth callback on {redirect_uri}")
        while handler.code is None and handler.error is None:
            server.handle_request()

    if handler.error:
        raise SystemExit(handler.error)
    if not handler.code:
        raise SystemExit("OAuth callback did not include an authorization code")
    return handler.code


def exchange_code_for_token(
    *,
    token_url: str,
    client_id: str,
    client_secret: str | None,
    redirect_uri: str,
    code: str,
    code_verifier: str,
) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code": code,
        "code_verifier": code_verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret

    response = httpx.post(token_url, data=data, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise SystemExit("Token response did not include access_token")
    return payload


def main() -> None:
    _load_env_file()

    authorization_url = _required_env("OAUTH_AI_AUTHORIZATION_URL")
    token_url = _required_env("OAUTH_AI_TOKEN_URL")
    client_id = _required_env("OAUTH_AI_CLIENT_ID")
    client_secret = _optional_env("OAUTH_AI_CLIENT_SECRET")
    redirect_uri = _optional_env("OAUTH_AI_REDIRECT_URI", DEFAULT_REDIRECT_URI) or DEFAULT_REDIRECT_URI
    scopes = _optional_env("OAUTH_AI_SCOPES")
    token_file = Path(_optional_env("OAUTH_AI_TOKEN_FILE", DEFAULT_TOKEN_FILE) or DEFAULT_TOKEN_FILE)

    state = secrets.token_urlsafe(24)
    code_verifier = generate_code_verifier()
    authorization_link = build_authorization_url(
        authorization_url=authorization_url,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=scopes,
        state=state,
        code_challenge=build_code_challenge(code_verifier),
    )

    print("Open this URL and authorize the AI gateway:")
    print(authorization_link)

    code = wait_for_authorization_code(redirect_uri, state)
    token_payload = exchange_code_for_token(
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        code=code,
        code_verifier=code_verifier,
    )
    write_token_file(token_file, token_payload)
    print(f"OAuth token saved to {token_file}")


if __name__ == "__main__":
    main()
