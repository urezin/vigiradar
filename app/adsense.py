"""
Google AdSense (Auto ads) for this site.

A small ASGI middleware that
  * inserts the AdSense loader and account meta tag before </head> of every public HTML page, and
  * serves /ads.txt,
so no page template has to be edited. Private areas (the logged-in app, admin, APIs) are excluded:
ads never appear inside the product. Which ad formats show, and where, is set in the AdSense
account (Ads > By site > Auto ads), not here.

Turn it off without a deploy by setting ADSENSE_CLIENT to an empty string; change account the same way.
"""
from __future__ import annotations

import os

ADSENSE_CLIENT = os.environ.get("ADSENSE_CLIENT", "ca-pub-4782116626518435").strip()


def _snippet() -> bytes:
    c = ADSENSE_CLIENT
    return (f'<meta name="google-adsense-account" content="{c}">'
            f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={c}" '
            f'crossorigin="anonymous"></script>').encode()


def _ads_txt() -> bytes:
    pub = ADSENSE_CLIENT.replace("ca-", "", 1)
    return f"google.com, {pub}, DIRECT, f08c47fec0942fa0\n".encode()


class AdSenseMiddleware:
    def __init__(self, app, exclude: tuple[str, ...] = ()):
        self.app = app
        self.exclude = tuple(exclude)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not ADSENSE_CLIENT:
            return await self.app(scope, receive, send)
        path = scope.get("path") or "/"
        if path == "/ads.txt" and scope.get("method") in ("GET", "HEAD"):
            body = _ads_txt()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode()),
                                    (b"cache-control", b"public, max-age=86400")]})
            await send({"type": "http.response.body", "body": body if scope["method"] == "GET" else b""})
            return
        if any(path == p or path.startswith(p.rstrip("/") + "/") for p in self.exclude):
            return await self.app(scope, receive, send)

        start: dict | None = None
        chunks: list[bytes] = []
        is_html = False

        async def _send(msg):
            nonlocal start, is_html
            if msg["type"] == "http.response.start":
                ctype = dict(msg.get("headers") or []).get(b"content-type", b"")
                enc = dict(msg.get("headers") or []).get(b"content-encoding", b"")
                is_html = ctype.startswith(b"text/html") and not enc and msg.get("status", 200) == 200
                if not is_html:
                    return await send(msg)
                start = msg
                return
            if not is_html:
                return await send(msg)
            chunks.append(msg.get("body", b""))
            if msg.get("more_body"):
                return
            body = b"".join(chunks)
            if b"adsbygoogle.js" not in body:
                i = body.lower().find(b"</head>")
                if i != -1:
                    body = body[:i] + _snippet() + body[i:]
            headers = [(k, v) for k, v in (start.get("headers") or []) if k.lower() != b"content-length"]
            headers.append((b"content-length", str(len(body)).encode()))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, _send)
