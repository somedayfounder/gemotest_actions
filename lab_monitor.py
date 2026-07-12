#!/usr/bin/env python3
"""
Мониторинг акций медлабораторий.
1. Заходим на страницу списка → собираем ссылки текущих акций
2. Сравниваем с предыдущим запуском → новые и исчезнувшие
3. Для каждой новой акции заходим на её страницу → полный текст
4. Отдаём в GPT → резюме + даты
5. Шлём уведомления
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


# ── Загрузка ─────────────────────────────────────────────────────────────────

def fetch_html(url, encoding="utf-8", timeout=20):
    req = Request(url, headers=HEADERS)
    return urlopen(req, timeout=timeout).read().decode(encoding, "replace")


def fetch_js(url):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=30000)
        html = page.content()
        browser.close()
    return html


def strip_html(html):
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', html, flags=re.DOTALL | re.I)
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL | re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# ── Список акций со страницы листинга ────────────────────────────────────────

def get_listing_links(site):
    try:
        if site.get("js"):
            html = fetch_js(site["url"])
        else:
            html = fetch_html(site["url"], encoding=site.get("encoding", "utf-8"))
    except Exception as e:
        print(f"  {site['name']}: ошибка листинга — {e}")
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


# ── Полная страница акции ─────────────────────────────────────────────────────

def fetch_promo_page(url, site):
    try:
        if site.get("js"):
            html = fetch_js(url)
        else:
            html = fetch_html(url, encoding=site.get("encoding", "utf-8"), timeout=15)
        return strip_html(html)
    except Exception as e:
        print(f"    не загрузилась {url}: {e}")
        return ""


# ── GPT ──────────────────────────────────────────────────────────────────────

def ai_analyze(promos):
    """
    promos: список {"lab", "url", "page_text"}
    Возвращает {url: {"title", "summary", "dates"}}
    """
    if not OPENAI_KEY or not promos:
        return {}

    blocks = []
    for p in promos:
        blocks.append(f"URL: {p['url']}\nЛаборатория: {p['lab']}\n\n{p['page_text'][:3000]}")

    prompt = (
        "Ты анализируешь страницы акций медицинских лабораторий.\n"
        "Для каждого блока ниже верни JSON с ключом = URL акции и полями:\n"
        "  title   — название акции\n"
        "  summary — одно предложение: суть акции и выгода для пациента\n"
        "  dates   — даты акции (например «до 31 июля» или «1–31 августа 2026»), или \"\"\n\n"
        'Формат: {"https://...": {"title": "...", "summary": "...", "dates": "..."}, ...}\n\n'
        + "\n\n---\n\n".join(blocks)
    )

    payload = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1500,
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
    if SEEN_FILE.exists():
        data = json.loads(SEEN_FILE.read_text())
        if isinstance(data, dict):
            return data
    return {}


def save_active(data):
    SEEN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ── Основной цикл ────────────────────────────────────────────────────────────

def run():
    active = load_active()
    is_init = not active
    current = {}  # url -> site

    # 1. Собираем текущие акции со всех листингов
    for site in SITES:
        links = get_listing_links(site)
        print(f"  {site['name']}: {len(links)} акций")
        for url in links:
            current[url] = site

    new_urls = [u for u in current if u not in active]
    gone_urls = [u for u in active if u not in current]
    print(f"Новых: {len(new_urls)}, исчезло: {len(gone_urls)}")

    if is_init:
        for url, site in current.items():
            active[url] = {"lab": site["name"], "title": "", "summary": "", "dates": ""}
        print(f"Первый запуск — запомнили {len(active)} акций, уведомления не отправлялись")
        save_active(active)
        return

    # 2. Для новых акций загружаем страницы
    promos_to_analyze = []
    for url in new_urls:
        site = current[url]
        print(f"  загружаем {url}")
        page_text = fetch_promo_page(url, site)
        promos_to_analyze.append({"lab": site["name"], "url": url, "page_text": page_text})
        time.sleep(0.5)

    # 3. GPT-анализ всех новых акций одним запросом
    ai = {}
    if promos_to_analyze:
        try:
            ai = ai_analyze(promos_to_analyze)
            print(f"AI: {len(ai)} проанализировано")
        except Exception as e:
            print(f"AI ошибка: {e}")

    # 4. Уведомления о новых акциях
    for url in new_urls:
        info = ai.get(url, {})
        site = current[url]
        title = info.get("title") or url.rstrip("/").split("/")[-1]
        summary = info.get("summary", "")
        dates = info.get("dates", "")

        active[url] = {"lab": site["name"], "title": title, "summary": summary, "dates": dates}

        text = f"🆕 <b>{site['name']}</b>\n<b>{title}</b>\n"
        if summary:
            text += f"{summary}\n"
        if dates:
            text += f"📅 {dates}\n"
        text += f'<a href="{url}">{url}</a>'
        try:
            tg_send(text)
            time.sleep(0.3)
        except Exception as e:
            print(f"TG error: {e}")

    # 5. Уведомления об исчезнувших акциях
    for url in gone_urls:
        info = active.pop(url)
        text = (
            f"❌ <b>{info['lab']}</b> — акция завершена\n"
            f"{info.get('title') or url}\n"
            f'<a href="{url}">{url}</a>'
        )
        try:
            tg_send(text)
            time.sleep(0.3)
        except Exception as e:
            print(f"TG error: {e}")

    save_active(active)
    print("Готово")


if __name__ == "__main__":
    run()
