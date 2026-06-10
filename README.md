# Domain Expiry Monitor

域名到期监控脚本。脚本会读取 `domains/` 目录下的 `.txt` 文件，按分组检查域名 whois 到期时间；当剩余天数小于等于阈值时，查询 NS 记录并发送 Telegram 告警。

## 功能

- 批量读取 `domains/*.txt` 域名列表
- 查询域名到期剩余天数
- 到期阈值内触发告警
- 使用 `dig` 查询 NS 记录
- 通过 Telegram Bot 推送提醒

## 环境要求

- Python 3.10+
- 系统需要能执行 `dig` 命令

Windows 可以安装 BIND tools：

```powershell
winget install --id ISC.Bind -e --source winget
```

安装 Python 依赖：

```powershell
pip install -r requirements.txt
```

## 配置

Telegram 配置通过环境变量提供，避免把 token 写进代码：

```powershell
$env:TELEGRAM_BOT_TOKEN="your_bot_token"
$env:TELEGRAM_CHAT_ID="your_chat_id"
```

脚本内主要配置：

```python
THRESHOLD = 14
MAX_WORKERS = 1
SLEEP_INTERVAL = 0.9
```

## 域名列表

在 `domains/` 目录下创建 `.txt` 文件。每个文件代表一个分组，每行一个域名：

```text
example.com
example.net
```

## 运行

```powershell
python .\domain-check.py
```

只有检测到即将到期的域名时，脚本才会发送 Telegram 消息。

## 生产环境 systemd 配置

推荐在 Linux 服务器上使用 `systemd timer` 定时执行脚本。下面示例假设项目部署在：

```text
/opt/domain-expiry-monitor
```

创建环境变量文件：

```bash
sudo mkdir -p /etc/domain-expiry-monitor
sudo nano /etc/domain-expiry-monitor/env
```

内容示例：

```ini
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

限制权限：

```bash
sudo chmod 600 /etc/domain-expiry-monitor/env
```

创建虚拟环境并安装依赖：

```bash
cd /opt/domain-expiry-monitor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

确认系统已安装 `dig`：

```bash
sudo apt-get update
sudo apt-get install -y dnsutils
```

创建 service 文件：

```bash
sudo nano /etc/systemd/system/domain-expiry-monitor.service
```

内容：

```ini
[Unit]
Description=Domain expiry monitor
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/domain-expiry-monitor
EnvironmentFile=/etc/domain-expiry-monitor/env
ExecStart=/opt/domain-expiry-monitor/.venv/bin/python /opt/domain-expiry-monitor/domain-check.py
```

创建 timer 文件：

```bash
sudo nano /etc/systemd/system/domain-expiry-monitor.timer
```

内容：

```ini
[Unit]
Description=Run domain expiry monitor daily

[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true
Unit=domain-expiry-monitor.service

[Install]
WantedBy=timers.target
```

启用定时任务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now domain-expiry-monitor.timer
```

手动测试：

```bash
sudo systemctl start domain-expiry-monitor.service
sudo systemctl status domain-expiry-monitor.service
```

查看日志：

```bash
journalctl -u domain-expiry-monitor.service -n 100 --no-pager
```

## 注意

- 不要把 `.env`、token、私钥等敏感信息提交到仓库。
- 如果 `dig` 不在 PATH 中，NS 查询会失败。
- whois 查询可能受注册商限制，脚本内已经做了简单重试。
