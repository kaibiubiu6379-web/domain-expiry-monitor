import os
import whois
import requests
import subprocess
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# ===== 配置 =====
THRESHOLD = 14
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOMAIN_DIR = os.path.join(BASE_DIR, "domains")
MAX_WORKERS = 1     
SLEEP_INTERVAL = 0.9 
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ===== Telegram =====
def send_telegram(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram token/chat id 未配置，跳过发送")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": CHAT_ID,
            "text": msg
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print("Telegram 发送失败:", e)
        return False


# ===== 读取域名 =====
def load_domains():
    result = {}
    files = sorted(os.listdir(DOMAIN_DIR))

    for file in files:
        if not file.endswith(".txt"):
            continue

        group = file.replace(".txt", "")
        path = os.path.join(DOMAIN_DIR, file)

        with open(path) as f:
            domains = [line.strip() for line in f if line.strip()]

        result[group] = domains

    return result


# ===== 获取到期时间（带重试）=====
def get_expire_days(domain):
    for attempt in range(2):  # ⭐ 重试2次
        try:
            w = whois.whois(domain)
            expire = w.expiration_date

            if isinstance(expire, list):
                expire = expire[0]

            if not expire:
                return None

            if expire.tzinfo is None:
                expire = expire.replace(tzinfo=timezone.utc)

            now = datetime.now(timezone.utc)

            return (expire - now).days

        except Exception as e:
            print(f"[RETRY {attempt+1}] {domain}: {e}")
            time.sleep(0.5)

    return None


# ===== DNS =====
def get_ns(domain):
    try:
        result = subprocess.run(
            ["dig", domain, "NS", "+short"],
            capture_output=True,
            text=True,
            timeout=5
        )

        output = result.stdout.strip()

        if not output:
            return False, "SERVFAIL"

        return True, output.replace("\n", ", ")

    except Exception as e:
        return False, str(e)


# ===== 单个域名处理 =====
def process_domain(group, domain):
    time.sleep(SLEEP_INTERVAL)  # ⭐ 限流核心

    days = get_expire_days(domain)

    if days is None:
        return None

    if days <= THRESHOLD:
        ok, ns_info = get_ns(domain)

        line = f"{domain:<22} 剩余 {days:>3} 天   "

        if ok:
            line += f"NS: {ns_info}"
        else:
            line += f"DNS异常: {ns_info}"

        return group, line

    return None


# ===== 主程序 =====
def main():
    start_time = datetime.now()

    print(f"\n===== 开始检测: {start_time.strftime('%Y-%m-%d %H:%M:%S')} =====")

    data = load_domains()
    total_domains = sum(len(v) for v in data.values())

    alerts = {}
    tasks = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for group, domains in data.items():
            for domain in domains:
                tasks.append(executor.submit(process_domain, group, domain))

        for future in as_completed(tasks):
            result = future.result()

            if result:
                group, line = result

                if group not in alerts:
                    alerts[group] = []

                alerts[group].append(line)

    # ===== 日志 =====
    alert_count = sum(len(v) for v in alerts.values())

    print(f"\n检测域名总数: {total_domains}")
    print(f"即将到期数量: {alert_count}")

    for group, items in alerts.items():
        print(f"\n=== {group} ===")
        for line in items:
            print(line)

    # ===== Telegram（仅有告警才发送）=====
    if alerts:
        msg = f"🚨 域名到期提醒 🚨\n\n"
        msg += f"检测时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        msg += f"域名总数: {total_domains}\n"
        msg += f"告警数量: {alert_count}\n\n"

        for group, items in alerts.items():
            msg += f"=== {group} ===\n"
            msg += "\n".join(items)
            msg += "\n\n"

        ok = send_telegram(msg)

        if ok:
            print("\nTelegram 发送成功 ✅")
        else:
            print("\nTelegram 发送失败 ❌")
    else:
        print("\n无告警，不发送 Telegram ✅")

    end_time = datetime.now()
    print(f"\n===== 结束: {end_time.strftime('%Y-%m-%d %H:%M:%S')} =====")
    print(f"耗时: {(end_time - start_time).seconds} 秒")


if __name__ == "__main__":
    main()
