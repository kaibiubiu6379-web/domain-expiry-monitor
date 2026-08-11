import csv
import io
import json
import os
import re
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import requests
import whois
from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

try:
    import certifi
except ImportError:
    certifi = None


BASE_DIR = Path(__file__).resolve().parent
DOMAIN_DIR = BASE_DIR / "domains"
METADATA_PATH = DOMAIN_DIR / ".metadata.json"
STATUS_CACHE_PATH = DOMAIN_DIR / ".status-cache.json"
SETTINGS_PATH = DOMAIN_DIR / ".settings.json"
APP_PASSWORD = os.getenv("DOMAIN_CHECK_PASSWORD", "admin")
SECRET_KEY = os.getenv("DOMAIN_CHECK_SECRET_KEY", "change-me-in-production")
GODADDY_BASE_URL = os.getenv("GODADDY_BASE_URL", "https://api.godaddy.com")
WHOIS_TIMEOUT = int(os.getenv("DOMAIN_CHECK_WHOIS_TIMEOUT", "8"))
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$")
DEFAULT_SETTINGS = {
    "scheduleEnabled": False,
    "scheduleTime": "09:00",
    "thresholdDays": 14,
    "telegramEnabled": False,
    "telegramBotToken": "",
    "telegramChatId": "",
    "telegramMention": "@bwops",
    "telegramVerifySsl": True,
    "lastRunDate": "",
    "lastRunAt": "",
    "lastResult": "",
    "lastAutoRunDate": "",
    "lastAutoRunAt": "",
    "lastAutoResult": "",
    "lastManualRunAt": "",
    "lastManualResult": "",
    "lastTelegramAt": "",
    "lastTelegramResult": "",
    "lastTelegramMessageId": "",
}

app = Flask(__name__)
app.secret_key = SECRET_KEY
scheduler_lock = threading.Lock()
scheduler_started = False


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def require_auth(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login"))
        return func(*args, **kwargs)

    return wrapper


def ensure_dirs():
    DOMAIN_DIR.mkdir(exist_ok=True)


def normalize_account(value):
    value = (value or "").strip()
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value)
    value = value.strip(".-")
    if not value:
        raise ValueError("账户名不能为空")
    return value


def normalize_domain(value):
    domain = (value or "").strip().lower().rstrip(".")
    if not DOMAIN_RE.match(domain):
        raise ValueError(f"无效域名: {value}")
    return domain


def account_path(account):
    return DOMAIN_DIR / f"{normalize_account(account)}.txt"


def read_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def load_metadata():
    return read_json(METADATA_PATH, {})


def save_metadata(data):
    write_json(METADATA_PATH, data)


def load_status_cache():
    return read_json(STATUS_CACHE_PATH, {})


def save_status_cache(data):
    write_json(STATUS_CACHE_PATH, data)


def refresh_cached_statuses(threshold_days=None):
    threshold_days = int(threshold_days or load_settings().get("thresholdDays") or 14)
    cache = load_status_cache()
    changed = False
    for item in cache.values():
        if "daysLeft" not in item:
            continue
        normalized_dns_status = normalize_dns_status(item.get("dnsStatus"), item.get("ns"))
        new_status = status_label(item.get("daysLeft"), normalized_dns_status, threshold_days, item.get("ns"))
        if item.get("dnsStatus") != normalized_dns_status:
            item["dnsStatus"] = normalized_dns_status
            changed = True
        if item.get("status") != new_status:
            item["status"] = new_status
            changed = True
    if changed:
        save_status_cache(cache)
    return cache


def load_settings(include_secret=True):
    settings = DEFAULT_SETTINGS | read_json(SETTINGS_PATH, {})
    if not settings.get("telegramBotToken"):
        settings["telegramBotToken"] = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not settings.get("telegramChatId"):
        settings["telegramChatId"] = os.getenv("TELEGRAM_CHAT_ID", "")
    sanitize_settings_messages(settings)
    migrate_legacy_run_fields(settings)
    if not include_secret and settings.get("telegramBotToken"):
        settings["telegramBotTokenConfigured"] = True
        settings["telegramBotToken"] = ""
    return settings


def save_settings(data):
    settings = DEFAULT_SETTINGS | data
    sanitize_settings_messages(settings)
    write_json(SETTINGS_PATH, settings)


def sanitize_settings_messages(settings):
    token = settings.get("telegramBotToken", "")
    for key in ("lastResult", "lastAutoResult", "lastManualResult", "lastTelegramResult"):
        if settings.get(key):
            settings[key] = sanitize_telegram_error(settings[key], token)


def migrate_legacy_run_fields(settings):
    legacy_result = settings.get("lastResult") or ""
    legacy_at = settings.get("lastRunAt") or ""
    if legacy_result.startswith("当前缓存") and not settings.get("lastTelegramAt"):
        settings["lastTelegramAt"] = legacy_at
        settings["lastTelegramResult"] = legacy_result
    elif legacy_result.startswith("已检测") and not settings.get("lastAutoRunAt"):
        settings["lastAutoRunDate"] = settings.get("lastRunDate") or ""
        settings["lastAutoRunAt"] = legacy_at
        settings["lastAutoResult"] = legacy_result


def load_domains():
    ensure_dirs()
    data = {}
    for path in sorted(DOMAIN_DIR.glob("*.txt")):
        domains = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                domains.append(line.lower())
        data[path.stem] = sorted(set(domains))
    return data


def save_account_domains(account, domains):
    path = account_path(account)
    unique_domains = sorted({normalize_domain(domain) for domain in domains})
    path.write_text("\n".join(unique_domains) + ("\n" if unique_domains else ""), encoding="utf-8")


def godaddy_accounts():
    raw_accounts = os.getenv("GODADDY_ACCOUNTS")
    if raw_accounts:
        try:
            accounts = json.loads(raw_accounts)
            return [
                {
                    "name": normalize_account(item["name"]),
                    "key": item["key"],
                    "secret": item["secret"],
                }
                for item in accounts
                if item.get("name") and item.get("key") and item.get("secret")
            ]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return []

    key = os.getenv("GODADDY_API_KEY")
    secret = os.getenv("GODADDY_API_SECRET")
    name = os.getenv("GODADDY_ACCOUNT_NAME", "godaddy")
    if key and secret:
        return [{"name": normalize_account(name), "key": key, "secret": secret}]
    return []


def account_credentials(account):
    for item in godaddy_accounts():
        if item["name"] == normalize_account(account):
            return item
    return None


def godaddy_headers(credentials):
    return {
        "Authorization": f"sso-key {credentials['key']}:{credentials['secret']}",
        "Accept": "application/json",
    }


def fetch_godaddy_domains(account):
    credentials = account_credentials(account)
    if not credentials:
        raise ValueError("未找到该 GoDaddy 账户的 API 配置")

    domains = []
    marker = None
    while True:
        params = {"limit": 100}
        if marker:
            params["marker"] = marker
        response = requests.get(
            f"{GODADDY_BASE_URL}/v1/domains",
            headers=godaddy_headers(credentials),
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        page = response.json()
        if not page:
            break
        domains.extend(item["domain"].lower() for item in page if item.get("domain"))
        if len(page) < 100:
            break
        marker = page[-1].get("domain")
        if not marker:
            break
    return sorted(set(domains))


def get_expiration(domain):
    previous_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(WHOIS_TIMEOUT)
        info = whois.whois(domain)
        expire = info.expiration_date
        if isinstance(expire, list):
            expire = expire[0]
        if not expire:
            return None, None
        if expire.tzinfo is None:
            expire = expire.replace(tzinfo=timezone.utc)
        days = (expire - datetime.now(timezone.utc)).days
        return expire.date().isoformat(), days
    except Exception as exc:
        return None, None
    finally:
        socket.setdefaulttimeout(previous_timeout)


def get_ns(domain):
    try:
        result = subprocess.run(
            ["dig", domain, "NS", "+short", "+time=2", "+tries=1"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        output = result.stdout.strip()
        if output:
            return "ok", output.replace("\n", ", ")
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"dig exited with {result.returncode}"
            return "dns_failed", detail
        return "ns_failed", "No NS records"
    except subprocess.TimeoutExpired:
        return "dns_failed", "DNS query timed out"
    except FileNotFoundError:
        return "check_error", "dig command not found"
    except Exception as exc:
        return "check_error", str(exc)


def normalize_dns_status(dns_status, ns_info=""):
    if not dns_status:
        return None
    if dns_status in {"ok", "dns_failed", "ns_failed", "check_error"}:
        return dns_status

    # Backward compatibility for older cache entries.
    text = (ns_info or "").lower()
    if dns_status == "dns_error":
        if "servfail" in text or "no ns" in text:
            return "ns_failed"
        if "timed out" in text or "timeout" in text:
            return "dns_failed"
        return "check_error"
    return "check_error"


def status_label(days, dns_status, threshold_days=None, ns_info=""):
    threshold_days = int(threshold_days or load_settings().get("thresholdDays") or 14)
    if days is None:
        return "unknown"
    if days < 0:
        return "expired"
    if days < threshold_days:
        return "warning"
    normalized_dns_status = normalize_dns_status(dns_status, ns_info)
    if normalized_dns_status and normalized_dns_status != "ok":
        return normalized_dns_status
    return "ok"


def check_domain_item(account, domain):
    threshold_days = int(load_settings().get("thresholdDays") or 14)
    expires_at, days_left = get_expiration(domain)
    dns_status, ns = get_ns(domain)
    return {
        "account": account,
        "domain": domain,
        "expiresAt": expires_at,
        "daysLeft": days_left,
        "dnsStatus": dns_status,
        "ns": ns,
        "status": status_label(days_left, dns_status, threshold_days, ns),
        "checkedAt": now_iso(),
    }


def run_domain_checks(items=None):
    domains_by_account = load_domains()
    if not items:
        items = [
            {"account": account, "domain": domain}
            for account, domains in domains_by_account.items()
            for domain in domains
        ]

    cache = load_status_cache()
    rows = []
    for item in items:
        account = normalize_account(item["account"])
        domain = normalize_domain(item["domain"])
        row = check_domain_item(account, domain)
        cache[f"{account}/{domain}"] = {
            "expiresAt": row["expiresAt"],
            "daysLeft": row["daysLeft"],
            "dnsStatus": row["dnsStatus"],
            "ns": row["ns"],
            "status": row["status"],
            "checkedAt": row["checkedAt"],
        }
        rows.append(row)
        time.sleep(0.25)
    save_status_cache(cache)
    return rows


def alert_rows(rows, threshold_days):
    alerts = []
    for row in rows:
        days_left = row["daysLeft"]
        if days_left is None:
            continue
        if days_left < threshold_days:
            alerts.append(row)
    return sorted(alerts, key=lambda item: (item["account"], item["daysLeft"], item["domain"]))


def format_alert_message(rows, checked_count, threshold_days, mention=""):
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    grouped = {}
    for row in rows:
        grouped.setdefault(row["account"], []).append(row)

    lines = [
        "域名到期提醒",
        "",
        f"检测时间: {started_at}",
        f"域名总数: {checked_count}",
        f"预警阈值: {threshold_days} 天",
        f"告警数量: {len(rows)}",
        "",
    ]
    for account, items in grouped.items():
        lines.append(f"=== {account} ===")
        for item in items:
            ns = item["ns"] or "-"
            lines.append(
                f"{item['domain']} 剩余 {item['daysLeft']} 天 到期 {item['expiresAt'] or '-'} NS: {ns}"
            )
        lines.append("")
    mention = (mention or "").strip()
    if mention:
        lines.append(mention)
    return "\n".join(lines).strip()


def sanitize_telegram_error(value, token=""):
    text = str(value)
    if token:
        text = text.replace(token, "***")
    text = re.sub(r"/bot[^/]+/sendMessage", "/bot***/sendMessage", text)
    return text


def send_telegram_message(settings, message):
    if not settings.get("telegramEnabled"):
        return False, "Telegram 未启用"
    token = settings.get("telegramBotToken")
    chat_id = settings.get("telegramChatId")
    if not token or not chat_id:
        return False, "Telegram token 或群 ID 未配置"
    try:
        session = requests.Session()
        session.trust_env = False
        verify_ssl = bool(settings.get("telegramVerifySsl", True))
        verify = certifi.where() if verify_ssl and certifi else verify_ssl
        response = session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
            timeout=15,
            verify=verify,
        )
        if response.status_code == 200:
            payload = response.json()
            message_id = payload.get("result", {}).get("message_id")
            if message_id:
                return True, f"Telegram 发送成功，message_id={message_id}"
            return True, "Telegram 发送成功"
        detail = sanitize_telegram_error(response.text[:300] if response.text else "", token)
        return False, f"Telegram 发送失败: HTTP {response.status_code} {detail}".strip()
    except Exception as exc:
        return False, f"Telegram 发送失败: {sanitize_telegram_error(exc, token)}"


def mark_check_started(source):
    settings = load_settings()
    now_date = datetime.now().strftime("%Y-%m-%d")
    now_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if source == "auto":
        settings["lastAutoRunDate"] = now_date
        settings["lastAutoRunAt"] = now_time
        settings["lastAutoResult"] = "正在执行自动检测"
        settings["lastRunDate"] = now_date
        settings["lastRunAt"] = now_time
        settings["lastResult"] = "正在执行自动检测"
    else:
        settings["lastManualRunAt"] = now_time
        settings["lastManualResult"] = "正在执行手动检测"
    save_settings(settings)


def scheduled_check(send_notification=True, source="auto"):
    mark_check_started(source)
    settings = load_settings()
    threshold_days = int(settings.get("thresholdDays") or 14)
    rows = run_domain_checks()
    alerts = alert_rows(rows, threshold_days)
    result = f"已检测 {len(rows)} 个域名，告警 {len(alerts)} 个"

    telegram_sent = False
    if send_notification and alerts:
        telegram_sent, message = send_telegram_message(
            settings,
            format_alert_message(alerts, len(rows), threshold_days, settings.get("telegramMention")),
        )
        result = f"{result}；{message}"
    elif send_notification:
        result = f"{result}；无告警，未发送 Telegram"

    now_date = datetime.now().strftime("%Y-%m-%d")
    now_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if source == "auto":
        settings["lastAutoRunDate"] = now_date
        settings["lastAutoRunAt"] = now_time
        settings["lastAutoResult"] = result
        settings["lastRunDate"] = now_date
        settings["lastRunAt"] = now_time
        settings["lastResult"] = result
    else:
        settings["lastManualRunAt"] = now_time
        settings["lastManualResult"] = result
    save_settings(settings)
    return rows, alerts, result, telegram_sent


def send_cached_alerts():
    settings = load_settings()
    threshold_days = int(settings.get("thresholdDays") or 14)
    rows = build_domain_rows()
    alerts = alert_rows(rows, threshold_days)
    if not alerts:
        result = f"当前无小于 {threshold_days} 天的域名预警，未发送 Telegram"
        telegram_sent = False
    else:
        telegram_sent, message = send_telegram_message(
            settings,
            format_alert_message(alerts, len(rows), threshold_days, settings.get("telegramMention")),
        )
        result = f"当前缓存告警 {len(alerts)} 个；{message}"

    settings["lastTelegramAt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    settings["lastTelegramResult"] = result
    save_settings(settings)
    return rows, alerts, result, telegram_sent


def scheduler_loop():
    while True:
        try:
            settings = load_settings()
            if settings.get("scheduleEnabled"):
                today = datetime.now().strftime("%Y-%m-%d")
                current_time = datetime.now().strftime("%H:%M")
                schedule_time = settings.get("scheduleTime") or "09:00"
                legacy_is_check = (settings.get("lastResult") or "").startswith("已检测")
                last_auto_run_date = settings.get("lastAutoRunDate") or (
                    settings.get("lastRunDate") if legacy_is_check else ""
                )
                if current_time >= schedule_time and last_auto_run_date != today:
                    if scheduler_lock.acquire(blocking=False):
                        try:
                            print(f"[scheduler] auto check started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
                            rows, alerts, result, telegram_sent = scheduled_check(
                                send_notification=True,
                                source="auto",
                            )
                            print(
                                f"[scheduler] auto check finished checked={len(rows)} alerts={len(alerts)} telegram_sent={telegram_sent} result={result}",
                                flush=True,
                            )
                        finally:
                            scheduler_lock.release()
            time.sleep(30)
        except Exception as exc:
            settings = load_settings()
            now_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            settings["lastAutoRunAt"] = settings.get("lastAutoRunAt") or now_time
            settings["lastAutoResult"] = f"定时任务失败: {exc}"
            settings["lastResult"] = settings["lastAutoResult"]
            save_settings(settings)
            time.sleep(60)


def start_scheduler():
    global scheduler_started
    if scheduler_started:
        return
    scheduler_started = True
    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()


def build_domain_rows():
    domains_by_account = load_domains()
    metadata = load_metadata()
    cache = load_status_cache()
    threshold_days = int(load_settings().get("thresholdDays") or 14)
    rows = []
    for account, domains in domains_by_account.items():
        for domain in domains:
            key = f"{account}/{domain}"
            status = cache.get(key, {})
            days_left = status.get("daysLeft")
            dns_status = normalize_dns_status(status.get("dnsStatus"), status.get("ns"))
            rows.append(
                {
                    "account": account,
                    "domain": domain,
                    "note": metadata.get(key, {}).get("note", ""),
                    "expiresAt": status.get("expiresAt"),
                    "daysLeft": days_left,
                    "dnsStatus": dns_status,
                    "ns": status.get("ns"),
                    "status": status_label(days_left, dns_status, threshold_days, status.get("ns")),
                    "checkedAt": status.get("checkedAt"),
                }
            )
    return rows


@app.get("/")
@require_auth
def index():
    return render_template("index.html")


@app.get("/login")
def login():
    if session.get("authenticated"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    if data.get("password") == APP_PASSWORD:
        session["authenticated"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "密码错误"}), 401


@app.post("/api/logout")
@require_auth
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/accounts")
@require_auth
def api_accounts():
    local_accounts = sorted(load_domains().keys())
    configured = sorted(item["name"] for item in godaddy_accounts())
    accounts = sorted(set(local_accounts + configured))
    return jsonify(
        {
            "accounts": [
                {
                    "name": account,
                    "configured": account in configured,
                    "domainCount": len(load_domains().get(account, [])),
                }
                for account in accounts
            ]
        }
    )


@app.get("/api/domains")
@require_auth
def api_domains():
    rows = build_domain_rows()
    account = request.args.get("account")
    query = (request.args.get("q") or "").strip().lower()
    query_terms = [term for term in re.split(r"[\s,;，；]+", query) if term]
    status = request.args.get("status")
    if account:
        rows = [row for row in rows if row["account"] == account]
    if query_terms:
        rows = [
            row
            for row in rows
            if any(term in row["domain"] or term in row["note"].lower() for term in query_terms)
        ]
    if status and status != "all":
        rows = [row for row in rows if row["status"] == status]
    return jsonify({"domains": rows})


@app.get("/api/settings")
@require_auth
def api_get_settings():
    return jsonify({"settings": load_settings(include_secret=False)})


@app.patch("/api/settings")
@require_auth
def api_update_settings():
    data = request.get_json(silent=True) or {}
    current = load_settings()
    settings = current.copy()

    if "scheduleEnabled" in data:
        settings["scheduleEnabled"] = bool(data.get("scheduleEnabled"))
    if "scheduleTime" in data:
        settings["scheduleTime"] = str(data.get("scheduleTime") or current.get("scheduleTime") or "09:00")
    if "thresholdDays" in data:
        threshold_days = int(data.get("thresholdDays") or current.get("thresholdDays") or 14)
        settings["thresholdDays"] = max(1, min(365, threshold_days))
    if "telegramEnabled" in data:
        settings["telegramEnabled"] = bool(data.get("telegramEnabled"))
    if "telegramChatId" in data:
        settings["telegramChatId"] = str(data.get("telegramChatId") or "")
    if "telegramMention" in data:
        settings["telegramMention"] = str(data.get("telegramMention") or "")
    if "telegramVerifySsl" in data:
        settings["telegramVerifySsl"] = bool(data.get("telegramVerifySsl"))
    if data.get("telegramBotToken"):
        settings["telegramBotToken"] = str(data["telegramBotToken"])

    save_settings(settings)
    refresh_cached_statuses(settings.get("thresholdDays"))
    return jsonify({"ok": True, "settings": load_settings(include_secret=False)})


@app.post("/api/scheduler/run")
@require_auth
def api_run_scheduler_now():
    if not scheduler_lock.acquire(blocking=False):
        return jsonify({"error": "检测任务正在运行"}), 409
    try:
        rows, alerts, result, telegram_sent = scheduled_check(send_notification=True, source="manual")
    finally:
        scheduler_lock.release()
    return jsonify(
        {
            "ok": True,
            "checked": len(rows),
            "alerts": len(alerts),
            "telegramSent": telegram_sent,
            "result": result,
        }
    )


@app.post("/api/telegram/send")
@require_auth
def api_send_telegram_now():
    if not scheduler_lock.acquire(blocking=False):
        return jsonify({"error": "检测/发送任务正在运行"}), 409
    try:
        rows, alerts, result, telegram_sent = send_cached_alerts()
    finally:
        scheduler_lock.release()
    return jsonify(
        {
            "ok": True,
            "checked": len(rows),
            "alerts": len(alerts),
            "telegramSent": telegram_sent,
            "result": result,
        }
    )


@app.post("/api/domains")
@require_auth
def api_add_domains():
    data = request.get_json(silent=True) or {}
    account = normalize_account(data.get("account"))
    incoming = data.get("domains", "")
    if isinstance(incoming, str):
        incoming = re.split(r"[\s,;]+", incoming)
    domains = [normalize_domain(domain) for domain in incoming if str(domain).strip()]
    existing = load_domains().get(account, [])
    save_account_domains(account, existing + domains)
    return jsonify({"ok": True, "added": len(set(domains))})


@app.patch("/api/domains/<account>/<domain>")
@require_auth
def api_update_domain(account, domain):
    account = normalize_account(account)
    old_domain = normalize_domain(domain)
    data = request.get_json(silent=True) or {}
    domains = load_domains().get(account, [])
    if old_domain not in domains:
        return jsonify({"error": "域名不存在"}), 404

    new_account = normalize_account(data.get("account", account))
    new_domain = normalize_domain(data.get("domain", old_domain))
    all_domains = load_domains()
    all_domains[account] = [item for item in all_domains.get(account, []) if item != old_domain]
    all_domains.setdefault(new_account, []).append(new_domain)
    for item_account, item_domains in all_domains.items():
        save_account_domains(item_account, item_domains)

    old_key = f"{account}/{old_domain}"
    new_key = f"{new_account}/{new_domain}"
    metadata = load_metadata()
    entry = metadata.pop(old_key, {})
    if "note" in data:
        entry["note"] = str(data.get("note") or "")
    metadata[new_key] = entry
    save_metadata(metadata)

    cache = load_status_cache()
    if old_key in cache:
        cache[new_key] = cache.pop(old_key)
        save_status_cache(cache)
    return jsonify({"ok": True})


@app.delete("/api/domains")
@require_auth
def api_delete_domains():
    data = request.get_json(silent=True) or {}
    items = data.get("items") or []
    all_domains = load_domains()
    metadata = load_metadata()
    cache = load_status_cache()
    deleted = 0
    for item in items:
        account = normalize_account(item.get("account"))
        domain = normalize_domain(item.get("domain"))
        if domain in all_domains.get(account, []):
            all_domains[account] = [value for value in all_domains[account] if value != domain]
            metadata.pop(f"{account}/{domain}", None)
            cache.pop(f"{account}/{domain}", None)
            deleted += 1
    for account, domains in all_domains.items():
        save_account_domains(account, domains)
    save_metadata(metadata)
    save_status_cache(cache)
    return jsonify({"ok": True, "deleted": deleted})


@app.post("/api/domains/sync/<account>")
@require_auth
def api_sync_godaddy(account):
    account = normalize_account(account)
    remote_domains = fetch_godaddy_domains(account)
    local_domains = load_domains().get(account, [])
    save_account_domains(account, local_domains + remote_domains)
    return jsonify({"ok": True, "synced": len(remote_domains)})


@app.post("/api/domains/check")
@require_auth
def api_check_domains():
    data = request.get_json(silent=True) or {}
    items = data.get("items")
    if not scheduler_lock.acquire(blocking=False):
        return jsonify({"error": "检测任务正在运行"}), 409
    try:
        rows = run_domain_checks(items)
    finally:
        scheduler_lock.release()
    return jsonify({"ok": True, "checked": len(rows)})


@app.get("/api/dashboard")
@require_auth
def api_dashboard():
    rows = build_domain_rows()
    counts = {
        "total": len(rows),
        "ok": 0,
        "warning": 0,
        "expired": 0,
        "unknown": 0,
        "dns_failed": 0,
        "ns_failed": 0,
        "check_error": 0,
    }
    accounts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        accounts[row["account"]] = accounts.get(row["account"], 0) + 1
    attention_statuses = {"warning", "expired", "dns_failed", "ns_failed", "check_error"}
    expiring = sorted(
        [row for row in rows if row["status"] in attention_statuses],
        key=lambda row: row["daysLeft"] if row["daysLeft"] is not None else 999999,
    )[:10]
    return jsonify({"counts": counts, "accounts": accounts, "expiring": expiring})


def csv_response(rows, suffix="domains"):
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "account",
            "domain",
            "status",
            "expiresAt",
            "daysLeft",
            "dnsStatus",
            "ns",
            "note",
            "checkedAt",
        ],
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    filename = f"{suffix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/export.csv")
@require_auth
def api_export():
    return csv_response(build_domain_rows())


@app.post("/api/export.csv")
@require_auth
def api_export_selected():
    data = request.get_json(silent=True) or {}
    items = data.get("items") or []
    selected_keys = set()
    for item in items:
        try:
            if item.get("account") and item.get("domain"):
                selected_keys.add(f"{normalize_account(item.get('account'))}/{normalize_domain(item.get('domain'))}")
        except (AttributeError, ValueError):
            continue
    if not selected_keys:
        return jsonify({"error": "先选择要导出的域名"}), 400
    rows = [row for row in build_domain_rows() if f"{row['account']}/{row['domain']}" in selected_keys]
    return csv_response(rows, "selected-domains")


if __name__ == "__main__":
    ensure_dirs()
    start_scheduler()
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=True, use_reloader=False)
