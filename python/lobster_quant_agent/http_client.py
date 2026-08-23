"""Small standard-library HTTP client for the packaged OpenClaw runtime.

The managed OpenClaw package installer intentionally ignores lifecycle scripts, so the
core runtime cannot assume that pip ran. AkShare remains an optional source-checkout
enhancement; ordinary public JSON/text endpoints use this dependency-free adapter.
"""

from __future__ import annotations

import json
from email.message import Message
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class Response:
    def __init__(self, body: bytes, status_code: int, headers: Message | None = None):
        self.content = body
        self.status_code = status_code
        self.headers = headers or Message()
        self.encoding = self.headers.get_content_charset() or "utf-8"

    @property
    def text(self) -> str:
        try:
            return self.content.decode(self.encoding or "utf-8")
        except (LookupError, UnicodeDecodeError):
            return self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def get(url, params=None, headers=None, timeout=10) -> Response:
    if params:
        query = urlencode(params, doseq=True)
        url = f"{url}{'&' if '?' in url else '?'}{query}"
    request = Request(url, headers=dict(headers or {}), method="GET")
    try:
        with urlopen(request, timeout=timeout) as handle:
            return Response(handle.read(), handle.status, handle.headers)
    except HTTPError as error:
        return Response(error.read(), error.code, error.headers)
