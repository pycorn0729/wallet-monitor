from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

RAO_PER_TAO = 10**9

KIND_TRANSFER = "transfer"  # Balances.Transfer (free TAO)
KIND_STAKE_TRANSFER = "stake_transfer"  # SubtensorModule.StakeTransferred (alpha)
KINDS = {KIND_TRANSFER: "TAO transfer", KIND_STAKE_TRANSFER: "Alpha (stake) transfer"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_approver: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    wallets: Mapped[list["Wallet"]] = relationship(back_populates="owner")

    @property
    def role_label(self) -> str:
        roles = [r for r, on in (("admin", self.is_admin), ("approver", self.is_approver)) if on]
        return ", ".join(roles) or "user"


class Wallet(Base):
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    address: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # coldkey SS58
    purpose: Mapped[str] = mapped_column(Text, default="")
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

    # Balances in RAO, refreshed from chain by the background balance task.
    free_rao: Mapped[int] = mapped_column(BigInteger, default=0)
    staked_rao: Mapped[int] = mapped_column(BigInteger, default=0)  # TAO value of all alpha positions
    stake_detail: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of positions
    balance_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    owner: Mapped[User] = relationship(back_populates="wallets")

    @property
    def free(self) -> float:
        return self.free_rao / RAO_PER_TAO

    @property
    def staked(self) -> float:
        return self.staked_rao / RAO_PER_TAO

    @property
    def total(self) -> float:
        return (self.free_rao + self.staked_rao) / RAO_PER_TAO


class Transfer(Base):
    """A fund movement on chain that touches at least one registered wallet."""

    __tablename__ = "transfers"
    __table_args__ = (UniqueConstraint("block_number", "event_index", name="uq_transfer_event"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    block_number: Mapped[int] = mapped_column(Integer, index=True)
    block_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    event_index: Mapped[int] = mapped_column(Integer)
    extrinsic_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extrinsic_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)  # "{block}-{idx:04}"
    extrinsic_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)

    from_address: Mapped[str] = mapped_column(String(64), index=True)
    to_address: Mapped[str] = mapped_column(String(64), index=True)
    # TAO amount in RAO. For stake transfers this is the TAO value moved.
    amount_rao: Mapped[int] = mapped_column(BigInteger, default=0)
    # Stake transfer only.
    alpha_rao: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    hotkey: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_netuid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    destination_netuid: Mapped[int | None] = mapped_column(Integer, nullable=True)

    source: Mapped[str] = mapped_column(String(16), default="chain")  # chain | taostats

    approved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    approved_by: Mapped[User | None] = relationship()
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="transfer", cascade="all, delete-orphan", order_by="Comment.created_at"
    )

    @property
    def amount(self) -> float:
        return self.amount_rao / RAO_PER_TAO

    @property
    def alpha(self) -> float | None:
        return None if self.alpha_rao is None else self.alpha_rao / RAO_PER_TAO

    @property
    def kind_label(self) -> str:
        return KINDS.get(self.kind, self.kind)


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    transfer_id: Mapped[int] = mapped_column(ForeignKey("transfers.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    transfer: Mapped[Transfer] = relationship(back_populates="comments")
    user: Mapped[User] = relationship()


class KV(Base):
    """Small key/value store for background task state (e.g. last scanned block)."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
