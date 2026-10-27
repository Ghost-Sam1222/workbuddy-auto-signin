import base64
import copy
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from cryptography.fernet import Fernet, InvalidToken
import cloud_login
import cloud_setup
import cloud_signin as cloud


def session():
    return {'auth': {'accessToken': 'synthetic-at', 'refreshToken': 'synthetic-rt',
                     'domain': 'copilot.tencent.com'}, 'account': {'uid': 'synthetic-user'}}


class MemoryStore:
    def __init__(self):
        self.state = None
        self.writes = []

    def load(self):
        return copy.deepcopy(self.state)

    def save(self, state):
        self.state = copy.deepcopy(state)
        self.writes.append(copy.deepcopy(state))


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.seed = json.dumps(session())
        self.store = MemoryStore()

    def test_rotation_persisted_before_claim_and_reused_next_run(self):
        with patch.object(cloud, 'request', return_value={
                'code': 0, 'data': {'accessToken': 'new-at', 'refreshToken': 'new-rt'}}) as request:
            def claim(*args):
                self.assertEqual(self.store.state['session']['auth']['refreshToken'], 'new-rt')
                return 0, {'result': 'CLAIMED', 'credit': 100, 'report': 'synthetic-private-data'}
            with patch.object(cloud.signin, 'run_auto', side_effect=claim):
                code, out = cloud.run(self.store, self.seed)
                self.assertEqual(out, {'result': 'CLAIMED', 'needs_attention': False, 'credit': 100})
                self.assertEqual(code, 0)
                cloud.run(self.store, self.seed)
            self.assertEqual(request.call_args.args[1]['X-Refresh-Token'], 'new-rt')

    def test_failed_state_save_prevents_claim(self):
        self.store.save = lambda _: (_ for _ in ()).throw(RuntimeError('write failed'))
        with patch.object(cloud, 'refresh') as refresh, patch.object(cloud.signin, 'run_auto') as claim:
            with self.assertRaises(RuntimeError):
                cloud.run(self.store, self.seed)
            refresh.assert_not_called()
            claim.assert_not_called()

    def test_rotation_save_failure_never_claims(self):
        self.store.save({'seed_id': cloud.hashlib.sha256(self.seed.encode()).hexdigest(), 'session': session()})
        self.store.save = lambda _: (_ for _ in ()).throw(RuntimeError('write failed'))
        with patch.object(cloud, 'refresh', return_value=session()), patch.object(cloud.signin, 'run_auto') as claim:
            with self.assertRaises(RuntimeError):
                cloud.run(self.store, self.seed)
            claim.assert_not_called()

    def test_query_only_refreshes_but_never_claims_or_runs_growth(self):
        with patch.object(cloud, 'refresh', return_value=session()), \
             patch.object(cloud.signin, 'post', return_value=(200, {'active': True, 'today_checked_in': False})) as post, \
             patch.object(cloud.signin, 'run_auto') as claim, patch.object(cloud.signin, 'run_growth') as growth:
            code, out = cloud.run(self.store, self.seed, check_only=True)
            self.assertEqual(out, {'result': 'CHECKED', 'active': True, 'today_signed': False})
            self.assertEqual(code, 0)
            self.assertTrue(post.call_args.args[0].endswith('checkin-activity-status'))
            claim.assert_not_called()
            growth.assert_not_called()

    def test_failed_refresh_preserves_state_and_never_claims(self):
        with patch.object(cloud, 'request', return_value={'code': 401, 'msg': 'private'}), \
             patch.object(cloud.signin, 'run_auto') as claim:
            with self.assertRaises(cloud.CloudError):
                cloud.run(self.store, self.seed)
            self.assertEqual(self.store.state['session'], session())
            claim.assert_not_called()

    def test_business_error_status_never_attempts_claim(self):
        with patch.object(cloud, 'refresh', return_value=session()), \
             patch.object(cloud.signin, 'post', return_value=(200, {'code': 10001})) as post:
            with self.assertRaises(cloud.CloudError):
                cloud.run(self.store, self.seed)
            self.assertEqual(post.call_count, 1)

    def test_http_failure_with_credit_cannot_be_reported_as_success(self):
        replies = [(200, {'active': True, 'today_checked_in': False}),
                   (500, {'credit': 100, 'msg': 'private'})]
        with patch.object(cloud, 'refresh', return_value=session()), \
             patch.object(cloud.signin, 'post', side_effect=replies):
            with self.assertRaises(cloud.CloudError):
                cloud.run(self.store, self.seed)

    def test_main_never_logs_exception_or_credentials(self):
        with patch.object(cloud, 'StateStore', side_effect=RuntimeError('synthetic-secret')), \
             patch('sys.argv', ['cloud_signin.py']), patch('sys.stdout', new_callable=io.StringIO) as stdout:
            self.assertEqual(cloud.main(), 1)
            self.assertNotIn('synthetic-secret', stdout.getvalue())
            self.assertEqual(json.loads(stdout.getvalue())['result'], 'FAILED')

    def test_encrypted_store_roundtrip_and_tamper_rejection(self):
        env = {'GITHUB_REPOSITORY': 'synthetic/repo', 'GH_TOKEN': 'synthetic-github-token',
               'GITHUB_SHA': '1' * 40, 'WB_STATE_KEY': Fernet.generate_key().decode()}
        with patch.dict(os.environ, env):
            store = cloud.StateStore()
        with patch.dict(os.environ, env), \
             patch.object(cloud, 'request', side_effect=[{}, {'content': {'sha': 'blob'}}]) as request:
            store.save({'session': session()})
            payload = request.call_args.args[2]
        encrypted = base64.b64decode(payload['content'])
        self.assertNotIn(b'synthetic-rt', encrypted)
        self.assertNotIn(b'synthetic-user', encrypted)
        reply = {'sha': 'blob', 'content': payload['content']}
        with patch.object(cloud, 'request', side_effect=[{}, reply]):
            self.assertEqual(store.load(), {'session': session()})
        reply['content'] = base64.b64encode(encrypted[:-5] + b'xxxxx').decode()
        with patch.object(cloud, 'request', side_effect=[{}, reply]):
            with self.assertRaises(InvalidToken):
                store.load()

    def test_existing_branch_missing_state_is_not_treated_as_first_login(self):
        env = {'GITHUB_REPOSITORY': 'synthetic/repo', 'GH_TOKEN': 'synthetic-github-token',
               'WB_STATE_KEY': Fernet.generate_key().decode()}
        with patch.dict(os.environ, env):
            store = cloud.StateStore()
        error = urllib.error.HTTPError('https://api.github.com', 404, '', {}, None)
        with patch.object(cloud, 'request', side_effect=[{}, error]):
            with self.assertRaises(urllib.error.HTTPError):
                store.load()

    def test_untrusted_browser_url_rejected(self):
        for url in ('https://codebuddy.cn.attacker.invalid/login', 'http://www.codebuddy.cn/login'):
            with self.assertRaises(cloud.CloudError):
                cloud_login.browser_url(url)
        self.assertEqual(cloud_login.browser_url('https://www.codebuddy.cn/login'), 'https://www.codebuddy.cn/login')

    def test_browser_login_extracts_separate_session_without_claim(self):
        claims = base64.urlsafe_b64encode(b'{"sub":"synthetic-user"}').decode().rstrip('=')
        at = 'header.' + claims + '.signature'
        replies = [{'data': {'authUrl': 'https://www.codebuddy.cn/login', 'state': 'fake-state'}},
                   {'code': 0, 'data': {'accessToken': at, 'refreshToken': 'new-rt'}}]
        with patch.object(cloud_login, 'request', side_effect=replies) as request, \
             patch.object(cloud_login.webbrowser, 'open'), patch('sys.stdout', new_callable=io.StringIO):
            result = cloud_login.login()
            self.assertEqual(result['account']['uid'], 'synthetic-user')
            self.assertEqual(result['auth']['refreshToken'], 'new-rt')
            self.assertEqual(request.call_count, 2)

    def test_setup_sends_secrets_by_stdin_and_does_not_enable_schedule(self):
        with patch.object(cloud_setup, 'login', return_value=session()), \
             patch.object(cloud_setup.subprocess, 'run') as run, \
             patch('sys.argv', ['cloud_setup.py', '--repo', 'synthetic/repo']), \
             patch('sys.stdout', new_callable=io.StringIO) as stdout:
            self.assertEqual(cloud_setup.main(), 0)
            secret_calls = [call for call in run.call_args_list if call.args[0][1:3] == ['secret', 'set']]
            self.assertEqual(len(secret_calls), 2)
            self.assertEqual(json.loads(secret_calls[0].kwargs['input']), session())
            self.assertNotIn('synthetic-rt', repr([call.args for call in run.call_args_list]))
            self.assertNotIn('synthetic-rt', stdout.getvalue())
            self.assertFalse(any('variable' in call.args[0] for call in run.call_args_list))


if __name__ == '__main__':
    unittest.main()
