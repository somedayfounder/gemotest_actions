#!/usr/bin/env python3
"""
Мониторинг новостей медлаб через Google News RSS.
Запускается каждые 6 часов, шлёт только новые статьи за сегодня в Telegram.
"""
import json, os, time
from datetime import datetime, timezone, date
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

TG_TOKEN  = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

LABS = [
    ("Гемотест", '"Гемотест"'),
    ("Helix",    '"Хеликс" лаборатория'),
    ("Инвитро",  '"ИНВИТРО" OR "Инвитро" лаборатория'),
    ("ДНКом",    '"ДНКОМ" OR "ДНКом"'),
    ("КДЛ",      '"КДЛ" лаборатория -Олимп -Казахстан'),
    ("CMD",      '"ЦМД" лаборатория анализы'),
    ("LabQuest", '"LabQuest"'),
    ("Ситилаб",  '"Ситилаб" OR "СИТИЛАБ"'),
]

DATA_DIR  = Path(__file__).parent
SEEN_FILE = DATA_DIR / "seen_news.json"


def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(sorted(seen)))


def tg_send(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = urlencode({
        "chat_id": TG_CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true"
    }).encode()
    urlopen(Request(url, data=data), timeout=10)


def tg_safe(text, label=""):
    try:
        tg_send(text)
    except Exception as e:
        print(f"TG error {label}: {e}")


def fetch_news(name, query):
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ru&gl=RU&ceid=RU:ru"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    r = urlopen(req, timeout=12)
    tree = ET.fromstring(r.read())
    today = date.today()
    results = []
    for item in tree.findall(".//item"):
        link  = item.findtext("link", "")
        title = item.findtext("title", "")
        pub   = item.findtext("pubDate", "")
        try:
            dt = parsedate_to_datetime(pub)
            if dt.astimezone(timezone.utc).date() != today:
                continue
        except:
            continue
        results.append((link, title, dt))
    return results


def run():
    if not TG_TOKEN:
        cfg = json.loads((DATA_DIR / "tg_config_labs.json").read_text())
        globals().update(TG_TOKEN=cfg["token"], TG_CHAT_ID=str(cfg["chat_id"]))

    seen = load_seen()
    new_seen = set()
    found_any = False

    for name, query in LABS:
        try:
            items = fetch_news(name, query)
        except Exception as e:
            print(f"{name}: ошибка — {e}")
            continue

        fresh = [(link, title, dt) for link, title, dt in items if link not in seen]
        print(f"{name}: {len(items)} сегодня, {len(fresh)} новых")

        if fresh:
            found_any = True
            lines = [f"<b>📰 {name}</b>"]
            for link, title, dt in sorted(fresh, key=lambda x: x[2]):
                lines.append(f"<a href=\"{link}\">{title}</a>")
                new_seen.add(link)
            tg_safe("\n".join(lines), name)
            time.sleep(0.5)

        new_seen.update(link for link, _, _ in items)

    if not found_any:
        print("Новых новостей нет")

    save_seen(seen | new_seen)
    print("Готово")


if __name__ == "__main__":
    run()
