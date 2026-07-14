#!/usr/bin/env python3
"""
Мониторинг новостей и статей медлаб.
Запускается ежедневно, шлёт только новые материалы в Telegram.
"""
import json, os, re, time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

TG_TOKEN  = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

DATA_DIR  = Path(__file__).parent
SEEN_FILE = DATA_DIR / "seen_content.json"

NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

_INVITRO_YEARS = list(range(2018, date.today().year + 1))

# ─── источники ────────────────────────────────────────────────────────────────
# method:
#   html_links   — одна страница, regex
#   paged_html   — пагинация через ?PAGEN_1=N, regex; extra = (pattern, start_page)
#   sitemap      — XML sitemap, optional filter fn

SOURCES = [
    ("Гемотест",  "news",    "paged_html",  "https://gemotest.ru/info/news/",
     (r'href="(/info/news/\d[^"?#]{3,})"', 1)),
    ("Гемотест",  "article", "sitemap",     "https://gemotest.ru/sitemap/ru/sitemap-iblocks/sitemap-info-articles.xml",
     lambda l: "/info/" in l and "/info/news/" not in l and l.count("/") > 3),

    ("CMD",       "news",    "sitemap",     "https://www.cmd-online.ru/sitemap-iblock-6.xml",
     lambda l: "/o-cmd/news/" in l and l.count("/") > 5),
    ("CMD",       "article", "paged_html",  "https://www.cmd-online.ru/patsientam/poleznyye-statii/",
     (r'href="(/patsientam/poleznyye-statii/[^"?#]{5,})"', 1)),

    ("Helix",     "article", "sitemap",     "https://helix.ru/sitemap-kb.xml",     None),

    ("ДНКом",     "news",    "paged_html",  "https://dnkom.ru/o-kompanii/novosti/",
     (r'href="(/o-kompanii/novosti/[^"?#]{10,})"', 1)),
    ("ДНКом",     "article", "html_links",  "https://dnkom.ru/o-kompanii/stati/",
     r'href="(/o-kompanii/stati/(?!tag)[^"?#]{5,})"'),

    ("LabQuest",  "news",    "sitemap",     "https://www.labquest.ru/sitemap-iblock-12.xml",
     lambda l: "/novosti/" in l),
    ("LabQuest",  "article", "paged_html",  "https://www.labquest.ru/articles/",
     (r'href="(/articles/[^"?#]{5,})"', 1)),

    ("Ситилаб",   "news",    "sitemap",     "https://citilab.ru/sitemaps/news.xml",     None),
    ("Ситилаб",   "article", "sitemap",     "https://citilab.ru/sitemaps/articles.xml", None),

    ("Горлаб",    "article", "html_links",  "https://gorlab.ru/book/",
     r'href="(/book/[^"?#]{5,})"'),
    # Горлаб новости — sequential pages, обрабатывается отдельно
    # Инвитро новости — по годам, обрабатывается отдельно
    # Инвитро статьи — sitemap
    ("Инвитро",   "article", "sitemap",     "https://www.invitro.ru/sitemap/library.xml",
     lambda l: "/library/" in l and l.count("/") > 4),
]


# ─── helpers ──────────────────────────────────────────────────────────────────

def fetch(url, encoding="utf-8"):
    req = Request(url, headers=HEADERS)
    r = urlopen(req, timeout=15)
    return r.read().decode(encoding, "replace")


def get_html_links(url, pattern, encoding="utf-8"):
    html = fetch(url, encoding)
    return list(dict.fromkeys(re.findall(pattern, html)))


def get_paged_html_links(base_url, pattern, start_page=1):
    """Итерирует ?PAGEN_1=N пока страницы не повторяются или не пустые."""
    all_links = []
    seen_on_pages = set()
    page = start_page
    while True:
        url = f"{base_url}?PAGEN_1={page}"
        try:
            html = fetch(url)
        except Exception as e:
            print(f"  paged {base_url} page {page}: ❌ {e}")
            break
        links = list(dict.fromkeys(re.findall(pattern, html)))
        if not links:
            break
        # Если первая ссылка страницы уже видели — дошли до конца (зациклилось)
        if links[0] in seen_on_pages:
            break
        for l in links:
            seen_on_pages.add(l)
            if l not in all_links:
                all_links.append(l)
        page += 1
        time.sleep(0.2)
    return all_links


def get_sitemap_links(url, filter_fn=None):
    html = fetch(url)
    tree = ET.fromstring(html)
    urls = [u.findtext("s:loc", namespaces=NS) for u in tree.findall("s:url", NS)]
    urls = [u for u in urls if u]
    if filter_fn:
        urls = [u for u in urls if filter_fn(u)]
    return urls


def get_invitro_news():
    """Собирает новости Инвитро по годам 2018-текущий."""
    pattern = r'href="(/moscow/about/news/(?!year)[^"?#]{10,})"'
    all_links = []
    for year in _INVITRO_YEARS:
        try:
            url = f"https://www.invitro.ru/moscow/about/news/year-{year}/"
            html = fetch(url)
            links = list(dict.fromkeys(re.findall(pattern, html)))
            print(f"  Инвитро news {year}: {len(links)}")
            all_links.extend(l for l in links if l not in all_links)
            time.sleep(0.3)
        except Exception as e:
            print(f"  Инвитро news {year}: ❌ {e}")
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

    # Обычные источники
    for lab, typ, method, url, extra in SOURCES:
        try:
            enc = "windows-1251" if "gorlab" in url else "utf-8"
            if method == "html_links":
                links = get_html_links(url, extra, enc)
                links = ["https://" + url.split("/")[2] + l if l.startswith("/") else l for l in links]
            elif method == "paged_html":
                pattern, start = extra
                links = get_paged_html_links(url, pattern, start)
                links = ["https://" + url.split("/")[2] + l if l.startswith("/") else l for l in links]
            elif method == "sitemap":
                links = get_sitemap_links(url, filter_fn=extra if callable(extra) else None)
            else:
                continue

            new_links = [l for l in links if l not in seen]
            print(f"{lab} {typ}: {len(links)} всего, {len(new_links)} новых")

            for link in new_links:
                seen[link] = {"lab": lab, "type": typ, "date": today}

            if new_links and not is_init:
                label = "новости" if typ == "news" else "статьи"
                lines = [f"<b>{'📰' if typ == 'news' else '📄'} {lab} — {label}</b>"]
                for l in new_links[:5]:
                    lines.append(f"<a href=\"{l}\">{l.rstrip('/').split('/')[-1]}</a>")
                if len(new_links) > 5:
                    lines.append(f"...и ещё {len(new_links) - 5}")
                tg_safe("\n".join(lines), f"{lab}-{typ}")
                time.sleep(0.5)
                new_count += len(new_links)

        except Exception as e:
            print(f"❌ {lab} {typ}: {e}")

        time.sleep(0.3)

    # Инвитро новости — по годам
    try:
        inv_links = get_invitro_news()
        inv_links_full = ["https://www.invitro.ru" + l for l in inv_links]
        new_inv = [l for l in inv_links_full if l not in seen]
        print(f"Инвитро news: {len(inv_links_full)} всего, {len(new_inv)} новых")
        for link in new_inv:
            seen[link] = {"lab": "Инвитро", "type": "news", "date": today}
        if new_inv and not is_init:
            lines = ["<b>📰 Инвитро — новости</b>"]
            for l in new_inv[:5]:
                lines.append(f"<a href=\"{l}\">{l.rstrip('/').split('/')[-1]}</a>")
            if len(new_inv) > 5:
                lines.append(f"...и ещё {len(new_inv) - 5}")
            tg_safe("\n".join(lines), "invitro-news")
            new_count += len(new_inv)
    except Exception as e:
        print(f"❌ Инвитро news: {e}")

    # Горлаб новости — sequential
    last_page = seen.get("_gorlab_last_page", 0)
    try:
        new_pages, last_page = get_gorlab_news(last_page)
        seen["_gorlab_last_page"] = last_page
        if new_pages:
            print(f"Горлаб news: {len(new_pages)} новых страниц")
            for url in new_pages:
                seen[url] = {"lab": "Горлаб", "type": "news", "date": today}
            if not is_init:
                lines = ["<b>📰 Горлаб — новости</b>"]
                for url in new_pages:
                    lines.append(f"<a href=\"{url}\">{url}</a>")
                tg_safe("\n".join(lines), "gorlab-news")
                new_count += len(new_pages)
        else:
            print(f"Горлаб news: 0 новых (последняя страница {last_page})")
    except Exception as e:
        print(f"❌ Горлаб news: {e}")

    if is_init:
        print(f"Первый запуск, запомнили {len(seen)} материалов")
    else:
        print(f"Новых материалов: {new_count}")

    save_seen(seen)
    print("Готово")


if __name__ == "__main__":
    run()
