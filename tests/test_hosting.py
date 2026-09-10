import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from api.source import authorized
from api.telegram import make_reply, accepts_update
from bot import SourceError, TZ, parse_week
from github_runner import GitStateBot, checked_recently, git, validate_relay_payload
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
        self.assertIn('ожидаю повторную проверку', result['caption'])

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

    def test_webhook_does_not_reply_to_another_group(self):
        result = make_reply({'update_id': 9, 'message': {'chat': {'id': -999, 'type': 'supergroup'},
                                                       'text': '/help'}}, {'kv': {}}, -100123)
        self.assertEqual(result, {'ok': True})

    def test_webhook_personal_text_and_staleness_warning(self):
        result = make_reply({'update_id': 10, 'message': {'chat': {'id': -100123, 'type': 'supergroup'},
                                                        'from': {'id': 42}, 'text': '/help'}}, {'kv': {}}, -100123)
        self.assertIn('для своих кентиков', result['text'])
        self.assertIn('Данные давно не проверялись', result['text'])
        self.assertEqual(result['chat_id'], -100123)
