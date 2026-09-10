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
INTRO = ('Я читаю EduPage за П2‑23, потому что самостоятельно открыть расписание — '
         'видимо, отдельная дисциплина по выбору.')
BOT_CONFIG_VERSION = 2
BOT_COMMANDS = [
    {'command': 'today', 'description': 'Что терпим сегодня'},
    {'command': 'tomorrow', 'description': 'К чему готовиться завтра'},
    {'command': 'next', 'description': 'Ближайшая пара и сколько до неё'},
    {'command': 'week', 'description': 'Эта неделя картинкой'},
    {'command': 'nextweek', 'description': 'Следующая неделя картинкой'},
    {'command': 'status', 'description': 'Свежесть данных и здоровье бота'},
    {'command': 'help', 'description': 'Что вообще умеет этот трудяга'},
]
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


def plural_ru(number, one, few, many):
    """Return a Russian noun form without pulling morphology into a tiny bot."""
    number = abs(int(number))
    if number % 100 in range(11, 15):
        return many
    if number % 10 == 1:
        return one
    if number % 10 in range(2, 5):
        return few
    return many


def clean_spaces(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def merge_adjacent_lessons(lessons):
    """Merge consecutive identical classes for display, never for comparison."""
    merged = []
    for source in sorted(lessons, key=lambda x: (x['date'], x['start'], x['end'], x['subject'])):
        item = dict(source)
        item['subject'] = clean_spaces(item['subject'])
        item['_pairs'] = 1
        if merged:
            previous = merged[-1]
            gap = (dt.datetime.strptime(item['start'], '%H:%M') -
                   dt.datetime.strptime(previous['end'], '%H:%M')).total_seconds()
            same = (previous['date'] == item['date'] and
                    all(previous[k] == item[k] for k in ('subject', 'teachers', 'rooms', 'groups')))
            if same and 0 <= gap <= 1200:
                previous['end'] = item['end']
                previous['_pairs'] += 1
                previous['_break'] = int(gap // 60)
                continue
        merged.append(item)
    return merged


def week_stats(snapshot):
    lessons = snapshot.get('lessons', [])
    days = len({item['date'] for item in lessons})
    rooms = sorted({room for item in lessons for room in item.get('rooms', [])})
    return len(lessons), days, rooms


def week_end(snapshot):
    start = dt.date.fromisoformat(snapshot['week'])
    has_sunday = any(item.get('date') == (start + dt.timedelta(days=6)).isoformat()
                     for item in snapshot.get('lessons', []))
    return start + dt.timedelta(days=6 if has_sunday else 5)


def week_caption(snapshot, checked_at=None):
    start = dt.date.fromisoformat(snapshot['week'])
    end = week_end(snapshot)
    lesson_count, day_count, rooms = week_stats(snapshot)
    pair_word = plural_ru(lesson_count, 'пара', 'пары', 'пар')
    day_word = plural_ru(day_count, 'учебный день', 'учебных дня', 'учебных дней')
    room_line = ''
    if rooms:
        room_line = '\nАудитории: ' + html.escape(', '.join(rooms))
    checked_line = ''
    if checked_at:
        try:
            checked = dt.datetime.fromisoformat(checked_at).astimezone(TZ)
            checked_line = f'\nПроверено: {checked:%d.%m в %H:%M}'
        except (TypeError, ValueError):
            pass
    return (f'<b>П2‑23 · {start:%d.%m}–{end:%d.%m.%Y}</b>\n'
            f'{lesson_count} {pair_word} · {day_count} {day_word}{room_line}{checked_line}\n\n'
            '<i>Сохрани. Память перед первой парой — источник менее надёжный.</i>')


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
        options = {'context': self._context}
        # Python 3.13 removed HTTPSHandler._check_hostname.
        if hasattr(self, '_check_hostname'):
            options['check_hostname'] = self._check_hostname
        return self.do_open(IPv4HTTPSConnection, request, **options)


class EduPage:
    def __init__(self, timeout=25, attempts=3):
        # EduPage advertises IPv6, while hosted runners currently have no IPv6 route.
        # Keep this transport scoped to EduPage; Telegram and GitHub retain defaults.
        self.http = urllib.request.build_opener(
            IPv4HTTPSHandler(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.gsh = ''
        self.timeout = max(1, int(timeout))
        self.attempts = max(1, int(attempts))

    def read(self, request):
        # These endpoints only read published timetable data, so retries are safe.
        for attempt in range(self.attempts):
            try:
                with self.http.open(request, timeout=self.timeout) as response:
                    return response.read()
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if isinstance(exc, urllib.error.HTTPError) and exc.code < 500 and exc.code != 429:
                    raise SourceError('EduPage HTTP ' + str(exc.code)) from None
                if attempt == self.attempts - 1:
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
    text = f"<b>{esc(item['start'])}–{esc(item['end'])}</b>  {esc(clean_spaces(item['subject']))}"
    details = [' / '.join(item['teachers']) or 'Преподаватель не указан',
               'ауд. ' + ', '.join(item['rooms']) if item['rooms'] else 'Аудитория не указана']
    if item['groups']:
        details.append(', '.join(item['groups']))
    return text + '\n' + esc(' · '.join(details))


def render_day(date, lessons):
    day = dt.date.fromisoformat(date)
    lessons = sorted(lessons, key=lambda x: (x['start'], x['end'], x['subject']))
    lines = [f'<b>{DAYS[day.weekday()]} · {day:%d.%m}</b>']
    if lessons:
        count = len(lessons)
        pair_word = plural_ru(count, 'пара', 'пары', 'пар')
        lines[0] += f"\n{count} {pair_word} · {lessons[0]['start']}–{lessons[-1]['end']}"
    # Merge adjacent pairs only for display; keep individual pairs in the change detector.
    merged = merge_adjacent_lessons(lessons)
    for item in merged:
        block = lesson_text(item)
        if item['_pairs'] > 1:
            pair_word = plural_ru(item['_pairs'], 'пара', 'пары', 'пар')
            block += f"\n{item['_pairs']} {pair_word} подряд"
            if item.get('_break'):
                block += f" · перерыв {item['_break']} мин"
        lines.append(block)
    if not lessons:
        lines.append('Пар нет. Редкая победа календаря над системой образования.')
    return '\n\n'.join(lines)


def short_wait(delta):
    minutes = max(0, int(delta.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f'{hours} ч {minutes} мин'
    if hours:
        return f'{hours} ч'
    return f'{minutes} мин'


def day_tease(date, lessons, now):
    """Useful timing first, friendly roast second."""
    if not lessons:
        return 'Можно продолжить делать вид, что все дедлайны под контролем.'
    lessons = sorted(lessons, key=lambda item: (item['start'], item['end'], item['subject']))
    start = dt.datetime.combine(date, dt.time.fromisoformat(lessons[0]['start']), TZ)
    end = dt.datetime.combine(date, dt.time.fromisoformat(lessons[-1]['end']), TZ)
    if date > now.date():
        return (f"Первая пара в {lessons[0]['start']}. Будильник поставь сейчас: "
                'утренний ты — крайне ненадёжный коллега.')
    if date < now.date() or now >= end:
        return 'На сегодня всё. Академический урон получен, можно восстанавливаться.'
    if now < start:
        return f'До первой пары {short_wait(start - now)}. Времени достаточно даже на отрицание.'
    for item in lessons:
        item_start = dt.datetime.combine(date, dt.time.fromisoformat(item['start']), TZ)
        item_end = dt.datetime.combine(date, dt.time.fromisoformat(item['end']), TZ)
        if item_start <= now < item_end:
            return f"Сейчас идёт «{clean_spaces(item['subject'])}» — до {item['end']}. Держимся научно."
        if now < item_start:
            return f"Следующая в {item['start']} — через {short_wait(item_start - now)}. Не потеряйся по дороге."
    return 'На сегодня всё. Академический урон получен, можно восстанавливаться.'


def render_day_reply(date, lessons, now):
    delta = (date - now.date()).days
    label = 'Сегодня' if delta == 0 else 'Завтра' if delta == 1 else DAYS[date.weekday()]
    return (f'<b>П2‑23 · {label}</b>\n\n{render_day(date.isoformat(), lessons)}\n\n'
            f'<i>{html.escape(day_tease(date, lessons, now))}</i>')


def render_week(snapshot):
    start = dt.date.fromisoformat(snapshot['week'])
    end = week_end(snapshot)
    lesson_count, day_count, _ = week_stats(snapshot)
    result = [f'<b>П2‑23 · Расписание</b>\n{start:%d.%m}–{end:%d.%m.%Y}\n'
              f"{lesson_count} {plural_ru(lesson_count, 'пара', 'пары', 'пар')} · "
              f"{day_count} {plural_ru(day_count, 'учебный день', 'учебных дня', 'учебных дней')}"]
    groups = collections.defaultdict(list)
    for item in snapshot['lessons']:
        groups[item['date']].append(item)
    if not groups:
        result.append('В этой публикации занятия П2‑23 пока не расставлены. Это не подтверждение отмены занятий.')
    for date, lessons in sorted(groups.items()):
        result.append(render_day(date, lessons))
    result.append('<a href="' + SOURCE + '/timetable/">Источник · EduPage</a> · Время Ташкента\n'
                  '<i>Можешь не запоминать. Я уже совершил эту ошибку за тебя.</i>')
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
    sections = ['<b>П2‑23 · Изменения в расписании</b>\n'
                'EduPage снова переобулся быстрее, чем вы успели запомнить аудиторию.']
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
            sections.append('<b>Убрали</b> — можете выдохнуть, но пока осторожно:\n' + lesson_text(a[key]))
        for key in sorted(added):
            sections.append('<b>Добавили</b> — расслабляться было преждевременно:\n' + lesson_text(b[key]))
    if not changes:
        return []
    sections.append('<a href="' + SOURCE + '/timetable/">Проверить источник</a> · Время Ташкента\n'
                    '<i>Перепроверьте, чтобы не проводить полевое исследование «Почему аудитория пустая».</i>')
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
            fields.update(message_id=str(message_id), media=json.dumps({
                'type': 'photo', 'media': 'attach://photo', 'caption': caption, 'parse_mode': 'HTML'
            }, ensure_ascii=False))
        else:
            fields.update(caption=caption, parse_mode='HTML', disable_notification='true')
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

    def configure(self):
        """Keep Telegram's visible command menu and profile copy in sync with the code."""
        if self.get('bot_config_version') == BOT_CONFIG_VERSION:
            return
        self.tg.call('setMyCommands', commands=BOT_COMMANDS)
        self.tg.call('setMyDescription', description=(
            'Расписание П2‑23 без квеста по EduPage: сегодня, завтра, неделя картинкой, '
            'изменения и вечерние напоминания. Иногда подкалывает, зато не опаздывает намеренно.'))
        self.tg.call('setMyShortDescription', short_description=(
            'Расписание П2‑23. Читает EduPage, считает пары, бережёт остатки вашей памяти.'))
        self.put('bot_config_version', BOT_CONFIG_VERSION)

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
            result=self.tg.photo(self.chat_id, image, week_caption(snapshot, self.get('last_success')),
                                 prior.get('message_id'))
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
            self.send_once('tomorrow:' + tomorrow.isoformat(), render_day_reply(tomorrow, lessons, now))

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

    def day_messages(self, date, now=None):
        now = now or dt.datetime.now(TZ)
        monday = date - dt.timedelta(days=date.weekday())
        snapshot = self.get('week:' + monday.isoformat())
        if not snapshot or not snapshot['lessons']:
            label = 'сегодня' if date == now.date() else 'завтра' if date == now.date() + dt.timedelta(days=1) else date.strftime('%d.%m')
            return [f'<b>П2‑23 · {label.capitalize()}</b>\n\nПодтверждённого расписания пока нет. '
                    'EduPage ещё думает — редкий случай, когда вы с ним заняты одним и тем же.\n\n'
                    f'<a href="{SOURCE}/timetable/">Проверить источник</a>']
        items = [x for x in snapshot['lessons'] if x['date'] == date.isoformat()]
        return [render_day_reply(date, items, now)]

    def next_message(self, now=None):
        now = now or dt.datetime.now(TZ)
        with self.lock:
            rows = self.db.execute("SELECT value FROM kv WHERE key LIKE 'week:%'").fetchall()
        lessons = []
        for row in rows:
            snapshot = json.loads(row[0])
            lessons.extend(snapshot.get('lessons', []))
        future_lessons = []
        for item in lessons:
            date = dt.date.fromisoformat(item['date'])
            end = dt.datetime.combine(date, dt.time.fromisoformat(item['end']), TZ)
            if end > now:
                future_lessons.append(item)
        if not future_lessons:
            return ('<b>П2‑23 · Что дальше?</b>\n\nВ опубликованных неделях будущих пар нет. '
                    'Либо свобода, либо EduPage ещё не родил следующую неделю — ставлю на второе.')
        blocks = merge_adjacent_lessons(future_lessons)
        item = min(blocks, key=lambda value: (value['date'], value['start'], value['subject']))
        date = dt.date.fromisoformat(item['date'])
        start = dt.datetime.combine(date, dt.time.fromisoformat(item['start']), TZ)
        end = dt.datetime.combine(date, dt.time.fromisoformat(item['end']), TZ)
        if start <= now < end:
            heading = 'Сейчас идёт'
            timing = f"До {item['end']} ещё {short_wait(end - now)}"
        else:
            heading = 'Следующая пара'
            day_delta = (start.date() - now.date()).days
            when = 'сегодня' if day_delta == 0 else 'завтра' if day_delta == 1 else DAYS[start.weekday()].lower()
            timing = f"{when}, {start:%d.%m} в {item['start']} · через {short_wait(start - now)}"
        pair_line = ''
        if item.get('_pairs', 1) > 1:
            count = item['_pairs']
            pair_line = f"\n{count} {plural_ru(count, 'пара', 'пары', 'пар')} подряд"
            if item.get('_break'):
                pair_line += f" · перерыв {item['_break']} мин"
        return (f'<b>П2‑23 · {heading}</b>\n{html.escape(timing)}\n\n{lesson_text(item)}{pair_line}\n\n'
                '<i>Теперь опоздание хотя бы нельзя списать на нехватку информации.</i>')

    def status_message(self, now=None):
        now = now or dt.datetime.now(TZ)
        success = self.get('last_success')
        checked = None
        if success:
            try:
                checked = dt.datetime.fromisoformat(success).astimezone(TZ)
            except (TypeError, ValueError):
                pass
        if checked:
            age = now - checked
            checked_text = f'{checked:%d.%m в %H:%M} · {short_wait(age)} назад'
        else:
            checked_text = 'ещё не было'
        source_problem = bool(self.last_error or self.get('source_error'))
        delivery_problem = bool(self.get('delivery_attention'))
        pending = any(value for key, value in self._items('candidate:') if value)
        lines = [
            '<b>П2‑23 · Статус бота</b>',
            'Последняя проверка: ' + checked_text,
            'Источник: ' + ('временно не отвечает' if source_problem else 'отвечает'),
            'Изменения: ' + ('проверяю повторно' if pending else 'не замечены'),
            'Доставка: ' + ('нужна проверка администратором' if delivery_problem else 'без ошибок'),
        ]
        if source_problem:
            lines.append('\n<i>Показываю сохранённое расписание. Паниковать можно, но строго по тайм-слоту.</i>')
        elif delivery_problem:
            lines.append('\n<i>Расписание сохранилось, но Telegram сыграл в «доставил — не доставил».</i>')
        else:
            lines.append('\n<i>Жив, работаю, расписание проверяю. Кто-то в этой группе всё-таки стабилен.</i>')
        return '\n'.join(lines)

    def _items(self, prefix):
        with self.lock:
            rows = self.db.execute('SELECT key, value FROM kv WHERE key LIKE ?', (prefix + '%',)).fetchall()
        return [(key, json.loads(value)) for key, value in rows]

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
        now = dt.datetime.now(TZ)
        today = now.date()
        if command in ('/today', '/tomorrow'):
            messages = self.day_messages(today + dt.timedelta(days=command == '/tomorrow'), now)
        elif command == '/next':
            messages = [self.next_message(now)]
        elif command in ('/week', '/nextweek'):
            monday = today - dt.timedelta(days=today.weekday()) + dt.timedelta(days=7 if command == '/nextweek' else 0)
            snapshot = self.get('week:' + monday.isoformat())
            messages = render_week(snapshot) if snapshot else ['На эту неделю расписание пока не получено.']
        elif command == '/status':
            messages = [self.status_message(now)]
        elif command in ('/start', '/help'):
            messages = ['<b>П2‑23 · Уебот</b>\n\n' + INTRO +
                        '\n\n/today — что терпим сегодня\n/tomorrow — к чему готовиться завтра\n'
                        '/next — ближайшая пара и сколько до неё\n/week — неделя картинкой\n'
                        '/nextweek — следующая неделя\n/status — жив ли бот и свежи ли данные\n\n'
                        'Изменение публикую только после повторной проверки, чтобы одна галлюцинация '
                        'EduPage не устроила миграцию всей группы. Вечером напоминаю пары на завтра. '
                        f'Время Ташкента.\n<a href="{SOURCE}/timetable/">Открыть первоисточник</a>']
        else:
            return
        for i, response in enumerate(messages):
            self.send_once('reply:' + str(update['update_id']) + ':' + str(i), response, destination)

    def run(self, interval=300):
        self.username = self.tg.call('getMe')['username']
        self.configure()
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
        bot.configure()
        bot.check()
    else:
        bot.run(max(60, int(os.getenv('CHECK_INTERVAL_SECONDS', '300'))))


if __name__ == '__main__':
    main()
