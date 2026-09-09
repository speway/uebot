"""One scheduled run. Persist every send reservation to Git before Telegram."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

from bot import Bot, Telegram


def git(*args, cwd=None, allowed=(0,)):
    result = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode not in allowed:
        # Credentials can occur in remote errors; never print raw command output.
        raise RuntimeError('Git operation failed: ' + args[0])
    return result


class GitStateBot(Bot):
    def __init__(self, telegram, chat_id, database, state_dir, require_existing=False):
        super().__init__(telegram, chat_id, database)
        self.state_dir = Path(state_dir)
        self.state_file = self.state_dir / 'state.json'
        if require_existing and not self.state_file.exists():
            raise RuntimeError('Existing state branch is incomplete; refusing to resend baseline')
        if self.state_file.exists():
            data = json.loads(self.state_file.read_text())
            if data.get('schema') != 1 or not isinstance(data.get('kv'), dict):
                raise RuntimeError('Unsupported persisted state; refusing fresh initialization')
            for key, value in data['kv'].items():
                Bot.put(self, key, value)

    def put(self, key, value):
        super().put(key, value)
        with self.lock:
            state = {key: json.loads(value) for key, value in self.db.execute('SELECT key, value FROM kv ORDER BY key')}
        self.state_file.write_text(json.dumps({'schema': 1, 'kv': state}, ensure_ascii=False, sort_keys=True, indent=2))
        # This file contains only published university lessons and check status.
        public = {key: value for key, value in state.items() if key.startswith('week:') or
                  key in ('last_success', 'source_error', 'delivery_attention')}
        public['pending_confirmation'] = any(value for key, value in state.items() if key.startswith('candidate:'))
        (self.state_dir / 'schedule.json').write_text(json.dumps({'schema': 1, 'kv': public}, ensure_ascii=False, sort_keys=True))
        git('add', 'state.json', 'schedule.json', cwd=self.state_dir)
        changed = git('diff', '--cached', '--quiet', cwd=self.state_dir, allowed=(0, 1)).returncode
        if changed:
            git('commit', '-m', 'Persist schedule check state', cwd=self.state_dir)
            git('push', 'origin', 'HEAD:state', cwd=self.state_dir)


def main():
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
        try:
            bot.check()
        except Exception as exc:
            bot.failed_check(exc)
            raise RuntimeError('Check failed: ' + type(exc).__name__) from None


if __name__ == '__main__':
    main()
