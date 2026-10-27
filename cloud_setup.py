"""Explicit opt-in: authorize a new session and send it directly to repo Secrets."""
import argparse
import json
import re
import subprocess

from cryptography.fernet import Fernet
from cloud_login import login


def main():
    parser = argparse.ArgumentParser(description='浏览器独立授权，并将签到凭据上传到指定 GitHub 仓库 Secrets')
    parser.add_argument('--repo', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
        parser.error('仓库格式应为 owner/name')
    try:
        subprocess.run(['gh', 'repo', 'view', args.repo, '--json', 'name'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        session = login()
        values = {'WB_AUTH_JSON': json.dumps(session), 'WB_STATE_KEY': Fernet.generate_key().decode()}
        for name, value in values.items():
            subprocess.run(['gh', 'secret', 'set', name, '--repo', args.repo],
                           input=value.encode(), check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print('两个 Secrets 已保存；没有落地明文文件，也没有启用定时签到。')
        print('请手动运行 check_only=true 验证，无需启用 WB_SIGNIN_ENABLED。')
        print('重新授权时须先处理旧 workbuddy-state 分支，详见 CLOUD_ACTIONS.md。')
        return 0
    except Exception:
        print('配置未完成；未输出凭据。定时任务保持原设置，请检查 GitHub 权限后重试。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
