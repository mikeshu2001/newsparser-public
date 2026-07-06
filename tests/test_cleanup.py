from __future__ import annotations

from types import SimpleNamespace

from app.services.cleanup import cleanup_old_data


class _FakeSession:
    def __init__(self):
        self.rowcounts = [3, 2, 1]
        self.executed = 0
        self.commits = 0

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def execute(self, statement: object) -> SimpleNamespace:
        self.executed += 1
        return SimpleNamespace(rowcount=self.rowcounts.pop(0))

    async def commit(self) -> None:
        self.commits += 1


async def test_cleanup_old_data_executes_three_deletes_and_commits() -> None:
    session = _FakeSession()

    result = await cleanup_old_data(session_factory=lambda: session)

    assert result == (3, 2, 1)
    assert session.executed == 3
    assert session.commits == 1
