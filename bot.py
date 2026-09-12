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
INTRO = ('Я читаю EduPage за П2‑23, потому что вы, ебучие гении, сами потеряетесь '
         'между выбором группы и кнопкой «следующая неделя».')
NO_SCHEDULE_ROAST = ('Расписания ещё нет. Так что сидите дальше в неведении, ебучие лохи. '
                     'Как только деканат родит таблицу, я первым испорчу вам настроение.')
ROASTS = {
    'unpublished': (
        NO_SCHEDULE_ROAST,
        'Деканат ещё не опубликовал расписание. Сидите красиво и изображайте людей, у которых есть план.',
        'Будущего в EduPage пока нет. Вы ебучие первопроходцы: идёте в неделю без карты и здравого смысла.',
        'Расписание не родилось. Пока можете тревожиться по свободному графику, талантливые вы мои.',
    ),
    'free': (
        'Пар нет. Наконец задача, с которой вы справились без методички.',
        'Свободный день. Можете профессионально ничего не делать — тут у вас уже приличный стаж.',
        'Сегодня занятий нет. Деканат случайно проявил человечность, не спугните.',
        'Пар нет. Сделайте удивлённое лицо и срочно продолжайте лежать.',
    ),
    'early': (
        'Первая пара ранняя. Ваши утренние лица снова станут аргументом против естественного отбора.',
        'Будильник ставьте сейчас: утром ваш мозг традиционно объявит себя недоступным.',
        'Начало раннее. Солнце ещё сомневается, а вас уже решили наказать.',
        'Придётся встать до того, как личность полностью загрузится. Соболезную всем очевидцам.',
    ),
    'heavy': (
        'День плотный. К вечеру от личности останутся студенческий и слабый пульс.',
        'Пар дохуя. Деканат аккуратно упаковал страдания и даже не приложил инструкцию.',
        'Сегодня учебный марафон. Победителю достанется право уснуть в одежде.',
        'Нагрузка солидная. Самое время выяснить, сколько пар выдерживает один потрёпанный студент.',
    ),
    'window': (
        'В расписании окно: достаточно длинное, чтобы начать курсовую, и достаточно короткое, чтобы снова ничего не сделать.',
        'Есть окно. Не потеряйтесь в нём — пространственное мышление у группы и так под наблюдением.',
        'Между парами дыра. Можно поесть, поныть и героически не успеть обратно.',
        'Окно найдено. Деканат подарил вам время, которое вы всё равно спустите в телефон.',
    ),
    'normal': (
        'Нагрузка терпимая. Но вы всё равно найдёте способ устать так, будто разгружали вагоны.',
        'Обычный учебный день: ничего смертельного, кроме желания туда идти.',
        'Расписание щадящее. Постарайтесь хотя бы не проиграть ему по очкам.',
        'Сегодня без особого пиздеца. Не переживайте, вы ещё можете организовать его самостоятельно.',
    ),
    'current': (
        'Пара уже идёт. Если читаешь это из кровати — эксперимент по последствиям начался успешно.',
        'Сейчас бы слушать преподавателя, но ты консультируешься с ботом. Академический приоритет впечатляет.',
        'Время идёт, пара тоже. Надеюсь, хотя бы один из вас находится в нужной аудитории.',
        'Занятие в процессе. Сделай умное лицо — иногда система принимает и такой отчёт.',
    ),
    'break': (
        'Сейчас перерыв. Главное — не превратить его в самовольный академический отпуск.',
        'До следующей пары есть время. Не проебите его вместе с дорогой до аудитории.',
        'Пауза между страданиями. Пользуйтесь, пока образовательный процесс отвернулся.',
        'Идёт перерыв. Мозг можно перезапустить, если вы вообще взяли его с собой.',
    ),
    'done': (
        'На сегодня всё. Организм можно снять с учебного дежурства.',
        'Пары закончились. Вы великолепно пережили то, куда могли вообще не прийти.',
        'Учебный пиздец на сегодня закрыт. Касса тоже, жалобы завтра.',
        'Свобода до следующей пары. Используйте её бездарно, как умеете.',
    ),
    'rooms': (
        'Маршрут построен. Заблудиться теперь можно только из принципа.',
        'Аудитории выписал. Осталось совершить невозможное — реально до них дойти.',
        'Координаты есть. Если придёте не туда, валить на интерфейс уже поздно.',
        'Все кабинеты перед глазами. Пространственный долбоебизм теперь не алиби.',
    ),
    'healthy': (
        'Жив, работаю, ничего не проебал. В этой группе хотя бы кто-то.',
        'Все системы в норме. Можете продолжать ломаться самостоятельно.',
        'Источник читается, доставка работает. Непривычно, но не пугайтесь.',
        'Бот здоров. Осталось провести такую же диагностику вашей дисциплины.',
    ),
    'source_error': (
        'Показываю сохранённое. Если попрётесь вслепую — это уже ваш личный долбоебизм.',
        'EduPage временно лежит. Я сохранил последнее расписание, потому что кто-то здесь предусмотрительный.',
        'Источник не отвечает. Старые данные целы; паниковать разрешается строго по очереди.',
        'EduPage ушёл подумать о своём поведении. Пока живём по последней подтверждённой версии.',
    ),
    'delivery_error': (
        'Расписание сохранилось, но Telegram устроил «доставил — не доставил». Ебучий квантовый курьер.',
        'Данные на месте, доставка требует проверки. Даже сообщения иногда боятся идти в эту группу.',
        'Источник отработал, Telegram споткнулся. Технологии тоже иногда ведут себя как студенты.',
        'Расписание сохранено, а доставка обосралась. Администратору оставлен диагноз без латыни.',
    ),
}
BOT_CONFIG_VERSION = 5
BOT_COMMANDS = [
    {'command': 'today', 'description': 'Какой сегодня учебный пиздец'},
    {'command': 'tomorrow', 'description': 'Чем испортят завтрашний день'},
    {'command': 'next', 'description': 'Куда тащиться следующим'},
    {'command': 'ask', 'description': 'Спроси что угодно, если думать лень'},
    {'command': 'when', 'description': 'Сколько до начала или конца пары'},
    {'command': 'free', 'description': 'Когда ближайший свободный день'},
    {'command': 'rooms', 'description': 'Аудитории на сегодня без квеста'},
    {'command': 'week', 'description': 'Вся неделя одним страданием'},
    {'command': 'nextweek', 'description': 'Будущее, если его опубликовали'},
    {'command': 'roast', 'description': 'Вердикт по сегодняшнему пиздецу'},
    {'command': 'status', 'description': 'Кто опять обосрался'},
    {'command': 'help', 'description': 'Инструкция для самых потерянных'},
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


def situational_roast(kind, seed=''):
    """Choose varied but stable copy, so rerenders never create random edits."""
    options = ROASTS[kind]
    fingerprint = hashlib.sha256(f'{kind}:{seed}'.encode()).digest()
    return options[int.from_bytes(fingerprint[:4], 'big') % len(options)]


def minutes_text(minutes):
    minutes = max(0, int(minutes))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f'{hours} ч {minutes} мин'
    if hours:
        return f'{hours} ч'
    return f'{minutes} мин'


def day_metrics(lessons):
    """Return exact day boundaries and real long windows, excluding normal breaks."""
    intervals = []
    for item in sorted(lessons, key=lambda value: (value['start'], value['end'])):
        start = int(item['start'][:2]) * 60 + int(item['start'][3:])
        end = int(item['end'][:2]) * 60 + int(item['end'][3:])
        if end > start:
            if intervals and start <= intervals[-1][1]:
                intervals[-1][1] = max(intervals[-1][1], end)
            else:
                intervals.append([start, end])
    if not intervals:
        return {'first': '', 'last': '', 'windows': [], 'largest_window': 0, 'window_total': 0}
    gaps = [current[0] - previous[1] for previous, current in zip(intervals, intervals[1:])]
    windows = [gap for gap in gaps if gap >= 30]
    return {
        'first': f'{intervals[0][0] // 60:02d}:{intervals[0][0] % 60:02d}',
        'last': f'{intervals[-1][1] // 60:02d}:{intervals[-1][1] % 60:02d}',
        'windows': windows,
        'largest_window': max(windows, default=0),
        'window_total': sum(windows),
    }


def day_roast_kind(lessons):
    metrics = day_metrics(lessons)
    if len(lessons) >= 4:
        return 'heavy'
    if metrics['windows']:
        return 'window'
    if metrics['first'] and metrics['first'] < '10:00':
        return 'early'
    return 'normal'


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
    if not snapshot.get('lessons'):
        return (f'<b>П2‑23 · {start:%d.%m}–{end:%d.%m.%Y}</b>\n'
                f'Расписание ещё не опубликовано.{checked_line}\n\n'
                f'<i>{NO_SCHEDULE_ROAST} Это не отмена пар, не обольщайтесь.</i>')
    return (f'<b>П2‑23 · {start:%d.%m}–{end:%d.%m.%Y}</b>\n'
            f'{lesson_count} {pair_word} · {day_count} {day_word}{room_line}{checked_line}\n\n'
            '<i>Сохрани. Утренний ты — бесполезный мудак, на его память надежды нет.</i>')


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
        required_weeks = (monday, monday + dt.timedelta(days=7))
        published = {}
        for row in meta['regular']['timetables']:
            if row.get('hidden') or not row.get('datefrom'):
                continue
            start = dt.date.fromisoformat(row['datefrom'])
            if monday <= start <= monday + dt.timedelta(days=20):
                # A republished week can have a new number. Compare by date, not number.
                published[start] = row
        if not published:
            # A completely empty window can also mean EduPage changed its API.
            # Keep confirmed data until at least one nearby publication proves
            # that this metadata response is the real timetable index.
            raise SourceError('Для ближайших недель пока нет подтверждённых публикаций')
        snapshots = []
        for start, row in sorted(published.items()):
            raw = self.rpc('regulartt', 'regularttGetData', [str(row['tt_num'])])
            snapshots.append(parse_week(raw, row))
        present = {dt.date.fromisoformat(snapshot['week']) for snapshot in snapshots}
        for start in required_weeks:
            if start not in present:
                # A successful metadata read with no row for this date is an
                # authoritative "not published yet", not a transport failure.
                end = start + dt.timedelta(days=5)
                snapshots.append({
                    'week': start.isoformat(),
                    'source_title': f'{start:%d.%m}–{end:%d.%m.%Y} · не опубликовано',
                    'class_name': GROUP,
                    'lessons': [],
                })
        return sorted(snapshots, key=lambda snapshot: snapshot['week'])


def parse_week(raw, meta):
    try:
        tables = {table['id']: {str(row['id']): row for row in table['data_rows']}
                  for table in raw['dbiAccessorRes']['tables']}
        for required in ('classes', 'lessons', 'cards', 'periods', 'subjects', 'teachers',
                         'classrooms', 'weeks'):
            if required not in tables:
                raise SourceError('Отсутствует таблица ' + required)
        matches = [row for row in tables['classes'].values() if normalized(row.get('short')) == GROUP]
        if len(matches) != 1:
            raise SourceError('Группа П2-23 не определена однозначно')
        class_id = str(matches[0]['id'])
        start = dt.date.fromisoformat(meta['datefrom'])
        monday = start - dt.timedelta(days=start.weekday())
        title = normalized(meta.get('text', ''))
        week_matches = []
        for week in tables['weeks'].values():
            labels = {normalized(week.get(field)) for field in ('name', 'short')}
            if any(label and label in title for label in labels):
                week_matches.append(int(week['id']))
        if len(week_matches) != 1:
            raise SourceError('Неделя публикации не определена однозначно')
        week_index = week_matches[0]
        if week_index < 0:
            raise SourceError('Изменился формат недель расписания')
        cards = []
        for card in tables['cards'].values():
            lesson = tables['lessons'][str(card['lessonid'])]
            if class_id not in list(map(str, lesson.get('classids', []))):
                continue
            if not card.get('days') and not card.get('period'):
                continue  # Unplaced lessons are not scheduled classes.
            if not card.get('days') or not card.get('period') or not card.get('weeks'):
                raise SourceError('Неполные данные занятия')
            week_mask = card['weeks']
            if set(week_mask) - {'0', '1'} or week_index >= len(week_mask):
                raise SourceError('Изменился формат недель расписания')
            if week_mask[week_index] != '1':
                continue
            cards.append((card, lesson))
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
        metrics = day_metrics(lessons)
        lines[0] += f"\n{count} {pair_word} · {metrics['first']}–{metrics['last']}"
        if metrics['windows']:
            window_count = len(metrics['windows'])
            if window_count == 1:
                lines[0] += f" · окно {minutes_text(metrics['largest_window'])}"
            else:
                word = plural_ru(window_count, 'окно', 'окна', 'окон')
                lines[0] += f" · {window_count} {word}, всего {minutes_text(metrics['window_total'])}"
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
        lines.append('Пар нет. Можете бездельничать официально, будто раньше вам требовалось разрешение.')
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
    """Useful timing first, situational roast second."""
    if not lessons:
        return situational_roast('free', 'day:' + date.isoformat())
    lessons = sorted(lessons, key=lambda item: (item['start'], item['end'], item['subject']))
    metrics = day_metrics(lessons)
    start = dt.datetime.combine(date, dt.time.fromisoformat(metrics['first']), TZ)
    end = dt.datetime.combine(date, dt.time.fromisoformat(metrics['last']), TZ)
    seed = f'day:{date.isoformat()}:{len(lessons)}:{metrics["largest_window"]}'
    day_kind = day_roast_kind(lessons)
    if date > now.date():
        return f"Первая пара в {lessons[0]['start']}. {situational_roast(day_kind, seed)}"
    if date < now.date() or now >= end:
        return situational_roast('done', seed)
    if now < start:
        return f'До первой пары {short_wait(start - now)}. {situational_roast(day_kind, seed)}'
    for item in lessons:
        item_start = dt.datetime.combine(date, dt.time.fromisoformat(item['start']), TZ)
        item_end = dt.datetime.combine(date, dt.time.fromisoformat(item['end']), TZ)
        if item_start <= now < item_end:
            return (f"Сейчас идёт «{clean_spaces(item['subject'])}» — до {item['end']}. "
                    + situational_roast('current', seed + ':' + item['start']))
        if now < item_start:
            return (f"Следующая в {item['start']} — через {short_wait(item_start - now)}. "
                    + situational_roast('break', seed + ':' + item['start']))
    return situational_roast('done', seed)


def render_day_reply(date, lessons, now):
    delta = (date - now.date()).days
    label = 'Сегодня' if delta == 0 else 'Завтра' if delta == 1 else DAYS[date.weekday()]
    return (f'<b>П2‑23 · {label}</b>\n\n{render_day(date.isoformat(), lessons)}\n\n'
            f'<i>{html.escape(day_tease(date, lessons, now))}</i>')


def unpublished_reply(title, seed):
    return (f'<b>{html.escape(title)}</b>\n\nПодтверждённого расписания пока нет.\n\n'
            f'<i>{html.escape(situational_roast("unpublished", seed))}</i>\n\n'
            'Это не официальная отмена пар — не начинайте радоваться без бумажки.\n'
            f'<a href="{SOURCE}/timetable/">Проверить источник</a>')


def render_week(snapshot):
    start = dt.date.fromisoformat(snapshot['week'])
    end = week_end(snapshot)
    lesson_count, day_count, _ = week_stats(snapshot)
    heading = f'<b>П2‑23 · Расписание</b>\n{start:%d.%m}–{end:%d.%m.%Y}'
    if snapshot['lessons']:
        heading += (f"\n{lesson_count} {plural_ru(lesson_count, 'пара', 'пары', 'пар')} · "
                    f"{day_count} {plural_ru(day_count, 'учебный день', 'учебных дня', 'учебных дней')}")
    else:
        heading += '\nСтатус: ещё не опубликовано'
    result = [heading]
    groups = collections.defaultdict(list)
    for item in snapshot['lessons']:
        groups[item['date']].append(item)
    if not groups:
        result.append(NO_SCHEDULE_ROAST + ' Это не подтверждение отмены пар — не радуйтесь раньше времени.')
    for date, lessons in sorted(groups.items()):
        result.append(render_day(date, lessons))
    result.append('<a href="' + SOURCE + '/timetable/">Источник · EduPage</a> · Время Ташкента\n'
                  '<i>Можешь не запоминать. Я уже сделал за тебя и эту жалкую часть взрослой жизни.</i>')
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
    if old_days and not new_days:
        return [('<b>П2‑23 · Расписание пока снято</b>\n\n'
                 'EduPage больше не подтверждает ни одной будущей пары из прошлой версии. '
                 'Старую карточку заменил на «ещё не опубликовано». Это не официальная отмена занятий.\n\n'
                 f'<i>{NO_SCHEDULE_ROAST}</i>\n\n'
                 f'<a href="{SOURCE}/timetable/">Проверить источник</a>')]
    if not old_days and new_days:
        count = sum(map(len, new_days.values()))
        return [(f'<b>П2‑23 · Расписание наконец опубликовано</b>\n\n'
                 f"В EduPage появилось {count} {plural_ru(count, 'пара', 'пары', 'пар')}. "
                 'Основную карточку уже обновил — можно начинать торг с будильником.\n\n'
                 '<i>Деканат наконец высрал расписание. Сериал закрыли, страдания оставили.</i>\n\n'
                 f'<a href="{SOURCE}/timetable/">Проверить источник</a>')]
    sections = ['<b>П2‑23 · Изменения в расписании</b>\n'
                'EduPage снова переобулся быстрее, чем вы успели запомнить хоть какую-то хуйню.']
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
            sections.append('<b>Убрали</b> — выдыхайте осторожно, от счастья тоже можно обосраться:\n' +
                            lesson_text(a[key]))
        for key in sorted(added):
            sections.append('<b>Добавили</b> — расслабились, ебать вас, преждевременно:\n' +
                            lesson_text(b[key]))
    if not changes:
        return []
    sections.append('<a href="' + SOURCE + '/timetable/">Проверить источник</a> · Время Ташкента\n'
                    '<i>Перепроверьте, чтобы потом не стоять у пустой аудитории толпой долбоёбов.</i>')
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

    def send_once(self, key, text, chat_id=None, **send_options):
        prior = self.get('sent:' + key)
        if prior:
            if prior.get('status') == 'pending':
                self.put('delivery_attention', True)
            return prior.get('message_id')
        # Reserve before sending. An interrupted send stays pending, never silently duplicated.
        self.put('sent:' + key, {'status': 'pending'})
        try:
            quiet = dt.datetime.now(TZ).hour < 7 or dt.datetime.now(TZ).hour >= 23
            result = self.tg.send(self.chat_id if chat_id is None else chat_id, text,
                                  disable_notification=quiet, **send_options)
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
            'Расписание П2‑23 без ебучего квеста по EduPage: пары, аудитории, окна, свободные дни, '
            'обратный отсчёт, изменения и AI-ответы. Подкалывает, потому что кто-то же должен вас воспитывать.'))
        self.tg.call('setMyShortDescription', short_description=(
            'Пары и AI для П2‑23. Ищу всё, кроме оправданий вашему опозданию.'))
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
                             text='П2‑23 · Этот кусок устарел. Смотрите основное сообщение, потерянные вы люди.')
        self.put(key, stored)

    def publish_image(self, snapshot):
        if not hasattr(self.tg, 'photo'):
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
            return [unpublished_reply('П2‑23 · ' + label.capitalize(), 'day:' + date.isoformat())]
        items = [x for x in snapshot['lessons'] if x['date'] == date.isoformat()]
        return [render_day_reply(date, items, now)]

    def when_message(self, now=None):
        now = now or dt.datetime.now(TZ)
        date = now.date()
        monday = date - dt.timedelta(days=date.weekday())
        snapshot = self.get('week:' + monday.isoformat())
        if not snapshot or not snapshot.get('lessons'):
            return unpublished_reply('П2‑23 · Когда уже?', 'when:' + date.isoformat())
        lessons = sorted((item for item in snapshot['lessons'] if item['date'] == date.isoformat()),
                         key=lambda item: (item['start'], item['end'], item['subject']))
        if not lessons:
            return (f'<b>П2‑23 · Когда уже?</b>\n\nСегодня пар нет. Таймер страданий не запущен.\n\n'
                    f'<i>{html.escape(situational_roast("free", "when:" + date.isoformat()))}</i>')
        seed = 'when:' + date.isoformat()
        for item in lessons:
            start = dt.datetime.combine(date, dt.time.fromisoformat(item['start']), TZ)
            end = dt.datetime.combine(date, dt.time.fromisoformat(item['end']), TZ)
            if start <= now < end:
                duration = max(1, int((end - start).total_seconds()))
                progress = min(99, int((now - start).total_seconds() * 100 / duration))
                return (f'<b>П2‑23 · Пара уже идёт</b>\n\n{lesson_text(item)}\n\n'
                        f'До конца: <b>{short_wait(end - now)}</b> · пройдено {progress}%\n\n'
                        f'<i>{html.escape(situational_roast("current", seed + ":" + item["start"]))}</i>')
            if now < start:
                had_lesson = any(
                    dt.datetime.combine(date, dt.time.fromisoformat(previous['end']), TZ) <= now
                    for previous in lessons
                )
                heading = 'Перерыв' if had_lesson else 'До первой пары'
                roast_kind = 'break' if had_lesson else day_roast_kind(lessons)
                return (f'<b>П2‑23 · {heading}</b>\n\nНачало через <b>{short_wait(start - now)}</b> · '
                        f'в {item["start"]}\n\n{lesson_text(item)}\n\n'
                        f'<i>{html.escape(situational_roast(roast_kind, seed + ":" + item["start"]))}</i>')
        metrics = day_metrics(lessons)
        return (f'<b>П2‑23 · На сегодня отстрелялись</b>\n\nПоследняя пара закончилась в '
                f'<b>{metrics["last"]}</b>.\n\n'
                f'<i>{html.escape(situational_roast("done", seed))}</i>')

    def free_message(self, now=None):
        now = now or dt.datetime.now(TZ)
        for offset in range(14):
            date = now.date() + dt.timedelta(days=offset)
            if date.weekday() == 6:
                continue  # Sunday is not presented as a special timetable discovery.
            monday = date - dt.timedelta(days=date.weekday())
            snapshot = self.get('week:' + monday.isoformat())
            if not snapshot or not snapshot.get('lessons'):
                continue  # An unpublished week is unknown, not six free days.
            if any(item['date'] == date.isoformat() for item in snapshot['lessons']):
                continue
            if offset == 0:
                when = 'Сегодня'
            elif offset == 1:
                when = 'Завтра'
            else:
                when = DAYS[date.weekday()]
            distance = '' if offset < 2 else f' · через {offset} {plural_ru(offset, "день", "дня", "дней")}'
            return (f'<b>П2‑23 · Ближайший свободный день</b>\n\n'
                    f'<b>{when}, {date:%d.%m}</b>{distance}\nПодтверждённых пар нет.\n\n'
                    f'<i>{html.escape(situational_roast("free", "free:" + date.isoformat()))}</i>')
        return unpublished_reply('П2‑23 · Ближайший свободный день', 'free:' + now.date().isoformat())

    def rooms_message(self, now=None):
        now = now or dt.datetime.now(TZ)
        date = now.date()
        monday = date - dt.timedelta(days=date.weekday())
        snapshot = self.get('week:' + monday.isoformat())
        if not snapshot or not snapshot.get('lessons'):
            return unpublished_reply('П2‑23 · Аудитории сегодня', 'rooms:' + date.isoformat())
        lessons = [item for item in snapshot['lessons'] if item['date'] == date.isoformat()]
        if not lessons:
            return (f'<b>П2‑23 · Аудитории сегодня</b>\n\nСегодня никуда тащиться не надо: пар нет.\n\n'
                    f'<i>{html.escape(situational_roast("free", "rooms:" + date.isoformat()))}</i>')
        sections = [f'<b>П2‑23 · Аудитории сегодня</b>\n{date:%d.%m.%Y}']
        for item in merge_adjacent_lessons(lessons):
            rooms = ', '.join(item.get('rooms', [])) or 'не указана'
            sections.append(f'<b>{html.escape(item["start"])}–{html.escape(item["end"])}</b> · '
                            f'ауд. {html.escape(rooms)}\n{html.escape(clean_spaces(item["subject"]))}')
        sections.append('<i>' + html.escape(situational_roast('rooms', 'rooms:' + date.isoformat())) + '</i>')
        return '\n\n'.join(sections)

    def roast_message(self, now=None):
        now = now or dt.datetime.now(TZ)
        date = now.date()
        monday = date - dt.timedelta(days=date.weekday())
        snapshot = self.get('week:' + monday.isoformat())
        if not snapshot or not snapshot.get('lessons'):
            return unpublished_reply('П2‑23 · Академический диагноз', 'roast:' + date.isoformat())
        lessons = [item for item in snapshot['lessons'] if item['date'] == date.isoformat()]
        if not lessons:
            facts = 'Сегодня 0 пар. Медицинское чудо: расписание вам не навредило.'
            kind = 'free'
        else:
            metrics = day_metrics(lessons)
            count = len(lessons)
            facts = (f"Сегодня {count} {plural_ru(count, 'пара', 'пары', 'пар')} · "
                     f"{metrics['first']}–{metrics['last']}.")
            if metrics['windows']:
                facts += f" Самое большое окно — {minutes_text(metrics['largest_window'])}."
            kind = day_roast_kind(lessons)
        verdict = situational_roast(kind, f'roast:{date.isoformat()}:{len(lessons)}')
        return (f'<b>П2‑23 · Академический диагноз</b>\n\n{html.escape(facts)}\n\n'
                f'<b>Вердикт:</b> <i>{html.escape(verdict)}</i>')

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
            return unpublished_reply('П2‑23 · Что дальше?', 'next:' + now.date().isoformat())
        active = []
        for item in future_lessons:
            date = dt.date.fromisoformat(item['date'])
            start = dt.datetime.combine(date, dt.time.fromisoformat(item['start']), TZ)
            end = dt.datetime.combine(date, dt.time.fromisoformat(item['end']), TZ)
            if start <= now < end:
                active.append(item)
        if active:
            # Do not merge a current pair with the next one: the ordinary
            # 15-minute break must not be reported as ongoing class time.
            item = min(active, key=lambda value: (value['date'], value['start'], value['subject']))
        else:
            blocks = merge_adjacent_lessons(future_lessons)
            item = min(blocks, key=lambda value: (value['date'], value['start'], value['subject']))
        date = dt.date.fromisoformat(item['date'])
        start = dt.datetime.combine(date, dt.time.fromisoformat(item['start']), TZ)
        end = dt.datetime.combine(date, dt.time.fromisoformat(item['end']), TZ)
        if start <= now < end:
            heading = 'Сейчас идёт'
            timing = f"До {item['end']} ещё {short_wait(end - now)}"
            tease = situational_roast('current', f'next:{date.isoformat()}:{item["start"]}')
        else:
            heading = 'Следующая пара'
            day_delta = (start.date() - now.date()).days
            when = 'сегодня' if day_delta == 0 else 'завтра' if day_delta == 1 else DAYS[start.weekday()].lower()
            timing = f"{when}, {start:%d.%m} в {item['start']} · через {short_wait(start - now)}"
            same_day = [lesson for lesson in lessons if lesson['date'] == item['date']]
            tease = situational_roast(day_roast_kind(same_day),
                                      f'next:{date.isoformat()}:{item["start"]}')
        pair_line = ''
        if item.get('_pairs', 1) > 1:
            count = item['_pairs']
            pair_line = f"\n{count} {plural_ru(count, 'пара', 'пары', 'пар')} подряд"
            if item.get('_break'):
                pair_line += f" · перерыв {item['_break']} мин"
        return (f'<b>П2‑23 · {heading}</b>\n{html.escape(timing)}\n\n{lesson_text(item)}{pair_line}\n\n'
                f'<i>{html.escape(tease)}</i>')

    def status_message(self, now=None):
        from ai_responder import provider_ready

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
        monday = now.date() - dt.timedelta(days=now.date().weekday())

        def week_status(start):
            snapshot = self.get('week:' + start.isoformat())
            if not snapshot:
                return 'нет подтверждённых данных'
            count = len(snapshot.get('lessons', []))
            if not count:
                return 'ещё не опубликована'
            return f"{count} {plural_ru(count, 'пара', 'пары', 'пар')}"

        lines = [
            '<b>П2‑23 · Статус бота</b>',
            'Последняя проверка: ' + checked_text,
            'Источник: ' + ('временно не отвечает' if source_problem else 'отвечает'),
            'Изменения: ' + ('проверяю повторно' if pending else 'не замечены'),
            'Доставка: ' + ('нужна проверка администратором' if delivery_problem else 'без ошибок'),
            'Эта неделя: ' + week_status(monday),
            'Следующая неделя: ' + week_status(monday + dt.timedelta(days=7)),
            'AI-ответы: ' + ('готовы' if provider_ready() else 'ждут настройки'),
            'Автопроверка: примерно каждые 5 минут',
        ]
        if source_problem:
            roast = situational_roast('source_error', 'status:' + now.date().isoformat())
        elif delivery_problem:
            roast = situational_roast('delivery_error', 'status:' + now.date().isoformat())
        else:
            roast = situational_roast('healthy', 'status:' + now.date().isoformat())
        lines.append('\n<i>' + html.escape(roast) + '</i>')
        return '\n'.join(lines)

    def _items(self, prefix):
        with self.lock:
            rows = self.db.execute('SELECT key, value FROM kv WHERE key LIKE ?', (prefix + '%',)).fetchall()
        return [(key, json.loads(value)) for key, value in rows]

    def handle(self, update, runtime_oidc_token=None):
        from ai_responder import BOT_USERNAME, is_ai_request, reply_to_update

        message = update.get('message', {})
        destination = int(message.get('chat', {}).get('id', 0))
        private = message.get('chat', {}).get('type') == 'private'
        if not message or (destination != self.chat_id and not private):
            return
        text = message.get('text', '').strip()
        username = self.username or BOT_USERNAME
        ai_request = is_ai_request(update, self.chat_id, username)
        if not text.startswith('/') and not ai_request:
            return
        if ai_request:
            reply_key = 'reply:' + str(update.get('update_id', 0)) + ':0'
            if self.get('sent:' + reply_key):
                return
            now = dt.datetime.now(TZ)
            monday = now.date() - dt.timedelta(days=now.date().weekday())
            state = {'schema': 1, 'kv': {
                'last_success': self.get('last_success'),
                'source_error': self.get('source_error'),
            }}
            for offset in (0, 7):
                start = monday + dt.timedelta(days=offset)
                snapshot = self.get('week:' + start.isoformat())
                if snapshot is not None:
                    state['kv']['week:' + start.isoformat()] = snapshot
            response = reply_to_update(update, state, self.chat_id, username, now,
                                       runtime_oidc_token)
            if response:
                reply_parameters = None
                if message.get('message_id'):
                    reply_parameters = {
                        'message_id': message['message_id'],
                        'allow_sending_without_reply': True,
                    }
                options = {'reply_parameters': reply_parameters} if reply_parameters else {}
                self.send_once(reply_key, response, destination, **options)
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
        elif command == '/when':
            messages = [self.when_message(now)]
        elif command == '/free':
            messages = [self.free_message(now)]
        elif command == '/rooms':
            messages = [self.rooms_message(now)]
        elif command == '/roast':
            messages = [self.roast_message(now)]
        elif command in ('/week', '/nextweek'):
            monday = today - dt.timedelta(days=today.weekday()) + dt.timedelta(days=7 if command == '/nextweek' else 0)
            snapshot = self.get('week:' + monday.isoformat())
            messages = render_week(snapshot) if snapshot else [NO_SCHEDULE_ROAST]
        elif command == '/status':
            messages = [self.status_message(now)]
        elif command in ('/start', '/help'):
            messages = ['<b>П2‑23 · Уебот</b>\n\n' + INTRO +
                        '\n\n/today — какой сегодня учебный пиздец\n'
                        '/tomorrow — чем испортят завтрашний день\n'
                        '/next — куда тащиться следующим\n'
                        '/ask &lt;вопрос&gt; — спросить AI о чём угодно\n'
                        '/when — сколько до начала или конца текущей пары\n'
                        '/free — ближайший свободный учебный день\n'
                        '/rooms — аудитории на сегодня без квеста\n'
                        '/roast — диагноз сегодняшней учебной нагрузке\n'
                        '/week — вся неделя одним страданием\n'
                        '/nextweek — будущее, если деканат его высрал\n'
                        '/status — кто опять обосрался\n\n'
                        'В группе AI отвечает только на /ask, прямое @упоминание или ответ на моё сообщение — '
                        'в чужой трёп без приглашения не лезу.\n\n'
                        'Изменение публикую только после повторной проверки, чтобы одна галлюцинация '
                        'EduPage не погнала всю толпу долбоёбов в пустую аудиторию. Вечером напоминаю пары на завтра. '
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
