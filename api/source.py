"""Authenticated Vercel relay for the public EduPage timetable."""
import datetime as dt
import hmac
from http.server import BaseHTTPRequestHandler
import json
import os

from bot import EduPage, SourceError, TZ


def authorized(expected, supplied):
    return bool(expected) and bool(supplied) and hmac.compare_digest(expected, supplied)


class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not log authentication headers or request metadata.

    def respond(self, code, data):
        payload = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        secret = os.getenv('WEBHOOK_SECRET', '')
        supplied = self.headers.get('X-Schedule-Source-Secret', '')
        if not authorized(secret, supplied):
            return self.respond(403, {'ok': False})
        try:
            # Fail fast enough for GitHub to use its direct fallback instead of
            # spending the whole serverless execution window on a blocked socket.
            snapshots = EduPage(timeout=12, attempts=1).fetch()
            self.respond(200, {'schema': 1, 'fetched_at': dt.datetime.now(TZ).isoformat(),
                               'snapshots': snapshots})
        except SourceError:
            self.respond(502, {'ok': False, 'error': 'source_unavailable'})
