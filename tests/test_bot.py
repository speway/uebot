import copy
import datetime as dt
import json
from pathlib import Path
import socket
import tempfile
import unittest

from bot import (Bot, SourceError, DeliveryError, TelegramRejected, EduPage, TZ,
                 IPv4HTTPSConnection, IPv4HTTPSHandler, digest, ipv4_connection,
                 parse_week, render_day_reply, render_week, week_caption)

META = {'datefrom': '2026-09-07', 'text': '7–12 сентября', 'tt_num': '130'}


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.edits = []
        self.fail = False

    def send(self, chat, text, **extra):
        if self.fail:
            raise RuntimeError('Simulated connection loss')
        self.messages.append((chat, text))
        return {'message_id': len(self.messages)}

    def call(self, method, **params):
        self.edits.append((method, params))
        return True


class ScheduleTests(unittest.TestCase):
    def test_schedule_available_when_publication_fails(self):
        self.api.fail = True
        with self.assertRaises(DeliveryError):
            self.bot.check([self.week], self.now)
        self.assertEqual(self.bot.get('week:' + self.week['week'])['lessons'], self.week['lessons'])
        self.assertEqual(self.bot.get('last_success'), self.now.isoformat())
        self.assertFalse(self.bot.get('source_error'))

    def test_rejected_request_can_be_retried_after_repair(self):
        from unittest.mock import Mock
        self.api.send = Mock(side_effect=TelegramRejected('Telegram HTTP 401'))
        with self.assertRaises(TelegramRejected):
            self.bot.send_once('repair', 'schedule')
        self.api.send.side_effect = None
        self.api.send.return_value = {'message_id': 77}
        self.assertEqual(self.bot.send_once('repair', 'schedule'), 77)

    def test_edupage_retries_transient_read_errors(self):
        from unittest.mock import Mock, patch, MagicMock
        from urllib.error import URLError
        source = EduPage()
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'published'
        source.http.open = Mock(side_effect=[URLError('timed out'), response])
        with patch('bot.time.sleep'):
            self.assertEqual(source.read('https://msu2006.edupage.org/timetable/'), b'published')
        self.assertEqual(source.http.open.call_count, 2)

    def test_edupage_transport_selects_only_ipv4(self):
        from unittest.mock import Mock, patch
        connection = Mock()
        with patch('bot.socket.getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.1', 443))]) as addresses, \
                patch('bot.socket.socket', return_value=connection):
            self.assertIs(ipv4_connection(('example.test', 443), timeout=12), connection)
        addresses.assert_called_once_with('example.test', 443, socket.AF_INET, socket.SOCK_STREAM)
        connection.settimeout.assert_called_once_with(12)
        connection.connect.assert_called_once_with(('192.0.2.1', 443))

    def test_edupage_handler_supports_python_313(self):
        from unittest.mock import Mock, patch
        transport = IPv4HTTPSHandler()
        if hasattr(transport, '_check_hostname'):
            del transport._check_hostname
        request = Mock()
        with patch.object(transport, 'do_open', return_value='response') as open_request:
            self.assertEqual(transport.https_open(request), 'response')
        self.assertEqual(open_request.call_args.args, (IPv4HTTPSConnection, request))
        self.assertIn('context', open_request.call_args.kwargs)
        self.assertNotIn('check_hostname', open_request.call_args.kwargs)

    def test_photo_publish_updates_without_duplicate(self):
        from unittest.mock import Mock
        self.api.photo = Mock(return_value={'message_id': 201, 'photo': [{'file_id': 'photo1'}]})
        self.bot.publish_image(self.week)
        self.bot.publish_image(self.week)
        self.assertEqual(self.api.photo.call_count, 1)
        changed = copy.deepcopy(self.week)
        changed['lessons'][0]['rooms'] = ['215']
        self.bot.publish_image(changed)
        self.assertEqual(self.api.photo.call_count, 2)
        self.assertEqual(self.api.photo.call_args.args[-1], 201)

    def test_uncertain_photo_send_is_not_repeated(self):
        from unittest.mock import Mock
        self.api.photo = Mock(side_effect=RuntimeError('timeout'))
        with self.assertRaises(RuntimeError):
            self.bot.publish_image(self.week)
        self.bot.publish_image(self.week)
        self.assertEqual(self.api.photo.call_count, 1)
        self.assertTrue(self.bot.get('delivery_attention'))

    def test_grid_is_valid_telegram_photo(self):
        from io import BytesIO
        from PIL import Image
        from schedule_image import render_image
        with Image.open(BytesIO(render_image(self.week))) as im:
            self.assertLess(im.width + im.height, 10000)
            self.assertLess(max(im.width/im.height, im.height/im.width), 20)
            self.assertEqual(im.format, 'PNG')

    def setUp(self):
        self.raw = json.loads((Path(__file__).parent / 'edupage_130.json').read_text())
        self.week = parse_week(self.raw, META)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = FakeTelegram()
        self.bot = Bot(self.api, -100123, str(Path(self.temp.name) / 'state.db'))
        self.addCleanup(self.bot.db.close)
        self.now = dt.datetime(2026, 9, 7, 7, tzinfo=TZ)

    def test_real_source_includes_shared_lessons_and_excludes_unplaced(self):
        self.assertEqual(len(self.week['lessons']), 12)
        monday = [x for x in self.week['lessons'] if x['date'] == '2026-09-07']
        self.assertEqual([(x['start'], x['end']) for x in monday], [('09:00', '10:30'), ('10:45', '12:15')])
        self.assertTrue(all(x['rooms'] == ['313'] for x in self.week['lessons']))

    def test_exact_group_required(self):
        table = next(t for t in self.raw['dbiAccessorRes']['tables'] if t['id'] == 'classes')
        table['data_rows'] = [x for x in table['data_rows'] if x['short'] != 'П2-23']
        with self.assertRaises(SourceError):
            parse_week(self.raw, META)

    def test_missing_table_is_not_a_cancellation(self):
        self.raw['dbiAccessorRes']['tables'] = []
        with self.assertRaises(SourceError):
            parse_week(self.raw, META)

    def test_display_changes_do_not_change_semantics(self):
        for table in self.raw['dbiAccessorRes']['tables']:
            table['data_rows'].reverse()
            for row in table['data_rows']:
                row['color'] = '#ff00ff'
        self.assertEqual(digest(self.week['lessons']), digest(parse_week(self.raw, META)['lessons']))

    def test_repeated_check_does_not_resend(self):
        self.bot.check([self.week], self.now)
        count = len(self.api.messages)
        self.bot.check([self.week], self.now)
        self.assertEqual(len(self.api.messages), count)

    def test_change_requires_two_observations_and_is_sent_once(self):
        self.bot.check([self.week], self.now)
        baseline = len(self.api.messages)
        changed = copy.deepcopy(self.week)
        changed['lessons'][0]['rooms'] = ['215']
        self.bot.check([changed], self.now)
        self.assertEqual(len(self.api.messages), baseline)
        self.bot.check([changed], self.now)
        self.assertGreater(len(self.api.messages), baseline)
        count = len(self.api.messages)
        self.bot.check([changed], self.now)
        self.assertEqual(len(self.api.messages), count)
        self.assertIn('215', self.api.messages[-1][1])

    def test_reverted_candidate_is_reset(self):
        self.bot.check([self.week], self.now)
        baseline = len(self.api.messages)
        changed = copy.deepcopy(self.week)
        changed['lessons'][0]['rooms'] = ['215']
        for snapshot in [changed, self.week, changed]:
            self.bot.check([snapshot], self.now)
        self.assertEqual(len(self.api.messages), baseline)

    def test_empty_response_preserves_previous_schedule(self):
        self.bot.check([self.week], self.now)
        empty = copy.deepcopy(self.week)
        empty['lessons'] = []
        with self.assertRaises(SourceError):
            self.bot.check([empty], self.now)
        self.assertEqual(len(self.bot.get('week:2026-09-07')['lessons']), 12)

    def test_uncertain_delivery_does_not_repeat(self):
        self.api.fail = True
        with self.assertRaises(RuntimeError):
            self.bot.send_once('test', 'example')
        self.api.fail = False
        self.bot.send_once('test', 'example')
        self.assertEqual(self.api.messages, [])
        self.assertTrue(self.bot.get('delivery_attention'))

    def test_html_escaped_and_message_limit_respected(self):
        self.week['lessons'][0]['subject'] = '<test> & text'
        texts = render_week(self.week)
        self.assertIn('&lt;test&gt; &amp; text', '\n'.join(texts))
        self.assertTrue(all(len(x) <= 3700 for x in texts))

    def test_other_chat_is_ignored(self):
        self.bot.handle({'update_id': 1, 'message': {'chat': {'id': 999}, 'text': '/week'}})
        self.assertEqual(self.api.messages, [])

    def test_private_help_stays_in_private_chat(self):
        self.bot.handle({'update_id': 2, 'message': {'chat': {'id': 999, 'type': 'private'},
                                                    'from': {'id': 999}, 'text': '/help'}})
        self.assertEqual(len(self.api.messages), 1)
        self.assertEqual(self.api.messages[0][0], 999)

    def test_weekly_post_is_edited_and_change_is_explained(self):
        self.bot.check([self.week], self.now)
        changed = copy.deepcopy(self.week)
        changed['lessons'][0]['rooms'] = ['215']
        self.bot.check([changed], self.now)
        self.bot.check([changed], self.now)
        self.assertEqual(len(self.api.edits), 1)
        self.assertIn('Аудитория: 313 → <b>215</b>', self.api.messages[-1][1])

    def test_same_transition_can_recur_later(self):
        self.bot.check([self.week], self.now)
        changed = copy.deepcopy(self.week)
        changed['lessons'][0]['rooms'] = ['215']
        for snapshot in [changed, changed, self.week, self.week, changed, changed]:
            self.bot.check([snapshot], self.now)
        changes = [text for _, text in self.api.messages if 'Изменения в расписании' in text]
        self.assertEqual(len(changes), 3)

    def test_failure_interrupts_confirmation_sequence(self):
        self.bot.check([self.week], self.now)
        baseline = len(self.api.messages)
        changed = copy.deepcopy(self.week)
        changed['lessons'][0]['rooms'] = ['215']
        self.bot.check([changed], self.now)
        self.bot.failed_check(SourceError('temporary outage'))
        self.bot.check([changed], self.now)
        self.assertEqual(len(self.api.messages), baseline)

    def test_tomorrow_digest_once_and_only_after_nineteen(self):
        self.bot.check([self.week], self.now)
        baseline = len(self.api.messages)
        self.bot.daily_digest(self.now.replace(hour=18))
        self.assertEqual(len(self.api.messages), baseline)
        self.bot.daily_digest(self.now.replace(hour=19))
        self.bot.daily_digest(self.now.replace(hour=20))
        self.assertEqual(len(self.api.messages), baseline + 1)
        self.assertIn('Вторник · 08.09', self.api.messages[-1][1])

    def test_day_reply_has_summary_and_useful_timing(self):
        items = [x for x in self.week['lessons'] if x['date'] == '2026-09-09']
        result = render_day_reply(dt.date(2026, 9, 9), items,
                                  dt.datetime(2026, 9, 9, 8, 10, tzinfo=TZ))
        self.assertIn('4 пары · 09:00–16:30', result)
        self.assertIn('До первой пары 50 мин', result)
        self.assertIn('2 пары подряд · перерыв 15 мин', result)

    def test_next_command_reports_countdown_and_room(self):
        self.bot.put('week:' + self.week['week'], self.week)
        result = self.bot.next_message(dt.datetime(2026, 9, 7, 8, 30, tzinfo=TZ))
        self.assertIn('Следующая пара', result)
        self.assertIn('через 30 мин', result)
        self.assertIn('09:00–12:15', result)
        self.assertIn('2 пары подряд', result)
        self.assertIn('ауд. 313', result)

    def test_status_is_diagnostic_but_human(self):
        self.bot.put('last_success', self.now.isoformat())
        result = self.bot.status_message(self.now + dt.timedelta(minutes=7))
        self.assertIn('7 мин назад', result)
        self.assertIn('Источник: отвечает', result)
        self.assertIn('Доставка: без ошибок', result)
        self.assertIn('Кто-то в этой группе', result)

    def test_week_caption_contains_counts_and_checked_time(self):
        result = week_caption(self.week, self.now.isoformat())
        self.assertIn('07.09–12.09.2026', result)
        self.assertIn('12 пар · 5 учебных дней', result)
        self.assertIn('Проверено: 07.09 в 07:00', result)

    def test_bot_profile_and_command_menu_are_configured_once(self):
        self.bot.configure()
        self.bot.configure()
        methods = [method for method, _ in self.api.edits]
        self.assertEqual(methods.count('setMyCommands'), 1)
        self.assertEqual(methods.count('setMyDescription'), 1)
        self.assertEqual(methods.count('setMyShortDescription'), 1)


if __name__ == '__main__':
    unittest.main()
