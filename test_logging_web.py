#!/usr/bin/env python3
"""Test web app that actually logs — for sandbox_logs verification."""
from http.server import HTTPServer, BaseHTTPRequestHandler
import datetime

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        print(f"[{datetime.datetime.now()}] GET {self.path}", flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h1>Logging Test App</h1><p>Check sandbox_logs!</p>")

if __name__ == "__main__":
    print("Starting logging test server on :8080", flush=True)
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
