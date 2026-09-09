"""Render exact timetable data into a readable, neutral Telegram card."""
from collections import defaultdict
from datetime import date, timedelta
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

def render_image(snapshot):
    fonts = {size: ImageFont.truetype('DejaVuSans.ttf', size) for size in (24, 28, 32, 36, 54)}
    bold = ImageFont.truetype('DejaVuSans-Bold.ttf', 36)
    probe = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    def wrap(text, size, width):
        lines = ['']
        for word in text.split():
            trial = (lines[-1] + ' ' + word).strip()
            if probe.textlength(trial, font=fonts[size]) > width and lines[-1]: lines.append(word)
            else: lines[-1] = trial
        return lines
    grouped = defaultdict(list)
    for item in snapshot['lessons']: grouped[item['date']].append(item)
    blocks = []
    for day, items in sorted(grouped.items()):
        rows=[]
        for item in sorted(items, key=lambda x:(x['start'],x['subject'])):
            title=wrap(item['subject'],32,710)
            details=wrap(' / '.join(item['teachers']) or 'Преподаватель не указан',24,710)
            extra='Ауд. '+', '.join(item['rooms']) if item['rooms'] else 'Аудитория не указана'
            if item['groups']: extra+=' · '+', '.join(item['groups'])
            details+=wrap(extra,24,710)
            rows.append((item,title,details, max(108, 22+len(title)*41+len(details)*32)))
        blocks.append((day,rows,76+sum(r[3] for r in rows)))
    height=300+sum(b[2]+20 for b in blocks)+100
    if height>8800: raise ValueError('Timetable too tall for one Telegram photo')
    im=Image.new('RGB',(1080,height),'#f3f1eb');d=ImageDraw.Draw(im)
    ink,muted,accent='#232a28','#626a65','#345b4d'
    start=date.fromisoformat(snapshot['week'])
    d.text((48,38),'УЕБОТ  /  ДЛЯ СВОИХ',font=fonts[24],fill=accent)
    d.text((48,86),'Расписание П2-23',font=fonts[54],fill=ink)
    d.text((48,161),f'{start:%d.%m} — {start+timedelta(days=5):%d.%m.%Y}',font=fonts[32],fill=muted)
    d.text((48,211),'Время Ташкента · каждая пара отдельно',font=fonts[24],fill=muted)
    weekdays=['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье']
    y=280
    for day,rows,h in blocks:
        d.rounded_rectangle((32,y,1048,y+h),radius=18,fill='white')
        dd=date.fromisoformat(day)
        d.text((52,y+17),f'{weekdays[dd.weekday()]}  ·  {dd:%d.%m}',font=bold,fill=accent)
        yy=y+76
        for item,title,details,rh in rows:
            d.line((52,yy,1028,yy),fill='#e5e8e3',width=1)
            d.text((52,yy+16),item['start'],font=fonts[32],fill=ink)
            d.text((52,yy+57),item['end'],font=fonts[28],fill=muted)
            ty=yy+14
            for line in title:
                d.text((270,ty),line,font=fonts[32],fill=ink);ty+=41
            for line in details:
                d.text((270,ty),line,font=fonts[24],fill=muted);ty+=32
            yy+=rh
        y+=h+20
    d.text((48,y+15),'Источник: msu2006.edupage.org · П2-23',font=fonts[24],fill=muted)
    d.text((48,y+52),'Об изменениях — отдельное уведомление в чате',font=fonts[24],fill=muted)
    out=BytesIO();im.save(out,format='PNG',optimize=True);return out.getvalue()
