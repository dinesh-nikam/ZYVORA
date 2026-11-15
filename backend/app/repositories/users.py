from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiKey, RefreshToken, Role, TelegramLink, User
from app.repositories.base import Page, apply_sort, paginate


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: uuid.UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def by_email(self, email: str) -> User | None:
        return (await self.session.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()

    async def role_by_name(self, name: str) -> Role | None:
        return (await self.session.execute(select(Role).where(Role.name == name))).scalar_one_or_none()

    async def add(self, user: User) -> User:
        self.session.add(user)
        await self.session.flush()
        return user

    async def list(self, *, status: str | None, role: str | None, email_contains: str | None, sort: str | None,
                   page: int, page_size: int) -> Page[User]:
        stmt = select(User).join(Role)
        if status:
            stmt = stmt.where(User.status == status)
        if role:
            stmt = stmt.where(Role.name == role)
        if email_contains:
            stmt = stmt.where(User.email.contains(email_contains.lower(), autoescape=True))
        stmt = apply_sort(stmt, sort, {"created_at": User.created_at, "email": User.email,
                                       "last_login_at": User.last_login_at, "status": User.status}, "-created_at")
        return await paginate(self.session, stmt, page, page_size)


class TokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, token: RefreshToken) -> RefreshToken:
        self.session.add(token)
        await self.session.flush()
        return token

    async def by_hash(self, token_hash: str) -> RefreshToken | None:
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def revoke_family(self, family_id: uuid.UUID, now: datetime) -> None:
        await self.session.execute(
            update(RefreshToken).where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    async def revoke_all_for_user(self, user_id: uuid.UUID, now: datetime) -> None:
        await self.session.execute(
            update(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )


class ApiKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, key: ApiKey) -> ApiKey:
        self.session.add(key)
        await self.session.flush()
        return key

    async def by_hash(self, key_hash: str) -> ApiKey | None:
        return (await self.session.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))).scalar_one_or_none()

    async def for_user(self, user_id: uuid.UUID) -> list[ApiKey]:
        stmt = select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_owned(self, key_id: uuid.UUID, user_id: uuid.UUID) -> ApiKey | None:
        stmt = select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def revoke_all_for_user(self, user_id: uuid.UUID, now: datetime) -> None:
        await self.session.execute(
            update(ApiKey).where(ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None)).values(revoked_at=now)
        )


class TelegramLinkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def by_telegram_hash(self, telegram_hash: str) -> TelegramLink | None:
        stmt = select(TelegramLink).where(TelegramLink.telegram_user_hash == telegram_hash)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def by_user(self, user_id: uuid.UUID) -> TelegramLink | None:
        stmt = select(TelegramLink).where(TelegramLink.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add(self, link: TelegramLink) -> TelegramLink:
        self.session.add(link)
        await self.session.flush()
        return link

    async def delete(self, link: TelegramLink) -> None:
        await self.session.delete(link)
        await self.session.flush()
