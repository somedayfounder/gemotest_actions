#!/usr/bin/env python3
"""
Мониторинг акций медлабораторий.
1. Заходим на страницу списка → собираем ссылки текущих акций
2. Сравниваем с предыдущим запуском → новые и исчезнувшие
3. Для каждой новой акции заходим на её страницу → полный текст
4. Отдаём в GPT → резюме + даты
5. Шлём уведомления
"""

import json, os, re, subprocess, time
from concurrent.futures import ThreadPoolExecutor, as_completed
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

VKT_TOKEN = os.environ.get("VKTEAMS_TOKEN", "")
VKT_CHAT_ID = os.environ.get("VKTEAMS_CHAT_ID", "")
VKT_API = "https://myteam.mail.ru/bot/v1"

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
        "url": "https://dnkom.ru/moscow/actions/",
        "base": "https://dnkom.ru",
        "pattern": r'href="(/actions/[a-z0-9_\-]+/)"',
        "skip": ["/actions/"],
    },
    {
        "name": "LabQuest",
        "url": "https://www.labquest.ru/moscow/aktsii/",
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
        "pre_urls": ["https://kdl.ru/analizy-i-tseny/msk"],  # устанавливает cookie города Москва
        "pattern": r'href="(/?akcii/[a-zA-Z0-9_\-]+)"',
        "skip": ["/akcii", "akcii"],
        "needs_vpn": True,
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

def fetch_html(url, encoding="utf-8", timeout=20, cookie=None):
    headers = dict(HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    req = Request(url, headers=headers)
    return urlopen(req, timeout=timeout).read().decode(encoding, "replace")


def fetch_html_with_session(urls, encoding="utf-8", timeout=20):
    """Открывает URL по очереди с сохранением Set-Cookie между запросами."""
    from http.cookiejar import CookieJar
    from urllib.request import build_opener, HTTPCookieProcessor
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    html = ""
    for url in urls:
        req = Request(url, headers=HEADERS)
        html = opener.open(req, timeout=timeout).read().decode(encoding, "replace")
    return html


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
                        intercepted_debug.append(f"[{rurl}]\n{body[:5000]}")
                    if "yandex" not in rurl and "google" not in rurl and "mc." not in rurl:
                        api_calls.append(rurl)
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            page.goto(url, wait_until="load", timeout=60000)
        except Exception:
            pass
        # прокручиваем страницу чтобы активировать lazy-loading
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
        except Exception:
            pass
        page.wait_for_timeout(wait_ms)

        if api_calls:
            print(f"    API calls: {api_calls[:10]}")
        for d in intercepted_debug[:5]:
            print(f"    intercept_url body: {d[:800]}")

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
        elif site.get("pre_urls"):
            # Сначала открываем pre_urls чтобы установить cookie (напр. выбор города)
            html = fetch_html_with_session(site["pre_urls"] + [site["url"]],
                                           encoding=site.get("encoding", "utf-8"))
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
        if idx != -1:
            html = html[:idx]

    raw = re.findall(site["pattern"], html, re.I)
    # если паттерн с несколькими группами — берём первую непустую
    if raw and isinstance(raw[0], tuple):
        raw = [next((g for g in groups if g), None) for groups in raw]
        raw = [r for r in raw if r]
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
            # ищем ссылки через data-href, to=, \"url\":
            alt = re.findall(rf'(?:data-href|\"to\"|\"url\"|\"href\"|\'to\')[\s:=]+[\"\'](/{key}[^\"\']{{0,100}})', html)[:10]
            if alt:
                print(f"    [{site['name']}] {key}* alt-links: {alt}")
            # ищем embedded JSON в script тегах
            scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
            for sc in scripts:
                if key in sc and len(sc) > 100:
                    print(f"    [{site['name']}] script with {key}: {sc[:300]}")
                    break
        print(f"    [{site['name']}] html_len: {len(html)}")
    skip = set(site.get("skip", []))
    skip_re = re.compile(site["skip_re"]) if site.get("skip_re") else None
    links, seen_slugs = [], set()
    for path in raw:
        if path in skip:
            continue
        if skip_re and skip_re.match(path):
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
        blocks.append(f"URL: {p['url']}\nЛаборатория: {p['lab']}\n\n{p['page_text'][:4000]}")

    prompt = (
        "Ты анализируешь страницы акций медицинских лабораторий.\n"
        "Определи тип акции и верни JSON с ключом = URL и полями:\n\n"
        "  title    — название акции (кратко)\n"
        "  dates    — срок действия (например «до 31 июля»); если не указан — «Бессрочно»\n"
        "  is_local     — true если акция только в одном конкретном филиале, иначе false\n"
        "  is_marketing — true если страница НЕ содержит реальной выгоды для пациента:\n"
        "                 нет скидки, нет спеццены, нет бонуса — это просто описание анализа\n"
        "                 или рекламная статья. false если есть конкретная скидка/цена/бонус.\n"
        "  kind     — «product» если акция на конкретный набор анализов/услуг со своей ценой;\n"
        "             «offer» если это скидка/промокод/условие без фиксированного состава\n\n"
        "Если kind = «product»:\n"
        "  tests      — список ПРОДУКТОВ/ПАКЕТОВ со страницы (то, что можно добавить в корзину),\n"
        "               каждый со своей ценой, формат: «Название — 490 ₽» через newline (\\n).\n"
        "               НЕ расписывай биомаркеры внутри пакета — только сам пакет как единицу.\n"
        "               Если цены нет — просто название. Не сокращай список.\n"
        "  price      — итоговая цена пакета (например «1 490 ₽») или «» если не указана\n"
        "  summary    — «»\n\n"
        "Если kind = «offer»:\n"
        "  summary    — 1–2 предложения: суть предложения и выгода для пациента\n"
        "  tests      — «»\n"
        "  price      — скидка или цена если есть (например «−20%», «от 390 ₽»), иначе «»\n\n"
        'Формат ответа: {"https://...": {"title":"...","dates":"...","is_local":false,"is_marketing":false,"kind":"product",'
        '"tests":"...","price":"...","summary":"..."}, ...}\n\n'
        + "\n\n---\n\n".join(blocks)
    )

    payload = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 3000,
        "temperature": 0,
    }).encode()

    req = Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
    )
    resp = json.loads(urlopen(req, timeout=60).read())
    content = resp["choices"][0]["message"]["content"].strip()
    content = re.sub(r'^```(?:json)?\s*|\s*```$', '', content, flags=re.I).strip()
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
        except Exception as e:
            print(f"  AI батч {i//batch_size + 1} ошибка: {e}")
        if i + batch_size < len(promos):
            time.sleep(1)
    return result


# ── VPN ──────────────────────────────────────────────────────────────────────

def _vpn_start():
    """Пробует vpn0..vpn4.ovpn, возвращает True если получили RU IP."""
    for i in range(5):
        cfg = Path(f"vpn{i}.ovpn")
        if not cfg.exists():
            continue
        print(f"  VPN: пробуем vpn{i}.ovpn…")
        subprocess.run(["sudo", "openvpn", "--config", str(cfg), "--daemon",
                        "--log", "/tmp/vpn.log"], check=False)
        time.sleep(15)
        try:
            ip = urlopen(Request("https://api.ipify.org"), timeout=10).read().decode().strip()
            geo = urlopen(Request(f"https://ipinfo.io/{ip}/country"), timeout=5).read().decode().strip()
            print(f"  VPN IP: {ip} ({geo})")
            if geo == "RU":
                print("  VPN OK — RU")
                return True
        except Exception as e:
            print(f"  VPN IP check error: {e}")
        subprocess.run(["sudo", "pkill", "openvpn"], check=False)
        time.sleep(3)
    print("  VPN: не удалось получить RU IP, работаем без VPN")
    return False


def _vpn_stop():
    subprocess.run(["sudo", "pkill", "openvpn"], check=False)
    time.sleep(2)
    print("  VPN отключён")


# ── Telegram ─────────────────────────────────────────────────────────────────

def tg_send(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urlencode({
        "chat_id": CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    urlopen(Request(url, data=data), timeout=10)


def tg_safe(text, label=""):
    try:
        tg_send(text)
    except Exception as e:
        print(f"TG error{' ' + label if label else ''}: {e}")


def vkt_send(text):
    if not VKT_TOKEN or not VKT_CHAT_ID:
        return
    clean = re.sub(r"<[^>]+>", "", text)
    url = f"{VKT_API}/messages/sendText"
    data = urlencode({"token": VKT_TOKEN, "chatId": VKT_CHAT_ID, "text": clean}).encode()
    urlopen(Request(url, data=data), timeout=10)


def notify(text, label=""):
    """Отправить в Telegram и VK Teams."""
    tg_safe(text, label)
    try:
        vkt_send(text)
    except Exception as e:
        print(f"VKT error{' ' + label if label else ''}: {e}")


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
    current = {}        # url -> site
    site_urls = {}      # site_name -> [urls]

    notify("Проверяем новые акции...", "start")

    # 1. Собираем листинги
    kdl_only  = bool(os.environ.get("KDL_ONLY"))
    skip_kdl  = bool(os.environ.get("SKIP_KDL"))
    vpn_external = kdl_only  # VPN поднят снаружи

    vpn_sites = [s for s in SITES if s.get("needs_vpn")]
    normal_sites = [s for s in SITES if not s.get("needs_vpn")]

    if not kdl_only:
        for site in normal_sites:
            links = get_listing_links(site)
            print(f"  {site['name']}: {len(links)} акций")
            site_urls[site["name"]] = links
            for url in links:
                current[url] = site

    if vpn_sites and not skip_kdl:
        if vpn_external:
            vpn_started = True
        else:
            vpn_started = _vpn_start()
        if vpn_started:
            for site in vpn_sites:
                links = get_listing_links(site)
                print(f"  {site['name']}: {len(links)} акций (VPN)")
                site_urls[site["name"]] = links
                for url in links:
                    current[url] = site
        else:
            for site in vpn_sites:
                site_urls[site["name"]] = []
        if not vpn_external:
            _vpn_stop()

    # Сравниваем только по сайтам, которые реально проверялись в этом запуске
    checked_sites = set(site_urls.keys())
    active_checked = {u: v for u, v in active.items() if v.get("lab") in checked_sites}

    new_urls = [u for u in current if u not in active]
    gone_urls = [u for u in active_checked if u not in current]
    print(f"Новых: {len(new_urls)}, исчезло: {len(gone_urls)}")

    # 2. Сводка по сайтам — разделяем на "есть новые" и "нет новых"
    new_by_site = {}
    for url in new_urls:
        name = current[url]["name"]
        new_by_site[name] = new_by_site.get(name, 0) + 1

    with_new, without_new = [], []
    for site in SITES:
        name = site["name"]
        if name not in checked_sites:
            continue
        total = len(site_urls.get(name, []))
        n = new_by_site.get(name, 0)
        if n:
            with_new.append(f"{name} ({total} / {n} {'новая' if n == 1 else 'новых'})")
        else:
            without_new.append(f"{name} ({total})")

    summary_parts = []
    if without_new:
        summary_parts.append("Нет новых: " + ", ".join(without_new))
    if with_new:
        summary_parts.append("Есть новые: " + ", ".join(with_new))
    notify("\n".join(summary_parts), "listing")

    # Защита от сбоя сети/VPN (только если проверяли все сайты)
    if not kdl_only and not skip_kdl and len(current) < 10 and len(gone_urls) > 5:
        msg = f"Нашли только {len(current)} акций (обычно 100+). Возможен сбой VPN ⚠️"
        print(msg)
        notify(msg, "safety")
        save_active(active)
        return

    # 3. Загружаем страницы новых акций
    promos_to_analyze = []
    if new_urls:
        notify(f"Сканируем {len(new_urls)} {'новую акцию' if len(new_urls) == 1 else 'новых акции' if len(new_urls) < 5 else 'новых акций'}...", "fetch")

        def _fetch_one(url):
            site = current[url]
            print(f"  загружаем {url}")
            return url, fetch_promo_page(url, site)

        with ThreadPoolExecutor(max_workers=8) as ex:
            for url, page_text in ex.map(_fetch_one, new_urls):
                site = current[url]
                promos_to_analyze.append({"lab": site["name"], "url": url, "page_text": page_text})

    # 4. GPT-анализ
    ai = {}
    if promos_to_analyze:
        notify("Обработка GPT-4o...", "gpt")
        try:
            ai = ai_analyze(promos_to_analyze)
            print(f"AI: {len(ai)} проанализировано")
        except Exception as e:
            print(f"AI ошибка: {e}")

    # 5. Уведомления о новых акциях
    for url in new_urls:
        info = ai.get(url, {})
        site = current[url]
        title = info.get("title") or url.rstrip("/").split("/")[-1]
        summary = info.get("summary", "")
        dates = info.get("dates", "") or "Бессрочно"
        price = info.get("price", "")
        is_local = info.get("is_local", False)
        kind = info.get("kind", "offer")
        tests = info.get("tests", "")

        active[url] = {
            "lab": site["name"], "title": title, "summary": summary,
            "dates": dates, "price": price, "kind": kind,
        }

        if is_local:
            print(f"  пропускаем локальную: {title}")
            continue
        if info.get("is_marketing", False):
            print(f"  пропускаем маркетинговую: {title}")
            continue

        lines = [site["name"], "", title, ""]
        if kind == "product" and tests:
            lines += [tests, ""]
            if price:
                lines += [price, ""]
        else:
            if summary:
                lines += [summary, ""]
            if price:
                lines += [price, ""]
        lines.append(dates)
        lines.append(url)

        text = "\n".join(lines)
        if len(text) > 4090:
            text = text[:4090] + "…"
        notify(text)
        time.sleep(0.3)

    # 6. Исчезнувшие акции — одним сообщением
    if gone_urls:
        gone_lines = [f"Завершены {len(gone_urls)} {'акция' if len(gone_urls) == 1 else 'акции' if len(gone_urls) < 5 else 'акций'}:", ""]
        for url in gone_urls:
            info = active.pop(url)
            gone_lines.append(info.get("title") or url)
            gone_lines.append(url)
            gone_lines.append("")
        notify("\n".join(gone_lines).strip(), "gone")

    if is_init:
        notify(f"Первый запуск, запомнили {len(active)} акций", "finish")
    save_active(active)
    print("Готово")


if __name__ == "__main__":
    run()
