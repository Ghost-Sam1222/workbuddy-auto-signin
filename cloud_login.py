"""Separate browser authorization for Actions; never reads desktop/proxy credentials."""
import argparse
import base64
import json
import os
from pathlib import Path
import time
import urllib.parse
import webbrowser

from cloud_signin import API, CloudError, request, validate


def browser_url(url):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ''
    allowed = ('codebuddy.cn', 'workbuddy.cn', 'tencent.com', 'qq.com')
    if parsed.scheme != 'https' or parsed.username or parsed.password or not any(
            host == domain or host.endswith('.' + domain) for domain in allowed):
        raise CloudError('untrusted_authorization_url')
    return url


def login():
    headers = {'Content-Type': 'application/json', 'User-Agent': 'WorkBuddy',
               'Origin': 'https://www.codebuddy.cn', 'Referer': 'https://www.codebuddy.cn/',
               'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}
    result = request(API + '/v2/plugin/auth/state?platform=CLI', headers, {}, 'POST')
    data = result['data']
    url = browser_url(data['authUrl'])
    webbrowser.open(url)
    print('请在官方浏览器页面完成登录；本次授权仅用于云端签到。', flush=True)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        result = request(API + '/v2/plugin/auth/token?' + urllib.parse.urlencode({'state': data['state']}), headers)
        if result.get('code') == 11217:
            time.sleep(2)
            continue
        if result.get('code') != 0:
            raise CloudError('authorization_failed')
        auth = result.get('data') or {}
        token = auth.get('accessToken')
        if not token:
            time.sleep(2)
            continue
        # Decode only to extract the UID, as in the existing browser flow.
        # Authentication is enforced by the remote API, not by this decoding.
        part = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        uid = claims.get('sub')
        session = {'auth': {'accessToken': token, 'refreshToken': auth.get('refreshToken'),
                            'domain': auth.get('domain') or 'copilot.tencent.com', 'endpoint': API},
                   'account': {'uid': uid}}
        validate(session)
        return session
    raise CloudError('authorization_timeout')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='cloud-auth.json')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        path = Path(args.output)
        # Reserve the output before login so existing credentials cannot be overwritten.
        with path.open('x', encoding='utf-8') as file:
            os.chmod(path, 0o600)
            try:
                session = login()
                json.dump(session, file)
            except BaseException:
                path.unlink()
                raise
        print('新授权已保存到指定本机文件；尚未上传或签到。')
        return 0
    except Exception:
        print('授权未完成；未输出任何凭据。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
