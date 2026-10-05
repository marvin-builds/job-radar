import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from job_radar import http


class Hello(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *args):
        pass


class ConnectTest(unittest.TestCase):
    """One address that swallows the connection attempt must not cost the whole call timeout."""

    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), Hello)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def addresses(self, host, port, *args, **kw):
        # First a TEST-NET address that never answers, then the server.
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    def test_slow_address_is_skipped_after_the_connect_timeout(self):
        with mock.patch.object(http, "CONNECT_TIMEOUT_S", 0.3), \
             mock.patch.object(http.socket, "getaddrinfo", self.addresses):
            started = time.monotonic()
            sock = http.connect(("example.test", self.port), timeout=30)
        self.addCleanup(sock.close)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(sock.gettimeout(), 30)  # reads keep the call's timeout

    def test_all_addresses_failing_raises_the_last_error(self):
        dead = lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 1))]
        with mock.patch.object(http.socket, "getaddrinfo", dead):
            with self.assertRaises(OSError):
                http.connect(("example.test", 1), timeout=5)

    def test_get_json_uses_it(self):
        with mock.patch.object(http.socket, "getaddrinfo", self.addresses), \
             mock.patch.object(http, "CONNECT_TIMEOUT_S", 0.3):
            self.assertEqual(http.get_json(f"http://example.test:{self.port}/"), {"ok": True})


if __name__ == "__main__":
    unittest.main()
