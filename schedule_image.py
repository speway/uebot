"""Mobile-first weekly agenda rendered as a Telegram-safe PNG."""
from collections import defaultdict
from datetime import date, datetime, timedelta
from io import BytesIO
import re

from PIL import Image, ImageDraw, ImageFont


DESIGN_VERSION = 3
WIDTH = 1080
MARGIN = 54

COLORS = {
    'paper': '#F3EFE6',
    'ink': '#173A32',
    'muted': '#61736D',
    'green': '#214C3F',
    'green_soft': '#DDE7DD',
    'orange': '#E56742',
    'orange_soft': '#F7DED2',
    'yellow': '#E9BC4A',
    'blue': '#3D669F',
    'line': '#D9DED7',
    'white': '#FFFFFF',
}


def _plural(number, one, few, many):
    if number % 100 in range(11, 15):
        return many
    if number % 10 == 1:
        return one
    if number % 10 in range(2, 5):
        return few
    return many


def _fonts():
    sizes = (20, 22, 24, 26, 28, 30, 32, 36, 42, 58)
    return ({size: ImageFont.truetype('DejaVuSans.ttf', size) for size in sizes},
            {size: ImageFont.truetype('DejaVuSans-Bold.ttf', size) for size in sizes})


def _wrap(draw, text, font, max_width):
    lines, current = [], ''
    for word in re.sub(r'\s+', ' ', str(text or '')).strip().split(' '):
        proposed = (current + ' ' + word).strip()
        if draw.textlength(proposed, font=font) <= max_width:
            current = proposed
            continue
        if current:
            lines.append(current)
            current = ''
        for char in word:
            if current and draw.textlength(current + char, font=font) > max_width:
                lines.append(current)
                current = ''
            current += char
    if current:
        lines.append(current)
    return lines or ['—']


def _subject_parts(subject):
    subject = re.sub(r'\s+', ' ', subject).strip()
    match = re.search(r'\s*\(([^()]{2,24})\)\s*$', subject)
    if not match:
        return subject, ''
    return subject[:match.start()].strip(), match.group(1).upper()


def _merge_day(lessons):
    merged = []
    for source in sorted(lessons, key=lambda item: (item['start'], item['end'], item['subject'])):
        item = dict(source)
        item['subject'] = re.sub(r'\s+', ' ', item['subject']).strip()
        item['_pairs'] = 1
        if merged:
            previous = merged[-1]
            gap = (datetime.strptime(item['start'], '%H:%M') -
                   datetime.strptime(previous['end'], '%H:%M')).total_seconds()
            if (0 <= gap <= 1200 and
                    all(previous[key] == item[key]
                        for key in ('subject', 'teachers', 'rooms', 'groups'))):
                previous['end'] = item['end']
                previous['_pairs'] += 1
                previous['_break'] = int(gap // 60)
                continue
        merged.append(item)
    return merged


def _block_layout(draw, item, regular, bold):
    title, kind = _subject_parts(item['subject'])
    subject_lines = _wrap(draw, title, bold[32], 670)
    teachers = ' / '.join(item.get('teachers', [])) or 'Преподаватель не указан'
    teacher_lines = _wrap(draw, teachers, regular[24], 670)
    groups = ', '.join(item.get('groups', []))
    group_lines = _wrap(draw, groups, regular[22], 670) if groups else []
    content_height = len(subject_lines) * 42 + len(teacher_lines) * 31 + len(group_lines) * 29
    if kind:
        content_height += 31
    height = max(158, 40 + content_height)
    return {
        'item': item,
        'title': subject_lines,
        'kind': kind,
        'teachers': teacher_lines,
        'groups': group_lines,
        'height': height,
    }


def _pill(draw, box, text, font, fill, color):
    draw.rounded_rectangle(box, radius=(box[3] - box[1]) // 2, fill=fill)
    left, top, right, bottom = box
    bounds = draw.textbbox((0, 0), text, font=font)
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    draw.text((left + (right-left-width)/2, top + (bottom-top-height)/2 - bounds[1]),
              text, font=font, fill=color)


def render_image(snapshot):
    regular, bold = _fonts()
    probe = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    monday = date.fromisoformat(snapshot['week'])
    lessons = snapshot.get('lessons', [])
    by_day = defaultdict(list)
    for item in lessons:
        by_day[item['date']].append(item)

    days = [monday + timedelta(days=index) for index in range(6)]
    if by_day.get((monday + timedelta(days=6)).isoformat()):
        days.append(monday + timedelta(days=6))

    rows = []
    for day in days:
        blocks = [_block_layout(probe, item, regular, bold)
                  for item in _merge_day(by_day[day.isoformat()])]
        row_height = 76 + (sum(block['height'] for block in blocks) if blocks else 92)
        rows.append((day, blocks, row_height))

    header_height = 330
    unpublished_height = 400
    gaps_height = 22 * (len(rows) - 1)
    footer_height = 132
    body_height = unpublished_height if not lessons else sum(row[2] for row in rows) + gaps_height
    height = header_height + body_height + footer_height + MARGIN
    if WIDTH + height > 9900 or max(WIDTH / height, height / WIDTH) > 20:
        raise ValueError('Timetable exceeds Telegram photo dimensions')

    image = Image.new('RGB', (WIDTH, height), COLORS['paper'])
    draw = ImageDraw.Draw(image)

    # Header: restrained Bauhaus geometry, useful information stays dominant.
    draw.rectangle((0, 0, WIDTH, 252), fill=COLORS['green'])
    draw.ellipse((846, -92, 1108, 170), fill=COLORS['yellow'])
    draw.rectangle((938, 150, 1080, 252), fill=COLORS['orange'])
    draw.text((MARGIN, 38), 'УЕБОТ  /  П2—23', font=bold[24], fill='#DCE8E1')
    draw.text((MARGIN, 85), 'РАСПИСАНИЕ', font=bold[58], fill=COLORS['white'])
    last_day = days[-1]
    draw.text((MARGIN, 166), f'{monday:%d.%m} — {last_day:%d.%m.%Y}',
              font=regular[30], fill='#DCE8E1')

    lesson_count = len(lessons)
    active_days = len({item['date'] for item in lessons})
    pair_word = _plural(lesson_count, 'ПАРА', 'ПАРЫ', 'ПАР')
    day_word = _plural(active_days, 'ДЕНЬ С ПАРАМИ', 'ДНЯ С ПАРАМИ', 'ДНЕЙ С ПАРАМИ')
    if lessons:
        _pill(draw, (MARGIN, 272, 264, 320), f'{lesson_count} {pair_word}', bold[22],
              COLORS['orange_soft'], COLORS['ink'])
        _pill(draw, (282, 272, 574, 320), f'{active_days} {day_word}', bold[20],
              COLORS['green_soft'], COLORS['ink'])
    else:
        _pill(draw, (MARGIN, 272, 466, 320), 'ЕЩЁ НЕ ОПУБЛИКОВАНО', bold[20],
              COLORS['orange_soft'], COLORS['ink'])
    _pill(draw, (720, 272, WIDTH-MARGIN, 320), 'ТАШКЕНТ · UTC+5', bold[20],
          COLORS['white'], COLORS['muted'])

    names = ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']
    accents = [COLORS['orange'], COLORS['blue'], COLORS['yellow'], COLORS['green'],
               COLORS['orange'], COLORS['blue'], COLORS['yellow']]
    y = header_height
    card_right = WIDTH - MARGIN
    if not lessons:
        bottom = y + unpublished_height
        draw.rounded_rectangle((MARGIN, y, card_right, bottom), radius=24, fill=COLORS['white'])
        draw.rounded_rectangle((MARGIN, y, MARGIN + 16, bottom), radius=8, fill=COLORS['orange'])
        draw.text((MARGIN + 42, y + 52), 'РАСПИСАНИЯ ЕЩЁ НЕТ', font=bold[42], fill=COLORS['ink'])
        copy = [
            'Сидите дальше в неведении, ебучие лохи.',
            'EduPage пока не опубликовал занятия П2—23.',
            '',
            'Это не официальная отмена пар.',
            'Как только деканат родит расписание,',
            'я первым испорчу вам настроение.',
        ]
        copy_y = y + 130
        for line in copy:
            draw.text((MARGIN + 42, copy_y), line, font=bold[28] if line.startswith('Это') else regular[28],
                      fill=COLORS['ink'] if line else COLORS['muted'])
            copy_y += 42
        y = bottom + 22
    for day, blocks, row_height in (rows if lessons else []):
        bottom = y + row_height
        draw.rounded_rectangle((MARGIN, y, card_right, bottom), radius=24, fill=COLORS['white'])
        accent = accents[day.weekday()]
        draw.rounded_rectangle((MARGIN, y, MARGIN + 16, bottom), radius=8, fill=accent)
        draw.text((MARGIN + 38, y + 23), names[day.weekday()], font=bold[28], fill=COLORS['ink'])
        draw.text((card_right - 28, y + 25), day.strftime('%d.%m'), font=bold[26],
                  fill=COLORS['muted'], anchor='ra')
        draw.line((MARGIN + 38, y + 75, card_right - 28, y + 75), fill=COLORS['line'], width=2)
        content_y = y + 76

        if not blocks:
            draw.text((MARGIN + 40, content_y + 25), 'ПАР НЕТ', font=bold[30], fill=COLORS['muted'])
            draw.text((MARGIN + 230, content_y + 28), 'можно бездельничать официально',
                      font=regular[24], fill=COLORS['muted'])
        for index, block in enumerate(blocks):
            item = block['item']
            block_bottom = content_y + block['height']
            if index:
                draw.line((MARGIN + 38, content_y, card_right - 28, content_y),
                          fill=COLORS['line'], width=2)
            time_text = f"{item['start']}–{item['end']}"
            draw.text((MARGIN + 40, content_y + 28), time_text, font=bold[28], fill=COLORS['ink'])
            if item['_pairs'] > 1:
                label = f"{item['_pairs']} ПАРЫ" if item['_pairs'] in (2, 3, 4) else f"{item['_pairs']} ПАР"
                _pill(draw, (MARGIN + 40, content_y + 75, MARGIN + 177, content_y + 113),
                      label, bold[20], COLORS['orange_soft'], COLORS['ink'])
            text_x = MARGIN + 235
            text_y = content_y + 23
            if block['kind']:
                draw.text((text_x, text_y), block['kind'], font=bold[20], fill=accent)
                text_y += 31
            for line in block['title']:
                draw.text((text_x, text_y), line, font=bold[32], fill=COLORS['ink'])
                text_y += 42
            text_y += 4
            for line in block['teachers']:
                draw.text((text_x, text_y), line, font=regular[24], fill=COLORS['muted'])
                text_y += 31
            rooms = ', '.join(item.get('rooms', [])) or 'не указана'
            draw.text((card_right - 28, block_bottom - 38), 'АУД. ' + rooms,
                      font=bold[24], fill=COLORS['ink'], anchor='ra')
            for line in block['groups']:
                draw.text((text_x, text_y), line, font=regular[22], fill=COLORS['blue'])
                text_y += 29
            content_y = block_bottom
        y = bottom + 22

    footer_y = y + 4
    draw.text((MARGIN, footer_y), 'Источник: msu2006.edupage.org  ·  только П2—23',
              font=regular[22], fill=COLORS['muted'])
    draw.text((MARGIN, footer_y + 38), '/today  сегодня   /tomorrow  завтра   /next  ближайшая пара',
              font=bold[22], fill=COLORS['ink'])
    draw.text((MARGIN, footer_y + 78), 'Сохрани. Утренний ты — бесполезный мудак без памяти.',
              font=regular[22], fill=COLORS['muted'])

    output = BytesIO()
    image.save(output, format='PNG', optimize=True)
    return output.getvalue()
