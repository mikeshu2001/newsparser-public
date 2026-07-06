from __future__ import annotations

from app.database.models import GeneratedArticle, NewsCluster
from app.services.generation_pipeline import record_delivery_result
from app.services.notifier import DeliveryResult


def test_record_delivery_result_treats_zero_delivery_as_failure() -> None:
    article = GeneratedArticle(id=5, cluster_id=8, headline="H", body="B")
    cluster = NewsCluster(id=8)
    delivery = DeliveryResult(total_recipients=2, delivered_count=0, failed_count=2)

    assert record_delivery_result(article, cluster, delivery) is False


def test_record_delivery_result_treats_any_delivery_as_success() -> None:
    article = GeneratedArticle(id=5, cluster_id=8, headline="H", body="B")
    cluster = NewsCluster(id=8)
    delivery = DeliveryResult(total_recipients=2, delivered_count=1, failed_count=1)

    assert record_delivery_result(article, cluster, delivery) is True
