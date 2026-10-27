# GitHub Actions 云端签到（二次开发）

基于 88lin/workbuddy-auto-signin，保留原版桌面入口；新增独立云端入口。无需安装 WorkBuddy 或 CodeBuddy，无需本机持续开机。仅面向国内个人账号，每天北京时间 09:17 查询签到状态，未签才领取；时间和奖励以平台实际响应为准。

## 改了什么

- 本机浏览器新建一次官方授权，专供云端使用，不读取 Harness 代理或桌面客户端凭据。
- 初始授权与随机状态密钥分别写入 GitHub Secrets：`WB_AUTH_JSON`、`WB_STATE_KEY`。
- 每次运行刷新令牌后，立即用 Fernet 加密完整会话，将密文保存到同仓库 `workbuddy-state` 分支的 `workbuddy-state.enc`。原文不进提交、日志或 artifact。即使公开 fork，分支中也只有授权密文，解密密钥仅在 Secrets。
- 后续运行读取轮换后的会话；不依赖旧刷新令牌始终有效，也不需要个人 PAT 回写 Secrets。`GITHUB_TOKEN` 只要求当前仓库 `contents: write`，用于加密状态分支。
- 全仓库串行运行，不取消正在续期的运行。保存失败时停止，不继续签到。
- 只调用原版 `run_auto` 签到流程，不调用成长中心、抽奖或模型；输出只保留固定结果枚举与数字积分。
- 默认关闭：需要仓库变量 `WB_SIGNIN_ENABLED=true` 才执行；只允许默认分支执行。

## 首次配置

先将包含本改动的分支合并到自己 fork 的默认分支。然后在可信本机安装 Python 3.12 和已登录的 GitHub CLI，并在本目录运行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-actions.txt
.venv/bin/python cloud_setup.py --repo 你的用户名/workbuddy-auto-signin
```

Windows 将解释器路径替换为 `.venv\Scripts\python.exe`。该命令会打开官方浏览器页面，登录完成后直接通过标准输入写入两个 Secrets，不需要复制 token，也不创建明文授权文件。执行它意味着同意把这份新授权交由 GitHub Actions 托管。

在 GitHub 仓库 Settings → Secrets and variables → Actions → Variables 添加 `WB_SIGNIN_ENABLED`，值 `true`。在 Actions 中启用工作流（fork 的 Actions 可能默认停用）。手动运行 **WorkBuddy Cloud Checkin**，先保持 `check_only=true`；此模式仅查询签到，但仍刷新并保存授权。确认返回 `CHECKED` 后，再手动取消该选项进行首次领取。定时触发自动执行领取。

配置 Secrets 与启用定时任务是两个独立步骤。代码测试不包含真实账号请求，首次云端验收仍需上述步骤。

## 停止、续期与恢复

停止：删除或设 `WB_SIGNIN_ENABLED=false`，也可在 Actions 中禁用工作流。取消正在运行的续期可能造成服务端令牌已轮换、密文尚未保存，因此应等待当前运行完成。

正常续期不需要人工操作，但平台可撤销授权、改变接口或拒绝 GitHub 机房请求；本实现不保证令牌永久有效，也不会绕过验证码或风控。

重新授权：先停用任务并等待当前运行结束，删除自己的 `workbuddy-state` 状态分支，再重新运行 `cloud_setup.py`，最后开启查询验收。必须先清理旧状态分支，因为重新配置会生成新的密钥，无法解密旧密文。不能只改密钥而保留旧分支。

状态分支存在但文件丢失、解密失败或接口拒绝时，任务返回失败，不悄悄使用旧令牌。若首次运行恰在创建分支后、写入密文前中断，同样按上述恢复流程处理。

GitHub 调度可能延迟或丢失；公开仓库长时间没有活动时定时工作流可能被停用。密文状态每天更新会产生仓库提交，但不能据此承诺任务永不停止。保存状态与远端刷新之间也无法实现跨服务原子事务，极端中断仍可能需要重新授权。

## 验证与来源

新增测试使用合成会话和模拟 HTTP，无真实签到。验证令牌轮换在签到前落库、保存失败停止、查询不领取、业务错误不误报成功、密文回读及篡改拒绝、浏览器链接白名单与日志不泄露异常内容。

- 原项目：https://github.com/88lin/workbuddy-auto-signin （MIT；本次基线 8c66f4b31676bc238663759872a9592ee0e10723）
- 云端接口交叉参考：https://github.com/123qwe-jl/workbuddy-checkin
- 轮换状态风险交叉参考：https://github.com/L0NE-6/WorkBuddy-Daily
- 浏览器授权协议参考：本机已核对的 workbuddy2api-hub v1.8.0 `wb_accounts.py`。此实现按接口协议独立编写，未引入其自动签到或后台调度。
- GitHub 权限：https://docs.github.com/en/actions/tutorials/authenticate-with-github_token
- GitHub 定时：https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
- 加密库：https://cryptography.io/en/latest/fernet/
