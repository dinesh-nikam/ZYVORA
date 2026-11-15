"""Authentication, session management, API keys and Telegram account linking."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import BlindIndexer
from app.core.errors import AuthenticationFailed, Conflict, NotFound, PermissionDenied, ValidationFailed
from app.core.permissions import DEFAULT_ROLE, Permission, parse_permissions
from app.core.security import (
    TokenService,
    hash_opaque_token,
    hash_password,
    new_api_key,
    new_link_code,
    new_refresh_token,
    password_needs_rehash,
    utcnow,
    validate_password_policy,
    verify_password,
)
from app.models import ApiKey, RefreshToken, TelegramLink, User
from app.models.enums import UserStatus
from app.repositories.users import ApiKeyRepository, TelegramLinkRepository, TokenRepository, UserRepository
from app.services import audit
from app.services.audit import AuditContext

_DENY_PREFIX = "pi:jti:deny:"
_LINK_PREFIX = "pi:tglink:"
LINK_CODE_TTL_SECONDS = 600


@dataclass(slots=True)
class Principal:
    user: User
    permissions: frozenset[Permission]
    via: str  # jwt | api_key | telegram
    api_key_id: uuid.UUID | None = None
    acting_service_user_id: uuid.UUID | None = None
    token_jti: str | None = None
    token_expires_at: object | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def id(self) -> uuid.UUID:
        return self.user.id

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        if permission not in self.permissions:
            raise PermissionDenied("You do not have permission to perform this action",
                                   details=[{"required": permission.value}])


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    access_expires_in: int
    refresh_token: str
    refresh_expires_in: int


def _role_permissions(user: User) -> frozenset[Permission]:
    return parse_permissions(user.role.permissions if user.role else [])


def _ensure_active(user: User) -> None:
    if user.status == UserStatus.PENDING.value:
        raise PermissionDenied("Account is pending administrator approval", code="account_pending")
    if user.status != UserStatus.ACTIVE.value or user.deleted_at is not None:
        raise AuthenticationFailed("Account is not active", code="account_inactive")


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings, tokens: TokenService, redis: Redis,
                 indexer: BlindIndexer) -> None:
        self.session = session
        self.settings = settings
        self.tokens = tokens
        self.redis = redis
        self.indexer = indexer
        self.users = UserRepository(session)
        self.refresh_tokens = TokenRepository(session)
        self.api_keys = ApiKeyRepository(session)
        self.links = TelegramLinkRepository(session)

    # ----------------------------------------------------------- registration
    async def register(self, email: str, password: str, ctx: AuditContext, *, role_name: str = DEFAULT_ROLE,
                       status: str | None = None, bypass_closed: bool = False) -> User:
        if not self.settings.registration_open and not bypass_closed:
            raise PermissionDenied("Self-registration is disabled", code="registration_closed")
        email = email.strip().lower()
        validate_password_policy(password, email=email, min_length=self.settings.password_min_length)
        if await self.users.by_email(email) is not None:
            raise Conflict("An account with this email already exists", code="email_taken")
        role = await self.users.role_by_name(role_name)
        if role is None:
            raise ValidationFailed("Unknown role", code="invalid_role")
        user = await self.users.add(User(
            email=email,
            password_hash=hash_password(password),
            status=status or self.settings.default_user_status,
            role_id=role.id,
            role=role,
        ))
        audit.record(self.session, AuditContext(user.id, ctx.ip_address, ctx.via), "auth.register", "user",
                     user.id, role=role.name, status=user.status)
        return user

    # ------------------------------------------------------------------ login
    async def login(self, email: str, password: str, ctx: AuditContext) -> TokenPair:
        user = await self.users.by_email(email.strip().lower())
        now = utcnow()
        locked = user is not None and user.locked_until is not None and user.locked_until > now
        valid = verify_password(password, user.password_hash if user else None)
        if user is None or not valid or locked:
            if user is not None:
                if not locked:
                    user.failed_login_count += 1
                    if user.failed_login_count >= self.settings.max_failed_logins:
                        user.locked_until = now + timedelta(minutes=self.settings.lockout_minutes)
                        user.failed_login_count = 0
                audit.record(self.session, AuditContext(user.id, ctx.ip_address, ctx.via), "auth.login_failed",
                             "user", user.id, reason="locked" if locked else "bad_password")
                await self.session.commit()
            raise AuthenticationFailed("Invalid email or password", code="invalid_credentials")
        _ensure_active(user)
        if password_needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = now
        pair = await self._issue_pair(user, family_id=uuid.uuid4())
        audit.record(self.session, AuditContext(user.id, ctx.ip_address, ctx.via), "auth.login", "user", user.id)
        return pair

    async def _issue_pair(self, user: User, *, family_id: uuid.UUID) -> TokenPair:
        access, claims = self.tokens.issue_access_token(user.id, user.role.name)
        refresh_plain, refresh_hash = new_refresh_token()
        await self.refresh_tokens.add(RefreshToken(
            user_id=user.id, token_hash=refresh_hash, family_id=family_id,
            expires_at=utcnow() + self.tokens.refresh_ttl,
        ))
        return TokenPair(
            access_token=access,
            access_expires_in=int((claims.expires_at - utcnow()).total_seconds()),
            refresh_token=refresh_plain,
            refresh_expires_in=int(self.tokens.refresh_ttl.total_seconds()),
        )

    # ---------------------------------------------------------------- refresh
    async def refresh(self, refresh_token: str, ctx: AuditContext) -> TokenPair:
        row = await self.refresh_tokens.by_hash(hash_opaque_token(refresh_token))
        now = utcnow()
        if row is None:
            raise AuthenticationFailed("Invalid refresh token", code="invalid_refresh_token")
        if row.revoked_at is not None:
            # Reuse of a rotated/revoked token => likely theft: kill the whole family.
            await self.refresh_tokens.revoke_family(row.family_id, now)
            audit.record(self.session, AuditContext(row.user_id, ctx.ip_address, ctx.via),
                         "auth.refresh_token_reuse_detected", "user", row.user_id)
            await self.session.commit()
            raise AuthenticationFailed("Refresh token reuse detected; all sessions in this chain were revoked",
                                       code="refresh_token_reused")
        if row.expires_at <= now:
            raise AuthenticationFailed("Refresh token expired", code="refresh_token_expired")
        user = await self.users.get(row.user_id)
        if user is None:
            raise AuthenticationFailed("Invalid refresh token", code="invalid_refresh_token")
        _ensure_active(user)
        pair = await self._issue_pair(user, family_id=row.family_id)
        new_row = await self.refresh_tokens.by_hash(hash_opaque_token(pair.refresh_token))
        row.revoked_at = now
        row.replaced_by_id = new_row.id if new_row else None
        audit.record(self.session, AuditContext(user.id, ctx.ip_address, ctx.via), "auth.refresh", "user", user.id)
        return pair

    # ----------------------------------------------------------------- logout
    async def logout(self, principal: Principal, refresh_token: str | None, ctx: AuditContext, *,
                     all_sessions: bool = False) -> None:
        now = utcnow()
        if all_sessions:
            await self.refresh_tokens.revoke_all_for_user(principal.id, now)
        elif refresh_token:
            row = await self.refresh_tokens.by_hash(hash_opaque_token(refresh_token))
            if row is not None and row.user_id == principal.id and row.revoked_at is None:
                await self.refresh_tokens.revoke_family(row.family_id, now)
        if principal.token_jti and principal.token_expires_at is not None:
            ttl = int((principal.token_expires_at - now).total_seconds()) + 5  # type: ignore[operator]
            if ttl > 0:
                await self.redis.set(_DENY_PREFIX + principal.token_jti, "1", ex=ttl)
        audit.record(self.session, ctx, "auth.logout", "user", principal.id, all_sessions=all_sessions)

    # --------------------------------------------------------- authentication
    async def authenticate_bearer(self, token: str) -> Principal:
        claims = self.tokens.decode_access_token(token)
        if await self.redis.exists(_DENY_PREFIX + claims.jti):
            raise AuthenticationFailed("Access token has been revoked", code="token_revoked")
        user = await self.users.get(claims.subject)
        if user is None:
            raise AuthenticationFailed("Invalid access token", code="invalid_token")
        _ensure_active(user)
        return Principal(user=user, permissions=_role_permissions(user), via="jwt", token_jti=claims.jti,
                         token_expires_at=claims.expires_at)

    async def authenticate_api_key(self, key: str) -> Principal:
        row = await self.api_keys.by_hash(hash_opaque_token(key))
        now = utcnow()
        if row is None or row.revoked_at is not None or (row.expires_at is not None and row.expires_at <= now):
            raise AuthenticationFailed("Invalid or expired API key", code="invalid_api_key")
        user = row.user
        _ensure_active(user)
        effective = parse_permissions(row.permissions) & _role_permissions(user)
        if row.last_used_at is None or (now - row.last_used_at) > timedelta(minutes=5):
            row.last_used_at = now
            await self.session.commit()
        return Principal(user=user, permissions=effective, via="api_key", api_key_id=row.id)

    # --------------------------------------------------------------- API keys
    async def create_api_key(self, principal: Principal, name: str, permissions: list[str] | None,
                             expires_in_days: int | None, ctx: AuditContext) -> tuple[ApiKey, str]:
        if principal.via == "telegram":
            raise PermissionDenied("API keys cannot be created through the Telegram integration")
        role_perms = _role_permissions(principal.user)
        requested = parse_permissions(permissions) if permissions else role_perms
        if permissions and len(requested) != len(set(permissions)):
            raise ValidationFailed("Unknown permission requested", code="invalid_permission")
        if not requested <= role_perms:
            raise PermissionDenied("Cannot grant permissions you do not hold",
                                   details=[{"not_held": sorted(p.value for p in requested - role_perms)}])
        plaintext, prefix, digest = new_api_key()
        key = await self.api_keys.add(ApiKey(
            user_id=principal.id, key_hash=digest, key_prefix=prefix, name=name,
            permissions=sorted(p.value for p in requested),
            expires_at=(utcnow() + timedelta(days=expires_in_days)) if expires_in_days else None,
        ))
        audit.record(self.session, ctx, "apikey.create", "api_key", key.id, name=name,
                     permissions=key.permissions)
        return key, plaintext

    async def revoke_api_key(self, principal: Principal, key_id: uuid.UUID, ctx: AuditContext) -> None:
        key = await self.api_keys.get_owned(key_id, principal.id)
        if key is None:
            raise NotFound("API key not found")
        if key.revoked_at is None:
            key.revoked_at = utcnow()
        audit.record(self.session, ctx, "apikey.revoke", "api_key", key.id)

    # ------------------------------------------------------- Telegram linking
    async def create_telegram_link_code(self, principal: Principal, ctx: AuditContext) -> str:
        if principal.via == "telegram":
            raise PermissionDenied("Link codes must be created outside Telegram")
        code = new_link_code()
        await self.redis.set(_LINK_PREFIX + hash_opaque_token(code), str(principal.id), ex=LINK_CODE_TTL_SECONDS)
        audit.record(self.session, ctx, "telegram.link_code_created", "user", principal.id)
        return code

    def telegram_hash(self, telegram_user_id: int) -> str:
        return self.indexer.digest(str(telegram_user_id), purpose="telegram_user")

    async def link_telegram(self, service: Principal, code: str, telegram_user_id: int, ctx: AuditContext) -> User:
        service.require(Permission.TELEGRAM_ACT_AS)
        key = _LINK_PREFIX + hash_opaque_token(code.strip().upper())
        user_id = await self.redis.getdel(key)
        if not user_id:
            raise ValidationFailed("Link code is invalid or expired", code="invalid_link_code")
        user = await self.users.get(uuid.UUID(user_id))
        if user is None:
            raise ValidationFailed("Link code is invalid or expired", code="invalid_link_code")
        _ensure_active(user)
        tg_hash = self.telegram_hash(telegram_user_id)
        existing = await self.links.by_telegram_hash(tg_hash)
        if existing is not None and existing.user_id != user.id:
            raise Conflict("This Telegram account is already linked to another user", code="telegram_already_linked")
        current = await self.links.by_user(user.id)
        if current is not None and current.telegram_user_hash != tg_hash:
            await self.links.delete(current)
            current = None
        if current is None and existing is None:
            await self.links.add(TelegramLink(user_id=user.id, telegram_user_hash=tg_hash))
        audit.record(self.session, AuditContext(user.id, ctx.ip_address, "telegram"), "telegram.link", "user",
                     user.id, service_user=str(service.id))
        return user

    async def unlink_telegram(self, service: Principal, telegram_user_id: int, ctx: AuditContext) -> bool:
        service.require(Permission.TELEGRAM_ACT_AS)
        link = await self.links.by_telegram_hash(self.telegram_hash(telegram_user_id))
        if link is None:
            return False
        user_id = link.user_id
        await self.links.delete(link)
        audit.record(self.session, AuditContext(user_id, ctx.ip_address, "telegram"), "telegram.unlink", "user",
                     user_id)
        return True

    async def act_as_telegram_user(self, service: Principal, telegram_user_id: int) -> Principal:
        service.require(Permission.TELEGRAM_ACT_AS)
        link = await self.links.by_telegram_hash(self.telegram_hash(telegram_user_id))
        if link is None:
            raise PermissionDenied("This Telegram account is not linked to a PHONEINTEL user",
                                   code="telegram_not_linked")
        user = await self.users.get(link.user_id)
        if user is None:
            raise PermissionDenied("Linked account no longer exists", code="telegram_not_linked")
        _ensure_active(user)
        # The service key itself never grants user permissions; the linked user's role does.
        perms = _role_permissions(user) - {Permission.TELEGRAM_ACT_AS, Permission.APIKEY_MANAGE}
        return Principal(user=user, permissions=perms, via="telegram", acting_service_user_id=service.id)
