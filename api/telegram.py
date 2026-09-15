"""Vercel webhook: replies with the Telegram method in the HTTPS response."""
import datetime as dt
import hmac
from http.server import BaseHTTPRequestHandler
import json
import logging
import os
import time
import urllib.request

from bot import (BOT_COMMAND_NAMES, BOT_USERNAME, SCHEDULE_JOKE_COMMAND, SCHEDULE_READ_COMMANDS,
                 PRIVATE_REFRESH_DENIED, Bot, EduPage, TZ, digest, is_schedule_joke_button,
                 parse_bot_command, trusted_private_user, week_caption)


STALE_AFTER = dt.timedelta(minutes=30)
STALE_WARNING = ('Автопроверка задержалась. Показываю сохранённое — перед выходом сверься с EduPage, '
                 'а то коллективно припрутся не туда только особо одарённые.')
LOG = logging.getLogger('schedule.telegram')
LOG.setLevel(logging.INFO)


def runtime_event(level, event, started=None, **fields):
    """Write bounded structured telemetry without user text, IDs, or headers."""
    record = {'event': event, 'route': '/api/telegram'}
    if started is not None:
        record['duration_ms'] = round((time.monotonic() - started) * 1000)
    record.update({key: value for key, value in fields.items() if value is not None})
    getattr(LOG, level)(json.dumps(record, ensure_ascii=False, sort_keys=True))


def accepts_update(update, chat_id):
    if not isinstance(update, dict):
        return False
    message = update.get('message') or {}
    chat = message.get('chat') or {}
    if (message.get('from') or {}).get('is_bot'):
        return False
    if chat.get('id') != chat_id and chat.get('type') != 'private':
        return False
    text = (message.get('text') or '').strip()
    if is_schedule_joke_button(text):
        return True
    command, address = parse_bot_command(text)
    known_command = command in BOT_COMMAND_NAMES and (not address or address == BOT_USERNAME)
    return known_command


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


def with_live_snapshots(state, snapshots, now=None, comparable=True):
    """Overlay a fast on-demand read without mutating the persisted Git snapshot."""
    result = {'schema': 1, 'kv': dict(state.get('kv', {})), 'live': True}
    changed_weeks = []
    for snapshot in snapshots:
        previous = result['kv'].get('week:' + snapshot['week'])
        if (not isinstance(previous, dict) or
                digest(previous.get('lessons', [])) != digest(snapshot.get('lessons', []))):
            changed_weeks.append(snapshot['week'])
        result['kv']['week:' + snapshot['week']] = snapshot
    checked_at = (now or dt.datetime.now(TZ)).isoformat()
    result['kv']['last_success'] = checked_at
    result['kv']['source_error'] = None
    result['kv']['live_refresh'] = {
        'status': ('changed' if changed_weeks else 'same') if comparable else 'uncompared',
        'changed_weeks': changed_weeks,
        'checked_at': checked_at,
    }
    return result


def with_live_failure(state, now=None):
    """Record a failed manual attempt while preserving every saved lesson."""
    result = {'schema': 1, 'kv': dict(state.get('kv', {}))}
    result['kv']['live_refresh'] = {
        'status': 'failed',
        'checked_at': (now or dt.datetime.now(TZ)).isoformat(),
    }
    return result


def fetch_state(url, opener=None):
    """Read and validate the bounded public schedule snapshot."""
    if not url.startswith('https://raw.githubusercontent.com/'):
        raise ValueError('STATE_URL must point to the public schedule snapshot')
    separator = '&' if '?' in url else '?'
    request = urllib.request.Request(
        url + separator + 'minute=' + str(int(time.time() // 60)),
        headers={'Cache-Control': 'no-cache', 'User-Agent': 'P223ScheduleBot/1.0'},
    )
    opener = opener or urllib.request.urlopen
    with opener(request, timeout=12) as response:
        data = response.read(1048577)
    if len(data) > 1048576:
        raise ValueError('Snapshot too large')
    state = json.loads(data)
    if state.get('schema') != 1 or not isinstance(state.get('kv'), dict):
        raise ValueError('Invalid snapshot')
    return state


class ReplyCollector:
    def __init__(self):
        self.messages = []

    def send(self, chat_id, text, **extra):
        self.messages.append({'method': 'sendMessage', 'chat_id': chat_id, 'text': text,
                              'parse_mode': 'HTML', 'link_preview_options': {'is_disabled': True}, **extra})
        return {'message_id': 0}


def make_reply(update, state, chat_id, runtime_oidc_token=None):
    if not accepts_update(update, chat_id):
        return {'ok': True}
    collector = ReplyCollector()
    bot = Bot(collector, chat_id, ':memory:')
    bot.username = BOT_USERNAME
    try:
        for key, value in state.get('kv', {}).items():
            bot.put(key, value)
        bot.handle(update, runtime_oidc_token)
        if not collector.messages:
            return {'ok': True}
        response = collector.messages[0]
        if len(collector.messages) > 1:
            response['text'] = ('<b>П2‑23 · Расписание</b>\nВ одну телеграмную простыню эта хуйня не влезает. '
                                'Используй /today или /tomorrow либо открой полное расписание: '
                                'https://msu2006.edupage.org/timetable/')
        checked = state.get('kv', {}).get('last_success')
        stale = state_is_stale(state)
        text = update.get('message', {}).get('text', '').strip()
        command, _address = parse_bot_command(text)
        if command in SCHEDULE_READ_COMMANDS:
            if stale:
                response['text'] += '\n\n<i>' + STALE_WARNING + '</i>'
            elif state.get('kv', {}).get('pending_confirmation'):
                response['text'] += ('\n\n<i>EduPage что-то поменял. Перепроверяю, потому что одного '
                                     'кривого ответа для вашего коллективного пиздеца достаточно.</i>')
        if command in ('/week','/nextweek'):
            today=dt.datetime.now(TZ).date()
            monday=today-dt.timedelta(days=today.weekday())+dt.timedelta(days=7 if command=='/nextweek' else 0)
            photo=state.get('kv',{}).get('image:'+monday.isoformat(),{}).get('file_id')
            if photo and not state.get('live'):
                snapshot = state.get('kv', {}).get('week:' + monday.isoformat())
                caption = week_caption(snapshot, checked) if snapshot else 'П2‑23 · Расписание недели'
                if stale:
                    caption += '\n\n<i>Автопроверка задержалась. Сверься с EduPage, если не хочешь выглядеть долбоёбом у пустой аудитории.</i>'
                elif state.get('kv',{}).get('pending_confirmation'):
                    caption += '\n\n<i>Замечено изменение; перепроверяю, чтобы вы не побежали не туда всей этой прекрасной толпой.</i>'
                photo_response = {
                    'method': 'sendPhoto',
                    'chat_id': response['chat_id'],
                    'photo': photo,
                    'caption': caption,
                    'parse_mode': 'HTML',
                }
                if 'reply_markup' in response:
                    photo_response['reply_markup'] = response['reply_markup']
                return photo_response
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
        self.respond(200, {
            'service': 'p223-schedule-bot',
            'mode': 'webhook',
            'ai': 'disabled',
        })

    def do_POST(self):
        secret = os.getenv('WEBHOOK_SECRET', '')
        supplied = self.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if not secret or not hmac.compare_digest(secret, supplied):
            return self.respond(403, {'ok': False})
        started = time.monotonic()
        operation = 'unknown'
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 262144:
                runtime_event('warning', 'webhook_rejected', started, reason='payload_size')
                return self.respond(413, {'ok': False})
            update = json.loads(self.rfile.read(size))
            chat_id = int(os.environ['TELEGRAM_CHAT_ID'])
            if not accepts_update(update, chat_id):
                return self.respond(200, {'ok': True})
            text = update['message']['text'].strip()
            command, _address = parse_bot_command(text)
            joke_request = command == SCHEDULE_JOKE_COMMAND
            manual_refresh = command == '/refresh'
            private = update['message'].get('chat', {}).get('type') == 'private'
            live_source_allowed = not private or trusted_private_user(update)
            operation = command.lstrip('/') or 'unknown'
            if command in ('/start', '/help'):
                reply = make_reply(update, {'kv': {}}, chat_id)
                runtime_event('info', 'webhook_completed', started, operation=operation,
                              source='not_needed', status=200)
                return self.respond(200, reply)
            # This is intentionally a local easter egg, not a disguised source
            # request. It must stay instant and work even when GitHub/EduPage is down.
            if joke_request:
                reply = make_reply(update, {'schema': 1, 'kv': {}}, chat_id)
                runtime_event('info', 'webhook_completed', started, operation=operation,
                              source='not_needed', status=200)
                return self.respond(200, reply)
            if manual_refresh and not live_source_allowed:
                response = {
                    'method': 'sendMessage',
                    'chat_id': update['message']['chat']['id'],
                    'text': PRIVATE_REFRESH_DENIED,
                    'parse_mode': 'HTML',
                    'link_preview_options': {'is_disabled': True},
                }
                if update['message'].get('message_id'):
                    response['reply_parameters'] = {
                        'message_id': update['message']['message_id'],
                        'allow_sending_without_reply': True,
                    }
                runtime_event('info', 'webhook_completed', started, operation=operation,
                              source='denied', status=200)
                return self.respond(200, response)
            state_available = True
            source_mode = 'saved'
            try:
                state = fetch_state(os.environ['STATE_URL'])
            except Exception as exc:
                runtime_event('warning', 'snapshot_read_failed', started,
                              operation=operation, error_type=type(exc).__name__)
                if not manual_refresh:
                    raise
                state_available = False
                state = {'schema': 1, 'kv': {'source_error': True}}
            if manual_refresh:
                try:
                    state = with_live_snapshots(
                        state, EduPage(timeout=8, attempts=2).fetch(), comparable=state_available)
                    source_mode = 'live'
                except Exception as exc:
                    runtime_event('warning', 'live_schedule_failed', started,
                                  operation=operation, error_type=type(exc).__name__)
                    state = with_live_failure(state)
                    source_mode = 'saved_after_live_failure'
            elif (command in SCHEDULE_READ_COMMANDS and state_is_stale(state)
                  and state_available and live_source_allowed):
                try:
                    # Keep Telegram's webhook comfortably below its timeout. If the
                    # live read fails, make_reply transparently uses saved data.
                    state = with_live_snapshots(state, EduPage(timeout=8, attempts=1).fetch())
                    source_mode = 'live'
                except Exception as exc:
                    runtime_event('warning', 'live_schedule_failed', started,
                                  operation=operation, error_type=type(exc).__name__)
                    source_mode = 'saved_after_live_failure'
            runtime_event('info', 'webhook_completed', started, operation=operation,
                          source=source_mode, status=200)
            self.respond(200, make_reply(update, state, chat_id))
        except Exception as exc:
            runtime_event('error', 'webhook_failed', started, operation=operation,
                          error_type=type(exc).__name__, status=503)
            # No message was sent. A failed webhook can be retried safely by Telegram.
            self.respond(503, {'ok': False})
