from __future__ import annotations

from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# The instance owner's workspace: created by the workspaces migration,
# serves DM interactions and pre-multi-tenant data.
DEFAULT_WORKSPACE_ID = 1


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Telegram group binding; NULL for the owner's default (DM) workspace
    chat_id: Mapped[Optional[int]] = mapped_column(
        sa.BigInteger, unique=True, nullable=True
    )
    name: Mapped[str] = mapped_column(sa.String(255))
    topic: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    # Newline/comma-separated relevance phrases; NULL -> built-in AI set
    keywords: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    news_prompt: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    tone_of_voice: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    # BYOK; NULL -> global provider chain (default workspace only)
    openrouter_api_key: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    generation_model: Mapped[Optional[str]] = mapped_column(
        sa.String(100), nullable=True
    )
    scoring_model: Mapped[Optional[str]] = mapped_column(sa.String(100), nullable=True)
    score_threshold: Mapped[Optional[int]] = mapped_column(nullable=True)
    hot_threshold: Mapped[Optional[int]] = mapped_column(nullable=True)
    cluster_wait_minutes: Mapped[Optional[int]] = mapped_column(nullable=True)
    active: Mapped[bool] = mapped_column(default=True, server_default="true")
    owner_user_id: Mapped[Optional[int]] = mapped_column(sa.BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(sa.String(255))
    url: Mapped[str] = mapped_column(sa.String(500))
    type: Mapped[str] = mapped_column(
        sa.String(20),
        sa.CheckConstraint("type IN ('rss', 'twitter', 'telegram', 'web', 'sitemap')"),
    )
    categories: Mapped[list[str]] = mapped_column(
        ARRAY(sa.String(50)), server_default="{}"
    )
    weight: Mapped[int] = mapped_column(
        default=5,
        server_default="5",
    )
    scraper_config: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    active: Mapped[bool] = mapped_column(default=True, server_default="true")
    added_by: Mapped[Optional[int]] = mapped_column(sa.BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(),
    )
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    etag: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True)
    last_modified: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True)

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "url", name="uq_sources_workspace_url"),
        sa.Index("idx_sources_workspace", "workspace_id"),
    )


class NewsCluster(Base):
    __tablename__ = "news_clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    topic: Mapped[Optional[str]] = mapped_column(sa.String(500), nullable=True)
    topic_original: Mapped[Optional[str]] = mapped_column(sa.String(500), nullable=True)
    category: Mapped[str] = mapped_column(
        sa.String(50), default="general", server_default="general"
    )
    news_type: Mapped[Optional[str]] = mapped_column(sa.String(50), nullable=True)
    score: Mapped[int] = mapped_column(default=0, server_default="0")
    is_hot: Mapped[bool] = mapped_column(default=False, server_default="false")
    status: Mapped[str] = mapped_column(
        sa.String(20),
        sa.CheckConstraint(
            "status IN ('new', 'waiting', 'generating', 'pending_review', 'approved', 'rejected')"
        ),
        default="new",
        server_default="new",
    )
    sources_count: Mapped[int] = mapped_column(default=1, server_default="1")
    first_seen_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )
    # Set on every status transition; lifecycle timers read this instead of
    # updated_at, which dedup/scoring bump on every matched article.
    status_changed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )

    __table_args__ = (
        sa.Index("idx_clusters_status", "status"),
        sa.Index("idx_clusters_created", created_at.desc()),
        sa.Index("idx_clusters_score", score.desc()),
        sa.Index("idx_clusters_workspace", "workspace_id"),
    )


class RawArticle(Base):
    __tablename__ = "raw_articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        sa.ForeignKey("sources.id", ondelete="CASCADE")
    )
    external_id: Mapped[Optional[str]] = mapped_column(sa.String(500), nullable=True)
    title: Mapped[str] = mapped_column(sa.Text)
    content: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    url: Mapped[Optional[str]] = mapped_column(sa.String(500), nullable=True)
    language: Mapped[str] = mapped_column(
        sa.String(5), default="en", server_default="en"
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    fetched_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    cluster_id: Mapped[Optional[int]] = mapped_column(
        sa.ForeignKey("news_clusters.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        sa.UniqueConstraint("source_id", "external_id"),
        sa.Index("idx_raw_articles_fetched", fetched_at.desc()),
        sa.Index("idx_raw_articles_cluster", "cluster_id"),
    )


class GeneratedArticle(Base):
    __tablename__ = "generated_articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        sa.ForeignKey("news_clusters.id", ondelete="CASCADE")
    )
    headline: Mapped[str] = mapped_column(sa.Text)
    body: Mapped[str] = mapped_column(sa.Text)
    summary: Mapped[Optional[str]] = mapped_column(sa.String(300), nullable=True)
    ai_provider: Mapped[Optional[str]] = mapped_column(sa.String(50), nullable=True)
    version: Mapped[int] = mapped_column(default=1, server_default="1")
    edit_comment: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    moderated_by: Mapped[Optional[int]] = mapped_column(sa.BigInteger, nullable=True)
    telegram_message_id: Mapped[Optional[int]] = mapped_column(
        sa.BigInteger, nullable=True
    )
    telegram_chat_id: Mapped[Optional[int]] = mapped_column(
        sa.BigInteger, nullable=True
    )
    status: Mapped[str] = mapped_column(
        sa.String(20),
        sa.CheckConstraint("status IN ('draft', 'approved', 'rejected')"),
        default="draft",
        server_default="draft",
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )

    __table_args__ = (
        sa.Index("idx_articles_cluster", "cluster_id"),
        sa.Index("idx_articles_status", "status"),
    )


class BotUser(Base):
    __tablename__ = "bot_users"

    id: Mapped[int] = mapped_column(
        sa.BigInteger,
        primary_key=True,
        autoincrement=False,
    )
    username: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True)
    role: Mapped[str] = mapped_column(
        sa.String(20),
        sa.CheckConstraint("role IN ('admin', 'moderator', 'viewer')"),
        default="viewer",
        server_default="viewer",
    )
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(sa.String(100), primary_key=True)
    value: Mapped[str] = mapped_column(sa.Text)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )
