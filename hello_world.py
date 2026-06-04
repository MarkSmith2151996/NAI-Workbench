#!/usr/bin/env python3
"""Hello World test app with giant smiley face."""

from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 8080

HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>Hello World!</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    background: #1e1e2e; color: #cdd6f4;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; min-height: 100vh; gap: 32px;
}
.smiley {
    font-size: 200px;
    line-height: 1;
    animation: bounce 2s ease-in-out infinite;
}
@keyframes bounce {
    0%, 100% { transform: translateY(0); }
    50% { transform: translateY(-30px); }
}
h1 {
    font-size: 64px;
    color: #22c55e;
    text-shadow: 0 0 40px #22c55e44;
    animation: glow 3s ease-in-out infinite alternate;
}
@keyframes glow {
    from { text-shadow: 0 0 20px #22c55e22; }
    to { text-shadow: 0 0 60px #22c55e88, 0 0 100px #22c55e44; }
}
.subtitle {
    color: #6c7086; font-size: 14px;
}
</style>
</head><body>
<div class="smiley">&#128578;</div>
<h1>Hello World!</h1>
<p class="subtitle">Running inside Docker &middot; Alpha Builds Sandbox</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(HTML.encode())

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Hello World app running on http://0.0.0.0:{PORT}")
    server.serve_forever()
