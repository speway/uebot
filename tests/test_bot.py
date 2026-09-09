import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from bot import Bot, SourceError, TZ, digest, parse_week, render_week

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


if __name__ == '__main__':
    unittest.main()
