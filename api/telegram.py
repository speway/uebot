"""Vercel webhook: replies with the Telegram method in the HTTPS response."""
import datetime as dt
import hmac
from http.server import BaseHTTPRequestHandler
import json
import os
import urllib.request

from bot import Bot, INTRO, TZ


class ReplyCollector:
    def __init__(self):
        self.messages = []

    def send(self, chat_id, text, **extra):
        self.messages.append({'method': 'sendMessage', 'chat_id': chat_id, 'text': text,
                              'parse_mode': 'HTML', 'link_preview_options': {'is_disabled': True}, **extra})
        return {'message_id': 0}


def make_reply(update, state, chat_id):
    collector = ReplyCollector()
    bot = Bot(collector, chat_id, ':memory:')
    bot.username = 'msutf_p223_schedule_bot'
    try:
        for key, value in state.get('kv', {}).items():
            bot.put(key, value)
        bot.handle(update)
        if not collector.messages:
            return {'ok': True}
        response = collector.messages[0]
        if len(collector.messages) > 1:
            response['text'] = '<b>П2‑23 · Расписание</b>\nНа эту неделю много занятий. Используй /today или /tomorrow либо открой полное расписание: https://msu2006.edupage.org/timetable/'
        checked = state.get('kv', {}).get('last_success')
        stale = not checked or dt.datetime.now(TZ) - dt.datetime.fromisoformat(checked) > dt.timedelta(minutes=60)
        if stale:
            response['text'] += '\n\n<i>Данные давно не проверялись. Сверься с EduPage.</i>'
        elif state.get('kv', {}).get('pending_confirmation'):
            response['text'] += '\n\n<i>На сайте замечено изменение; ожидаю повторную проверку.</i>'
        command=update.get('message',{}).get('text','').split()[0].split('@')[0]
        if command in ('/week','/nextweek'):
            today=dt.datetime.now(TZ).date()
            monday=today-dt.timedelta(days=today.weekday())+dt.timedelta(days=7 if command=='/nextweek' else 0)
            photo=state.get('kv',{}).get('image:'+monday.isoformat(),{}).get('file_id')
            if photo:
                caption='П2-23 · Неделя с '+monday.strftime('%d.%m.%Y')+' · Время Ташкента'
                if stale: caption+='\nДанные давно не проверялись. Сверься с EduPage.'
                elif state.get('kv',{}).get('pending_confirmation'): caption+='\nНа сайте замечено изменение; ожидаю повторную проверку.'
                return {'method':'sendPhoto','chat_id':response['chat_id'],'photo':photo,'caption':caption}
        return response
    finally:
        bot.db.close()


class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log private webhook payloads or request paths.

    def respond(self, code, data):
        payload = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.respond(200, {'service': 'p223-schedule-bot', 'mode': 'webhook'})

    def do_POST(self):
        secret = os.getenv('WEBHOOK_SECRET', '')
        supplied = self.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if not secret or not hmac.compare_digest(secret, supplied):
            return self.respond(403, {'ok': False})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 262144:
                return self.respond(413, {'ok': False})
            update = json.loads(self.rfile.read(size))
            chat_id = int(os.environ['TELEGRAM_CHAT_ID'])
            url = os.environ['STATE_URL']
            if not url.startswith('https://raw.githubusercontent.com/'):
                raise ValueError('STATE_URL must point to the public schedule snapshot')
            with urllib.request.urlopen(url, timeout=12) as response:
                data = response.read(1048577)
            if len(data) > 1048576:
                raise ValueError('Snapshot too large')
            state = json.loads(data)
            if state.get('schema') != 1 or not isinstance(state.get('kv'), dict):
                raise ValueError('Invalid snapshot')
            self.respond(200, make_reply(update, state, chat_id))
        except Exception:
            # No message was sent. A failed webhook can be retried safely by Telegram.
            self.respond(503, {'ok': False})
