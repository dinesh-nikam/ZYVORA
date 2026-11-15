from fastapi import APIRouter

from app.api.v1 import admin, api_keys, auth, investigations, me, phone, profiles, telegram, ws

api_router = APIRouter()
for module in (auth, api_keys, phone, investigations, profiles, admin, me, telegram, ws):
    api_router.include_router(module.router)
