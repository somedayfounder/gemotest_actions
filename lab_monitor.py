#!/usr/bin/env python3
"""
Мониторинг акций медлабораторий.
- Отслеживает появление и исчезновение акций
- Для каждой новой акции загружает полную страницу и передаёт GPT для анализа
- GPT возвращает: резюме акции + даты
"""

import json, os, re, time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urljoin, urlencode

DATA_DIR = Path(__file__).parent
SEEN_FILE = DATA_DIR / "seen_promos.json"

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
        "pattern": r'href="(/patsientam/akcii/[a-z0-9\-]+/)"',
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
        "pattern": r"href='(/news/[a-z0-9\-]+\.html)'",
        "encoding": "windows-1251",
    },
    {
        "name": "КДЛ",
        "url": "https://kdl.ru/akcii",
        "base": "https://kdl.ru",
        "pattern": r'href="(/akcii/[a-z0-9\-]+)"',
        "skip": ["/akcii"],
        "js": True,
    },
    {
        "name": "Инвитро",
        "url": "https://www.invitro.ru/moscow/ak/",
        "base": "https://www.invitro.ru",
        "pattern": r'href="(/moscow/ak/[a-z0-9\-]+/)"',
        "skip": ["/moscow/ak/"],
        "js": True,
    },
]


# ── HTTP fetch ──────────────────────────────────────────────────────────────

def fetch_html(url, encoding="utf-8", timeout=15):
    req = Request(url, headers=HEADERS)
    return urlopen(req, timeout=timeout).read().decode(encoding, "replace")


def fetch_js(url):
    """Загружает JS-рендеренную страницу через Playwright."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=30000)
        html = page.content()
        browser.close()
    return html


# ── Парсинг ─────────────────────────────────────────────────────────────────

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
        return re.split(r'\s*[|–—]\s*', m.group(1).strip())[0].strip()
    return ""


def get_promo_links(site):
    try:
        if site.get("js"):
            html = fetch_js(site["url"])
        else:
            html = fetch_html(site["url"], encoding=site.get("encoding", "utf-8"))
    except Exception as e:
        print(f"  {site['name']}: ошибка загрузки — {e}")
        return []

    raw = re.findall(site["pattern"], html, re.I)
    skip = set(site.get("skip", []))
    links, seen_slugs = [], set()
    for path in raw:
        if path in skip:
            continue
        slug = path.rstrip("/").split("/")[-1]
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        links.append(urljoin(site["base"], path))
    return links


# ── GPT-анализ ───────────────────────────────────────────────────────────────

def ai_analyze(promos):
    """Одним запросом: резюме + даты для каждой новой акции."""
    if not OPENAI_KEY or not promos:
        return {}

    blocks = []
    for i, p in enumerate(promos):
        blocks.append(f"[{i}] {p['lab']} — {p['url']}\n{p['page_text']}")

    prompt = (
        "Ты помощник, анализирующий страницы акций медицинских лабораторий.\n"
        "Для каждой акции ниже верни JSON с полями:\n"
        "  summary — одно предложение по-русски: что за акция и какая скидка/выгода\n"
        "  dates   — строка с датами акции (например «до 31 июля» или «1–31 августа 2026»), "
        "или пустая строка если дат нет\n\n"
        "Формат ответа строго: {\"0\": {\"summary\": \"...\", \"dates\": \"...\"}, \"1\": {...}, ...}\n\n"
        + "\n\n---\n\n".join(blocks)
    )

    payload = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1000,
        "temperature": 0,
    }).encode()

    req = Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
    )
    resp = json.loads(urlopen(req, timeout=60).read())
    content = resp["choices"][0]["message"]["content"].strip()
    content = re.sub(r'^```json\s*|\s*```$', '', content).strip()
    return json.loads(content)


# ── Telegram ─────────────────────────────────────────────────────────────────

def tg_send(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urlencode({
        "chat_id": CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    urlopen(Request(url, data=data), timeout=10)


# ── Хранилище ────────────────────────────────────────────────────────────────

def load_active():
    """Возвращает {url: {lab, title, summary, dates}}."""
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_active(data):
    SEEN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ── Основной цикл ────────────────────────────────────────────────────────────

def run():
    active = load_active()
    current_urls = {}  # url -> lab

    # 1. Собираем все текущие акции со всех сайтов
    for site in SITES:
        links = get_promo_links(site)
        print(f"  {site['name']}: {len(links)} акций")
        for url in links:
            current_urls[url] = site["name"]
        time.sleep(0.5)

    # 2. Новые акции (есть сейчас, не было раньше)
    new_urls = [url for url in current_urls if url not in active]
    # 3. Исчезнувшие акции (были раньше, нет сейчас)
    gone_urls = [url for url in active if url not in current_urls]

    print(f"Новых: {len(new_urls)}, исчезло: {len(gone_urls)}")

    # 4. Загружаем страницы новых акций
    new_promos = []
    for url in new_urls:
        lab = current_urls[url]
        page_text = ""
        title = ""
        try:
            # Для JS-сайтов используем Playwright
            site = next(s for s in SITES if s["name"] == lab)
            if site.get("js"):
                html = fetch_js(url)
            else:
                html = fetch_html(url, encoding=site.get("encoding", "utf-8"), timeout=10)
            title = get_title(html)
            page_text = strip_html(html)[:3000]
            time.sleep(0.4)
        except Exception as e:
            print(f"    не загрузилась {url}: {e}")

        new_promos.append({
            "lab": lab,
            "url": url,
            "title": title or url.rstrip("/").split("/")[-1],
            "page_text": page_text,
        })

    # 5. GPT-анализ новых акций
    ai = {}
    if new_promos and OPENAI_KEY:
        try:
            ai = ai_analyze(new_promos)
            print(f"AI: {len(ai)} акций проанализировано")
        except Exception as e:
            print(f"AI ошибка: {e}")

    # 6. Уведомления о новых акциях
    for i, p in enumerate(new_promos):
        info = ai.get(str(i), {})
        summary = info.get("summary", "")
        dates = info.get("dates", "")

        text = f"🆕 <b>{p['lab']}</b>\n<b>{p['title']}</b>\n"
        if summary:
            text += f"{summary}\n"
        if dates:
            text += f"📅 {dates}\n"
        text += f'<a href="{p["url"]}">{p["url"]}</a>'

        try:
            tg_send(text)
            time.sleep(0.3)
        except Exception as e:
            print(f"TG error: {e}")

        # Сохраняем в active
        active[p["url"]] = {
            "lab": p["lab"],
            "title": p["title"],
            "summary": summary,
            "dates": dates,
        }

    # 7. Уведомления об исчезнувших акциях
    for url in gone_urls:
        info = active[url]
        text = (
            f"❌ <b>{info['lab']}</b> — акция завершена\n"
            f"{info.get('title', url)}\n"
            f'<a href="{url}">{url}</a>'
        )
        try:
            tg_send(text)
            time.sleep(0.3)
        except Exception as e:
            print(f"TG error: {e}")

        del active[url]

    save_active(active)
    print("Готово")


if __name__ == "__main__":
    run()
