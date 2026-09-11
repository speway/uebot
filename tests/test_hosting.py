import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from api.source import authorized
from api.telegram import make_reply, accepts_update, state_is_stale, with_live_snapshots
from bot import SourceError, TZ, parse_week
from github_runner import (GitStateBot, KNOWN_FALSE_WEEK_DIGEST, checked_recently,
                           check_with_confirmation, git, refresh_stored_publications,
                           repair_stored_week_mask_bug, validate_relay_payload)
from test_bot import FakeTelegram, META


class HostingTests(unittest.TestCase):
    def test_rapid_source_checks_are_skipped_but_normal_cron_is_not(self):
        now = dt.datetime(2026, 9, 10, 21, 0, tzinfo=TZ)
        self.assertTrue(checked_recently((now - dt.timedelta(minutes=2)).isoformat(), now))
        self.assertFalse(checked_recently((now - dt.timedelta(minutes=5)).isoformat(), now))
        self.assertFalse(checked_recently('not-a-date', now))

    def test_source_relay_requires_exact_nonempty_secret(self):
        self.assertTrue(authorized('expected', 'expected'))
        self.assertFalse(authorized('', ''))
        self.assertFalse(authorized('expected', 'different'))

    def test_source_relay_payload_is_strictly_validated(self):
        valid = {'schema': 1, 'snapshots': [{'week': '2026-09-07', 'lessons': []}]}
        self.assertEqual(validate_relay_payload(valid), valid['snapshots'])
        for invalid in ({}, {'schema': 2, 'snapshots': []}, {'schema': 1, 'snapshots': []},
                        {'schema': 1, 'snapshots': [{'week': 1, 'lessons': []}]}):
            with self.assertRaises(SourceError):
                validate_relay_payload(invalid)

    def test_unrelated_updates_are_ignored_before_fetch(self):
        for message in ({}, {'text': 'привет'}, {'text': '/today@another_bot'},
                        {'text': '/unknown'}, {'photo': [{}]}, {'text': '   '}):
            message['chat'] = {'id': -100123, 'type': 'supergroup'}
            self.assertFalse(accepts_update({'message': message}, -100123))

    def test_photo_reply_preserves_pending_warning(self):
        now = dt.datetime.now(TZ)
        monday = (now.date()-dt.timedelta(days=now.weekday())).isoformat()
        state = {'kv': {'last_success': now.isoformat(), 'pending_confirmation': True,
                        'image:'+monday: {'file_id': 'confirmed-photo'}}}
        result = make_reply({'update_id': 101, 'message': {'chat': {'id': -100123, 'type': 'supergroup'},
                            'from': {'id': 42}, 'text': ' /week@msutf_p223_schedule_bot '}}, state, -100123)
        self.assertEqual(result['method'], 'sendPhoto')
        self.assertEqual(result['photo'], 'confirmed-photo')
        self.assertIn('перепроверяю', result['caption'])
        self.assertEqual(result['parse_mode'], 'HTML')

    def test_live_overlay_never_reuses_a_possibly_stale_photo(self):
        now = dt.datetime.now(TZ)
        monday = now.date() - dt.timedelta(days=now.weekday())
        state = {'live': True, 'kv': {
            'last_success': now.isoformat(),
            'week:' + monday.isoformat(): {'week': monday.isoformat(), 'lessons': []},
            'image:' + monday.isoformat(): {'file_id': 'stale-photo'},
        }}
        result = make_reply({'update_id': 102, 'message': {
            'chat': {'id': -100123, 'type': 'supergroup'},
            'from': {'id': 42}, 'text': '/week',
        }}, state, -100123)
        self.assertEqual(result['method'], 'sendMessage')
        self.assertIn('ебучие лохи', result['text'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / 'remote.git'
        self.state = self.root / 'state'
        self.state.mkdir()
        git('init', '--bare', str(self.remote))
        git('init', str(self.state))
        git('config', 'user.name', 'Test', cwd=self.state)
        git('config', 'user.email', 'test@example.invalid', cwd=self.state)
        git('remote', 'add', 'origin', str(self.remote), cwd=self.state)
        self.api = FakeTelegram()

    def test_restart_restores_baseline_without_duplicate(self):
        raw = json.loads((Path(__file__).parent / 'edupage_130.json').read_text())
        snapshot = parse_week(raw, META)
        bot = GitStateBot(self.api, -100123, str(self.root / 'one.db'), self.state)
        bot.check([snapshot], dt.datetime(2026, 9, 7, 7, tzinfo=TZ))
        count = len(self.api.messages)
        bot.db.close()
        restored = GitStateBot(self.api, -100123, str(self.root / 'two.db'), self.state, True)
        try:
            restored.check([snapshot], dt.datetime(2026, 9, 7, 8, tzinfo=TZ))
            self.assertEqual(len(self.api.messages), count)
            public = json.loads((self.state / 'schedule.json').read_text())
            self.assertIn('week:2026-09-07', public['kv'])
            self.assertFalse(any(k.startswith('sent:') for k in public['kv']))
            state_vercel = json.loads((self.state / 'vercel.json').read_text())
            self.assertIs(state_vercel['git']['deploymentEnabled'], False)
        finally:
            restored.db.close()

    def test_git_failure_prevents_telegram_send(self):
        git('remote', 'set-url', 'origin', str(self.root / 'absent.git'), cwd=self.state)
        bot = GitStateBot(self.api, -100123, str(self.root / 'fail.db'), self.state)
        try:
            with self.assertRaises(RuntimeError):
                bot.send_once('test', 'do not send')
            self.assertEqual(self.api.messages, [])
        finally:
            bot.db.close()

    def test_missing_existing_state_never_resets_baseline(self):
        with self.assertRaises(RuntimeError):
            GitStateBot(self.api, -100123, str(self.root / 'missing.db'), self.state, True)

    def test_persisted_false_next_week_is_repaired_without_source_access(self):
        from unittest.mock import Mock
        raw = json.loads((Path(__file__).parent / 'edupage_130.json').read_text())
        current = parse_week(raw, META)
        snapshot = json.loads(json.dumps(current))
        snapshot['week'] = '2026-09-14'
        snapshot['source_title'] = '14 - 19 сентября'
        for lesson in snapshot['lessons']:
            lesson['date'] = (dt.date.fromisoformat(lesson['date']) + dt.timedelta(days=7)).isoformat()
        from bot import digest
        self.assertEqual(digest(snapshot['lessons']), KNOWN_FALSE_WEEK_DIGEST)
        self.api.photo = Mock(side_effect=[
            {'message_id': 77, 'photo': [{'file_id': 'fixed-photo'}]},
            {'message_id': 70, 'photo': [{'file_id': 'current-photo'}]},
        ])
        bot = GitStateBot(self.api, -100123, str(self.root / 'repair.db'), self.state)
        try:
            bot.put('week:2026-09-07', current)
            bot.put('publication:2026-09-07', [{'message_id': 50, 'hash': 'old'}])
            bot.put('image:2026-09-07', {'message_id': 70, 'hash': 'old', 'file_id': 'old-current'})
            bot.put('week:2026-09-14', snapshot)
            bot.put('publication:2026-09-14', [{'message_id': 55, 'hash': 'old'}])
            bot.put('image:2026-09-14', {'message_id': 77, 'hash': 'old', 'file_id': 'wrong-photo'})
            self.assertTrue(repair_stored_week_mask_bug(
                bot, dt.datetime(2026, 9, 11, 2, 0, tzinfo=TZ)))
            self.assertEqual(bot.get('week:2026-09-14')['lessons'], [])
            self.assertEqual(bot.get('image:2026-09-07')['file_id'], 'current-photo')
            self.assertEqual(bot.get('image:2026-09-14')['file_id'], 'fixed-photo')
            self.assertEqual([call.args[-1] for call in self.api.photo.call_args_list], [77, 70])
            self.assertTrue(any('Исправил свой косяк' in text for _, text in self.api.messages))
            self.assertFalse(repair_stored_week_mask_bug(
                bot, dt.datetime(2026, 9, 11, 2, 1, tzinfo=TZ)))
            self.assertTrue(refresh_stored_publications(
                bot, dt.datetime(2026, 9, 11, 2, 1, tzinfo=TZ)))
            self.assertFalse(refresh_stored_publications(
                bot, dt.datetime(2026, 9, 11, 2, 2, tzinfo=TZ)))
            self.assertEqual(self.api.photo.call_count, 2)
        finally:
            bot.db.close()

    def test_webhook_does_not_reply_to_another_group(self):
        result = make_reply({'update_id': 9, 'message': {'chat': {'id': -999, 'type': 'supergroup'},
                                                       'text': '/help'}}, {'kv': {}}, -100123)
        self.assertEqual(result, {'ok': True})

    def test_webhook_personal_text_and_staleness_warning(self):
        result = make_reply({'update_id': 10, 'message': {'chat': {'id': -100123, 'type': 'supergroup'},
                                                        'from': {'id': 42}, 'text': '/help'}}, {'kv': {}}, -100123)
        self.assertIn('читаю EduPage за П2‑23', result['text'])
        self.assertIn('Автопроверка задержалась', result['text'])
        self.assertEqual(result['chat_id'], -100123)

    def test_next_command_is_accepted(self):
        update = {'message': {'chat': {'id': -100123, 'type': 'supergroup'},
                              'text': '/next@msutf_p223_schedule_bot'}}
        self.assertTrue(accepts_update(update, -100123))

    def test_stale_state_can_be_overlaid_with_live_schedule(self):
        old = {'schema': 1, 'kv': {'last_success': '2026-09-10T00:00:00+05:00',
                                   'delivery_attention': False}}
        now = dt.datetime(2026, 9, 10, 1, 0, tzinfo=TZ)
        self.assertTrue(state_is_stale(old, now))
        fresh = with_live_snapshots(old, [{'week': '2026-09-14', 'lessons': []}], now)
        self.assertFalse(state_is_stale(fresh, now))
        self.assertIn('week:2026-09-14', fresh['kv'])
        self.assertTrue(fresh['live'])

    def test_detected_change_is_rechecked_inside_same_run(self):
        raw = json.loads((Path(__file__).parent / 'edupage_130.json').read_text())
        snapshot = parse_week(raw, META)
        bot = GitStateBot(self.api, -100123, str(self.root / 'confirm.db'), self.state)
        now = dt.datetime(2026, 9, 7, 7, tzinfo=TZ)
        try:
            bot.check([snapshot], now)
            changed = json.loads(json.dumps(snapshot))
            changed['lessons'][-1]['rooms'] = ['215']
            calls = []

            def fetcher():
                calls.append(True)
                return [changed]

            waits = []
            check_with_confirmation(bot, fetcher, waits.append, now)
            self.assertEqual(len(calls), 2)
            self.assertEqual(waits, [15])
            self.assertIsNone(bot.get('candidate:week:' + snapshot['week']))
            self.assertEqual(bot.get('week:' + snapshot['week'])['lessons'][-1]['rooms'], ['215'])
        finally:
            bot.db.close()
