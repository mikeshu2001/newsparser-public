from __future__ import annotations

from scripts import local_runtime_check


class _FakeValidationError(Exception):
    def errors(self) -> list[dict[str, object]]:
        return [
            {
                "loc": ("bot_token",),
                "msg": "Field required",
                "input": "secret-token-value",
            },
            {
                "loc": ("openrouter_api_key",),
                "msg": "must not be blank",
                "input": "sk-or-secret",
            },
        ]


def test_validation_error_lines_do_not_include_secret_inputs() -> None:
    lines = local_runtime_check._validation_error_lines(_FakeValidationError())

    assert lines == [
        "bot_token: Field required",
        "openrouter_api_key: must not be blank",
    ]
    assert "secret" not in "\n".join(lines)


def test_parser_accepts_local_smoke_options() -> None:
    args = local_runtime_check.build_parser().parse_args([
        "--prepare-db",
        "--telegram",
        "--check-ai",
    ])

    assert args.prepare_db is True
    assert args.telegram is True
    assert args.check_ai is True
