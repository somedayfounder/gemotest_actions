#!/usr/bin/env python3
"""Читает /tmp/monitor_results.json и отправляет итоговое сообщение в Telegram."""
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TG_TOKEN   = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

DATA_DIR     = Path(__file__).parent
RESULTS_FILE = Path("/tmp/monitor_results.json")


def _cfg():
    if TG_TOKEN and TG_CHAT_ID:
        return TG_TOKEN, TG_CHAT_ID
    cfg = json.loads((DATA_DIR / "tg_config_labs.json").read_text())
    return cfg["token"], str(cfg["chat_id"])


def tg_send(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urlencode({
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    urlopen(Request(url, data=data), timeout=15)


def tg_send_parts(token, chat_id, text):
    """Разбить на части если > 4000 символов."""
    MAX = 4000
    if len(text) <= MAX:
        tg_send(token, chat_id, text)
        return
    parts = []
    while text:
        if len(text) <= MAX:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, MAX)
        if cut == -1:
            cut = MAX
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    for i, part in enumerate(parts):
        tg_send(token, chat_id, part)


SEP = "・・・"

def section(title, items, show_url=True, show_summary=False):
    if not items:
        return ""
    lines = [SEP, f"<b>{title}</b>"]
    by_lab = {}
    for item in items:
        by_lab.setdefault(item["lab"], []).append(item)
    for lab, lab_items in by_lab.items():
        lines.append(f"\n<b>{lab}</b>")
        for item in lab_items:
            t = item.get("title") or item.get("url", "").rstrip("/").split("/")[-1] or "—"
            u = item.get("url", "")
            s = item.get("summary", "")
            if show_url and u:
                lines.append(f'<a href="{u}"><b>{t}</b></a>')
            else:
                lines.append(f"<b>{t}</b>")
            if show_summary and s:
                lines.append(s)
    return "\n".join(lines)


def run():
    token, chat_id = _cfg()

    now_msk = datetime.now(timezone.utc)
    date_str = now_msk.strftime("%d.%m  %H:%M UTC")

    if not RESULTS_FILE.exists():
        tg_send(token, chat_id, f"✅ Проверка {date_str} — изменений нет")
        print("Нет файла результатов — изменений нет")
        return

    try:
        results = json.loads(RESULTS_FILE.read_text())
    except Exception as e:
        tg_send(token, chat_id, f"⚠️ Проверка {date_str} — ошибка чтения результатов: {e}")
        return

    errors   = results.get("errors", [])
    new_p    = results.get("new_promos", [])
    gone_p   = results.get("gone_promos", [])
    new_art  = results.get("new_articles", [])
    new_news = results.get("new_news", [])
    gnews    = results.get("google_news", [])

    has_changes = any([new_p, gone_p, new_art, new_news, gnews])

    header = f"Проверка {date_str}"
    if errors:
        header += "\n⚠️ " + " / ".join(errors)
    else:
        header += "\n✅ Все проверки ОК"

    if not has_changes:
        tg_send(token, chat_id, header + "\n\nИзменений нет")
        print("Изменений нет")
        return

    blocks = [
        header,
        section("Новые акции", new_p, show_summary=True),
        section("Завершившиеся акции", gone_p, show_url=False),
        section("Новые статьи", new_art),
        section("Новости", new_news),
        section("Google News", gnews),
    ]
    text = "\n\n".join(b for b in blocks if b)
    tg_send_parts(token, chat_id, text)
    print("Итоговое сообщение отправлено")


if __name__ == "__main__":
    run()
