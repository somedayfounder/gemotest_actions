#!/usr/bin/env python3
"""Генерирует stats.html со статистикой по акциям, новостям и статьям."""
import json
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(__file__).parent

LABS = ["Гемотест", "CMD", "Helix", "ДНКом", "LabQuest", "КДЛ", "Инвитро", "Ситилаб", "Горлаб"]

TYPE_LABELS = {"news": "Новости", "article": "Статьи"}


def load_json(path):
    p = DATA_DIR / path
    if p.exists():
        return json.loads(p.read_text())
    return {}


def run():
    promos   = load_json("seen_promos.json")
    news     = load_json("seen_news.json")    # list of URLs
    content  = load_json("seen_content.json") # url -> {lab, type, date}

    # Акции по лабам
    promo_by_lab = defaultdict(int)
    for url, info in promos.items():
        if isinstance(info, dict):
            promo_by_lab[info.get("lab", "?")] += 1

    # Контент по лабам и типам
    content_by_lab = defaultdict(lambda: defaultdict(int))
    for url, info in content.items():
        if isinstance(info, dict) and "lab" in info:
            content_by_lab[info["lab"]][info.get("type", "?")] += 1

    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    rows = []
    for lab in LABS:
        promo = promo_by_lab.get(lab, 0)
        news_c = content_by_lab[lab].get("news", 0)
        art_c  = content_by_lab[lab].get("article", 0)
        rows.append(f"""
        <tr>
            <td class="lab">{lab}</td>
            <td class="num {'pos' if promo else 'zero'}">{promo}</td>
            <td class="num {'pos' if news_c else 'zero'}">{news_c}</td>
            <td class="num {'pos' if art_c else 'zero'}">{art_c}</td>
        </tr>""")

    total_promos  = sum(promo_by_lab.values())
    total_news_g  = len(news) if isinstance(news, list) else 0
    total_news_c  = sum(v.get("news", 0) for v in content_by_lab.values())
    total_art     = sum(v.get("article", 0) for v in content_by_lab.values())

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Медлабы — статистика</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;background:#f5f5f3;color:#1a1a19;padding:24px}}
h1{{font-size:18px;font-weight:600;margin-bottom:4px}}
.sub{{color:#888;font-size:12px;margin-bottom:24px}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:28px}}
.card{{background:#fff;border:1px solid #e0dfd8;border-radius:10px;padding:16px 22px;min-width:140px}}
.card .val{{font-size:28px;font-weight:600;color:#1d4ed8}}
.card .lbl{{font-size:11px;color:#888;margin-top:2px}}
.tbl-wrap{{background:#fff;border:1px solid #e0dfd8;border-radius:10px;overflow:hidden}}
table{{width:100%;border-collapse:collapse}}
thead th{{background:#f8f8f6;font-size:11px;font-weight:500;color:#666;padding:10px 14px;text-align:left;border-bottom:1px solid #e8e8e0}}
td{{padding:9px 14px;border-bottom:0.5px solid #eee;vertical-align:middle}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#fafaf8}}
td.lab{{font-weight:500}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}}
td.pos{{color:#166534;font-weight:600}}
td.zero{{color:#ccc}}
@media(prefers-color-scheme:dark){{
  body{{background:#1a1a1a;color:#e5e5e5}}
  .card{{background:#2a2a2a;border-color:#333}}
  .card .lbl{{color:#aaa}}
  thead th{{background:#222;color:#aaa;border-color:#333}}
  td{{border-color:#2a2a2a}}
  tr:hover td{{background:#222}}
  .tbl-wrap{{background:#2a2a2a;border-color:#333}}
  .sub{{color:#aaa}}
}}
</style>
</head>
<body>
<h1>Медлабы — мониторинг</h1>
<div class="sub">Обновлено: {now}</div>

<div class="cards">
  <div class="card"><div class="val">{total_promos}</div><div class="lbl">Активных акций</div></div>
  <div class="card"><div class="val">{total_news_c}</div><div class="lbl">Новостей в базе</div></div>
  <div class="card"><div class="val">{total_art}</div><div class="lbl">Статей в базе</div></div>
  <div class="card"><div class="val">{total_news_g}</div><div class="lbl">Google News просмотрено</div></div>
</div>

<div class="tbl-wrap">
<table>
<thead><tr>
  <th>Лаборатория</th>
  <th style="text-align:right">Акции</th>
  <th style="text-align:right">Новости</th>
  <th style="text-align:right">Статьи</th>
</tr></thead>
<tbody>{''.join(rows)}
</tbody>
</table>
</div>
</body>
</html>"""

    out = DATA_DIR / "stats.html"
    out.write_text(html, encoding="utf-8")
    print(f"stats.html записан ({len(html)} байт)")


if __name__ == "__main__":
    run()
