"""Роли и токены доступа (шаг 3).

Модель доступа: ``role`` ↔ набор сущностей (глобальный флаг сущности +
явные grants по ролям), ``api_token`` привязан к роли и имеет ``kind``:

- ``data``  — для GET /api/v1/*;
- ``admin`` — для /admin/v1/* (и manual-sync в воркере).

Секрет токена нигде не хранится: при создании отдаётся один раз, в БД —
только SHA-256 hex-дайджест. Отзыв = ``revoked_at`` (запись остаётся для
аудита). ``expires_at`` опционален (бессрочные токены допустимы).
"""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from odata_cache.db.base import Base
from odata_cache.models.common import TimestampMixin, utcnow_iso

TOKEN_PREFIX_DATA = "odt_"    # OData token
TOKEN_PREFIX_ADMIN = "adm_"   # admin token
_TOKEN_BYTES = 32             # 256 бит энтропии


def hash_token(secret: str) -> str:
    """SHA-256 от секрета — единственное, что ложится в БД."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_secret(kind: str) -> str:
    """Одноразовый секрет вида odt_<hex>/adm_<hex>; префикс упрощает лог/поиск."""
    prefix = TOKEN_PREFIX_ADMIN if kind == "admin" else TOKEN_PREFIX_DATA
    return f"{prefix}{secrets.token_hex(_TOKEN_BYTES)}"


class Role(TimestampMixin, Base):
    """Роль = именованный набор доступных сущностей."""

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    grants: Mapped[list[RoleEntity]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )
    tokens: Mapped[list[ApiToken]] = relationship(back_populates="role")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Role {self.name}>"


class RoleEntity(Base):
    """Разрешение роли на сущность (сущьность должна быть ещё и is_active)."""

    __tablename__ = "role_entities"
    __table_args__ = (UniqueConstraint("role_id", "entity_id", name="uq_role_entity"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    # read-only пока всё равно единственный режим (сервис только GET);
    # поле закладываем на будущее (PATCH/POST в 1С через API не предусмотрены)
    can_read: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    role: Mapped[Role] = relationship(back_populates="grants")
    entity: Mapped["Entity"] = relationship()  # noqa: F821

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RoleEntity role={self.role_id} entity={self.entity_id}>"


class ApiToken(TimestampMixin, Base):
    """Bearer-токен. Секрет — только дайджест."""

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="data", nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # видимость для админки: первые символы секрета (не секрет, а «подпись»)
    secret_hint: Mapped[str] = mapped_column(String(16), nullable=False)

    role_id: Mapped[int | None] = mapped_column(
        ForeignKey("roles.id", ondelete="SET NULL")
    )
    # admin-токены работают без роли; data-токен без роли не даёт доступа ни к
    # одной сущности (проверка на шаге 5 вернёт 403 на любой entity-маршрут)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    expires_at: Mapped[str | None] = mapped_column(Text)  # NULL = бессрочный
    revoked_at: Mapped[str | None] = mapped_column(Text)
    last_used_at: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(64))

    role: Mapped[Role | None] = relationship(back_populates="tokens")

    @classmethod
    def create(cls, *, name: str, kind: str, role_id: int | None = None,
               expires_at: str | None = None, created_by: str | None = None,
               ) -> tuple[ApiToken, str]:
        """Вернуть (строку для INSERT, одноразовый секрет)."""
        secret = generate_secret(kind)
        obj = cls(
            name=name,
            kind=kind,
            token_hash=hash_token(secret),
            secret_hint=secret[:12],
            role_id=role_id,
            expires_at=expires_at,
            created_by=created_by,
        )
        return obj, secret

    def is_valid(self, now_iso: str | None = None) -> bool:
        """Живой ли токен (без учёта прав роли — это проверяет auth-слой)."""
        if not self.is_active or self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at <= (now_iso or utcnow_iso()):
            return False
        return True

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ApiToken {self.kind} {self.secret_hint}… role={self.role_id}>"
