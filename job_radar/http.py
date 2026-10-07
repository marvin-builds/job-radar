from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import urllib.request

USER_AGENT = "job-radar/0.1 (personal job search)"
CONNECT_TIMEOUT_S = 5  # per address; one lost SYN must not cost a whole call timeout


def connect(address: tuple[str, int], timeout=None, source_address=None, *args, public: bool = False) -> socket.socket:
    """Like socket.create_connection, but gives each address only CONNECT_TIMEOUT_S to answer
    before trying the next (e.g. IPv4 after a stuck IPv6); reads then use `timeout`.
    With `public`, only addresses on the internet: not this machine, the home network or a router."""
    host, port = address
    timeout = timeout if isinstance(timeout, (int, float)) else None  # urllib may pass a sentinel
    error = None
    for family, kind, proto, _, addr in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        if public and not ipaddress.ip_address(addr[0].split("%")[0]).is_global:
            error = OSError(f"{host} is not a public address")
            continue
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(CONNECT_TIMEOUT_S if timeout is None else min(timeout, CONNECT_TIMEOUT_S))
            if source_address:
                sock.bind(source_address)
            sock.connect(addr)
            sock.settimeout(timeout)
            return sock
        except OSError as e:
            error = e
            sock.close()
    raise error or OSError(f"no address for {host}")


class _HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._create_connection = connect  # __init__ sets socket.create_connection per instance


class _HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._create_connection = connect


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


def _connect_public(address, timeout=None, source_address=None, *args) -> socket.socket:
    return connect(address, timeout, source_address, public=True)


class _PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._create_connection = _connect_public


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._create_connection = _connect_public


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PublicHTTPConnection, req)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req, context=self._context)


_opener = urllib.request.build_opener(_HTTPHandler, _HTTPSHandler)
_public_opener = urllib.request.build_opener(_PublicHTTPHandler, _PublicHTTPSHandler)  # redirects too


def get_public_text(url: str, timeout: int = 15, limit: int = 2_000_000) -> str:
    """A page whose URL came from untrusted text (e.g. a website triage named): only on the internet."""
    if not re.match(r"https?://", url):
        raise ValueError(f"not an http(s) URL: {url[:100]}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _public_opener.open(req, timeout=timeout) as resp:
        return resp.read(limit).decode(resp.headers.get_content_charset() or "utf-8", "replace")


def urlopen(req, timeout: int = 30):
    return _opener.open(req, timeout=timeout)


def get_json(url: str, headers: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def get_text(url: str, timeout: int = 30, limit: int = 5_000_000) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read(limit).decode(resp.headers.get_content_charset() or "utf-8", "replace")


def post_json(url: str, payload: dict, timeout: int = 60, headers: dict | None = None):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", **(headers or {})},
    )
    with urlopen(req, timeout=timeout) as resp:
        return json.load(resp)
