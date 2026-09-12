import copy
import datetime as dt
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from ai_responder import (BOT_USERNAME, DIRECT_URL, GATEWAY_URL, extract_question,
                          generate_answer, is_ai_request, provider_ready, reply_to_update,
                          reset_runtime_state, schedule_context)
from bot import TZ, parse_week
from test_bot import META


CHAT_ID = -100123


def update(text, *, update_id=1, private=False, reply=False, sender=42):
    message = {
        'message_id': 11,
        'chat': {'id': sender if private else CHAT_ID,
                 'type': 'private' if private else 'supergroup'},
        'from': {'id': sender, 'is_bot': False},
        'text': text,
    }
    if reply:
        message['reply_to_message'] = {
            'message_id': 10,
            'from': {'id': 999, 'is_bot': True, 'username': BOT_USERNAME},
            'text': 'Предыдущий ответ бота',
        }
    return {'update_id': update_id, 'message': message}


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload, ensure_ascii=False).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit=None):
        return self.payload


class AIResponderTests(unittest.TestCase):
    def setUp(self):
        reset_runtime_state()
        raw = json.loads((Path(__file__).parent / 'edupage_130.json').read_text())
        self.week = parse_week(raw, META)
        next_week = copy.deepcopy(self.week)
        next_week['week'] = '2026-09-14'
        next_week['lessons'] = []
        self.now = dt.datetime(2026, 9, 12, 12, 0, tzinfo=TZ)
        self.state = {'schema': 1, 'kv': {
            'last_success': self.now.isoformat(),
            'week:2026-09-07': self.week,
            'week:2026-09-14': next_week,
        }}

    def test_group_ai_requires_an_explicit_trigger(self):
        self.assertFalse(is_ai_request(update('привет'), CHAT_ID))
        self.assertTrue(is_ai_request(update('/ask почему небо синее?'), CHAT_ID))
        self.assertTrue(is_ai_request(update('@msutf_p223_schedule_bot, ты жив?'), CHAT_ID))
        self.assertTrue(is_ai_request(update('а почему?', reply=True), CHAT_ID))
        self.assertFalse(is_ai_request(update('/unknown @msutf_p223_schedule_bot'), CHAT_ID))
        automated = update('/ask зациклись')
        automated['message']['from']['is_bot'] = True
        self.assertFalse(is_ai_request(automated, CHAT_ID))

    def test_private_text_is_detected_but_access_is_allowlisted(self):
        request = update('ответь мне', private=True, sender=77)
        self.assertTrue(is_ai_request(request, CHAT_ID))
        with patch.dict(os.environ, {}, clear=True):
            result = reply_to_update(request, self.state, CHAT_ID, now=self.now)
        self.assertIn('В личке этот цирк закрыт', result)
        with patch.dict(os.environ, {'AI_PRIVATE_USER_IDS': '77'}, clear=True), \
                patch('ai_responder.generate_answer', return_value='Можно.'):
            reset_runtime_state()
            result = reply_to_update(request, self.state, CHAT_ID, now=self.now)
        self.assertIn('Можно.', result)

    def test_question_and_reply_context_are_extracted(self):
        question, context = extract_question(
            update('@msutf_p223_schedule_bot: а почему?', reply=True))
        self.assertEqual(question, 'а почему?')
        self.assertEqual(context, 'Предыдущий ответ бота')
        self.assertEqual(extract_question(update('/ask@msutf_p223_schedule_bot вопрос'))[0],
                         'вопрос')

    def test_schedule_context_keeps_unpublished_distinct_from_free(self):
        context = schedule_context(self.state, self.now)
        self.assertEqual(context['weeks']['current']['status'], 'published')
        self.assertEqual(context['weeks']['next']['status'], 'not_published')
        self.assertEqual(context['weeks']['next']['lessons'], [])
        self.assertFalse(context['stale'])

    def test_gateway_request_is_bounded_tagged_and_uses_current_model(self):
        seen = {}

        def opener(request, timeout):
            seen['url'] = request.full_url
            seen['timeout'] = timeout
            seen['authorization'] = request.get_header('Authorization')
            seen['body'] = json.loads(request.data)
            return FakeResponse({'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': 'Небо синее. Вот это открытие, Ньютон.'}
            ]}]})

        env = {'AI_GATEWAY_API_KEY': 'test-gateway-key', 'WEBHOOK_SECRET': 'pepper'}
        with patch.dict(os.environ, env, clear=True):
            answer = generate_answer('Почему небо синее?', self.state, 42, now=self.now,
                                     opener=opener)
        self.assertEqual(seen['url'], GATEWAY_URL)
        self.assertEqual(seen['authorization'], 'Bearer test-gateway-key')
        self.assertEqual(seen['body']['model'], 'openai/gpt-5.4-mini')
        self.assertEqual(seen['body']['max_output_tokens'], 1200)
        self.assertFalse(seen['body']['store'])
        self.assertEqual(seen['body']['tools'][0]['type'], 'web_search')
        gateway_user = seen['body']['providerOptions']['gateway']['user']
        self.assertTrue(gateway_user.startswith('telegram-'))
        self.assertNotEqual(gateway_user, 'telegram-42')
        self.assertIn('not_published', seen['body']['instructions'])
        self.assertEqual(answer, 'Небо синее. Вот это открытие, Ньютон.')

    def test_direct_openai_fallback_strips_gateway_provider_prefix(self):
        seen = {}

        def opener(request, timeout):
            seen['url'] = request.full_url
            seen['body'] = json.loads(request.data)
            return FakeResponse({'output_text': 'Прямой ответ'})

        with patch.dict(os.environ, {
                'OPENAI_API_KEY': 'test-openai-key', 'AI_MODEL': 'openai/gpt-5.4-mini',
                'AI_WEB_SEARCH': '0'}, clear=True):
            answer = generate_answer('Тест', self.state, 42, now=self.now, opener=opener)
        self.assertEqual(seen['url'], DIRECT_URL)
        self.assertEqual(seen['body']['model'], 'gpt-5.4-mini')
        self.assertNotIn('providerOptions', seen['body'])
        self.assertNotIn('tools', seen['body'])
        self.assertEqual(answer, 'Прямой ответ')

    def test_runtime_oidc_header_authenticates_gateway_without_an_env_secret(self):
        seen = {}

        def opener(request, timeout):
            seen['url'] = request.full_url
            seen['authorization'] = request.get_header('Authorization')
            return FakeResponse({'output_text': 'OIDC работает'})

        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(provider_ready('runtime-oidc-token'))
            answer = generate_answer('Тест', self.state, 42, now=self.now, opener=opener,
                                     runtime_oidc_token='runtime-oidc-token')
        self.assertEqual(seen['url'], GATEWAY_URL)
        self.assertEqual(seen['authorization'], 'Bearer runtime-oidc-token')
        self.assertEqual(answer, 'OIDC работает')

    def test_missing_question_and_missing_provider_fail_without_network(self):
        with patch.dict(os.environ, {}, clear=True):
            missing = reply_to_update(update('/ask', update_id=100), self.state, CHAT_ID,
                                      now=self.now)
            unconfigured = reply_to_update(update('/ask тест', update_id=101), self.state,
                                           CHAT_ID, now=self.now)
        self.assertIn('Напиши сам вопрос', missing)
        self.assertIn('сервер пока не выдал ему питание', unconfigured)

    def test_answer_is_html_escaped_and_duplicate_update_is_cached(self):
        request = update('/ask сравни 2 < 3', update_id=500)
        with patch.dict(os.environ, {'VERCEL_OIDC_TOKEN': 'oidc'}, clear=True), \
                patch('ai_responder.generate_answer', return_value='Да: 2 < 3 & всё.') as call:
            first = reply_to_update(request, self.state, CHAT_ID, now=self.now)
            second = reply_to_update(request, self.state, CHAT_ID, now=self.now)
        self.assertEqual(first, second)
        self.assertIn('2 &lt; 3 &amp; всё', first)
        self.assertEqual(call.call_count, 1)

    def test_cooldown_stops_a_second_paid_request(self):
        with patch.dict(os.environ, {'VERCEL_OIDC_TOKEN': 'oidc'}, clear=True), \
                patch('ai_responder.generate_answer', return_value='Ответ') as call, \
                patch('ai_responder.time.monotonic', side_effect=[100.0, 101.0]):
            reply_to_update(update('/ask первый', update_id=601), self.state, CHAT_ID, now=self.now)
            result = reply_to_update(update('/ask второй', update_id=602), self.state, CHAT_ID,
                                     now=self.now)
        self.assertIn('Один вопрос раз в 10 секунд', result)
        self.assertEqual(call.call_count, 1)


if __name__ == '__main__':
    unittest.main()
