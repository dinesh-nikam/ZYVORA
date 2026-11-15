"""Dependency-injection container: wires infrastructure and services once per
process (API or worker).  Tests build it with overrides (e.g. a fake search
transport) instead of patching globals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import JobBackend, Settings
from app.core.crypto import BlindIndexer, FieldCipher, configure_cipher
from app.core.ratelimit import RateLimiter
from app.core.redis import create_redis
from app.core.security import TokenService
from app.db.session import create_engine, create_sessionmaker
from app.integrations.carrier.twilio import TwilioCarrierProvider
from app.integrations.http import Resolver, SafeHttpClient
from app.integrations.platforms.clients import (
    GitHubClient,
    PlatformApiClient,
    RedditClient,
    TelegramBotApiClient,
    YouTubeClient,
)
from app.integrations.search.providers import SearchProvider, build_search_providers
from app.services.discovery.service import DiscoveryService
from app.services.events import EventBus
from app.services.phone.service import PhoneIntelService
from app.services.verification.engine import VerificationEngine
from app.services.verification.robots import RobotsPolicy
from app.workers.queue import ArqJobQueue, InlineJobQueue, JobQueue


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    cipher: FieldCipher
    indexer: BlindIndexer
    tokens: TokenService
    http: SafeHttpClient
    phone: PhoneIntelService
    verification: VerificationEngine
    discovery: DiscoveryService
    events: EventBus
    rate_limiter: RateLimiter
    jobs: JobQueue
    carrier_provider: TwilioCarrierProvider | None
    search_providers: list[SearchProvider]
    platform_clients: dict[str, PlatformApiClient]
    _closeables: list[Any] = field(default_factory=list)

    async def aclose(self) -> None:
        await self.jobs.aclose()
        for item in self._closeables:
            await item.aclose()
        await self.redis.aclose()
        await self.engine.dispose()


async def build_container(
    settings: Settings,
    *,
    resolver: Resolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    search_providers: list[SearchProvider] | None = None,
    platform_clients: dict[str, PlatformApiClient] | None = None,
    eager_jobs: bool = False,
    redis: Redis | None = None,
) -> Container:
    cipher = FieldCipher([k.get_secret_value() for k in settings.field_encryption_keys])
    configure_cipher(cipher)
    engine = create_engine(settings)
    redis_client = redis or create_redis(settings.redis_url.get_secret_value())
    http = SafeHttpClient(settings, resolver=resolver, transport=transport)
    closeables: list[Any] = [http]

    searxng_http = http
    if settings.searxng_base_url and settings.searxng_allow_private_network:
        host = httpx.URL(settings.searxng_base_url).host
        searxng_http = SafeHttpClient(settings, resolver=resolver, transport=transport, allow_private_hosts=[host])
        closeables.append(searxng_http)

    providers = search_providers if search_providers is not None else build_search_providers(
        settings, http, searxng_http)

    if platform_clients is None:
        platform_clients = {"github": GitHubClient(http, settings.github_token.get_secret_value()
                                                   if settings.github_token else None)}
        if settings.youtube_api_key:
            platform_clients["youtube"] = YouTubeClient(http, settings.youtube_api_key.get_secret_value())
        if settings.reddit_client_id and settings.reddit_client_secret:
            platform_clients["reddit"] = RedditClient(http, settings.reddit_client_id.get_secret_value(),
                                                      settings.reddit_client_secret.get_secret_value(),
                                                      settings.outbound_user_agent)
        if settings.telegram_bot_token:
            platform_clients["telegram"] = TelegramBotApiClient(http, settings.telegram_bot_token.get_secret_value())

    carrier = None
    if settings.twilio_account_sid and settings.twilio_auth_token:
        carrier = TwilioCarrierProvider(http, settings.twilio_account_sid.get_secret_value(),
                                        settings.twilio_auth_token.get_secret_value())

    container = Container(
        settings=settings,
        engine=engine,
        sessionmaker=create_sessionmaker(engine),
        redis=redis_client,
        cipher=cipher,
        indexer=BlindIndexer(settings.blind_index_key.get_secret_value()),
        tokens=TokenService(settings),
        http=http,
        phone=PhoneIntelService(),
        verification=VerificationEngine(http, RobotsPolicy(http), platform_clients,
                                        respect_robots=settings.respect_robots_txt),
        discovery=DiscoveryService(providers, max_queries=settings.search_max_queries,
                                   results_per_query=settings.search_results_per_query,
                                   concurrency=settings.discovery_max_concurrency),
        events=EventBus(redis_client),
        rate_limiter=RateLimiter(redis_client),
        jobs=InlineJobQueue(_noop),  # replaced below
        carrier_provider=carrier,
        search_providers=providers,
        platform_clients=platform_clients,
        _closeables=closeables,
    )
    from app.workers.runner import DiscoveryJobRunner  # noqa: PLC0415  (runner depends on the container)

    if settings.job_backend is JobBackend.ARQ:
        container.jobs = ArqJobQueue(settings.redis_url.get_secret_value())
    else:
        runner = DiscoveryJobRunner(container)
        container.jobs = InlineJobQueue(runner.run, eager=eager_jobs, max_tries=settings.job_max_tries)
    return container


async def _noop(job_id: object, attempt: int) -> None:
    raise RuntimeError("job queue not initialised")
