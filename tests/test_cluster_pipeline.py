from __future__ import annotations

from types import SimpleNamespace

from app.services.cluster_pipeline import (
    expire_old_clusters,
    get_thresholds,
    recover_stuck_clusters,
)


class _ScalarResult:
    def __init__(self, items: list[object]):
        self._items = items

    def all(self) -> list[object]:
        return self._items


class _FakeSession:
    def __init__(self, *result_sets: list[object]):
        self._result_sets = list(result_sets)
        self.commits = 0
        self.deleted: list[object] = []
        self.executed: list[object] = []
        self.execute_rowcounts: list[int] = []

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def scalars(self, statement: object) -> _ScalarResult:
        return _ScalarResult(self._result_sets.pop(0))

    async def delete(self, item: object) -> None:
        self.deleted.append(item)

    async def execute(self, statement: object) -> SimpleNamespace:
        self.executed.append(statement)
        rowcount = self.execute_rowcounts.pop(0) if self.execute_rowcounts else 0
        return SimpleNamespace(rowcount=rowcount)

    async def commit(self) -> None:
        self.commits += 1


async def test_get_thresholds_reads_valid_settings_and_ignores_invalid() -> None:
    session = _FakeSession([
        SimpleNamespace(key="score_threshold", value="60"),
        SimpleNamespace(key="hot_threshold", value="bad"),
        SimpleNamespace(key="cluster_wait_minutes", value="45"),
    ])

    thresholds = await get_thresholds(session_factory=lambda: session)

    assert thresholds.score_threshold == 60
    assert thresholds.hot_threshold == 80
    assert thresholds.cluster_wait_minutes == 45


async def test_expire_old_clusters_rejects_drafted_and_deletes_undrafted() -> None:
    session = _FakeSession()
    session.execute_rowcounts = [1, 2]  # 1 marked rejected (has drafts), 2 deleted

    count = await expire_old_clusters(
        max_cluster_age_hours=24,
        session_factory=lambda: session,
    )

    assert count == 3
    assert len(session.executed) == 2
    assert str(session.executed[0]).startswith("UPDATE")
    assert str(session.executed[1]).startswith("DELETE")
    assert session.deleted == []
    assert session.commits == 1


async def test_expire_old_clusters_without_matches_does_not_commit() -> None:
    session = _FakeSession()
    session.execute_rowcounts = [0, 0]

    count = await expire_old_clusters(
        max_cluster_age_hours=24,
        session_factory=lambda: session,
    )

    assert count == 0
    assert session.commits == 0


async def test_recover_stuck_clusters_reverts_matches_and_commits() -> None:
    clusters = [
        SimpleNamespace(id=1, status="generating"),
        SimpleNamespace(id=2, status="generating"),
    ]
    session = _FakeSession(clusters)

    count = await recover_stuck_clusters(
        generating_timeout_minutes=30,
        session_factory=lambda: session,
    )

    assert count == 2
    assert [cluster.status for cluster in clusters] == ["waiting", "waiting"]
    assert session.commits == 1
