"""Local DB. Keitaro is the source of truth for campaigns/streams; this is history + audit."""
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class CreatedCampaign(Base):
    __tablename__ = "created_campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    keitaro_id: Mapped[int | None] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    alias: Mapped[str] = mapped_column(String(255))
    geo: Mapped[str] = mapped_column(String(2))
    offer_id: Mapped[int] = mapped_column(Integer)
    geo_stream_id: Mapped[int | None] = mapped_column(Integer)
    offer_stream_id: Mapped[int | None] = mapped_column(Integer)
    # created | rolled_back | rollback_failed | failed | deleted
    status: Mapped[str] = mapped_column(String(32), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    # campaign_created | campaign_rolled_back | campaign_deleted | fetch | push | cancel
    # offer_added | offer_removed | offer_brought_back | pin | unpin
    action: Mapped[str] = mapped_column(String(32), index=True)
    campaign_id: Mapped[int | None] = mapped_column(Integer, index=True)
    stream_id: Mapped[int | None] = mapped_column(Integer)
    offer_id: Mapped[int | None] = mapped_column(Integer)
    shares_before: Mapped[list | None] = mapped_column(JSON)
    shares_after: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Stream(Base):
    """Local copy of a Keitaro stream + draft state of its offers."""

    __tablename__ = "streams"

    id: Mapped[int] = mapped_column(primary_key=True)
    keitaro_stream_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    campaign_id: Mapped[int] = mapped_column(Integer, index=True)
    campaign_name: Mapped[str] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(String(255))
    position: Mapped[int] = mapped_column(Integer, default=0)
    schema: Mapped[str | None] = mapped_column(String(32))
    has_offers: Mapped[bool] = mapped_column(Boolean, default=False)
    # True when offers differ from published_snapshot (unpublished draft) -> yellow in UI
    dirty: Mapped[bool] = mapped_column(Boolean, default=False)
    # offers as of last Fetch/Push: [{offer_id, offer_name, share, pinned, removed, in_keitaro, position}]
    published_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    offers: Mapped[list["StreamOffer"]] = relationship(
        back_populates="stream", cascade="all, delete-orphan", order_by="StreamOffer.position"
    )


class StreamOffer(Base):
    __tablename__ = "stream_offers"
    __table_args__ = (UniqueConstraint("stream_id", "offer_id", name="uq_stream_offer"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stream_id: Mapped[int] = mapped_column(ForeignKey("streams.id", ondelete="CASCADE"), index=True)
    offer_id: Mapped[int] = mapped_column(Integer)
    offer_name: Mapped[str | None] = mapped_column(String(255))
    position: Mapped[int] = mapped_column(Integer, default=0)
    share: Mapped[int] = mapped_column(Integer, default=0)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    # removed = archived (grey): share 0, not sent to Keitaro on Push
    removed: Mapped[bool] = mapped_column(Boolean, default=False)
    # present in Keitaro as of last Fetch/Push
    in_keitaro: Mapped[bool] = mapped_column(Boolean, default=False)
    # offer state inside the stream as Keitaro has it ("active", "disabled", ...); sent back unchanged on Push
    state: Mapped[str] = mapped_column(String(32), default="active", server_default="active")

    stream: Mapped[Stream] = relationship(back_populates="offers")
