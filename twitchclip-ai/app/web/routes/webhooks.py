"""Öffentliche Webhook-Endpunkte (ohne Login, dafür kryptografisch geprüft)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse, Response

from app.config import get_settings
from app.db import session_scope
from app.integrations.twitch.eventsub import (
    TYPE_VERIFICATION,
    EventSubVerificationError,
    is_duplicate,
    verify_and_parse,
)
from app.redis_client import get_redis
from app.services.chat_stats import record_chat_message
from app.services.eventsub_handler import FollowUps, handle_message
from app.services.twitch_monitor import mark_subscription_status
from app.workers.jobs.twitch import schedule_eventsub_sync, schedule_post_stream, schedule_stream_details

logger = logging.getLogger("tcai.webhooks")
router = APIRouter(prefix="/webhooks")

MAX_BODY_BYTES = 256 * 1024


def _process(message) -> FollowUps:
    with session_scope() as db:
        return handle_message(db, message)


def _mark_verified(subscription_id: str) -> None:
    """Nach erfolgreicher Challenge setzt Twitch das Abo auf 'enabled' – lokal sofort spiegeln."""
    with session_scope() as db:
        mark_subscription_status(db, subscription_id, "enabled")


def _schedule(follow: FollowUps) -> None:
    for streamer_id in follow.stream_details_for:
        schedule_stream_details(streamer_id)
    schedule_post_stream(follow.ended_sessions, follow.ended_streamers)
    if follow.resync_eventsub:
        schedule_eventsub_sync(delay_seconds=60)


@router.post("/twitch/eventsub", include_in_schema=False)
async def twitch_eventsub(request: Request) -> Response:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return Response(status_code=413)
    settings = get_settings()
    try:
        message = verify_and_parse(
            dict(request.headers), body, settings.twitch_eventsub_secret.get_secret_value()
        )
    except EventSubVerificationError as exc:
        logger.warning("EventSub-Nachricht abgelehnt", extra={"reason": str(exc)})
        return Response(status_code=403)

    if message.message_type == TYPE_VERIFICATION:
        logger.info("EventSub-Abo bestätigt", extra={"type": message.subscription_type})
        await run_in_threadpool(_mark_verified, message.subscription_id)
        return PlainTextResponse(message.challenge or "", status_code=200)

    redis = get_redis()
    if is_duplicate(redis, message.message_id):
        return Response(status_code=204)

    if message.subscription_type == "channel.chat.message":
        # Hochfrequent: nur Zähler in Redis, keine Datenbank, keine Texte speichern
        if settings.chat_stats_enabled:
            event = message.event
            fragments = (event.get("message") or {}).get("fragments") or []
            await run_in_threadpool(
                record_chat_message,
                redis,
                str(event.get("broadcaster_user_id") or ""),
                str(event.get("chatter_user_id") or ""),
                (event.get("message") or {}).get("text") or "",
                sum(1 for f in fragments if f.get("type") == "emote"),
            )
        return Response(status_code=204)

    follow = await run_in_threadpool(_process, message)
    await run_in_threadpool(_schedule, follow)
    return Response(status_code=204)
