"""Exact EduPage-style weekly grid with neutral colors and adaptive row heights."""
from collections import defaultdict
from datetime import date, timedelta
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

DESIGN_VERSION = 2

def render_image(snapshot):
    fonts = {n: ImageFont.truetype('DejaVuSans.ttf', n) for n in (22, 24, 26, 30, 54)}
    bold = {n: ImageFont.truetype('DejaVuSans-Bold.ttf', n) for n in (24, 30, 54)}
    probe = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    def wrap(text, font, width):
        lines, line = [], ''
        for word in str(text).split():
            if probe.textlength((line+' '+word).strip(), font=font) <= width:
                line = (line+' '+word).strip()
                continue
            if line:
                lines.append(line)
                line = ''
            for char in word:
                if line and probe.textlength(line+char, font=font) > width:
                    lines.append(line)
                    line = ''
                line += char
        if line: lines.append(line)
        return lines

    monday = date.fromisoformat(snapshot['week'])
    lessons = snapshot['lessons']
    periods = sorted({(x['start'], x['end']) for x in lessons}, key=lambda p: tuple(map(int, p[0].split(':'))))
    if not periods:
        periods = [('—', '—')]
    if len(periods) > 12:
        raise ValueError('Too many distinct time slots for one timetable')
    cell_width, day_width, margin = max(360, 1070 // len(periods)), 150, 40
    width = 2*margin+day_width+cell_width*len(periods)
    cells = defaultdict(list)
    for item in lessons:
        cells[(item['date'], (item['start'], item['end']))].append(item)
    days = [monday+timedelta(days=i) for i in range(6)]
    if any(x['date'] == (monday+timedelta(days=6)).isoformat() for x in lessons):
        days.append(monday+timedelta(days=6))
    layout = []
    for day in days:
        row, height = [], 126
        for period in periods:
            runs = []
            for item in sorted(cells[(day.isoformat(), period)], key=lambda x: x['subject']):
                if runs: runs.append(('', fonts[22], '#52665f', 18))
                for text, font, color in [
                    (item['subject'], bold[24], '#213b32'),
                    (' / '.join(item['teachers']) or 'Преподаватель не указан', fonts[22], '#52665f'),
                    ('Ауд. '+', '.join(item['rooms']) if item['rooms'] else 'Аудитория не указана', bold[24], '#213b32'),
                ]:
                    for line in wrap(text, font, cell_width-36):
                        runs.append((line, font, color, 33))
                    runs.append(('', font, color, 8))
            height = max(height, sum(x[3] for x in runs)+36)
            row.append(runs)
        layout.append((day, row, height))
    height = 306+sum(x[2] for x in layout)+120
    if width+height > 9900 or max(width/height, height/width) > 20:
        raise ValueError('Timetable exceeds Telegram photo dimensions')
    image = Image.new('RGB', (width, height), '#f4f3ee')
    draw = ImageDraw.Draw(image)
    draw.text((margin, 30), 'УЕБОТ / ДЛЯ СВОИХ', font=fonts[24], fill='#52665f')
    draw.text((margin, 75), 'Расписание П2-23', font=bold[54], fill='#213b32')
    draw.text((margin, 151), f'{monday:%d.%m} — {monday+timedelta(days=5):%d.%m.%Y} · Время Ташкента', font=fonts[30], fill='#52665f')
    top = 222
    draw.rectangle((margin, top, width-margin, top+84), fill='#284c3e')
    draw.text((margin+20, top+26), 'День', font=bold[24], fill='white')
    for i, (start, end) in enumerate(periods):
        x = margin+day_width+i*cell_width
        draw.text((x+18, top+12), 'Время пары', font=bold[24], fill='white')
        draw.text((x+18, top+46), f'{start} – {end}', font=fonts[22], fill='#e0e9e2')
    y = top+84
    names = ['ПН', 'ВТ', 'СР', 'ЧТ', 'ПТ', 'СБ', 'ВС']
    for day, row, row_height in layout:
        draw.rectangle((margin, y, margin+day_width, y+row_height), fill='#e3e9e1')
        draw.text((margin+20, y+22), names[day.weekday()], font=bold[30], fill='#213b32')
        draw.text((margin+20, y+66), day.strftime('%d.%m'), font=fonts[24], fill='#52665f')
        for i, runs in enumerate(row):
            x = margin+day_width+i*cell_width
            draw.rectangle((x, y, x+cell_width, y+row_height), fill='#ffffff' if runs else '#efefe9', outline='#d2d9d1', width=1)
            yy = y+18
            if not runs: draw.text((x+18, yy), '—', font=fonts[26], fill='#a4afa6')
            for text, font, color, step in runs:
                draw.text((x+18, yy), text, font=font, fill=color)
                yy += step
        draw.line((margin, y+row_height, width-margin, y+row_height), fill='#c6d0c6')
        y += row_height
    draw.text((margin, y+24), 'Источник: msu2006.edupage.org · Только П2-23, включая общие занятия', font=fonts[22], fill='#52665f')
    draw.text((margin, y+61), 'Увеличь картинку для деталей · /today — пары на сегодня · /tomorrow — на завтра', font=fonts[22], fill='#52665f')
    output = BytesIO()
    image.save(output, format='PNG', optimize=True)
    return output.getvalue()
