"""П2-23 timetable bot. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import fcntl
import hashlib
import html
import http.client
import http.cookiejar
import json
import logging
import os
from pathlib import Path
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

SOURCE = 'https://msu2006.edupage.org'
GROUP = 'П2-23'
TZ = ZoneInfo('Asia/Tashkent')
DAYS = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
INTRO = 'Бот чисто для своих кентиков из лучшей группы П2‑23.'
LOG = logging.getLogger('schedule')


class SourceError(Exception):
    pass


class DeliveryError(RuntimeError):
    """The source was checked, but publication needs attention."""


class TelegramRejected(DeliveryError):
    """Telegram explicitly refused a request; it did not deliver it."""


def normalized(value):
    return re.sub(r'\s+', '', str(value)).upper().replace('‑', '-').replace('–', '-')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def ipv4_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    """Open one TCP connection without selecting EduPage's unreachable IPv6 route."""
    host, port = address
    last_error = None
    for family, socktype, proto, _, socket_address in socket.getaddrinfo(
            host, port, socket.AF_INET, socket.SOCK_STREAM):
        connection = None
        try:
            connection = socket.socket(family, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(socket_address)
            return connection
        except OSError as exc:
            last_error = exc
            if connection is not None:
                connection.close()
    if last_error is not None:
        raise last_error
    raise OSError('EduPage has no IPv4 address')


class IPv4HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = ipv4_connection


class IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(IPv4HTTPSConnection, request, context=self._context,
                            check_hostname=self._check_hostname)


class EduPage:
    def __init__(self):
        # EduPage advertises IPv6, while hosted runners currently have no IPv6 route.
        # Keep this transport scoped to EduPage; Telegram and GitHub retain defaults.
        self.http = urllib.request.build_opener(
            IPv4HTTPSHandler(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.gsh = ''

    def read(self, request):
        # These endpoints only read published timetable data, so retries are safe.
        for attempt in range(3):
            try:
                with self.http.open(request, timeout=25) as response:
                    return response.read()
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if isinstance(exc, urllib.error.HTTPError) and exc.code < 500 and exc.code != 429:
                    raise SourceError('EduPage HTTP ' + str(exc.code)) from None
                if attempt == 2:
                    reason = getattr(exc, 'reason', exc)
                    raise SourceError('EduPage connection: ' + type(reason).__name__ + ': ' + str(reason)) from None
                time.sleep(2 ** attempt)

    def connect(self):
        page = self.read(SOURCE + '/timetable/').decode()
        match = re.search(r'gsechash\s*[=:]\s*[\x22\x27]([^\x22\x27]+)', page)
        if not match:
            raise SourceError('EduPage: изменился формат страницы')
        self.gsh = match.group(1)

    def rpc(self, module, function, args):
        request = urllib.request.Request(
            f'{SOURCE}/timetable/server/{module}.js?__func={function}',
            data=json.dumps({'__args': [None, *args], '__gsh': self.gsh}).encode(),
            headers={'Content-Type': 'application/json', 'User-Agent': 'P223ScheduleBot/1.0'})
        response = json.loads(self.read(request))
        if not isinstance(response, dict) or 'r' not in response or response.get('reload') or response.get('e'):
            raise SourceError('EduPage: ответ не содержит подтверждённого расписания')
        return response['r']

    def fetch(self, today=None):
        today = today or dt.datetime.now(TZ).date()
        year = today.year if today.month >= 8 else today.year - 1
        self.connect()
        meta = self.rpc('ttviewer', 'getTTViewerData', [year])
        monday = today - dt.timedelta(days=today.weekday())
        published = {}
        for row in meta['regular']['timetables']:
            if row.get('hidden') or not row.get('datefrom'):
                continue
            start = dt.date.fromisoformat(row['datefrom'])
            if monday <= start <= monday + dt.timedelta(days=20):
                # A republished week can have a new number. Compare by date, not number.
                published[start] = row
        if not published:
            raise SourceError('Для ближайших недель пока нет опубликованного расписания')
        snapshots = []
        for start, row in sorted(published.items()):
            raw = self.rpc('regulartt', 'regularttGetData', [str(row['tt_num'])])
            snapshots.append(parse_week(raw, row))
        return snapshots


def parse_week(raw, meta):
    try:
        tables = {table['id']: {str(row['id']): row for row in table['data_rows']}
                  for table in raw['dbiAccessorRes']['tables']}
        for required in ('classes', 'lessons', 'cards', 'periods', 'subjects', 'teachers', 'classrooms'):
            if required not in tables:
                raise SourceError('Отсутствует таблица ' + required)
        matches = [row for row in tables['classes'].values() if normalized(row.get('short')) == GROUP]
        if len(matches) != 1:
            raise SourceError('Группа П2-23 не определена однозначно')
        class_id = str(matches[0]['id'])
        start = dt.date.fromisoformat(meta['datefrom'])
        monday = start - dt.timedelta(days=start.weekday())
        cards = []
        active_weeks = set()
        for card in tables['cards'].values():
            lesson = tables['lessons'][str(card['lessonid'])]
            if class_id not in list(map(str, lesson.get('classids', []))):
                continue
            if not card.get('days') and not card.get('period'):
                continue  # Unplaced lessons are not scheduled classes.
            if not card.get('days') or not card.get('period') or not card.get('weeks'):
                raise SourceError('Неполные данные занятия')
            active_weeks.update(i for i, bit in enumerate(card['weeks']) if bit == '1')
            cards.append((card, lesson))
        if len(active_weeks) > 1:
            raise SourceError('Обнаружен многонедельный шаблон: требуется проверка привязки дат')
        result = []
        for card, lesson in cards:
            if set(card['days']) - {'0', '1'} or len(card['days']) > 7:
                raise SourceError('Изменился формат дней недели')
            period = int(card['period'])
            duration = int(lesson.get('durationperiods') or 1)
            if duration < 1:
                raise SourceError('Некорректная длительность занятия')
            first = tables['periods'][str(period)]
            last = tables['periods'][str(period + duration - 1)]
            if first.get('daydata') or last.get('daydata'):
                raise SourceError('Индивидуальные звонки требуют проверки')
            subject = tables['subjects'][str(lesson['subjectid'])]['name']
            teachers = sorted(tables['teachers'][str(i)].get('short') or tables['teachers'][str(i)].get('name', '')
                              for i in lesson.get('teacherids', []))
            rooms = sorted(tables['classrooms'][str(i)]['name'] for i in card.get('classroomids', []))
            for day, enabled in enumerate(card['days']):
                if enabled != '1':
                    continue
                date = monday + dt.timedelta(days=day)
                if date < start:
                    continue
                result.append({'date': date.isoformat(), 'start': first['starttime'], 'end': last['endtime'],
                               'subject': subject, 'teachers': teachers, 'rooms': rooms,
                               'groups': sorted(filter(None, lesson.get('groupnames', [])))})
        # Do not use row IDs, colours, or publication numbers in semantic comparisons.
        unique = {digest(item): item for item in result}
        ordered = sorted(unique.values(), key=lambda x: (x['date'], x['start'], x['subject'], str(x['groups'])))
        return {'week': monday.isoformat(), 'source_title': meta['text'], 'class_name': matches[0]['name'],
                'lessons': ordered}
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        raise SourceError('EduPage: структура расписания изменилась') from exc


def lesson_text(item):
    esc = html.escape
    text = f"<b>{esc(item['start'])}–{esc(item['end'])}</b>  {esc(item['subject'])}"
    details = [' / '.join(item['teachers']) or 'Преподаватель не указан',
               'ауд. ' + ', '.join(item['rooms']) if item['rooms'] else 'Аудитория не указана']
    if item['groups']:
        details.append(', '.join(item['groups']))
    return text + '\n' + esc(' · '.join(details))


def render_day(date, lessons):
    day = dt.date.fromisoformat(date)
    lines = [f'<b>{DAYS[day.weekday()]} · {day:%d.%m}</b>']
    # Merge adjacent pairs only for display; keep individual pairs in the change detector.
    merged = []
    for item in lessons:
        item = dict(item)
        if merged:
            previous = merged[-1]
            gap = (dt.datetime.strptime(item['start'], '%H:%M') - dt.datetime.strptime(previous['end'], '%H:%M')).total_seconds()
            if 0 <= gap <= 1200 and all(previous[k] == item[k] for k in ('subject', 'teachers', 'rooms', 'groups')):
                previous['end'] = item['end']
                previous['_pairs'] = previous.get('_pairs', 1) + 1
                continue
        merged.append(item)
    lines.extend(lesson_text(item) + (f"\n{item['_pairs']} пары · с перерывом" if item.get('_pairs') else '') for item in merged)
    if not lessons:
        lines.append('В опубликованном расписании занятий нет.')
    return '\n\n'.join(lines)


def render_week(snapshot):
    start = dt.date.fromisoformat(snapshot['week'])
    result = [f'<b>П2‑23 · Расписание</b>\n{start:%d.%m}–{start + dt.timedelta(days=5):%d.%m.%Y}']
    groups = collections.defaultdict(list)
    for item in snapshot['lessons']:
        groups[item['date']].append(item)
    if not groups:
        result.append('В этой публикации занятия П2‑23 пока не расставлены. Это не подтверждение отмены занятий.')
    for date, lessons in sorted(groups.items()):
        result.append(render_day(date, lessons))
    result.append('<a href="' + SOURCE + '/timetable/">Источник · EduPage</a>\nВремя Ташкента')
    return split_sections(result)


def split_sections(sections, limit=3700):
    chunks, current = [], ''
    for section in sections:
        if len(section) > limit:
            raise SourceError('Сообщение слишком длинное для безопасной отправки')
        proposed = current + ('\n\n' if current else '') + section
        if len(proposed) > limit:
            chunks.append(current)
            current = section
        else:
            current = proposed
    if current:
        chunks.append(current)
    return chunks


def describe_changes(old, new, today):
    old_days, new_days = collections.defaultdict(list), collections.defaultdict(list)
    for item in old:
        if item['date'] >= today:
            old_days[item['date']].append(item)
    for item in new:
        if item['date'] >= today:
            new_days[item['date']].append(item)
    sections = ['<b>П2‑23 · Изменения в расписании</b>']
    changes = 0
    for date in sorted(old_days.keys() | new_days.keys()):
        a = {digest(x): x for x in old_days[date]}
        b = {digest(x): x for x in new_days[date]}
        if a == b:
            continue
        changes += 1
        day = dt.date.fromisoformat(date)
        sections.append(f'<b>{DAYS[day.weekday()]} · {day:%d.%m}</b>')
        removed = {k: a[k] for k in a.keys() - b.keys()}
        added = {k: b[k] for k in b.keys() - a.keys()}
        # Match only unambiguous pairs: same subject/group, then same time/group.
        for fields in [('subject', 'groups'), ('start', 'end', 'groups')]:
            for key, before in list(removed.items()):
                matches = [(k, x) for k, x in added.items() if all(x[f] == before[f] for f in fields)]
                backwards = [x for x in removed.values() if all(x[f] == before[f] for f in fields)]
                if len(matches) != 1 or len(backwards) != 1:
                    continue
                new_key, after = matches[0]
                details = []
                for field, label in [('subject', 'Предмет'), ('rooms', 'Аудитория'), ('teachers', 'Преподаватель'), ('groups', 'Подгруппа')]:
                    if before[field] != after[field]:
                        def value(x):
                            return html.escape(', '.join(x) if isinstance(x, list) else str(x)) or 'не указано'
                        details.append(f'{label}: {value(before[field])} → <b>{value(after[field])}</b>')
                if (before['start'], before['end']) != (after['start'], after['end']):
                    details.append(f"Время: {before['start']}–{before['end']} → <b>{after['start']}–{after['end']}</b>")
                heading = html.escape(after['subject']) + f" · {after['start']}–{after['end']}"
                sections.append('<b>' + heading + '</b>\n' + '\n'.join(details))
                del removed[key]
                del added[new_key]
        for key in sorted(removed):
            sections.append('Убрано из расписания:\n' + lesson_text(a[key]))
        for key in sorted(added):
            sections.append('Добавлено в расписание:\n' + lesson_text(b[key]))
    if not changes:
        return []
    sections.append('<a href="' + SOURCE + '/timetable/">Проверить источник</a> · Время Ташкента')
    return split_sections(sections)


class Telegram:
    def __init__(self, token):
        self.token = token

    def call(self, method, **params):
        request = urllib.request.Request('https://api.telegram.org/bot' + self.token + '/' + method,
                                         data=json.dumps(params).encode(), headers={'Content-Type': 'application/json'})
        try:
            answer = json.loads(urllib.request.urlopen(request, timeout=45).read())
        except urllib.error.HTTPError as exc:
            if method == 'editMessageText' and exc.code == 400:
                try:
                    if 'message is not modified' in json.loads(exc.read()).get('description', '').lower():
                        return True
                except (ValueError, UnicodeError):
                    pass
            # Never log request URLs: they contain the token.
            if 400 <= exc.code < 500:
                raise TelegramRejected('Telegram HTTP ' + str(exc.code)) from None
            raise DeliveryError('Telegram HTTP ' + str(exc.code)) from None
        except Exception:
            raise DeliveryError('Telegram: результат запроса неизвестен; автоматический повтор отправки отключён') from None
        if not answer.get('ok'):
            raise TelegramRejected('Telegram отклонил запрос')
        return answer['result']

    def photo(self, chat_id, image, caption, message_id=None):
        import uuid
        boundary = uuid.uuid4().hex
        fields = {'chat_id': str(chat_id)}
        method = 'sendPhoto'
        if message_id:
            method = 'editMessageMedia'
            fields.update(message_id=str(message_id), media=json.dumps({'type': 'photo', 'media': 'attach://photo', 'caption': caption}))
        else:
            fields.update(caption=caption, disable_notification='true')
        chunks=[]
        for key,value in fields.items():
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="schedule.png"\r\nContent-Type: image/png\r\n\r\n'.encode()+image+b'\r\n')
        chunks.append(f'--{boundary}--\r\n'.encode())
        request=urllib.request.Request('https://api.telegram.org/bot' + self.token + '/' + method, data=b''.join(chunks), headers={'Content-Type': 'multipart/form-data; boundary='+boundary})
        try:
            with urllib.request.urlopen(request, timeout=60) as response: result=json.load(response)
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise TelegramRejected('Telegram photo HTTP ' + str(exc.code)) from None
            raise DeliveryError('Telegram photo HTTP ' + str(exc.code)) from None
        except Exception:
            raise DeliveryError('Telegram photo delivery failed') from None
        if not result.get('ok'): raise TelegramRejected('Telegram rejected photo')
        return result['result']

    def send(self, chat_id, text, **extra):
        return self.call('sendMessage', chat_id=chat_id, text=text, parse_mode='HTML',
                         link_preview_options={'is_disabled': True}, **extra)


class Bot:
    def __init__(self, telegram, chat_id, database, owner_id=0):
        self.tg, self.chat_id, self.owner_id = telegram, int(chat_id), int(owner_id)
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(database, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.lock = threading.RLock()
        self.last_error = ''
        self.username = ''

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def send_once(self, key, text, chat_id=None):
        prior = self.get('sent:' + key)
        if prior:
            if prior.get('status') == 'pending':
                self.put('delivery_attention', True)
            return prior.get('message_id')
        # Reserve before sending. An interrupted send stays pending, never silently duplicated.
        self.put('sent:' + key, {'status': 'pending'})
        try:
            quiet = dt.datetime.now(TZ).hour < 7 or dt.datetime.now(TZ).hour >= 23
            result = self.tg.send(self.chat_id if chat_id is None else chat_id, text, disable_notification=quiet)
        except TelegramRejected:
            self.put('sent:' + key, None)
            raise
        except Exception:
            self.put('delivery_attention', True)
            raise
        self.put('sent:' + key, {'status': 'sent', 'message_id': result['message_id']})
        return result['message_id']

    def publish_week(self, snapshot):
        key = 'publication:' + snapshot['week']
        previous = self.get(key, [])
        pages = render_week(snapshot)
        stored = []
        for i, page in enumerate(pages):
            prior = previous[i] if i < len(previous) else None
            if prior and prior.get('message_id'):
                if prior['hash'] != digest(page):
                    self.tg.call('editMessageText', chat_id=self.chat_id, message_id=prior['message_id'],
                                 text=page, parse_mode='HTML', link_preview_options={'is_disabled': True})
                message_id = prior['message_id']
            else:
                message_id = self.send_once(f'weekly:{key}:{snapshot.get("revision", 0)}:{i}', page)
            stored.append({'message_id': message_id, 'hash': digest(page)})
        for obsolete in previous[len(pages):]:
            if obsolete.get('message_id'):
                self.tg.call('editMessageText', chat_id=self.chat_id, message_id=obsolete['message_id'],
                             text='П2‑23 · Расписание обновлено в основном сообщении за эту неделю.')
        self.put(key, stored)

    def publish_image(self, snapshot):
        if not hasattr(self.tg, 'photo') or not snapshot['lessons']:
            return
        key='image:'+snapshot['week']
        prior=self.get(key, {})
        from schedule_image import DESIGN_VERSION
        fingerprint=digest({'lessons': snapshot['lessons'], 'design': DESIGN_VERSION})
        if prior.get('hash') == fingerprint: return
        from schedule_image import render_image
        image=render_image(snapshot)
        reservation='image-send:'+snapshot['week']+':'+str(snapshot.get('revision',0))+':'+fingerprint
        if not prior.get('message_id'):
            if self.get(reservation):
                self.put('delivery_attention', True)
                return
            self.put(reservation, True)
        try:
            result=self.tg.photo(self.chat_id, image, 'П2-23 · '+snapshot['week']+' · Время Ташкента', prior.get('message_id'))
        except TelegramRejected:
            if not prior.get('message_id'):
                self.put(reservation, None)
            raise
        except Exception:
            self.put('delivery_attention', True)
            raise
        self.put(key, {'hash':fingerprint,'message_id':result['message_id'],'file_id':result['photo'][-1]['file_id']})

    def daily_digest(self, now):
        if not 19 <= now.hour < 23:
            return
        tomorrow = now.date() + dt.timedelta(days=1)
        key = 'week:' + (tomorrow - dt.timedelta(days=tomorrow.weekday())).isoformat()
        snapshot = self.get(key)
        if not snapshot or self.get('candidate:' + key):
            return
        lessons = [x for x in snapshot['lessons'] if x['date'] == tomorrow.isoformat()]
        if lessons:
            self.send_once('tomorrow:' + tomorrow.isoformat(), '<b>П2‑23 · Завтра на учёбу</b>\n\n' + render_day(tomorrow.isoformat(), lessons))

    def failed_check(self, exc):
        self.last_error = type(exc).__name__
        self.put('source_error', {'type': self.last_error, 'at': dt.datetime.now(TZ).isoformat()})
        with self.lock:
            keys = [r[0] for r in self.db.execute("SELECT key FROM kv WHERE key LIKE 'candidate:%'")]
        for key in keys:
            self.put(key, None)

    def check(self, snapshots=None, now=None):
        now = now or dt.datetime.now(TZ)
        snapshots = snapshots if snapshots is not None else EduPage().fetch(now.date())
        ready = []
        for snapshot in snapshots:
            key = 'week:' + snapshot['week']
            old = self.get(key)
            lessons = snapshot['lessons']
            fingerprint = digest(lessons)
            changed = old is not None and digest(old['lessons']) != fingerprint
            snapshot = dict(snapshot, revision=(old or {}).get('revision', 0) + int(changed))
            if old and not lessons and old['lessons']:
                raise SourceError('Пустой ответ после заполненной недели: отмена занятий не подтверждена')
            if changed:
                candidate = self.get('candidate:' + key)
                if candidate != fingerprint:
                    self.put('candidate:' + key, fingerprint)
                    continue  # Require the same change in two independent successful checks.
                messages = describe_changes(old['lessons'], lessons, now.date().isoformat())
                outbox = self.get('outbox:' + key, [])
                outbox.extend({'key': f'change:{key}:{snapshot["revision"]}:{fingerprint}:{i}', 'text': message}
                              for i, message in enumerate(messages))
                self.put('outbox:' + key, outbox)
            # Commands must keep working even if Telegram publication fails.
            self.put(key, snapshot)
            self.put('candidate:' + key, None)
            ready.append(snapshot)
        self.put('last_success', now.isoformat())
        self.put('source_error', None)
        self.last_error = ''
        errors = []
        for snapshot in ready:
            key = 'week:' + snapshot['week']
            try:
                if snapshot['lessons']:
                    self.publish_week(snapshot)
                self.publish_image(snapshot)
                for item in self.get('outbox:' + key, []):
                    self.send_once(item['key'], item['text'])
                self.put('outbox:' + key, [])
            except Exception as exc:
                errors.append(exc)
        if errors:
            self.put('delivery_attention', True)
            raise DeliveryError('Publication failed: ' + type(errors[0]).__name__) from None
        self.daily_digest(now)

    def day_messages(self, date):
        monday = date - dt.timedelta(days=date.weekday())
        snapshot = self.get('week:' + monday.isoformat())
        if not snapshot or not snapshot['lessons']:
            return ['Для этой даты подтверждённого расписания пока нет.\n' + SOURCE + '/timetable/']
        items = [x for x in snapshot['lessons'] if x['date'] == date.isoformat()]
        return ['<b>П2‑23</b>\n\n' + render_day(date.isoformat(), items)]

    def handle(self, update):
        message = update.get('message', {})
        destination = int(message.get('chat', {}).get('id', 0))
        private = message.get('chat', {}).get('type') == 'private'
        if not message or (destination != self.chat_id and not private):
            return
        text = message.get('text', '').strip()
        if not text.startswith('/'):
            return
        command = text.split()[0]
        if '@' in command:
            command, address = command.split('@', 1)
            if address.lower() != self.username.lower():
                return
        sender = message.get('from', {}).get('id', 0)
        last = self.get('rate:' + str(sender), 0)
        if time.time() - last < 5:
            return
        self.put('rate:' + str(sender), time.time())
        today = dt.datetime.now(TZ).date()
        if command in ('/today', '/tomorrow'):
            messages = self.day_messages(today + dt.timedelta(days=command == '/tomorrow'))
        elif command in ('/week', '/nextweek'):
            monday = today - dt.timedelta(days=today.weekday()) + dt.timedelta(days=7 if command == '/nextweek' else 0)
            snapshot = self.get('week:' + monday.isoformat())
            messages = render_week(snapshot) if snapshot else ['На эту неделю расписание пока не получено.']
        elif command == '/status':
            success = self.get('last_success')
            checked = dt.datetime.fromisoformat(success).astimezone(TZ).strftime('%d.%m %H:%M') if success else 'ещё не выполнена'
            messages = ['<b>П2‑23 · Статус</b>\nПоследняя успешная проверка: ' + checked +
                        ('\nИсточник временно недоступен. Показываю сохранённые данные.' if self.last_error or self.get('source_error') else '') +
                        ('\nЕсть отправка с неподтверждённой доставкой; требуется проверка администратором.' if self.get('delivery_attention') else '')]
        elif command in ('/start', '/help'):
            messages = ['<b>П2‑23 · Расписание</b>\n\n' + INTRO + '\n\n/today — сегодня\n/tomorrow — завтра\n/week — эта неделя\n/nextweek — следующая неделя\n/status — последняя проверка\n\nИзменения приходят после повторной проверки. Вечером — пары на завтра. Время Ташкента.\n' + SOURCE + '/timetable/']
        else:
            return
        for i, response in enumerate(messages):
            self.send_once('reply:' + str(update['update_id']) + ':' + str(i), response, destination)

    def run(self, interval=300):
        self.username = self.tg.call('getMe')['username']
        webhook = self.tg.call('getWebhookInfo')
        if webhook.get('url'):
            raise RuntimeError('У бота установлен webhook. Не запускайте второй обработчик одновременно.')
        def checker():
            while True:
                try:
                    self.check()
                except Exception as exc:
                    self.failed_check(exc)
                    LOG.warning('Schedule check failed: %s', type(exc).__name__)
                time.sleep(interval)
        threading.Thread(target=checker, daemon=True).start()
        while True:
            try:
                updates = self.tg.call('getUpdates', offset=self.get('offset', 0), timeout=25,
                                       allowed_updates=['message'])
                for update in updates:
                    self.handle(update)
                    self.put('offset', update['update_id'] + 1)
            except Exception as exc:
                LOG.warning('Telegram polling failed: %s', type(exc).__name__)
                time.sleep(10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preview', action='store_true')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.preview:
        for snapshot in EduPage().fetch():
            for text in render_week(snapshot):
                print(text + '\n')
        return
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token and os.environ.get('TELEGRAM_TOKEN_FILE'):
        token = Path(os.environ['TELEGRAM_TOKEN_FILE']).read_text().strip()
    if not token or not os.environ.get('TELEGRAM_CHAT_ID'):
        parser.error('Задайте TELEGRAM_BOT_TOKEN (или TELEGRAM_TOKEN_FILE) и TELEGRAM_CHAT_ID')
    database = os.getenv('DATABASE', '/data/schedule.sqlite3')
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    process_lock = open(database + '.lock', 'a')
    try:
        fcntl.flock(process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error('С этой базой уже работает другой экземпляр бота')
    bot = Bot(Telegram(token), os.environ['TELEGRAM_CHAT_ID'], database,
              os.getenv('OWNER_ID', '0'))
    if args.once:
        bot.check()
    else:
        bot.run(max(60, int(os.getenv('CHECK_INTERVAL_SECONDS', '300'))))


if __name__ == '__main__':
    main()
