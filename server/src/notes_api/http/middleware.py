"""Pure ASGI middleware that stamps ``Cache-Control: no-store`` on every HTTP response.

The contract requires the header on every response, including errors, so HTTP caches never serve
protected content after access changes. The last-resort 500 handler adds it separately, because
Starlette's server-error layer sits outside this middleware.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

NO_STORE = "no-store"


class NoStoreMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = NO_STORE
            await send(message)

        await self.app(scope, receive, send_with_no_store)
