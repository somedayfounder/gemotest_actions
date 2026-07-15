#!/usr/bin/env python3
"""
Мониторинг новостей и статей медлаб.
Запускается ежедневно, шлёт только новые материалы в Telegram.
"""
import json, os, re, time
from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

TG_TOKEN   = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

DATA_DIR  = Path(__file__).parent
SEEN_FILE = DATA_DIR / "seen_content.json"

NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "identity",
}

_INVITRO_YEARS = list(range(2005, date.today().year + 1))

# ─── источники ────────────────────────────────────────────────────────────────
# method:
#   html_links  — одна страница, regex
#   paged_html  — пагинация ?PARAM=N; extra = (pattern, start_page[, param_name])
#   sitemap     — XML sitemap, optional filter fn
# needs_vpn=True — пропускается если VPN_READY не установлен

SOURCES = [
    ("Гемотест",  "news",    "paged_html",  "https://gemotest.ru/info/news/",
     (r'href="(/info/news/\d[^"?#]{3,})"', 1)),
    ("Гемотест",  "article", "sitemap",     "https://gemotest.ru/sitemap/ru/sitemap-iblocks/sitemap-info-articles.xml",
     lambda l: "/info/" in l and "/info/news/" not in l and l.count("/") > 3),

    ("CMD",       "news",    "sitemap",     "https://www.cmd-online.ru/sitemap-iblock-6.xml",
     lambda l: "/o-cmd/news/" in l and l.count("/") > 5),
    ("CMD",       "article", "paged_html",  "https://www.cmd-online.ru/patsientam/poleznyye-statii/",
     (r'href="(/patsientam/poleznyye-statii/[^"?#]{5,})"', 1)),

    ("Helix",     "article", "sitemap",     "https://helix.ru/sitemap-kb.xml", None),

    ("ДНКом",     "news",    "paged_html",  "https://dnkom.ru/o-kompanii/novosti/",
     (r'href="(/o-kompanii/novosti/[^"?#]{10,})"', 1)),
    # ДНКом articles — обрабатывается через get_dnkom_articles() в run()


    ("LabQuest",  "news",    "sitemap",     "https://www.labquest.ru/sitemap-iblock-12.xml",
     lambda l: "/novosti/" in l),
    ("LabQuest",  "article", "paged_html",  "https://www.labquest.ru/articles/",
     (r'href="(/articles/[^"?#]{5,})"', 1)),

    ("Ситилаб",   "news",    "sitemap",     "https://citilab.ru/sitemaps/news.xml",     None),
    ("Ситилаб",   "article", "sitemap",     "https://citilab.ru/sitemaps/articles.xml", None),

    ("Инвитро",   "article", "sitemap",     "https://www.invitro.ru/sitemap/library.xml",
     lambda l: "/library/" in l and l.count("/") > 4),

    # КДЛ — только с VPN; новости и статьи через HTML (sitemap содержит только анализы)
    ("КДЛ",       "news",    "paged_html",  "https://kdl.ru/press-center/",
     (r'href="(/press-center/[^"?#]{5,})"', 1)),
    ("КДЛ",       "article", "paged_html",  "https://kdl.ru/blog/",
     (r'href="(/blog/[^"?#]{5,})"', 1)),
]

# Источники только под VPN
VPN_LABS = {"КДЛ"}

# ─── helpers ──────────────────────────────────────────────────────────────────

def fetch(url, encoding="utf-8", timeout=20):
    req = Request(url, headers=HEADERS)
    r = urlopen(req, timeout=timeout)
    return r.read().decode(encoding, "replace")


def fetch_retry(url, encoding="utf-8", retries=2):
    for attempt in range(retries + 1):
        try:
            return fetch(url, encoding)
        except Exception as e:
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))


def get_title(url, encoding="utf-8"):
    """Извлекает заголовок страницы из <h1> или <title>."""
    try:
        html = fetch(url, encoding, timeout=12)
        h1 = re.search(r'<h1[^>]*>([^<]{3,200})</h1>', html)
        if h1:
            return re.sub(r'\s+', ' ', h1.group(1)).strip()
        title = re.search(r'<title>([^<]{3,200})</title>', html)
        if title:
            return re.sub(r'\s+', ' ', title.group(1)).strip().split('|')[0].split('—')[0].strip()
    except:
        pass
    return None


def get_html_links(url, pattern, encoding="utf-8"):
    html = fetch(url, encoding)
    return list(dict.fromkeys(re.findall(pattern, html)))


def get_paged_html_links(base_url, pattern, start_page=1, pagen_param="PAGEN_1", progress_cb=None, already_seen=None):
    """Итерирует ?PARAM=N.
    Останавливается когда страница не даёт ни одной новой ссылки (относительно already_seen)
    или когда первая ссылка повторяется (конец пагинации).
    """
    all_links = []
    seen_on_pages = set()
    page = start_page
    while True:
        url = base_url if page == start_page else f"{base_url}?{pagen_param}={page}"
        try:
            html = fetch_retry(url)
        except Exception as e:
            print(f"  paged page {page}: ❌ {e}")
            break
        links = list(dict.fromkeys(re.findall(pattern, html)))
        if not links:
            break
        if links[0] in seen_on_pages:
            break
        for l in links:
            seen_on_pages.add(l)
            if l not in all_links:
                all_links.append(l)
        if progress_cb and (page - start_page + 1) % 10 == 0:
            progress_cb(page, len(all_links))
        # Стоп когда все ссылки страницы уже есть в seen (сравниваем с полными URL)
        if already_seen is not None:
            proto_host = base_url.split("/")[0] + "//" + base_url.split("/")[2]
            full = [proto_host + l if l.startswith("/") else l for l in links]
            if all(l in already_seen for l in full):
                break
        page += 1
        time.sleep(0.2)
    return all_links


def get_sitemap_links(url, filter_fn=None):
    html = fetch(url)
    # sitemap index → рекурсивно обходим дочерние sitemaps
    if "<sitemapindex" in html:
        tree = ET.fromstring(html)
        all_urls = []
        children = [s.findtext("s:loc", namespaces=NS) for s in tree.findall("s:sitemap", NS)]
        children = [c for c in children if c]
        print(f"  sitemap index: {len(children)} дочерних")
        for child in children:
            try:
                child_html = fetch(child)
                child_tree = ET.fromstring(child_html)
                child_urls = [u.findtext("s:loc", namespaces=NS) for u in child_tree.findall("s:url", NS)]
                all_urls.extend(u for u in child_urls if u)
            except Exception as e:
                print(f"  ❌ child sitemap {child}: {e}")
        urls = all_urls
    else:
        tree = ET.fromstring(html)
        urls = [u.findtext("s:loc", namespaces=NS) for u in tree.findall("s:url", NS)]
        urls = [u for u in urls if u]
    if filter_fn:
        # Диагностика для КДЛ: показать уникальные сегменты путей
        if "kdl.ru" in url and not any(filter_fn(u) for u in urls[:100]):
            segments = set()
            for u in urls[:200]:
                parts = u.replace("https://kdl.ru/", "").split("/")
                if parts:
                    segments.add(parts[0])
            print(f"  КДЛ sitemap segments: {sorted(segments)[:20]}")
        urls = [u for u in urls if filter_fn(u)]
    return urls


def get_invitro_news(progress_cb=None):
    """Собирает новости Инвитро по годам и месяцам 2005-текущий."""
    pattern = r'href="(/moscow/about/news/(?!year)[^"?#]{2,})"'
    today = date.today()
    all_links = []
    for year in _INVITRO_YEARS:
        year_count = 0
        for month in range(1, 13):
            if year == today.year and month > today.month:
                break
            try:
                url = f"https://www.invitro.ru/moscow/about/news/year-{year}/{month:02d}/"
                links = list(dict.fromkeys(re.findall(pattern, fetch_retry(url, retries=3))))
                new = [l for l in links if l not in all_links]
                all_links.extend(new)
                year_count += len(new)
                time.sleep(0.5 if year < 2010 else 0.2)
            except Exception as e:
                print(f"    Инвитро {year}/{month:02d}: ❌ {e}")
        print(f"  Инвитро news {year}: {year_count} уникальных")
        if progress_cb:
            progress_cb(year, len(all_links))
    return all_links


def get_helix_news_all(seen_urls):
    """Последовательный обход /feed/select/N с допуском пропусков."""
    id_pat = re.compile(r'helix\.ru/feed/select/(\d+)')
    known = [int(m.group(1)) for k in seen_urls if (m := id_pat.search(k))]
    start = max(known) + 1 if known else 18
    found = []
    n = start
    miss = 0
    while miss < 50:
        url = f"https://helix.ru/feed/select/{n}"
        try:
            html = fetch(url, timeout=10)
            if len(html) >= 2000:
                found.append(url)
                miss = 0
            else:
                if n == start:
                    print(f"  Helix {n}: size={len(html)} (< 2000), первый ответ")
                miss += 1
        except Exception as e:
            if n == start:
                print(f"  Helix {n}: ❌ {e}")
            miss += 1
        n += 1
        time.sleep(0.15)
    return found


def get_dnkom_articles(already_seen=None, progress_cb=None):
    """Статьи ДНКом через PAGEN_2; останавливается когда страница не даёт новых ссылок."""
    base = "https://dnkom.ru/o-kompanii/stati/"
    pattern = r'href="(/o-kompanii/stati/(?!tag)[^"?#]{5,})"'
    all_links = []
    no_new = 0
    page = 1
    while no_new < 3 and page <= 30:
        url = base if page == 1 else f"{base}?PAGEN_2={page}"
        try:
            html = fetch_retry(url)
            links = list(dict.fromkeys(re.findall(pattern, html)))
            new = [l for l in links if l not in all_links]
            all_links.extend(new)
            # Стоп если страница полностью известна И мы уже прошли хотя бы 3 страницы
            if already_seen is not None and page >= 3:
                full = ["https://dnkom.ru" + l if l.startswith("/") else l for l in links]
                if all(l in already_seen for l in full):
                    break
            no_new = 0 if new else no_new + 1
            if progress_cb and page % 5 == 0:
                progress_cb(page, len(all_links))
        except Exception as e:
            print(f"  ДНКом articles p{page}: ❌ {e}")
            no_new += 1
        page += 1
        time.sleep(0.3)
    return all_links


def get_gorlab_news(last_page):
    """Проверяем страницы pageN+1, pageN+2 ... пока существуют."""
    found = []
    n = last_page + 1
    while True:
        url = f"https://gorlab.ru/news/page{n}.html"
        try:
            html = fetch(url, encoding="windows-1251")
            if len(html) < 10000 or "Горлаб" not in html:
                break
            found.append(url)
            n += 1
            time.sleep(0.3)
        except:
            break
    return found, n - 1 if found else last_page


def get_gorlab_book(last_item):
    """Проверяем /book/itemN.html начиная с last_item+1."""
    found = []
    n = last_item + 1
    while True:
        url = f"https://gorlab.ru/book/item{n}.html"
        try:
            html = fetch(url, encoding="windows-1251")
            if len(html) < 5000:
                break
            found.append(url)
            n += 1
            time.sleep(0.3)
        except:
            break
    return found, n - 1 if found else last_item


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


def notify_new(lab, typ, new_links, is_init):
    if not new_links or is_init:
        return
    label = "новости" if typ == "news" else "статьи"
    lines = [f"<b>{'📰' if typ == 'news' else '📄'} {lab} — {label}</b>"]
    for l in new_links[:5]:
        lines.append(f"<a href=\"{l}\">{l.rstrip('/').split('/')[-1]}</a>")
    if len(new_links) > 5:
        lines.append(f"...и ещё {len(new_links) - 5}")
    tg_safe("\n".join(lines), f"{lab}-{typ}")
    time.sleep(0.5)


def _send_stats(stats, new_count, is_init, vpn_only=False):
    if not stats:
        return
    lines = ["📊 <b>Контент" + (" VPN" if vpn_only else "") + (" (init)" if is_init else "") + "</b>"]
    for key, (total, new) in stats.items():
        mark = "🆕" if new > 0 else "·"
        total_str = f"/{total}" if total else ""
        lines.append(f"{mark} {key}: {new}{total_str}")
    if not is_init:
        lines.append(f"<b>Новых: {new_count}</b>")
    tg_safe("\n".join(lines), "stats")


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2))


# ─── run ──────────────────────────────────────────────────────────────────────

def run():
    if not TG_TOKEN:
        cfg = json.loads((DATA_DIR / "tg_config_labs.json").read_text())
        globals().update(TG_TOKEN=cfg["token"], TG_CHAT_ID=str(cfg["chat_id"]))

    seen = load_seen()
    is_init = not seen
    today = date.today().isoformat()
    new_count = 0
    vpn_ready = bool(os.environ.get("VPN_READY"))
    vpn_only  = bool(os.environ.get("VPN_ONLY"))

    label = "VPN" if vpn_only else "контент"
    tg_safe(f"🔄 Сканируем {label}{'  (init)' if is_init else ''}...")

    stats = {}

    def tg_step(key, fn, *args, **kwargs):
        tg_safe(f"⏳ {key}...")
        t0 = time.time()
        try:
            result = fn(*args, **kwargs)
            elapsed = int(time.time() - t0)
            return result, elapsed
        except Exception as e:
            elapsed = int(time.time() - t0)
            tg_safe(f"❌ {key} ({elapsed}s): {e}")
            print(f"❌ {key}: {e}")
            return None, elapsed

    def done(key, total, new, elapsed):
        stats[key] = (total, new)
        mark = "🆕" if new > 0 else "✅"
        tg_safe(f"{mark} {key}: {new} новых / {total} ({elapsed}s)")
        print(f"{key}: {total} всего, {new} новых ({elapsed}s)")

    # Обычные источники
    for lab, typ, method, url, extra in SOURCES:
        needs_vpn = lab in VPN_LABS
        if vpn_only and not needs_vpn:
            continue
        if needs_vpn and not vpn_ready:
            print(f"⏭ {lab} {typ}: пропущен (нет VPN)")
            continue
        key = f"{lab} {typ}"
        tg_safe(f"⏳ {key}...")
        t0 = time.time()
        try:
            enc = "windows-1251" if "gorlab" in url else "utf-8"
            if method == "html_links":
                links = get_html_links(url, extra, enc)
                links = ["https://" + url.split("/")[2] + l if l.startswith("/") else l for l in links]
            elif method == "paged_html":
                pagen = extra[2] if len(extra) > 2 else "PAGEN_1"
                cb = lambda p, n, k=key: tg_safe(f"  ↳ {k}: стр.{p}, найдено {n}...")
                links = get_paged_html_links(url, extra[0], extra[1], pagen, progress_cb=cb, already_seen=seen)
                links = ["https://" + url.split("/")[2] + l if l.startswith("/") else l for l in links]
            elif method == "sitemap":
                links = get_sitemap_links(url, filter_fn=extra if callable(extra) else None)
            else:
                continue
            new_links = [l for l in links if l not in seen]
            elapsed = int(time.time() - t0)
            # links — только с проверенных страниц; для display берём из seen сколько всего знаем
            known_total = sum(1 for v in seen.values() if isinstance(v, dict) and v.get("lab") == lab and v.get("type") == typ)
            display_total = known_total + len(new_links) if known_total else len(links)
            done(key, display_total, len(new_links), elapsed)
            enc = "windows-1251" if "gorlab" in url else "utf-8"
            fetch_titles = not is_init and len(new_links) <= 50
            for link in new_links:
                title = get_title(link, enc) if fetch_titles else None
                seen[link] = {"lab": lab, "type": typ, "date": today, "title": title}
            notify_new(lab, typ, new_links, is_init)
            new_count += len(new_links) if not is_init else 0
        except Exception as e:
            elapsed = int(time.time() - t0)
            tg_safe(f"❌ {key} ({elapsed}s): {e}")
            print(f"❌ {key}: {e}")
        time.sleep(0.3)

    if vpn_only:
        _send_stats(stats, new_count, is_init, vpn_only=True)
        save_seen(seen)
        print("Готово")
        return

    # Инвитро новости — по месяцам 2005-текущий
    inv_cb = lambda yr, n: tg_safe(f"  ↳ Инвитро news: год {yr}, найдено {n}...")
    inv_links_raw, elapsed = tg_step("Инвитро news (2005–сейчас)", get_invitro_news, inv_cb)
    if inv_links_raw is not None:
        inv_links = ["https://www.invitro.ru" + l for l in inv_links_raw]
        new_inv = [l for l in inv_links if l not in seen]
        done("Инвитро news", len(inv_links), len(new_inv), elapsed)
        fetch_titles_inv = not is_init and len(new_inv) <= 50
        for link in new_inv:
            title = get_title(link) if fetch_titles_inv else None
            seen[link] = {"lab": "Инвитро", "type": "news", "date": today, "title": title}
        notify_new("Инвитро", "news", new_inv, is_init)
        new_count += len(new_inv) if not is_init else 0

    # Helix новости — sequential scan
    helix_all, elapsed = tg_step("Helix news (sequential scan)", get_helix_news_all, seen)
    if helix_all is not None:
        new_helix = [l for l in helix_all if l not in seen]
        known_helix = sum(1 for v in seen.values() if isinstance(v, dict) and v.get("lab") == "Helix" and v.get("type") == "news")
        done("Helix news", known_helix + len(new_helix), len(new_helix), elapsed)
        fetch_titles_hx = not is_init and len(new_helix) <= 50
        for u in new_helix:
            title = get_title(u) if fetch_titles_hx else None
            seen[u] = {"lab": "Helix", "type": "news", "date": today, "title": title}
        notify_new("Helix", "news", new_helix, is_init)
        new_count += len(new_helix) if not is_init else 0

    # ДНКом статьи — PAGEN_2
    dnk_cb = lambda p, n: tg_safe(f"  ↳ ДНКом article: стр.{p}, найдено {n}...")
    dnkom_raw, elapsed = tg_step("ДНКом article (PAGEN_2)", get_dnkom_articles, seen, dnk_cb)
    if dnkom_raw is not None:
        dnkom_links = ["https://dnkom.ru" + l if l.startswith("/") else l for l in dnkom_raw]
        new_dnkom = [l for l in dnkom_links if l not in seen]
        done("ДНКом article", len(dnkom_links), len(new_dnkom), elapsed)
        fetch_titles_dk = not is_init and len(new_dnkom) <= 50
        for link in new_dnkom:
            title = get_title(link) if fetch_titles_dk else None
            seen[link] = {"lab": "ДНКом", "type": "article", "date": today, "title": title}
        notify_new("ДНКом", "article", new_dnkom, is_init)
        new_count += len(new_dnkom) if not is_init else 0

    # Горлаб новости
    last_page = seen.get("_gorlab_last_page", 81)
    gorlab_news_res, elapsed = tg_step("Горлаб news (sequential pages)", get_gorlab_news, last_page)
    if gorlab_news_res is not None:
        new_pages, last_page = gorlab_news_res
        seen["_gorlab_last_page"] = last_page
        done("Горлаб news", last_page, len(new_pages), elapsed)
        fetch_titles_gl = not is_init and len(new_pages) <= 50
        for u in new_pages:
            title = get_title(u, "windows-1251") if fetch_titles_gl else None
            seen[u] = {"lab": "Горлаб", "type": "news", "date": today, "title": title}
        notify_new("Горлаб", "news", new_pages, is_init)
        new_count += len(new_pages) if not is_init else 0

    # Горлаб статьи
    last_item = seen.get("_gorlab_last_book_item", 100)
    gorlab_art_res, elapsed = tg_step("Горлаб article (sequential items)", get_gorlab_book, last_item)
    if gorlab_art_res is not None:
        new_items, last_item = gorlab_art_res
        seen["_gorlab_last_book_item"] = last_item
        done("Горлаб article", last_item, len(new_items), elapsed)
        fetch_titles_gl2 = not is_init and len(new_items) <= 50
        for u in new_items:
            title = get_title(u, "windows-1251") if fetch_titles_gl2 else None
            seen[u] = {"lab": "Горлаб", "type": "article", "date": today, "title": title}
        notify_new("Горлаб", "article", new_items, is_init)
        new_count += len(new_items) if not is_init else 0

    if is_init:
        print(f"Первый запуск, запомнили {len(seen)} материалов")
    else:
        print(f"Новых материалов: {new_count}")

    _send_stats(stats, new_count, is_init)
    save_seen(seen)
    print("Готово")


if __name__ == "__main__":
    run()
