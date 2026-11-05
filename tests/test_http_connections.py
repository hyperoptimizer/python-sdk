"""Exercise HTTPX's real wire behavior rather than a mocked connection pool."""

import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from hyperoptimizer._transport import Transport


class ConnectionTests(unittest.TestCase):
    def test_keepalive_and_truncated_response_retry(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append((self.client_address, body))
                payload = json.dumps({"data": {"cliIdentity": {"organizationId": "org"}}}).encode()
                self.send_response(200)
                # The third request deliberately terminates before the declared length.
                self.send_header(
                    "Content-Length",
                    str(len(payload) + (10 if len(received) == 3 else 0)),
                )
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
                if len(received) == 3:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with (
                patch.dict("os.environ", {"NO_PROXY": "127.0.0.1"}),
                Transport(f"http://127.0.0.1:{server.server_port}", "test") as transport,
            ):
                transport.request("SDKIdentity", {})
                transport.request("SDKIdentity", {})
                with patch("hyperoptimizer._transport.time.sleep"):
                    transport.request("SDKIdentity", {})
            self.assertEqual(len(received), 4)
            self.assertEqual(received[0][0], received[1][0])
            self.assertEqual(received[2][1], received[3][1])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
