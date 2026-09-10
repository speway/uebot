"""Vercel webhook: replies with the Telegram method in the HTTPS response."""
import datetime as dt
import hmac
from http.server import BaseHTTPRequestHandler
import json
import os
import time
import urllib.request

from bot import Bot, EduPage, TZ, week_caption


STALE_AFTER = dt.timedelta(minutes=30)
STALE_WARNING = ('Автопроверка задержалась. Показываю сохранённое — сверься с EduPage, '
                 'если идёшь ва-банк.')


def accepts_update(update, chat_id):
    if not isinstance(update, dict):
        return False
    message = update.get('message') or {}
    chat = message.get('chat') or {}
    if chat.get('id') != chat_id and chat.get('type') != 'private':
        return False
    parts = (message.get('text') or '').strip().split()
    if not parts:
        return False
    command, _, address = parts[0].partition('@')
    return (command in ('/start', '/help', '/today', '/tomorrow', '/next', '/week', '/nextweek', '/status')
            and (not address or address.lower() == 'msutf_p223_schedule_bot'))


def state_is_stale(state, now=None):
    checked = state.get('kv', {}).get('last_success')
    if not checked:
        return True
    try:
        value = dt.datetime.fromisoformat(checked)
    except (TypeError, ValueError):
        return True
    if value.tzinfo is None:
        return True
    return (now or dt.datetime.now(TZ)) - value.astimezone(TZ) > STALE_AFTER


def with_live_snapshots(state, snapshots, now=None):
    """Overlay a fast on-demand read without mutating the persisted Git snapshot."""
    result = {'schema': 1, 'kv': dict(state.get('kv', {})), 'live': True}
    for snapshot in snapshots:
        result['kv']['week:' + snapshot['week']] = snapshot
    result['kv']['last_success'] = (now or dt.datetime.now(TZ)).isoformat()
    result['kv']['source_error'] = None
    return result


class ReplyCollector:
    def __init__(self):
        self.messages = []

    def send(self, chat_id, text, **extra):
        self.messages.append({'method': 'sendMessage', 'chat_id': chat_id, 'text': text,
                              'parse_mode': 'HTML', 'link_preview_options': {'is_disabled': True}, **extra})
        return {'message_id': 0}


def make_reply(update, state, chat_id):
    if not accepts_update(update, chat_id):
        return {'ok': True}
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
        stale = state_is_stale(state)
        if stale:
            response['text'] += '\n\n<i>' + STALE_WARNING + '</i>'
        elif state.get('kv', {}).get('pending_confirmation'):
            response['text'] += '\n\n<i>На сайте замечено изменение. Перепроверяю, потому что доверие — хорошо, а две проверки лучше.</i>'
        command=update.get('message',{}).get('text','').strip().split()[0].split('@')[0]
        if command in ('/week','/nextweek'):
            today=dt.datetime.now(TZ).date()
            monday=today-dt.timedelta(days=today.weekday())+dt.timedelta(days=7 if command=='/nextweek' else 0)
            photo=state.get('kv',{}).get('image:'+monday.isoformat(),{}).get('file_id')
            if photo:
                snapshot = state.get('kv', {}).get('week:' + monday.isoformat())
                caption = week_caption(snapshot, checked) if snapshot else 'П2‑23 · Расписание недели'
                if stale:
                    caption += '\n\n<i>Автопроверка задержалась. Перед выходом сверься с EduPage.</i>'
                elif state.get('kv',{}).get('pending_confirmation'):
                    caption += '\n\n<i>Замечено изменение; сейчас перепроверяю.</i>'
                return {'method':'sendPhoto','chat_id':response['chat_id'],'photo':photo,'caption':caption,
                        'parse_mode':'HTML'}
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
        self.send_header('Content-Length', str(len(payload)))
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
            if not accepts_update(update, chat_id):
                return self.respond(200, {'ok': True})
            command = update['message']['text'].strip().split()[0].split('@')[0]
            if command in ('/start', '/help'):
                reply = make_reply(update, {'kv': {}}, chat_id)
                reply['text'] = reply['text'].replace('\n\n<i>' + STALE_WARNING + '</i>', '')
                return self.respond(200, reply)
            url = os.environ['STATE_URL']
            if not url.startswith('https://raw.githubusercontent.com/'):
                raise ValueError('STATE_URL must point to the public schedule snapshot')
            separator = '&' if '?' in url else '?'
            request = urllib.request.Request(url + separator + 'minute=' + str(int(time.time() // 60)),
                                             headers={'Cache-Control': 'no-cache',
                                                      'User-Agent': 'P223ScheduleBot/1.0'})
            with urllib.request.urlopen(request, timeout=12) as response:
                data = response.read(1048577)
            if len(data) > 1048576:
                raise ValueError('Snapshot too large')
            state = json.loads(data)
            if state.get('schema') != 1 or not isinstance(state.get('kv'), dict):
                raise ValueError('Invalid snapshot')
            if command in ('/today', '/tomorrow', '/next') and state_is_stale(state):
                try:
                    # Keep Telegram's webhook comfortably below its timeout. If the
                    # live read fails, make_reply transparently uses saved data.
                    state = with_live_snapshots(state, EduPage(timeout=6, attempts=1).fetch())
                except Exception:
                    pass
            self.respond(200, make_reply(update, state, chat_id))
        except Exception:
            # No message was sent. A failed webhook can be retried safely by Telegram.
            self.respond(503, {'ok': False})
