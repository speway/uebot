"""Bounded schedule watcher. Persist every send reservation before Telegram."""
import datetime as dt
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from bot import Bot, Telegram, DeliveryError, EduPage, SourceError, TZ, digest, NO_SCHEDULE_ROAST

SOURCE_RELAY = 'https://uebot.vercel.app/api/source'
STATE_BRANCH_VERCEL_CONFIG = json.dumps({
    '$schema': 'https://openapi.vercel.sh/vercel.json',
    'git': {'deploymentEnabled': False},
}, indent=2) + '\n'
MINIMUM_SOURCE_INTERVAL = dt.timedelta(minutes=4)
CONFIRMATION_RECHECK_SECONDS = 15
STATE_HEARTBEAT_INTERVAL = dt.timedelta(minutes=10)
SOURCE_ERROR_HEARTBEAT_INTERVAL = dt.timedelta(minutes=30)
DEFAULT_CHECK_INTERVAL_SECONDS = 90
MAX_CONSECUTIVE_SOURCE_FAILURES = 10
WEEK_MASK_STATE_VERSION = 2
PUBLICATION_STYLE_VERSION = 6
KNOWN_FALSE_WEEK = '2026-09-14'
KNOWN_FALSE_WEEK_DIGEST = '90e022f57812e01b234e8a46e7b25ffba1db027a467ff7d9df1b2dd6d95fdac8'


def git(*args, cwd=None, allowed=(0,)):
    result = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode not in allowed:
        # Credentials can occur in remote errors; never print raw command output.
        raise RuntimeError('Git operation failed: ' + args[0])
    return result


def validate_relay_payload(payload):
    if payload.get('schema') != 1 or not isinstance(payload.get('snapshots'), list):
        raise SourceError('Schedule relay returned invalid data')
    snapshots = payload['snapshots']
    if not 1 <= len(snapshots) <= 4:
        raise SourceError('Schedule relay returned an invalid week count')
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('week'), str) or \
                not isinstance(snapshot.get('lessons'), list):
            raise SourceError('Schedule relay returned an invalid timetable')
    return snapshots


def fetch_snapshots():
    """Read EduPage directly, with the authenticated Vercel route as fallback."""
    url = os.getenv('SOURCE_RELAY_URL', '')
    secret = os.getenv('WEBHOOK_SECRET', '')
    if url and url != SOURCE_RELAY:
        raise RuntimeError('Unexpected schedule relay URL')
    direct_error = None
    try:
        # Frequent watchers should not spend Vercel function time when the
        # runner can reach the public source itself.
        return EduPage(timeout=15, attempts=1).fetch()
    except SourceError as exc:
        direct_error = exc
    relay_error = None
    if url and secret:
        logging.warning('Direct EduPage read failed; trying the authenticated relay: %s',
                        direct_error)
        request = urllib.request.Request(url, headers={'X-Schedule-Source-Secret': secret,
                                                       'User-Agent': 'P223ScheduleBot/1.0'})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read(2097153)
            if len(data) > 2097152:
                raise SourceError('Schedule relay response is too large')
            return validate_relay_payload(json.loads(data))
        except urllib.error.HTTPError as exc:
            relay_error = SourceError('Schedule relay HTTP ' + str(exc.code))
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, TypeError) as exc:
            reason = getattr(exc, 'reason', exc)
            relay_error = SourceError('Schedule relay connection: ' + type(reason).__name__)
    if relay_error:
        raise SourceError(str(direct_error) + '; relay: ' + str(relay_error)) from None
    raise direct_error


def checked_recently(value, now=None):
    """Avoid hammering EduPage when several code pushes queue together."""
    if not value:
        return False
    try:
        checked = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    if checked.tzinfo is None:
        return False
    age = (now or dt.datetime.now(TZ)) - checked.astimezone(TZ)
    return dt.timedelta(0) <= age < MINIMUM_SOURCE_INTERVAL


def check_with_confirmation(bot, fetcher=fetch_snapshots, sleeper=time.sleep, now=None):
    """Confirm a detected change inside the same run when scheduled runs are delayed."""
    bot.check(fetcher(), now)
    pending = any(value for _, value in bot._items('candidate:') if value)
    if pending:
        sleeper(CONFIRMATION_RECHECK_SECONDS)
        bot.check(fetcher(), now)


def run_checks(bot, fetcher=fetch_snapshots, watch_seconds=0,
               interval_seconds=DEFAULT_CHECK_INTERVAL_SECONDS, sleeper=time.sleep,
               clock=time.monotonic, stop_requested=None,
               max_source_failures=MAX_CONSECUTIVE_SOURCE_FAILURES):
    """Run once or keep a bounded watcher alive between unreliable cron events."""
    watch_seconds = max(0, int(watch_seconds))
    interval_seconds = max(60, int(interval_seconds))
    max_source_failures = max(1, int(max_source_failures))
    watching = watch_seconds > 0
    deadline = clock() + watch_seconds
    first = True
    consecutive_source_failures = 0
    while first or not watching or clock() < deadline:
        first = False
        started = clock()
        try:
            check_with_confirmation(bot, fetcher, sleeper)
            consecutive_source_failures = 0
            logging.info('Schedule check succeeded; next poll is due within %s seconds.',
                         interval_seconds)
        except DeliveryError:
            # Telegram failures need a red workflow and operator-visible state;
            # blindly continuing could conceal an unresolved send reservation.
            raise
        except SourceError as exc:
            bot.failed_check(exc)
            consecutive_source_failures += 1
            if not watching or consecutive_source_failures >= max_source_failures:
                raise RuntimeError('Check failed: ' + str(exc)) from None
            logging.warning('Schedule source unavailable (%s/%s): %s',
                            consecutive_source_failures, max_source_failures, exc)
        if not watching:
            return
        if stop_requested and stop_requested():
            logging.info('A newer main revision is available; handing off to the queued watcher.')
            return
        remaining = deadline - clock()
        if remaining <= 0:
            return
        elapsed = clock() - started
        sleeper(min(max(0, interval_seconds - elapsed), remaining))


def repair_stored_week_mask_bug(bot, now=None):
    """Repair the one persisted week produced before week masks were applied."""
    if bot.get('week_mask_state_version', 0) >= WEEK_MASK_STATE_VERSION:
        return False
    key = 'week:' + KNOWN_FALSE_WEEK
    pending_key = 'repair:week-mask:' + KNOWN_FALSE_WEEK
    snapshot = bot.get(key)
    pending = bot.get(pending_key, False)
    if not pending:
        if not snapshot or digest(snapshot.get('lessons', [])) != KNOWN_FALSE_WEEK_DIGEST:
            bot.put('week_mask_state_version', WEEK_MASK_STATE_VERSION)
            return False
        corrected = dict(snapshot, lessons=[], revision=snapshot.get('revision', 0) + 1)
        # Persist the safe answer before touching Telegram. While the photo is
        # being replaced, webhook replies fall back to text instead of reusing it.
        bot.put(pending_key, True)
        bot.put(key, corrected)
        bot.put('candidate:' + key, None)
        image_key = 'image:' + KNOWN_FALSE_WEEK
        image = bot.get(image_key, {})
        if image:
            bot.put(image_key, dict(image, hash='', file_id=''))
        snapshot = corrected
    if not snapshot or snapshot.get('lessons'):
        raise RuntimeError('Stored week-mask repair has an invalid snapshot')
    # Fix the harmful future card first. Cosmetic refreshes must not be able to
    # delay removal of a timetable we now know is false.
    bot.publish_week(snapshot)
    bot.publish_image(snapshot)
    bot.send_once('repair:week-mask-notice:' + KNOWN_FALSE_WEEK,
                  '<b>П2‑23 · Исправил свой косяк</b>\n\n'
                  'Следующая неделя раньше показывала копию текущей. Это была ошибка чтения недельной '
                  'маски EduPage, а не опубликованное расписание. Ложную карточку заменил.\n\n'
                  f'<i>{NO_SCHEDULE_ROAST}</i>')
    today = (now or dt.datetime.now(TZ)).date()
    current_monday = today - dt.timedelta(days=today.weekday())
    current = bot.get('week:' + current_monday.isoformat())
    if current:
        # The same migration refreshes the current card and copy, so the design
        # release does not depend on EduPage being reachable at deploy time.
        bot.publish_week(current)
        bot.publish_image(current)
    bot.put('week_mask_state_version', WEEK_MASK_STATE_VERSION)
    bot.put(pending_key, None)
    return True


def refresh_stored_publications(bot, now=None):
    """Refresh current cards after a copy or image design release, without source access."""
    if bot.get('publication_style_version', 0) >= PUBLICATION_STYLE_VERSION:
        return False
    today = (now or dt.datetime.now(TZ)).date()
    monday = today - dt.timedelta(days=today.weekday())
    for offset in (0, 7):
        snapshot = bot.get('week:' + (monday + dt.timedelta(days=offset)).isoformat())
        if snapshot:
            bot.publish_week(snapshot)
            bot.publish_image(snapshot)
    bot.put('publication_style_version', PUBLICATION_STYLE_VERSION)
    return True


class GitStateBot(Bot):
    def __init__(self, telegram, chat_id, database, state_dir, require_existing=False):
        super().__init__(telegram, chat_id, database)
        self.state_dir = Path(state_dir)
        self.state_file = self.state_dir / 'state.json'
        self._last_persisted_success = None
        if require_existing and not self.state_file.exists():
            raise RuntimeError('Existing state branch is incomplete; refusing to resend baseline')
        if self.state_file.exists():
            data = json.loads(self.state_file.read_text())
            if data.get('schema') != 1 or not isinstance(data.get('kv'), dict):
                raise RuntimeError('Unsupported persisted state; refusing fresh initialization')
            for key, value in data['kv'].items():
                Bot.put(self, key, value)
        self._last_persisted_success = self._timestamp(self.get('last_success'))

    @staticmethod
    def _timestamp(value):
        try:
            parsed = dt.datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo else None

    def _persist(self):
        with self.lock:
            state = {key: json.loads(value) for key, value in self.db.execute('SELECT key, value FROM kv ORDER BY key')}
        self.state_file.write_text(json.dumps({'schema': 1, 'kv': state}, ensure_ascii=False, sort_keys=True, indent=2))
        # This file contains only published university lessons and check status.
        public = {key: value for key, value in state.items() if key.startswith('week:') or
                  key in ('last_success', 'source_error', 'delivery_attention')}
        public.update({key: {'file_id':value['file_id']} for key,value in state.items() if key.startswith('image:') and value})
        public['pending_confirmation'] = any(value for key, value in state.items() if key.startswith('candidate:'))
        (self.state_dir / 'schedule.json').write_text(json.dumps({'schema': 1, 'kv': public}, ensure_ascii=False, sort_keys=True))
        # Vercel evaluates configuration from the branch being deployed.  The
        # state branch therefore needs its own opt-out file; keeping the rule
        # only on main still creates a failed preview for every state commit.
        (self.state_dir / 'vercel.json').write_text(STATE_BRANCH_VERCEL_CONFIG)
        git('add', 'state.json', 'schedule.json', 'vercel.json', cwd=self.state_dir)
        changed = git('diff', '--cached', '--quiet', cwd=self.state_dir, allowed=(0, 1)).returncode
        if changed:
            git('commit', '-m', 'Persist schedule check state', cwd=self.state_dir)
            git('push', 'origin', 'HEAD:state', cwd=self.state_dir)
        self._last_persisted_success = self._timestamp(state.get('last_success'))

    def put(self, key, value):
        previous = self.get(key, object())
        if previous == value:
            return
        if key == 'source_error' and value and isinstance(previous, dict):
            previous_at = self._timestamp(previous.get('at'))
            current_at = self._timestamp(value.get('at'))
            same_error = (previous.get('type'), previous.get('detail')) == \
                         (value.get('type'), value.get('detail'))
            if same_error and previous_at and current_at and \
                    dt.timedelta(0) <= current_at - previous_at < SOURCE_ERROR_HEARTBEAT_INTERVAL:
                return
        super().put(key, value)
        if key == 'last_success':
            current = self._timestamp(value)
            if current and self._last_persisted_success and \
                    dt.timedelta(0) <= current - self._last_persisted_success < STATE_HEARTBEAT_INTERVAL:
                return
        self._persist()


def main_branch_changed(expected_sha):
    """Let a safe completed cycle yield quickly after code is updated."""
    if not expected_sha:
        return False
    try:
        result = git('ls-remote', 'origin', 'refs/heads/main')
    except RuntimeError:
        logging.warning('Could not check whether main changed; keeping the current watcher alive.')
        return False
    fields = result.stdout.strip().split()
    return len(fields) >= 2 and fields[0] != expected_sha


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.environ['TELEGRAM_BOT_TOKEN']
    chat_id = os.environ['TELEGRAM_CHAT_ID']
    # GitHub Actions serializes all runs through a single concurrency group.
    with tempfile.TemporaryDirectory(prefix='p223-') as temporary:
        state_dir = Path(temporary) / 'state'
        exists = git('ls-remote', '--exit-code', '--heads', 'origin', 'state', allowed=(0, 2)).returncode == 0
        if exists:
            git('fetch', 'origin', 'state', '--depth=1')
            git('worktree', 'add', '--detach', str(state_dir), 'FETCH_HEAD')
        else:
            git('worktree', 'add', '--detach', str(state_dir), 'HEAD')
            git('switch', '--orphan', 'state-initialization', cwd=state_dir)
        git('config', 'user.name', 'github-actions[bot]', cwd=state_dir)
        git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com', cwd=state_dir)
        bot = GitStateBot(Telegram(token), chat_id, str(Path(temporary) / 'state.sqlite3'), state_dir, exists)
        bot.configure()
        try:
            repair_stored_week_mask_bug(bot)
            refresh_stored_publications(bot)
        except DeliveryError:
            bot.put('delivery_attention', True)
            raise
        if os.getenv('RECOVER_LAUNCH') == 'true':
            # Explicit one-time operator recovery after the revoked-token launch.
            # Leave all confirmed message IDs and successful publication records intact.
            with bot.lock:
                reservations = [(key, json.loads(value)) for key, value in bot.db.execute('SELECT key, value FROM kv')]
            for key, value in reservations:
                if key.startswith('sent:weekly:') and value and value.get('status') == 'pending':
                    bot.put(key, None)
                if key.startswith('image-send:') and value:
                    week = key.split(':')[1]
                    if not bot.get('image:' + week):
                        bot.put(key, None)
            bot.put('delivery_attention', False)
        watch_seconds = max(0, int(os.getenv('WATCH_SECONDS', '0')))
        if not watch_seconds and os.getenv('GITHUB_EVENT_NAME') == 'schedule' and checked_recently(bot.get('last_success')):
            # Scheduler delays can release queued jobs only seconds apart.
            # Only overlapping scheduled runs are skipped;
            # code pushes and manual runs must publish their requested refresh.
            return
        last_ref_check = [0.0]

        def newer_main_available():
            current = time.monotonic()
            if current - last_ref_check[0] < 300:
                return False
            last_ref_check[0] = current
            return main_branch_changed(os.getenv('GITHUB_SHA', ''))

        run_checks(
            bot,
            watch_seconds=watch_seconds,
            interval_seconds=os.getenv('CHECK_INTERVAL_SECONDS', str(DEFAULT_CHECK_INTERVAL_SECONDS)),
            stop_requested=newer_main_available if watch_seconds else None,
        )


if __name__ == '__main__':
    main()
