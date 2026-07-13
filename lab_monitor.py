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

_api_cache = {}  # url -> prefetched text from listing API

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
        # только <a class="articles-grid__item" href="..."> — 18 карточек акций
        "pattern": r'<a\s[^>]*class="articles-grid__item"[^>]*href="(/actions/[a-z0-9_\-]+/)"'
                   r'|<a\s[^>]*href="(/actions/[a-z0-9_\-]+/)"[^>]*class="articles-grid__item"',
        "multi_group": True,
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
        # Angular SSR — ссылки с data-testid="button-detail"
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
        # обрезать страницу до раздела "Завершившиеся акции"
        "cut_at": "Завершившиеся акции",
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
        "pattern": r'href="(/akcii/[a-zA-Z0-9_\-]+)"',
        "skip": ["/akcii"],
        "js": True,
        "js_wait_ms": 20000,
        "intercept_url": "kdl.ru",
    },
    {
        "name": "Инвитро",
        "url": "https://www.invitro.ru/moscow/ak/",
        "base": "https://www.invitro.ru",
        "api_fetch": "invitro",
        "pattern": r'href="(/moscow/ak/[a-z0-9\-]+/)"',
    },
    {
        "name": "Ситилаб",
        "url": "https://citilab.ru/discounts/",
        "base": "https://citilab.ru",
        "pattern": r'href="(/discounts/[a-z0-9\-]+/)"',
        "skip": ["/discounts/"],
    },
]


# ── Загрузка ─────────────────────────────────────────────────────────────────

def fetch_html(url, encoding="utf-8", timeout=20):
    req = Request(url, headers=HEADERS)
    return urlopen(req, timeout=timeout).read().decode(encoding, "replace")


def fetch_js(url, wait_ms=5000, intercept_key=None, intercept_url=None):
    """intercept_key: перехватываем JSON содержащий ключ (возвращаем вместо HTML).
    intercept_url: перехватываем JSON с этим URL (для отладки логируем, не заменяем HTML)."""
    from playwright.sync_api import sync_playwright
    captured = []
    intercepted_debug = []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )
        page = ctx.new_page()

        api_calls = []

        def on_response(response):
            ct = response.headers.get("content-type", "")
            rurl = response.url
            if "json" in ct and response.status == 200:
                try:
                    body = response.text()
                    if intercept_key and intercept_key in body:
                        captured.append(body)
                    if intercept_url and intercept_url in rurl:
                        intercepted_debug.append(f"[{rurl}] {body[:3000]}")
                    if "yandex" not in rurl and "google" not in rurl and "mc." not in rurl:
                        api_calls.append(rurl)
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            page.goto(url, wait_until="load", timeout=60000)
        except Exception:
            pass
        page.wait_for_timeout(wait_ms)

        if api_calls:
            print(f"    API calls: {api_calls[:10]}")
        for d in intercepted_debug[:2]:
            print(f"    intercept_url body: {d[:500]}")

        if captured:
            html = "\n".join(captured)
        else:
            html = page.content()
        browser.close()
    return html


def strip_html(html):
    # Пробуем вырезать основной контент (main/article/div#content)
    for tag_pat in [
        r'<main\b[^>]*>(.*?)</main>',
        r'<article\b[^>]*>(.*?)</article>',
        r'<div[^>]+id="content"[^>]*>(.*?)</div>',
        r'<div[^>]+class="[^"]*content[^"]*"[^>]*>(.*?)</div>',
    ]:
        m = re.search(tag_pat, html, re.DOTALL | re.I)
        if m and len(m.group(1)) > 500:
            html = m.group(1)
            break

    text = re.sub(r'<style[^>]*>.*?</style>', ' ', html, flags=re.DOTALL | re.I)
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL | re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# ── Список акций со страницы листинга ────────────────────────────────────────

def get_invitro_links():
    """Получаем акции Инвитро напрямую из CMS API."""
    api_url = (
        "https://www.invitro.ru/golk/cms/cms-proxy/promotions/filtered"
        "?targetPage=promotions&cityId=f1c3c4f0-3426-4cda-8449-e5d326e02f97&depth=3"
    )
    headers = {**HEADERS, "Referer": "https://www.invitro.ru/moscow/ak/"}
    raw = urlopen(Request(api_url, headers=headers), timeout=20).read()
    docs = json.loads(raw)["docs"]
    links = []
    for doc in docs:
        url = None
        for block in (doc.get("page") or []):
            if block.get("blockType") == "newPageLink":
                slug = (block.get("newPage") or {}).get("slug", "")
                if slug:
                    url = f"https://www.invitro.ru/moscow/ak/{slug}/"
            elif block.get("blockType") == "oldPageLink":
                old = block.get("oldPageUrl", "")
                if old:
                    url = old if old.startswith("http") else f"https://www.invitro.ru{old}"
        if not url:
            continue
        title = doc.get("title", "")
        desc = doc.get("description", "")
        _api_cache[url] = f"Инвитро | {title}\n{desc}"
        links.append(url)
    return links


def get_listing_links(site):
    if site.get("api_fetch") == "invitro":
        try:
            return get_invitro_links()
        except Exception as e:
            print(f"  {site['name']}: ошибка API — {e}")
            return []

    try:
        if site.get("js"):
            html = fetch_js(site["url"], wait_ms=site.get("js_wait_ms", 5000),
                            intercept_key=site.get("intercept_key"),
                            intercept_url=site.get("intercept_url"))
        else:
            html = fetch_html(site["url"], encoding=site.get("encoding", "utf-8"))
    except Exception as e:
        print(f"  {site['name']}: ошибка листинга — {e}")
        return []

    # вырезаем только нужный раздел страницы (по классу div/section)
    section = site.get("section")
    if section:
        m = re.search(rf'class="[^"]*{re.escape(section)}[^"]*"', html)
        if m:
            html = html[m.start():]

    # обрезаем страницу если задан cut_at (убираем архивный раздел)
    cut = site.get("cut_at")
    if cut:
        idx = html.find(cut)
        if idx > 0:
            html = html[:idx]

    raw = re.findall(site["pattern"], html, re.I)
    # если паттерн с несколькими группами — берём первую непустую
    if raw and isinstance(raw[0], tuple):
        raw = [next(g for g in groups if g) for groups in raw]
    if not raw:
        # отладка: показываем первые href и кусок HTML
        sample = re.findall(r'href="(/[^"]{5,60})"', html)[:8]
        print(f"    [{site['name']}] sample hrefs: {sample}")
        title = re.search(r'<title[^>]*>(.*?)</title>', html, re.I)
        print(f"    [{site['name']}] title: {title.group(1)[:100] if title else 'no title'}")
        # Ищем все ссылки начинающиеся с основного ключевого слова паттерна
        pat_prefix = re.search(r'\(/([a-zA-Z]+)', site["pattern"])
        if pat_prefix:
            key = pat_prefix.group(1)
            key_hrefs = re.findall(rf'href="(/{key}[^"{{}}]{{0,100}})"', html)[:15]
            print(f"    [{site['name']}] {key}* hrefs: {key_hrefs}")
        print(f"    [{site['name']}] html_len: {len(html)}, tail[-200]: {html[-200:]}")
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
    if url in _api_cache:
        return _api_cache[url]
    try:
        if site.get("js"):
            html = fetch_js(url, wait_ms=site.get("js_wait_ms", 5000),
                            intercept_key=site.get("intercept_key"))
        else:
            html = fetch_html(url, encoding=site.get("encoding", "utf-8"), timeout=15)
        return strip_html(html)
    except Exception as e:
        print(f"    не загрузилась {url}: {e}")
        return ""


# ── GPT ──────────────────────────────────────────────────────────────────────

def ai_analyze_batch(promos):
    """Один GPT-запрос для батча акций. Возвращает {url: {title, summary, dates}}."""
    blocks = []
    for p in promos:
        blocks.append(f"URL: {p['url']}\nЛаборатория: {p['lab']}\n\n{p['page_text'][:2500]}")

    prompt = (
        "Ты анализируешь страницы акций медицинских лабораторий.\n"
        "Для каждого блока верни JSON с ключом = URL и полями:\n"
        "  title       — название акции\n"
        "  summary     — одно предложение: суть акции и выгода для пациента\n"
        "  dates       — даты акции (например «до 31 июля»); если срок не указан — «Бессрочно»\n"
        "  price       — цена для Москвы (например «от 390 ₽» или «1 490 ₽»), или \"\" если не указана\n"
        "  composition — состав: перечень анализов/услуг через запятую (кратко), или \"\" если не указан\n"
        "  is_local    — true если акция только в конкретном филиале/точке, иначе false\n\n"
        'Формат: {"https://...": {"title":"...","summary":"...","dates":"...","price":"...","composition":"...","is_local":false}, ...}\n\n'
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


def ai_analyze(promos):
    """Анализирует акции батчами по 5 штук."""
    if not OPENAI_KEY or not promos:
        return {}
    result = {}
    batch_size = 5
    for i in range(0, len(promos), batch_size):
        batch = promos[i:i + batch_size]
        try:
            result.update(ai_analyze_batch(batch))
            if i + batch_size < len(promos):
                time.sleep(1)
        except Exception as e:
            print(f"  AI батч {i//batch_size + 1} ошибка: {e}")
    return result


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

    try:
        tg_send(f"⏳ <b>Акции</b>: запуск…")
    except Exception as e:
        print(f"TG start error: {e}")

    # 1. Собираем текущие акции со всех листингов
    for site in SITES:
        links = get_listing_links(site)
        print(f"  {site['name']}: {len(links)} акций")
        for url in links:
            current[url] = site

    new_urls = [u for u in current if u not in active]
    gone_urls = [u for u in active if u not in current]
    print(f"Новых: {len(new_urls)}, исчезло: {len(gone_urls)}")

    # Защита от сбоя сети/VPN: если нашли мало акций — не обрабатываем исчезнувшие
    if len(current) < 10 and len(gone_urls) > 5:
        msg = f"⚠️ <b>Акции</b>: нашли только {len(current)} акций (обычно 100+). Возможен сбой VPN. Пропускаем обработку исчезнувших."
        print(msg)
        try:
            tg_send(msg)
        except Exception:
            pass
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
        dates = info.get("dates", "") or "Бессрочно"
        price = info.get("price", "")
        composition = info.get("composition", "")
        is_local = info.get("is_local", False)

        active[url] = {
            "lab": site["name"], "title": title, "summary": summary,
            "dates": dates, "price": price, "composition": composition,
        }

        # Локальные акции (только в одном филиале) — не отправляем
        if is_local:
            print(f"  пропускаем локальную: {title}")
            continue

        text = f"🆕 <b>{site['name']}</b>\n<b>{title}</b>\n"
        if summary:
            text += f"{summary}\n"
        if price:
            text += f"💰 {price}\n"
        if composition:
            text += f"📋 {composition}\n"
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

    if is_init:
        msg = f"✅ <b>Акции</b>: первый запуск, запомнили {len(active)} акций"
        print(msg)
    else:
        msg = f"✅ <b>Акции</b>: готово. Новых {len(new_urls)}, исчезло {len(gone_urls)}"
        print(msg)
    try:
        tg_send(msg)
    except Exception as e:
        print(f"TG finish error: {e}")
    save_active(active)
    print("Готово")


if __name__ == "__main__":
    run()
