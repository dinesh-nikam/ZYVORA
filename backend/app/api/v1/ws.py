"""Real-time investigation events over WebSocket.

Protocol: connect to ``/api/v1/ws/investigations/{id}``, then send within 10 s::

    {"type": "auth", "token": "<access token or API key>", "telegram_user_id": null}

Credentials are never accepted in the URL (they would leak into logs).
The server replays recent events, then streams live ones until the
investigation reaches a terminal state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.errors import AppError
from app.core.permissions import Permission
from app.core.security import looks_like_api_key
from app.services.auth import AuthService
from app.services.container import Container
from app.services.investigations import owner_scope

router = APIRouter(tags=["websocket"])
logger = logging.getLogger(__name__)

_TERMINAL = {"investigation.completed", "investigation.failed", "investigation.cancelled"}


async def _authenticate(container: Container, message: dict[str, Any], investigation_id: uuid.UUID) -> bool:
    token = str(message.get("token") or "")
    async with container.sessionmaker() as session:
        auth = AuthService(session, container.settings, container.tokens, container.redis, container.indexer)
        principal = (await auth.authenticate_api_key(token) if looks_like_api_key(token)
                     else await auth.authenticate_bearer(token))
        tg = message.get("telegram_user_id")
        if tg is not None:
            principal = await auth.act_as_telegram_user(principal, int(tg))
        principal.require(Permission.INVESTIGATION_READ)
        from app.repositories.investigations import InvestigationRepository  # noqa: PLC0415

        inv = await InvestigationRepository(session).get(investigation_id, owner_id=owner_scope(principal))
        return inv is not None


@router.websocket("/ws/investigations/{investigation_id}")
async def investigation_events(websocket: WebSocket, investigation_id: uuid.UUID) -> None:
    container: Container = websocket.app.state.container
    await websocket.accept()
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        message = json.loads(raw)
        if not isinstance(message, dict) or message.get("type") != "auth":
            await websocket.close(code=4400, reason="first message must be an auth message")
            return
        if not await _authenticate(container, message, investigation_id):
            await websocket.close(code=4404, reason="investigation not found")
            return
    except (TimeoutError, ValueError, TypeError):
        await websocket.close(code=4401, reason="authentication required")
        return
    except AppError:
        await websocket.close(code=4401, reason="authentication failed")
        return
    except WebSocketDisconnect:
        return

    await websocket.send_json({"type": "auth.ok", "investigation_id": str(investigation_id)})
    try:
        for event in await container.events.history(investigation_id):
            await websocket.send_json(event)
            if event.get("type") in _TERMINAL:
                await websocket.close(code=1000)
                return
        async for event in container.events.subscribe(investigation_id):
            await websocket.send_json(event)
            if event.get("type") in _TERMINAL:
                break
        await websocket.close(code=1000)
    except WebSocketDisconnect:
        logger.debug("websocket client disconnected")
