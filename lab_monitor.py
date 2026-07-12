#!/usr/bin/env python3
"""
Мониторинг акций медлабораторий.
Каждый день проверяет страницы акций, шлёт новые в Telegram с AI-резюме.
"""

import json, os, re, time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urljoin, urlencode

DATA_DIR = Path(__file__).parent
SEEN_FILE = DATA_DIR / "seen_promos.json"

# Credentials — из окружения (GitHub Secrets) или из файла
def _cfg():
    token = os.environ.get("TG_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if token and chat_id:
        return token, int(chat_id)
    cfg = json.loads((DATA_DIR / "tg_config_labs.json").read_text())
    return cfg["token"], cfg["chat_id"]

TOKEN, CHAT_ID = _cfg()
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

SITES = [
    {
        "name": "Гемотест",
        "url": "https://gemotest.ru/actions/",
        "base": "https://gemotest.ru",
        "pattern": r'href="(/actions/[a-z0-9_\-]+/)"',
        "skip": ["/actions/"],
    },
    {
        "name": "CMD",
        "url": "https://www.cmd-online.ru/patsientam/akcii/",
        "base": "https://www.cmd-online.ru",
        "pattern": r'href="(/patsientam/akcii/[a-z0-9\-]+/)',
    },
    {
        "name": "Helix",
        "url": "https://helix.ru/moskva/promotions",
        "base": "https://helix.ru",
        "pattern": r'href="(/promotions/select/\d+)"',
    },
    {
        "name": "ДНКом",
        "url": "https://dnkom.ru/actions/",
        "base": "https://dnkom.ru",
        "pattern": r'href="(/actions/[a-z0-9_\-]+/)"',
        "skip": ["/actions/"],
    },
    {
        "name": "LabQuest",
        "url": "https://www.labquest.ru/aktsii/",
        "base": "https://www.labquest.ru",
        "pattern": r'href="(/aktsii/[a-z0-9\-]+/)"',
    },
    {
        "name": "Горлаб",
        "url": "https://gorlab.ru/promo/",
        "base": "https://gorlab.ru",
        "pattern": r'href="(/promo/[a-z0-9\-]+/)"',
        "skip": ["/promo/"],
    },
    {
        "name": "Инвитро",
        "url": "https://www.invitro.ru/moscow/ak/",
        "base": "https://www.invitro.ru",
        "pattern": r'href="(/moscow/ak/[a-z0-9\-]+/)"',
        "skip": ["/moscow/ak/"],
    },
]


def fetch(url, timeout=15, encoding="utf-8"):
    req = Request(url, headers=HEADERS)
    raw = urlopen(req, timeout=timeout).read()
    return raw.decode(encoding, "replace")


def strip_html(html):
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', html, flags=re.DOTALL | re.I)
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL | re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def get_title(html):
    m = re.search(r'<h1[^>]*>([^<]{5,150})</h1>', html, re.I)
    if m:
        return re.sub(r'\s+', ' ', m.group(1)).strip()
    m = re.search(r'<title>([^<]{5,150})</title>', html, re.I)
    if m:
        title = m.group(1).strip()
        return re.split(r'\s*[|–—]\s*', title)[0].strip()
    return ""


def ai_summary(promos):
    """Одним запросом к GPT получаем резюме для всех новых акций."""
    if not OPENAI_KEY or not promos:
        return {}

    lines = []
    for i, p in enumerate(promos):
        text = p.get("page_text", "")[:800]
        lines.append(f"[{i}] {p['lab']} | {p['url']}\n{text}")

    prompt = (
        "Ты помощник, который читает страницы акций медицинских лабораторий. "
        "Для каждой акции ниже напиши ОДНО предложение по-русски: что за акция и какая скидка/выгода. "
        "Отвечай строго в формате JSON: {\"0\": \"резюме\", \"1\": \"резюме\", ...}\n\n"
        + "\n\n".join(lines)
    )

    payload = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 500,
        "temperature": 0,
    }).encode()

    req = Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {OPENAI_KEY}",
            "Content-Type": "application/json",
        },
    )
    resp = json.loads(urlopen(req, timeout=30).read())
    content = resp["choices"][0]["message"]["content"].strip()
    # убираем ```json если есть
    content = re.sub(r'^```json\s*|\s*```$', '', content).strip()
    return json.loads(content)


def get_promo_links(site):
    try:
        html = fetch(site["url"])
    except Exception as e:
        print(f"  {site['name']}: ошибка загрузки — {e}")
        return []

    raw = re.findall(site["pattern"], html, re.I)
    skip = set(site.get("skip", []))
    links = []
    seen_slugs = set()
    for path in raw:
        if path in skip:
            continue
        slug = path.rstrip("/").split("/")[-1]
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        links.append(urljoin(site["base"], path))
    return links


def tg_send(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urlencode({
        "chat_id": CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    urlopen(Request(url, data=data), timeout=10)


def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(urls):
    SEEN_FILE.write_text(json.dumps(sorted(urls)))


def run():
    seen = load_seen()
    all_found = set()
    new_promos = []

    for site in SITES:
        links = get_promo_links(site)
        print(f"  {site['name']}: {len(links)} акций")
        all_found.update(links)

        for url in links:
            if url not in seen:
                page_text = ""
                title = ""
                try:
                    html = fetch(url, timeout=10)
                    title = get_title(html)
                    page_text = strip_html(html)[:1200]
                    time.sleep(0.4)
                except Exception as e:
                    print(f"    не загрузилась {url}: {e}")

                new_promos.append({
                    "lab": site["name"],
                    "url": url,
                    "title": title or url.rstrip("/").split("/")[-1],
                    "page_text": page_text,
                })

    print(f"Новых акций: {len(new_promos)}")

    # Получаем AI-резюме одним запросом
    summaries = {}
    if new_promos and OPENAI_KEY:
        try:
            summaries = ai_summary(new_promos)
            print(f"AI резюме получено для {len(summaries)} акций")
        except Exception as e:
            print(f"AI ошибка: {e}")

    if not new_promos:
        tg_send("📋 Новых акций не найдено")
    else:
        for i, p in enumerate(new_promos):
            summary = summaries.get(str(i), "")
            text = (
                f"🆕 <b>{p['lab']}</b>\n"
                f"<b>{p['title']}</b>\n"
            )
            if summary:
                text += f"{summary}\n"
            text += f'<a href="{p["url"]}">{p["url"]}</a>'

            try:
                tg_send(text)
                time.sleep(0.3)
            except Exception as e:
                print(f"TG error: {e}")

    save_seen(seen | all_found)
    print("Готово")


if __name__ == "__main__":
    run()
