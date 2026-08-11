# Domain Expiry Monitor

域名到期监控和管理工具。项目包含两部分：

- `domain-check.py`：原有命令行检测脚本，保留兼容。
- `app.py`：Flask Web 控制台，用于管理域名、查看 Dashboard、配置定时检测和发送 Telegram 预警。

## Web 控制台功能

- 登录保护。
- 按 GoDaddy 账户 / 本地分组列出域名。
- 批量添加域名、批量删除域名。
- 修改域名备注。
- 搜索域名或备注。
- 查询 whois 到期时间和剩余天数。
- 查询 DNS / NS 状态并单独展示。
- Dashboard 显示总数、正常、预警、已过期、DNS 失效、NS 失效、检测错误。
- 状态列显示域名综合状态：到期预警 / 已过期优先，其次显示 DNS 失效、NS 失效、检测错误。
- 只按“剩余天数”触发 Telegram；DNS / NS 异常会显示状态标签，但不会发送 Telegram 告警。
- 导出 CSV。
- 前端配置每日自动检测时间、预警阈值、Telegram Bot Token 和群 ID。
- “刷新状态”会重新检测域名。
- “发送当前预警到 Telegram”只发送当前缓存里的预警域名，不重新检测全部域名。

## 安装依赖

```powershell
pip install -r requirements.txt
```

依赖包括：

- Flask
- python-whois
- requests

系统还需要可执行 `dig` 命令，用于查询 NS。

Windows 可安装 BIND tools：

```powershell
winget install --id ISC.Bind -e --source winget
```

## 启动 Web 控制台

设置登录密码和 Flask secret：

```powershell
$env:DOMAIN_CHECK_PASSWORD="your_password"
$env:DOMAIN_CHECK_SECRET_KEY="random_secret"
python .\app.py
```

默认地址：

```text
http://服务器IP:5000
```

默认监听 `0.0.0.0`。如果只想本机访问，可以指定 `HOST=127.0.0.1`。

如果端口被占用，可指定其它端口：

```powershell
$env:HOST="127.0.0.1"
$env:PORT="5001"
python .\app.py
```

如果没有设置 `DOMAIN_CHECK_PASSWORD`，默认密码是 `admin`，只建议本地临时使用。

## GoDaddy 配置

配置一个 GoDaddy 账户：

```powershell
$env:GODADDY_ACCOUNT_NAME="kai87319752"
$env:GODADDY_API_KEY="your_key"
$env:GODADDY_API_SECRET="your_secret"
```

配置多个 GoDaddy 账户：

```powershell
$env:GODADDY_ACCOUNTS='[{"name":"account-a","key":"key","secret":"secret"},{"name":"account-b","key":"key","secret":"secret"}]'
```

前端点击“同步 GoDaddy”会把远端域名合并进对应的 `domains/<account>.txt`。

## Telegram 配置

Telegram 配置可以在 Web 前端填写：

- Bot Token
- Telegram 群 ID，例如 `-1001234567890`
- Telegram @提醒，例如 `@bwops`，会追加在告警消息末尾
- 是否启用 Telegram
- 是否校验 Telegram SSL 证书

配置会保存到本地：

```text
domains/.settings.json
```

该文件已加入 `.gitignore`，不要提交到 GitHub。

如果本机访问 Telegram 出现 `CERTIFICATE_VERIFY_FAILED`，可以在前端关闭“校验 Telegram SSL 证书”。关闭后会使用不校验证书的 HTTPS 请求。

## 预警规则

预警只看域名剩余天数：

```text
剩余天数 < 预警阈值
```

例如阈值是 `16`：

- 剩余 `15` 天：预警
- 剩余 `16` 天：正常
- 剩余 `35` 天但 DNS / NS 异常：状态显示 DNS 失效、NS 失效或检测错误，但不发送 Telegram 告警

Telegram 只发送预警或已过期相关域名，不会因为 DNS / NS 异常发送。
如果配置了 Telegram @提醒，告警消息末尾会额外追加该提醒内容。

## 状态说明

- 正常：到期天数正常，DNS / NS 检测正常。
- 预警：剩余天数小于前端配置的预警阈值。
- 已过期：剩余天数小于 `0`。
- DNS 失效：DNS 查询失败或超时。
- NS 失效：DNS 查询成功，但没有可用 NS 记录。
- 检测错误：本机缺少 `dig` 或检测过程出现其它异常。
- 未检测：还没有取得该域名的到期天数。

## 本地数据文件

域名列表保存在：

```text
domains/*.txt
```

每个 `.txt` 文件代表一个账户或分组，每行一个域名：

```text
example.com
example.net
```

`domains/*.txt` 是生产数据，已加入 `.gitignore`，不要提交到 GitHub。仓库只保留 `domains/example.txt` 作为格式示例。

Web 控制台生成的本地文件：

```text
domains/.settings.json      # 前端配置，包含 Telegram 配置，已忽略
domains/.status-cache.json  # 检测状态缓存，已忽略
domains/.metadata.json      # 域名备注，已忽略
```

如果从旧版本升级，生产上的 `domains/*.txt` 之前可能还被 Git 跟踪。第一次拉取“停止跟踪域名数据”的版本前，先备份并在拉取后恢复：

```bash
cd /data/app/domain-expiry-monitor
cp -a domains domains.bak.$(date +%F-%H%M%S)
git pull --ff-only
cp -a domains.bak.时间/. domains/
```

## 命令行脚本

仍可运行原有脚本：

```powershell
python .\domain-check.py
```

原脚本会读取 `domains/*.txt`，检查 whois 到期时间，并在触发阈值时发送 Telegram。原脚本的 Telegram 配置来自环境变量：

```powershell
$env:TELEGRAM_BOT_TOKEN="your_bot_token"
$env:TELEGRAM_CHAT_ID="your_chat_id"
```

## 生产运行建议

如果使用 Web 控制台的定时检测，不需要 crontab，但必须保持 `app.py` 进程常驻。

自动检测是否执行可以从两处查看：

- Web 控制台会分别显示“上次自动检测”、“上次手动检测”和“上次 Telegram 发送”。
- systemd 日志会记录自动检测开始和结束：

```bash
journalctl -u domain-expiry-monitor.service --since "today" --no-pager
```

如果今天已经有 `lastAutoRunDate` 记录，自动检测当天不会再次执行；修改执行时间只会影响下一次未执行的日期。

Linux 推荐用 systemd 运行 Web 服务，例如：

```ini
[Unit]
Description=Domain Expiry Monitor Web
Wants=network-online.target
After=network-online.target

[Service]
WorkingDirectory=/opt/domain-expiry-monitor
Environment=DOMAIN_CHECK_PASSWORD=your_password
Environment=DOMAIN_CHECK_SECRET_KEY=random_secret
Environment=HOST=0.0.0.0
Environment=PORT=5000
ExecStart=/opt/domain-expiry-monitor/.venv/bin/python /opt/domain-expiry-monitor/app.py
Restart=always

[Install]
WantedBy=multi-user.target
```

然后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now domain-expiry-monitor-web.service
```

## 注意事项

- 不要提交 `.env`、`domains/.settings.json`、token、私钥等敏感信息。
- 如果 `dig` 不在 PATH 中，DNS / NS 查询会失败，但不会触发域名到期预警。
- whois 查询可能受注册商限流影响，域名很多时刷新状态会比较慢。
- “发送当前预警到 Telegram”不会重新检测域名，只发送缓存中的当前预警结果。
