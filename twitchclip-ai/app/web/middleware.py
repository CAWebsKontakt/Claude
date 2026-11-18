"""Sicherheits-Header für alle Antworten (zusätzlich zu Caddy)."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def build_csp(form_action_origins: tuple[str, ...] = ()) -> str:
    """CSP. form_action_origins: Ziele, zu denen ein Formular-POST weiterleiten darf (OAuth-Start).

    Browser wenden form-action auch auf Weiterleitungen nach dem Absenden an – ohne diese Ausnahme
    würde z. B. „TikTok-Konto verbinden“ (POST → 303 zu tiktok.com) blockiert.
    """
    form_action = " ".join(("'self'", *form_action_origins))
    return "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data: https://static-cdn.jtvnw.net https://clips-media-assets2.twitch.tv "
            "https://*.tiktokcdn.com https://*.tiktokcdn-us.com https://*.tiktokcdn-eu.com",
            "media-src 'self'",
            "frame-src https://clips.twitch.tv https://player.twitch.tv",
            "connect-src 'self'",
            f"form-action {form_action}",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "object-src 'none'",
        ]
    )


CONTENT_SECURITY_POLICY = build_csp()

STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
}


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, form_action_origins: tuple[str, ...] = ()) -> None:
        self.app = app
        self.headers = {**STATIC_HEADERS, "Content-Security-Policy": build_csp(form_action_origins)}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in self.headers.items():
                    if key not in headers:
                        headers[key] = value
                content_type = headers.get("content-type", "")
                if (
                    content_type.startswith(("text/html", "application/json"))
                    and "cache-control" not in headers
                ):
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_wrapper)
