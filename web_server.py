"""Password-protected independent Web server for generated theater HTML."""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable
from typing import Final

from aiohttp import web

from .storage import TheaterStorage

THEATER_CSP: Final[str] = ""


class TheaterWebServer:
    """Serve only indexed play IDs and a small password login page."""

    def __init__(
        self,
        storage: TheaterStorage,
        host: str,
        port: int,
        password: str,
        debug_log: Callable[[str], None] | None = None,
    ) -> None:
        self.storage = storage
        self.host = str(host or "127.0.0.1")
        self.port = int(port or 7315)
        self.password = str(password or "")
        self.debug_log = debug_log
        self._cookie_name = "html_theater_auth"
        self._auth_token = secrets.token_urlsafe(32)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    def _authenticated(self, request: web.Request) -> bool:
        """Check the process-local authentication cookie.

        Args:
            request: Incoming aiohttp request.

        Returns:
            True when no password is configured or the cookie matches.
        """
        if not self.password:
            return True
        token = request.cookies.get(self._cookie_name, "")
        return bool(token) and hmac.compare_digest(token, self._auth_token)

    @staticmethod
    def _security_headers(_allowed_image_urls: object = ()) -> dict[str, str]:
        return {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "SAMEORIGIN",
        }

    def _login_page(self, error: bool = False) -> web.Response:
        """Build the password form without loading external assets.

        Args:
            error: Whether to display a password error.

        Returns:
            HTML response.
        """
        error_html = '<p class="error">密码错误，请重试。</p>' if error else ""
        page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>小剧场登录</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#17151c;
color:#f8f4ff;font-family:"Segoe UI","Microsoft YaHei",sans-serif}}
form{{width:min(380px,calc(100vw - 32px));padding:30px;border:1px solid #40384c;
border-radius:22px;background:#221e29;box-shadow:0 24px 80px #0008}}
h1{{margin:0 0 8px}}p{{color:#c9bdcf;line-height:1.6}}
input,button{{width:100%;height:46px;border-radius:12px;font-size:16px}}
input{{border:1px solid #5e526a;background:#16131b;color:#fff;padding:0 12px}}
button{{margin-top:16px;border:0;background:#d8b4fe;color:#241331;font-weight:700}}
.error{{padding:10px 12px;border-radius:10px;background:#6b2435;color:#ffd7df}}
</style>
</head>
<body>
<form method="post" action="/auth">
<h1>小剧场</h1>
<p>请输入 Web 访问密码。</p>
{error_html}
<input name="password" type="password" autocomplete="current-password" autofocus>
<button type="submit">进入小剧场</button>
</form>
</body>
</html>"""
        return web.Response(
            text=page,
            content_type="text/html",
            charset="utf-8",
            headers={
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "form-action 'self'; base-uri 'none'"
                ),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def _auth(self, request: web.Request) -> web.StreamResponse:
        payload = await request.post()
        password = str(payload.get("password", ""))
        if not hmac.compare_digest(password, self.password):
            raise web.HTTPFound("/?login_error=1")
        response = web.HTTPFound("/")
        response.set_cookie(
            self._cookie_name,
            self._auth_token,
            max_age=7 * 24 * 3600,
            httponly=True,
            samesite="Lax",
            path="/",
        )
        return response

    async def _logout(self, request: web.Request) -> web.StreamResponse:
        response = web.HTTPFound("/")
        response.del_cookie(self._cookie_name, path="/")
        return response

    async def _root(self, request: web.Request) -> web.Response:
        if not self._authenticated(request):
            return self._login_page(request.query.get("login_error") == "1")
        play = self.storage.current_play()
        if play is None:
            page = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>小剧场</title><style>body{margin:0;min-height:100vh;display:grid;
place-items:center;background:#17151c;color:#eee;font-family:sans-serif}</style>
</head><body><p>还没有生成小剧场。</p></body></html>"""
            return web.Response(
                text=page,
                content_type="text/html",
                charset="utf-8",
                headers=self._security_headers(),
            )
        return await self._play_response(str(play["id"]))

    async def _play(self, request: web.Request) -> web.Response:
        if not self._authenticated(request):
            return self._login_page(request.query.get("login_error") == "1")
        return await self._play_response(request.match_info["play_id"])

    async def _play_response(self, play_id: str) -> web.Response:
        """Read an indexed play and return the saved HTML unchanged.

        Args:
            play_id: Opaque play identifier from the route or current selection.

        Returns:
            Generated HTML or a safe 404 response.
        """
        play = self.storage.get_play(play_id)
        try:
            content = self.storage.read_play_html(play_id)
        except FileNotFoundError:
            if self.debug_log:
                self.debug_log("Web play lookup returned 404")
            return web.Response(
                status=404,
                text=(
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    "<title>未找到</title></head><body><p>小剧场不存在。</p>"
                    "</body></html>"
                ),
                content_type="text/html",
                charset="utf-8",
                headers=self._security_headers(),
            )
        return web.Response(
            text=content,
            content_type="text/html",
            charset="utf-8",
            headers=self._security_headers(),
        )

    async def start(self) -> None:
        """Start the independent aiohttp server."""
        if self._runner is not None:
            return
        app = web.Application(client_max_size=2 * 1024 * 1024)
        app.router.add_post("/auth", self._auth)
        app.router.add_post("/logout", self._logout)
        app.router.add_get("/", self._root)
        app.router.add_get("/plays/{play_id:[a-fA-F0-9]{32}}", self._play)
        self._runner = web.AppRunner(app)
        try:
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self.host, self.port)
            await self._site.start()
            if self.debug_log:
                self.debug_log(
                    f"Web server started host={self.host!r} port={self.port} "
                    f"password_enabled={bool(self.password)}"
                )
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        """Stop the independent Web server and release its port."""
        if self._runner is not None:
            await self._runner.cleanup()
            if self.debug_log:
                self.debug_log("Web server stopped")
        self._runner = None
        self._site = None
