"""Minimal HTTP client on urllib. One place, so tests can swap `send`."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

USER_AGENT = "teslamate-suc/0.1"


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = ""
    headers: dict = field(default_factory=dict)  # names in lowercase

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def text(self, limit: int = 300) -> str:
        return self.body[:limit].decode("utf-8", errors="replace")


def send(req: urllib.request.Request, timeout: float) -> Response:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Response(r.status, r.read(), r.headers.get("Content-Type", ""),
                            {k.lower(): v for k, v in r.headers.items()})
    except urllib.error.HTTPError as e:
        hdrs = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        return Response(e.code, e.read() or b"", hdrs.get("content-type", ""), hdrs)


def request(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    form: dict | None = None,
    json_body=None,
    headers: dict | None = None,
    timeout: float = 30,
) -> Response:
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
    data = None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    return send(req, timeout)
