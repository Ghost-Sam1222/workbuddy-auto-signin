"""Actions adapter: refresh, persist encrypted credentials, then check in only."""
import argparse
import base64
import hashlib
import json
import os
import urllib.error
import urllib.request

from cryptography.fernet import Fernet
import signin

BRANCH = 'workbuddy-state'
STATE_PATH = 'workbuddy-state.enc'
API = 'https://copilot.tencent.com'


class CloudError(Exception):
    pass


def request(url, headers, payload=None, method=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


class StateStore:
    def __init__(self):
        self.base = 'https://api.github.com/repos/' + os.environ['GITHUB_REPOSITORY']
        self.headers = {'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                        'Accept': 'application/vnd.github+json',
                        'Content-Type': 'application/json', 'User-Agent': 'workbuddy-actions'}
        self.cipher = Fernet(os.environ['WB_STATE_KEY'].encode())
        self.sha = None
        self.branch_exists = False

    def load(self):
        try:
            request(self.base + '/git/ref/heads/' + BRANCH, self.headers)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise
        self.branch_exists = True
        # A missing/corrupt file on an existing state branch fails closed.
        data = request(self.base + '/contents/' + STATE_PATH + '?ref=' + BRANCH, self.headers)
        self.sha = data['sha']
        encrypted = base64.b64decode(data['content'])
        return json.loads(self.cipher.decrypt(encrypted))

    def save(self, state):
        encrypted = self.cipher.encrypt(json.dumps(state).encode())
        if not self.branch_exists:
            request(self.base + '/git/refs', self.headers,
                    {'ref': 'refs/heads/' + BRANCH, 'sha': os.environ['GITHUB_SHA']}, 'POST')
            self.branch_exists = True
        body = {'message': 'Update encrypted WorkBuddy session', 'branch': BRANCH,
                'content': base64.b64encode(encrypted).decode()}
        if self.sha:
            body['sha'] = self.sha
        saved = request(self.base + '/contents/' + STATE_PATH, self.headers, body, 'PUT')
        self.sha = saved['content']['sha']


def validate(session):
    signin._validate_session(session)
    if session['account'].get('enterpriseId'):
        raise CloudError('personal_account_required')
    if not signin._valid_token(session['auth'].get('refreshToken')):
        raise CloudError('plaintext_refresh_token_required')
    if session['auth'].get('domain') not in (None, '', 'copilot.tencent.com',
                                            'www.codebuddy.cn', 'www.workbuddy.cn'):
        raise CloudError('china_account_required')


def refresh(session):
    headers = signin.build_headers(session)
    headers.pop('Authorization')
    headers.update({'X-Refresh-Token': session['auth']['refreshToken'],
                    'X-Auth-Refresh-Source': 'workbuddy', 'X-CodeBuddy-Request': '1'})
    body = request(API + '/v2/plugin/auth/token/refresh', headers, {}, 'POST')
    if body.get('code') != 0:
        raise CloudError('refresh_rejected')
    data = body.get('data') or {}
    data = data.get('data') or data
    token = data.get('accessToken')
    if not signin._valid_token(token):
        raise CloudError('refresh_missing_access_token')
    result = dict(session, auth=dict(session['auth'], accessToken=token,
                                     refreshToken=data.get('refreshToken') or session['auth']['refreshToken']))
    validate(result)
    return result


def run(store, seed, check_only=False):
    initial = json.loads(seed)
    validate(initial)
    seed_id = hashlib.sha256(seed.encode()).hexdigest()
    stored = store.load()
    session = stored['session'] if stored and stored.get('seed_id') == seed_id else initial
    validate(session)
    # Check write permission and encryption before consuming a refresh token.
    if not stored or stored.get('seed_id') != seed_id:
        store.save({'seed_id': seed_id, 'session': session})
    updated = refresh(session)
    # Persist rotation before any checkin, including when checkin later fails.
    store.save({'seed_id': seed_id, 'session': updated})
    signin._start_budget('auto')
    headers = signin.build_headers(updated)
    original_post = signin.post

    def checked_post(*args, **kwargs):
        code, body = original_post(*args, **kwargs)
        if code >= 300 and not (code == 400 and signin._is_already_checked_in(body)):
            raise CloudError('checkin_http_failed')
        if 200 <= code < 300 and (not isinstance(body, dict) or body.get('code', 0) != 0):
            raise CloudError('checkin_business_failed')
        return code, body

    signin.post = checked_post
    try:
        if check_only:
            code, body = signin.post(API + '/v2/billing/meter/checkin-activity-status', headers, retry=True)
            if not 200 <= code < 300:
                raise CloudError('query_failed')
            active = signin.dig(body, 'active')
            signed = signin.dig(body, 'today_checked_in')
            if not isinstance(active, bool) or signed not in (True, False, 0, 1):
                raise CloudError('query_unrecognized')
            return 0, {'result': 'CHECKED', 'active': active, 'today_signed': bool(signed)}
        code, out = signin.run_auto(headers, API)
        # Only fixed enum values and numeric totals may enter public logs.
        allowed = {'CLAIMED', 'ALREADY', 'INACTIVE', 'NETWORK', 'TIMEOUT',
                   'AUTH_REJECTED', 'FORBIDDEN', 'ERROR', 'UNKNOWN'}
        result = out.get('result')
        safe = {'result': result if result in allowed else 'ERROR', 'needs_attention': code != 0}
        for key in ('credit', 'streak_days', 'total_credits'):
            if type(out.get(key)) in (int, float):
                safe[key] = out[key]
        return code, safe
    finally:
        signin.post = original_post


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    try:
        code, out = run(StateStore(), os.environ['WB_AUTH_JSON'], args.check_only)
    except Exception:
        # Exceptions and upstream bodies can contain credentials; never print them.
        code, out = 1, {'result': 'FAILED', 'needs_attention': True,
                        'hint': '检查授权、状态密钥、仓库写权限及平台是否接受云端请求'}
    print(json.dumps(out, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
