"""Landscape weekly agenda rendered as a Telegram-safe PNG."""
from collections import defaultdict
from datetime import date, datetime, timedelta
from io import BytesIO
import re

from PIL import Image, ImageDraw, ImageFont


DESIGN_VERSION = 6
WIDTH = 1920
HEIGHT = 1080
MARGIN = 48

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
    sizes = (15, 16, 17, 18, 19, 20, 22, 24, 26, 28, 30, 32, 36, 42, 58, 64)
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


def _ellipsize(draw, text, font, max_width):
    text = re.sub(r'\s+', ' ', str(text or '')).strip()
    if draw.textlength(text, font=font) <= max_width:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if draw.textlength(text[:middle].rstrip() + '…', font=font) <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + '…'


def _limited_lines(draw, text, font, max_width, max_lines):
    lines = _wrap(draw, text, font, max_width)
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines - 1]
    kept.append(_ellipsize(draw, ' '.join(lines[max_lines - 1:]), font, max_width))
    return kept


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

    if WIDTH + HEIGHT > 9900 or max(WIDTH / HEIGHT, HEIGHT / WIDTH) > 20:
        raise ValueError('Timetable exceeds Telegram photo dimensions')

    image = Image.new('RGB', (WIDTH, HEIGHT), COLORS['paper'])
    draw = ImageDraw.Draw(image)

    # Wide Bauhaus header: the useful hierarchy remains louder than decoration.
    draw.rectangle((0, 0, WIDTH, 196), fill=COLORS['green'])
    draw.ellipse((1635, -152, 1975, 188), fill=COLORS['yellow'])
    draw.rectangle((1772, 126, WIDTH, 196), fill=COLORS['orange'])
    draw.text((MARGIN, 25), 'УЕБОТ  /  П2—23', font=bold[22], fill='#DCE8E1')
    draw.text((MARGIN, 58), 'РАСПИСАНИЕ', font=bold[64], fill=COLORS['white'])
    last_day = days[-1]
    draw.text((MARGIN, 139), f'{monday:%d.%m} — {last_day:%d.%m.%Y}',
              font=regular[26], fill='#DCE8E1')

    lesson_count = len(lessons)
    active_days = len({item['date'] for item in lessons})
    pair_word = _plural(lesson_count, 'ПАРА', 'ПАРЫ', 'ПАР')
    day_word = _plural(active_days, 'ДЕНЬ С ПАРАМИ', 'ДНЯ С ПАРАМИ', 'ДНЕЙ С ПАРАМИ')
    if lessons:
        _pill(draw, (1070, 45, 1295, 97), f'{lesson_count} {pair_word}', bold[20],
              COLORS['orange_soft'], COLORS['ink'])
        _pill(draw, (1315, 45, 1660, 97), f'{active_days} {day_word}', bold[18],
              COLORS['green_soft'], COLORS['ink'])
    else:
        _pill(draw, (1070, 45, 1515, 97), 'ЕЩЁ НЕ ОПУБЛИКОВАНО', bold[19],
              COLORS['orange_soft'], COLORS['ink'])
    _pill(draw, (1070, 116, 1440, 166), 'ТАШКЕНТ · UTC+5', bold[18],
          COLORS['white'], COLORS['muted'])

    names = ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']
    accents = [COLORS['orange'], COLORS['blue'], COLORS['yellow'], COLORS['green'],
               COLORS['orange'], COLORS['blue'], COLORS['yellow']]
    if not lessons:
        top, bottom = 230, 905
        card_right = WIDTH - MARGIN
        draw.rounded_rectangle((MARGIN, top, card_right, bottom), radius=30, fill=COLORS['white'])
        draw.rounded_rectangle((MARGIN, top, MARGIN + 18, bottom), radius=9, fill=COLORS['orange'])
        draw.ellipse((1395, 315, 1775, 695), fill=COLORS['green_soft'])
        draw.rectangle((1575, 600, card_right, bottom), fill=COLORS['orange_soft'])
        draw.text((MARGIN + 70, top + 80), 'РАСПИСАНИЯ ЕЩЁ НЕТ', font=bold[58], fill=COLORS['ink'])
        copy = [
            'Сидите дальше в неведении, ебучие лохи.',
            'EduPage пока не опубликовал занятия П2—23.',
            '',
            'Это не официальная отмена пар.',
            'Как только деканат родит расписание,',
            'я первым испорчу вам настроение.',
        ]
        copy_y = top + 185
        for line in copy:
            draw.text((MARGIN + 72, copy_y), line, font=bold[30] if line.startswith('Это') else regular[30],
                      fill=COLORS['ink'] if line else COLORS['muted'])
            copy_y += 46
    else:
        grid_top, grid_bottom, gap = 220, 928, 22
        columns = 4 if len(days) > 6 else 3
        card_width = (WIDTH - 2 * MARGIN - gap * (columns - 1)) // columns
        card_height = (grid_bottom - grid_top - gap) // 2
        for index, day in enumerate(days):
            row, column = divmod(index, columns)
            left = MARGIN + column * (card_width + gap)
            top = grid_top + row * (card_height + gap)
            right, bottom = left + card_width, top + card_height
            accent = accents[day.weekday()]
            draw.rounded_rectangle((left, top, right, bottom), radius=24, fill=COLORS['white'])
            draw.rounded_rectangle((left, top, left + 12, bottom), radius=6, fill=accent)
            draw.text((left + 28, top + 18), names[day.weekday()], font=bold[24], fill=COLORS['ink'])
            draw.text((right - 24, top + 19), day.strftime('%d.%m'), font=bold[22],
                      fill=COLORS['muted'], anchor='ra')
            header_bottom = top + 62
            draw.line((left + 28, header_bottom, right - 22, header_bottom),
                      fill=COLORS['line'], width=2)
            blocks = _merge_day(by_day[day.isoformat()])
            if not blocks:
                draw.text((left + 30, top + 135), 'ПАР НЕТ', font=bold[36], fill=COLORS['muted'])
                draw.text((left + 30, top + 190), 'можно бездельничать официально',
                          font=regular[20], fill=COLORS['muted'])
                continue

            available = bottom - header_bottom - 12
            block_height = available / len(blocks)
            if len(blocks) <= 1:
                subject_size, detail_size, time_size = 28, 19, 22
            elif len(blocks) == 2:
                subject_size, detail_size, time_size = 24, 18, 20
            elif len(blocks) == 3:
                subject_size, detail_size, time_size = 20, 16, 18
            else:
                subject_size, detail_size, time_size = 18, 15, 17
            if card_width < 500:
                subject_size, detail_size, time_size = min(subject_size, 18), 15, 15
            subject_font, detail_font, time_font = bold[subject_size], regular[detail_size], bold[time_size]
            subject_line_height = subject_size + 5
            # Keep a real gutter after the widest HH:MM–HH:MM label.  The old
            # portrait coordinates looked acceptable in tests but overlapped as
            # soon as the cards became columns in the landscape grid.
            time_width = 174 if card_width >= 560 else 116
            text_left = left + 28 + time_width
            text_width = right - 24 - text_left
            for block_index, item in enumerate(blocks):
                block_top = header_bottom + block_index * block_height
                block_bottom = header_bottom + (block_index + 1) * block_height
                if block_index:
                    draw.line((left + 28, round(block_top), right - 22, round(block_top)),
                              fill=COLORS['line'], width=2)
                draw.text((left + 28, block_top + 11), f"{item['start']}–{item['end']}",
                          font=time_font, fill=COLORS['ink'])
                title, kind = _subject_parts(item['subject'])
                pair_label = ''
                if item['_pairs'] > 1:
                    pair_label = f"{item['_pairs']} ПАРЫ" if item['_pairs'] in (2, 3, 4) else f"{item['_pairs']} ПАР"
                meta = ' · '.join(filter(None, (kind, pair_label)))
                if meta:
                    draw.text((left + 28, block_top + 39),
                              _ellipsize(draw, meta, bold[15], time_width - 10),
                              font=bold[15], fill=accent)
                max_title_lines = 2 if block_height >= 84 else 1
                title_lines = _limited_lines(draw, title, subject_font, text_width, max_title_lines)
                title_y = block_top + 8
                for line in title_lines:
                    draw.text((text_left, title_y), line, font=subject_font, fill=COLORS['ink'])
                    title_y += subject_line_height
                teachers = ' / '.join(item.get('teachers', [])) or 'Преподаватель не указан'
                rooms = ', '.join(item.get('rooms', [])) or 'не указана'
                groups = ', '.join(item.get('groups', []))
                details = teachers + '  ·  ауд. ' + rooms + (('  ·  ' + groups) if groups else '')
                detail_y = min(title_y + 2, block_bottom - detail_size - 9)
                draw.text((text_left, detail_y), _ellipsize(draw, details, detail_font, text_width),
                          font=detail_font, fill=COLORS['muted'])

    footer_y = 952
    draw.line((MARGIN, footer_y - 14, WIDTH - MARGIN, footer_y - 14), fill=COLORS['line'], width=2)
    draw.text((MARGIN, footer_y), 'Источник: msu2006.edupage.org  ·  только П2—23  ·  время Ташкента',
              font=regular[20], fill=COLORS['muted'])
    draw.text((MARGIN, footer_y + 39),
              '/today  день   /next  дальше   /refresh  проверить   /ask  спросить AI   /rooms  аудитории',
              font=bold[20], fill=COLORS['ink'])
    draw.text((MARGIN, footer_y + 78), 'Сохрани. Утренний ты — бесполезный мудак без памяти.',
              font=regular[19], fill=COLORS['muted'])

    output = BytesIO()
    image.save(output, format='PNG', optimize=True)
    return output.getvalue()
