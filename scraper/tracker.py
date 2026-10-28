#!/usr/bin/env python3
"""
Vintage game sales scraper
==========================
Pulls sold listings for specific early-print Japanese video games from
Yahoo! Auctions, Yahoo! Flea Market (Yahoo!フリマ) and Mercari, classifies
them, and writes data/sales.json for the website (index.html) to display.

Runs every 6 hours on GitHub Actions (.github/workflows/scrape.yml). You can
also run it on your own computer:

    pip install -r scraper/requirements.txt
    python -m playwright install chromium
    python scraper/tracker.py

Useful flags:
    --no-mercari / --no-yahoo   skip a marketplace
    --full                      re-page Yahoo all the way back (~180 days) instead of
                                stopping once it reaches sales it already has
    --mercari-pages N           Mercari result pages per search term (120 items each)
    --offline                   don't fetch anything; just re-classify what's stored
    --import FILE               merge an old history.json / sales JSON into the store
    --add URL                   add one listing the searches missed (Yahoo or Mercari)

Where the data comes from
-------------------------
* Yahoo! Auctions closed search (auctions.yahoo.co.jp/closedsearch). This also
  returns Yahoo!フリマ (PayPay Flea Market) sales, which link to
  paypayfleamarket.yahoo.co.jp and are tagged "Yahoo Flea". Exact end times.
  Yahoo keeps roughly the last 120-180 days.
* Mercari (jp.mercari.com). The search page is a JavaScript app, so this opens
  it in a headless Chromium (Playwright) and reads the JSON the page itself
  loads. That JSON includes each item's last-updated time, which for a sold
  item is when it sold (or shortly after) -- shown on the site as "≈ date".

What gets charted
-----------------
Only complete-in-box copies of the exact print being tracked. Every listing
is sorted into one of three buckets:
  charted  - box mentioned AND the print is confirmed by the title
             (Pokémon: バーコード大 or a survey postcard;
              Mario: no-barcode box AND the 5000-point misprint, or an errata sheet)
  review   - probably the right item but the title doesn't prove it (no box
             mentioned, barcode size not stated...). Listed on the site under
             "Needs review", not charted. Tick ✓ on the site to chart it.
  dropped  - loose carts, manual-only, sealed, graded, lots, wrong version or
             wrong print. Not stored.
Anything you hide with ✕ on the site is recorded in data/overrides.json and
is never charted again, even if a later scrape sees it again.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SALES_PATH = DATA / "sales.json"
OVERRIDES_PATH = DATA / "overrides.json"

JST = timezone(timedelta(hours=9))
YAHOO_CLOSED_URL = "https://auctions.yahoo.co.jp/closedsearch/closedsearch"
MERCARI_SEARCH_URL = "https://jp.mercari.com/search"
REQUEST_DELAY_SEC = 2.0
LOW_PRICE_FRACTION = 0.20
HIGH_PRICE_MULTIPLE = 3.0
REQUEST_TIMEOUT = 25
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"}
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def now_jst() -> datetime:
    """Naive datetime in Japan time. All stored times are JST, like the marketplaces."""
    return datetime.now(JST).replace(tzinfo=None, second=0, microsecond=0)


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(title: str, msg: str) -> None:
    """Prints a warning; on GitHub Actions it also shows as a yellow annotation on the run."""
    if IN_ACTIONS:
        print(f"::warning title={title}::{msg}", flush=True)
    else:
        print(f"  ! {title}: {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# What we track
# --------------------------------------------------------------------------

@dataclass
class Variant:
    key: str
    label: str        # long label for the site
    short: str        # toggle button label
    pattern: re.Pattern
    color: str


@dataclass
class TitleSpec:
    key: str
    name: str
    platform: str
    search_terms: list[str]
    must_match: re.Pattern          # it's this game at all
    in_scope: re.Pattern            # ...and the listing is about an early print
    verify: callable                # (title, has_box) -> (verified, reason_if_not)
    variants: list[Variant]         # first match wins; last is the fallback
    exclude: list[tuple[str, re.Pattern]] = field(default_factory=list)


BIG_BARCODE = re.compile(r"バーコード\s*[（(]?\s*大|大\s*バーコード|ラージバーコード")
NO_BARCODE = re.compile(r"バーコード\s*(?:ラベル)?\s*(?:な[しい]|無し?|ナシ)|ノーバーコード|バーコード\s*無")
MISPRINT = re.compile(r"5000\s*点|５０００\s*点|誤植|誤表記|初期エラー|エラー版|正誤表")
_NOT = r"(?!\s*(?:な[しい]|無し?|欠品|欠け|付いておりません|ありません))"
POSTCARD = re.compile(rf"(?:ハガキ|はがき|葉書|アンケート){_NOT}")
ERRATA = re.compile(rf"正誤表{_NOT}")


def _verify_pokemon(title: str, has_box: bool) -> tuple[bool, str]:
    if BIG_BARCODE.search(title) or POSTCARD.search(title):
        return True, ""
    return False, "barcode size not stated"


def _verify_mario(title: str, has_box: bool) -> tuple[bool, str]:
    nob, mis = bool(NO_BARCODE.search(title)), bool(MISPRINT.search(title))
    if (nob and mis) or (ERRATA.search(title) and has_box):
        return True, ""
    if nob:
        return False, "5000-pt misprint not stated"
    if mis:
        return False, "no-barcode box not stated"
    return False, "print not stated"


_POKEMON_COMMON_EXCLUDE = [
    ("overseas version", re.compile(r"海外版|北米版|英語版|US版")),
    ("Virtual Console / digital", re.compile(r"バーチャルコンソール|(?<![A-Za-z])VC(?![A-Za-z])|ダウンロード")),
    ("junk / for parts", re.compile(r"ジャンク")),
    ("later small-barcode box", re.compile(r"箱は?\s*後期|後期\s*(?:版\s*)?箱|バーコード\s*小|小\s*バーコード")),
    ("console bundle", re.compile(r"本体\s*(?:セット|付き?|同梱)|ゲームボーイ\s*(?:ポケット|カラー|ライト)\s*(?:本体|と|＆|&)")),
]

TITLES: list[TitleSpec] = [
    TitleSpec(
        key="pokemon-red",
        name="Pocket Monsters Red",
        platform="Game Boy · large barcode",
        search_terms=["ポケットモンスター 赤 バーコード大", "ポケットモンスター 赤 初期ロット",
                      "ポケットモンスター 赤 初期版 ハガキ", "ポケモン 赤 バーコード大", "ポケモン 赤 刻印22"],
        must_match=re.compile(r"ポケットモンスター.{0,6}赤|ポケモン.{0,6}赤|Pokemon Red", re.IGNORECASE),
        in_scope=re.compile(r"バーコード\s*[（(]?大|大バーコード|初期ロット|初期版|初版|初期|刻印|ロット\s*\d|ハガキ"),
        verify=_verify_pokemon,
        variants=[
            Variant("with-postcard", "with survey postcard", "With postcard", POSTCARD, "#D93A2B"),
            Variant("no-postcard", "without postcard", "No postcard", re.compile(r"."), "#E0786C"),
        ],
        exclude=[("other version / multi-pack",
                  re.compile(r"緑|グリーン|青|ピカチュウ|金|銀|クリスタル|(?<!赤)バージョン"))] + _POKEMON_COMMON_EXCLUDE,
    ),
    TitleSpec(
        key="pokemon-green",
        name="Pocket Monsters Green",
        platform="Game Boy · large barcode",
        search_terms=["ポケットモンスター 緑 バーコード大", "ポケットモンスター 緑 初期ロット",
                      "ポケモン 緑 バーコード大", "ポケットモンスター 緑 初版"],
        must_match=re.compile(r"ポケットモンスター.{0,6}(緑|グリーン)|ポケモン.{0,6}(緑|グリーン)|Pokemon Green", re.IGNORECASE),
        in_scope=re.compile(r"バーコード\s*[（(]?大|大バーコード|初期ロット|初期版|初版|初期|刻印|ロット\s*\d|ハガキ"),
        verify=_verify_pokemon,
        variants=[
            Variant("with-postcard", "with survey postcard", "With postcard", POSTCARD, "#1E8A47"),
            Variant("no-postcard", "without postcard", "No postcard", re.compile(r"."), "#5FB07D"),
        ],
        exclude=[("other version / multi-pack",
                  re.compile(r"赤・緑|赤緑|(?<![緑ン])\s*赤|青|ピカチュウ|金|銀|クリスタル"))] + _POKEMON_COMMON_EXCLUDE,
    ),
    TitleSpec(
        key="smb-nobarcode",
        name="Super Mario Bros.",
        platform="Famicom · no barcode, 5000-pt misprint manual",
        search_terms=["スーパーマリオブラザーズ ファミコン バーコードなし", "スーパーマリオブラザーズ 5000点 誤植",
                      "スーパーマリオブラザーズ 初期版 正誤表"],
        must_match=re.compile(r"スーパーマリオ\s*ブラザーズ|Super Mario Bros", re.IGNORECASE),
        in_scope=re.compile(r"バーコード|5000\s*点|誤植|誤表記|初期|初版|エラー|正誤表"),
        verify=_verify_mario,
        variants=[
            Variant("with-errata", "with errata sheet", "With errata", ERRATA, "#2563D9"),
            Variant("no-errata", "without errata sheet", "No errata", re.compile(r"."), "#6E9BE8"),
        ],
        exclude=[
            ("sequel / different game", re.compile(r"ブラザーズ\s*[23２３]|USA|ワールド|ランド|コレクション|(?<!スーパー)マリオブラザーズ")),
            ("NES / overseas", re.compile(r"(?<![A-Za-z])NES(?![A-Za-z])|海外版|北米版")),
            ("Disk System", re.compile(r"ディスク")),
            ("console bundle", re.compile(r"本体\s*(?:セット|付き?|同梱)|ファミコン本体")),
            ("box has a barcode (later print)", re.compile(r"バーコード\s*(?:あり|有り?|付き?)(?!.*バーコード\s*な)")),
            ("junk / for parts", re.compile(r"ジャンク")),
        ],
    ),
]

# Condition. Order matters: the first rule that fires decides.
_PARTIAL = re.compile(r"(?:取扱説明書|説明書|取説|外箱|空箱|箱説|箱|ソフト|カセット|カートリッジ|本体)\s*のみ|空箱|ソフト\s*(?:な[しい]|無し?)")
_LOOSE = re.compile(r"(?:箱説|箱|説明書|取説)\s*(?:な[しい]|無し?|欠品)|裸ソフト")
_LOT = re.compile(r"まとめ|セット売り|大量|\d+\s*本\s*セット|\d+\s*点\s*セット|ソフト\s*\d+\s*本")
_GRADED = re.compile(r"(?<![A-Za-z])(?:VGA|WATA|CGC)(?![A-Za-z])|PSA\s*\d|PSA鑑定|鑑定品|グレーディング", re.IGNORECASE)
_SEALED = re.compile(r"未開封|新品未使用|シュリンク")
_HAS_BOX = re.compile(r"箱|ボックス|(?<![A-Za-z])BOX(?![A-Za-z])|完品|付属品\s*完備|付属品\s*完品", re.IGNORECASE)
# No box mentioned + one of these = a loose cart listing (sellers describe the cart, not the box).
_LOOSE_HINT = re.compile(r"カセット|カートリッジ|動作確認|起動確認|通電確認|動作品|動作保証|端子清掃|"
                         r"\d\d\s*[AＡ](?![A-Za-z])|ソフト\s*単品")


def classify_condition(title: str) -> tuple[str, bool]:
    """Returns (condition, has_box). condition: cib | unknown | partial | loose | lot | graded | sealed."""
    implied_box = bool(BIG_BARCODE.search(title) or NO_BARCODE.search(title)
                       or POSTCARD.search(title) or ERRATA.search(title))
    has_box = bool(_HAS_BOX.search(title)) or implied_box
    if _PARTIAL.search(title):
        return "partial", has_box
    if _LOOSE.search(title):
        return "loose", has_box
    if _LOT.search(title):
        return "lot", has_box
    if _GRADED.search(title):
        return "graded", has_box
    if _SEALED.search(title):
        return "sealed", has_box
    if has_box:
        return "cib", True
    if _LOOSE_HINT.search(title):
        return "loose", False
    return "unknown", False


def clean_title(t: str) -> str:
    t = re.sub(r"のサムネイル$", "", (t or "").strip())
    return re.sub(r"\s+", " ", t)


def classify(title: str, rejections: Counter) -> Optional[dict]:
    """Decide what a listing is. Returns derived fields, or None if it isn't something we keep."""
    title = clean_title(title)
    for spec in TITLES:
        if not spec.must_match.search(title):
            continue
        for reason, pat in spec.exclude:
            if pat.search(title):
                rejections[f"{spec.key}: {reason}"] += 1
                return None
        if not spec.in_scope.search(title):
            rejections[f"{spec.key}: not about an early print"] += 1
            return None
        cond, has_box = classify_condition(title)
        if cond not in ("cib", "unknown"):
            rejections[f"{spec.key}: {cond}"] += 1
            return None
        verified, why = spec.verify(title, has_box)
        reasons = []
        if cond == "unknown":
            reasons.append("box not mentioned")
        if not verified:
            reasons.append(why)
        variant = next(v for v in spec.variants if v.pattern.search(title))
        m = re.search(r"(?:刻印|ロット|初期)\s*[（(]?\s*(00|22|20|0|2)\s*[AＡ]?(?![0-9])", title)
        stamp = {"0": "00", "2": "22"}.get(m.group(1), m.group(1)) if m else None
        return {"title_key": spec.key, "variant_key": variant.key, "condition": cond,
                "status": "charted" if not reasons else "review",
                "review_reason": " · ".join(reasons), "stamp": stamp}
    return None


# --------------------------------------------------------------------------
# Listing ids / urls
# --------------------------------------------------------------------------

_MERCARI_ITEM_RE = re.compile(r"jp\.mercari\.com/(?:item|shops/product)/([A-Za-z0-9]+)")
_YAHOO_ITEM_RE = re.compile(r"auctions\.yahoo\.co\.jp/jp/auction/([A-Za-z0-9]+)|paypayfleamarket\.yahoo\.co\.jp/item/([A-Za-z0-9]+)")


def clean_url(url: str) -> str:
    m = _MERCARI_ITEM_RE.search(url)
    if m:
        kind = "item" if m.group(1).startswith("m") and m.group(1)[1:].isdigit() else "shops/product"
        return f"https://jp.mercari.com/{kind}/{m.group(1)}"
    m = _YAHOO_ITEM_RE.search(url)
    if m:
        return (f"https://auctions.yahoo.co.jp/jp/auction/{m.group(1)}" if m.group(1)
                else f"https://paypayfleamarket.yahoo.co.jp/item/{m.group(2)}")
    return url.split("?")[0]


def listing_id(url_or_id: str) -> str:
    return url_or_id.split("?")[0].rstrip("/").rsplit("/", 1)[-1].split("#")[0].strip()


def source_of(url: str) -> str:
    if "mercari" in url:
        return "Mercari"
    if "paypayfleamarket" in url:
        return "Yahoo Flea"
    return "Yahoo Auction"


# --------------------------------------------------------------------------
# Yahoo! Auctions + Yahoo! Flea (closed search)
# --------------------------------------------------------------------------

_Y_LINK_RE = re.compile(r"/jp/auction/[A-Za-z0-9]+$|paypayfleamarket\.yahoo\.co\.jp/item/")
_Y_BIDS_RE = re.compile(r"bid_hist\?aID=")
_Y_TIME_RE = re.compile(r"(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\s*終了")


def _get(url: str, params: dict | None = None) -> Optional[str]:
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.text
    except requests.RequestException as exc:
        warn("request failed", f"{url} -- {exc}")
        return None
    finally:
        time.sleep(REQUEST_DELAY_SEC)


def _md_to_dt(m: re.Match) -> datetime:
    month, day, h, mi = (int(g) for g in m.groups())
    now = now_jst()
    dt = datetime(now.year, month, day, h, mi)
    return dt if dt <= now + timedelta(days=1) else datetime(now.year - 1, month, day, h, mi)


def parse_yahoo_closed(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not _Y_LINK_RE.search(href):
            continue
        title = a.get_text(strip=True)
        if not title or href in seen:
            continue
        node, text = a, ""
        for _ in range(8):
            if node.parent is None:
                break
            node = node.parent
            text = node.get_text(" ", strip=True)
            if "落札" in text and "終了" in text:
                break
        pm = re.search(r"落札\s*([\d,]+)\s*円", text)
        tm = _Y_TIME_RE.search(text)
        if not pm or not tm:
            continue
        bid_a = node.find("a", href=_Y_BIDS_RE)
        bids = int(bid_a.get_text(strip=True)) if bid_a and bid_a.get_text(strip=True).isdigit() else 0
        url = clean_url(href if href.startswith("http") else "https://auctions.yahoo.co.jp" + href)
        seen.add(href)
        rows.append({"title": title, "price": int(pm.group(1).replace(",", "")), "bids": bids,
                     "closed_at": _md_to_dt(tm), "url": url, "source": source_of(url),
                     "date_basis": "auction_end"})
    return rows


def fetch_yahoo(term: str, max_pages: int, stop_before: Optional[datetime]) -> list[dict]:
    """Closed search, newest first, 50 per page (the size verified to work). Stops at max_pages, at the last page,
    or once a page reaches sales older than `stop_before` (we already have those)."""
    out = []
    for p in range(max_pages):
        html = _get(YAHOO_CLOSED_URL, {"p": term, "b": p * 50 + 1, "n": 50})
        if not html:
            break
        rows = parse_yahoo_closed(html)
        out.extend(rows)
        if len(rows) < 40:          # last page (a few cards lack a parsable price, hence < 50)
            break
        if stop_before and min(r["closed_at"] for r in rows) < stop_before:
            break
    return out


# --------------------------------------------------------------------------
# Mercari (headless browser; reads the page's own search API response)
# --------------------------------------------------------------------------

SOLD_STATUSES = {"ITEM_STATUS_SOLD_OUT", "ITEM_STATUS_TRADING", "sold_out", "trading"}


def mercari_search_url(term: str, page_token: Optional[str] = None) -> str:
    url = (f"{MERCARI_SEARCH_URL}?keyword={quote(term)}&status=sold_out%7Ctrading"
           f"&sort=created_time&order=desc")
    if page_token:
        url += f"&page_token={quote(page_token)}"
    return url


def parse_mercari_api(data: dict) -> list[dict]:
    """Rows from the JSON Mercari's search page loads (api.mercari.jp/v2/entities:search)."""
    rows = []
    for it in data.get("items") or []:
        if str(it.get("status", "")) not in SOLD_STATUSES:
            continue
        iid = str(it.get("id", ""))
        if not iid:
            continue
        ts = it.get("updated") or it.get("created")
        try:
            when = datetime.fromtimestamp(int(ts), JST).replace(tzinfo=None, second=0)
        except (TypeError, ValueError):
            continue
        is_c2c = iid.startswith("m") and iid[1:].isdigit()
        url = f"https://jp.mercari.com/item/{iid}" if is_c2c else f"https://jp.mercari.com/shops/product/{iid}"
        try:
            price = int(str(it.get("price", "0")).replace(",", ""))
        except ValueError:
            continue
        rows.append({"title": clean_title(it.get("name", "")), "price": price, "bids": 0,
                     "closed_at": when, "url": url, "source": "Mercari", "date_basis": "mercari_updated"})
    return rows


_M_LINK_RE = re.compile(r"/(?:item|shops/product)/([A-Za-z0-9]{8,})")
_M_PRICE_RE = re.compile(r"[¥￥]\s*([\d,]{3,})")


def parse_mercari_dom(html: str) -> list[dict]:
    """Fallback if the API response couldn't be captured: read the rendered cards.
    No sold date is available this way, so these get today's date ("first seen")."""
    soup = BeautifulSoup(html, "html.parser")
    rows, seen = [], set()
    for a in soup.find_all("a", href=True):
        m = _M_LINK_RE.search(a["href"])
        if not m or m.group(1) in seen:
            continue
        node, text = a, a.get_text(" ", strip=True)
        for _ in range(5):
            if _M_PRICE_RE.search(text) or node.parent is None:
                break
            node = node.parent
            text = node.get_text(" ", strip=True)
        pm = _M_PRICE_RE.search(text)
        if not pm:
            continue
        title = (a.get("aria-label") or "").strip()
        if not title:
            img = node.find("img", alt=True)
            title = (img["alt"] or "").strip() if img else ""
        title = clean_title(re.sub(r"\s*(売り切れ|SOLD)\s*", " ", title or text[:pm.start()]))
        blob = (text + " " + str(node)).lower()
        if len(title) < 6 or ("sold" not in blob and "売り切れ" not in blob):
            continue
        seen.add(m.group(1))
        rows.append({"title": title, "price": int(pm.group(1).replace(",", "")), "bids": 0,
                     "closed_at": now_jst(), "url": clean_url("https://jp.mercari.com" + a["href"]
                                                               if a["href"].startswith("/") else a["href"]),
                     "source": "Mercari", "date_basis": "first_seen"})
    return rows


def fetch_mercari(terms: list[str], max_pages: int, known: dict[str, dict]) -> tuple[list[dict], str]:
    """Returns (rows, status). Pages through results; stops early for a term once a
    whole page brings nothing new (after the first run, that's usually page 1)."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        warn("Mercari skipped", "Playwright isn't installed: pip install playwright && python -m playwright install chromium")
        return [], "playwright missing"

    rows: list[dict] = []
    status = "ok"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, locale="ja-JP", timezone_id="Asia/Tokyo",
                                  viewport={"width": 1280, "height": 1800})
        page = ctx.new_page()
        for term in terms:
            token, got_term = None, 0
            for pno in range(max_pages):
                url = mercari_search_url(term, token)
                data = None
                try:
                    with page.expect_response(lambda r: "entities:search" in r.url and r.request.method == "POST",
                                              timeout=30000) as resp_info:
                        page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    resp = resp_info.value
                    if resp.ok:
                        data = resp.json()
                except PWTimeout:
                    pass
                except Exception as exc:
                    warn("Mercari error", f"'{term}' page {pno + 1}: {exc}")
                if data is None:
                    # Fallback: parse whatever rendered.
                    try:
                        page.wait_for_selector("a[href*='/item/'], a[href*='/shops/product/']", timeout=15000)
                        found = parse_mercari_dom(page.content())
                        rows.extend(found)
                        got_term += len(found)
                        status = "dom-fallback"
                    except Exception:
                        warn("Mercari", f"no results rendered for '{term}' (blocked, or the page changed)")
                        status = "no-results" if status == "ok" else status
                    break
                found = parse_mercari_api(data)
                rows.extend(found)
                got_term += len(found)
                new = [r for r in found
                       if listing_id(r["url"]) not in known
                       or known[listing_id(r["url"])].get("date_basis") != "mercari_updated"]
                token = (data.get("meta") or {}).get("nextPageToken")
                if not token or not data.get("items") or (pno >= 1 and not new):
                    break
                time.sleep(REQUEST_DELAY_SEC)
            log(f"    Mercari '{term}': {got_term} sold items")
            time.sleep(REQUEST_DELAY_SEC)
        browser.close()
    return rows, status


# --------------------------------------------------------------------------
# One-off additions (--add URL)
# --------------------------------------------------------------------------

def fetch_single(url: str) -> Optional[dict]:
    url = clean_url(url)
    src = source_of(url)
    if src == "Mercari":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            warn("--add", "Mercari links need Playwright installed")
            return None
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_context(user_agent=UA, locale="ja-JP").new_page()
            item = None
            try:
                with page.expect_response(lambda r: "items/get" in r.url or "/v1/items/" in r.url,
                                          timeout=30000) as info:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                j = info.value.json()
                item = j.get("data", j)
            except Exception:
                pass
            if not item:
                html = page.content()
            browser.close()
        if item:
            row = parse_mercari_api({"items": [{**item, "status": item.get("status", "ITEM_STATUS_SOLD_OUT")}]})
            return row[0] if row else None
    else:
        html = _get(url)
        if not html:
            return None
    soup = BeautifulSoup(html, "html.parser")
    og = lambda k: (soup.find("meta", property=f"og:{k}") or {}).get("content")
    title = og("title") or (soup.title.string if soup.title else "") or ""
    title = re.sub(r"\s*[-|｜]\s*(メルカリ|Yahoo!.*|ヤフオク!?).*$", "", title).strip()
    pm = re.search(r"[¥￥]\s*([\d,]{4,})|([\d,]{4,})\s*円", (og("description") or "") + " " + soup.get_text(" ", strip=True))
    if not (title and pm):
        warn("--add", f"couldn't read a title and price from {url}")
        return None
    return {"title": clean_title(title), "price": int((pm.group(1) or pm.group(2)).replace(",", "")), "bids": 0,
            "closed_at": now_jst(), "url": url, "source": src, "date_basis": "first_seen"}


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

BASIS_RANK = {"auction_end": 3, "mercari_updated": 2, "first_seen": 1}


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def _parse_dt(s: str) -> datetime:
    s = s.replace(" ", "T")[:16]
    return datetime.strptime(s, "%Y-%m-%dT%H:%M")


def load_store() -> dict[str, dict]:
    """id -> raw row. Raw fields only; classification is recomputed every run."""
    if not SALES_PATH.exists():
        return {}
    data = json.loads(SALES_PATH.read_text(encoding="utf-8"))
    store = {}
    for r in data.get("sales", []):
        store[r["id"]] = {"title": r["title"], "price": r["price"], "bids": r.get("bids", 0),
                          "closed_at": _parse_dt(r["closed_at"]), "url": r["url"], "source": r["source"],
                          "date_basis": r.get("date_basis", "auction_end")}
    return store


def import_legacy(path: Path) -> list[dict]:
    """Old history.json / collected-sales JSON: {"sales":[{title,price,bids,closed_at,url}]}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for r in data.get("sales", []):
        url = clean_url(r["url"])
        src = source_of(url)
        out.append({"title": clean_title(r["title"]), "price": int(r["price"]), "bids": int(r.get("bids", 0)),
                    "closed_at": _parse_dt(r["closed_at"]), "url": url, "source": src,
                    "date_basis": "auction_end" if src != "Mercari" else "first_seen"})
    return out


def merge(store: dict[str, dict], rows: list[dict]) -> tuple[int, int]:
    """Merge fetched rows into the store. Returns (added, upgraded)."""
    added = upgraded = 0
    for r in rows:
        r = {**r, "url": clean_url(r["url"]), "title": clean_title(r["title"])}
        lid = listing_id(r["url"])
        old = store.get(lid)
        if old is None:
            store[lid] = r
            added += 1
            continue
        new_rank, old_rank = BASIS_RANK[r["date_basis"]], BASIS_RANK[old["date_basis"]]
        if new_rank > old_rank:
            store[lid] = r
            upgraded += 1
        elif new_rank == old_rank and r["date_basis"] == "first_seen":
            store[lid] = {**r, "closed_at": min(old["closed_at"], r["closed_at"])}
        elif new_rank == old_rank:
            store[lid] = r
    return added, upgraded


def load_overrides() -> dict:
    if OVERRIDES_PATH.exists():
        data = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    else:
        data = {}
    data.setdefault("hidden", {})
    data.setdefault("confirmed", {})
    return data


def fetch_rates() -> dict:
    try:
        r = requests.get("https://api.frankfurter.app/latest", params={"from": "JPY", "to": "USD,CAD"},
                         headers=HEADERS, timeout=10)
        r.raise_for_status()
        j = r.json()
        return {**{k: round(1 / v, 3) for k, v in j["rates"].items()}, "note": f"ECB rate {j['date']}"}
    except Exception as exc:
        warn("FX", f"live rate fetch failed ({exc}); keeping previous rates")
        return {}


def write_sales(store: dict[str, dict], rejections: Counter, run: dict, fx: dict) -> Counter:
    prev = json.loads(SALES_PATH.read_text(encoding="utf-8")) if SALES_PATH.exists() else {}
    fx = fx or prev.get("fx") or {"USD": 152.0, "CAD": 110.0, "note": "fallback rate"}
    out, counts = [], Counter()
    for lid, r in store.items():
        info = classify(r["title"], rejections)
        if not info:
            continue
        counts[(info["title_key"], info["status"])] += 1
        out.append({"id": lid, **info, "title": r["title"], "price": r["price"], "bids": r["bids"],
                    "closed_at": _fmt(r["closed_at"]), "date_basis": r["date_basis"],
                    "url": r["url"], "source": r["source"]})
    # A "charted" sale under LOW_PRICE_FRACTION of its chart's median is almost always something the
    # title doesn't reveal (damage, a repro box, cart-only with a misleading title). Move it to review;
    # one ✓ on the site puts it back.
    groups: dict[tuple, list[int]] = {}
    for r in out:
        if r["status"] == "charted":
            groups.setdefault((r["title_key"], r["variant_key"]), []).append(r["price"])
    medians = {k: sorted(v)[len(v) // 2] for k, v in groups.items() if len(v) >= 8}
    # The mirror case: in the "without postcard / errata" chart, a sale priced like a with-insert copy
    # almost always HAD the insert and the title just doesn't say so. Those are what stretch the scale.
    fallback = {t.key: (t.variants[-1].key, t.variants[0]) for t in TITLES}
    for r in out:
        med = medians.get((r["title_key"], r["variant_key"]))
        fb_key, with_v = fallback[r["title_key"]]
        if (r["status"] == "charted" and med and r["variant_key"] == fb_key
                and r["price"] >= med * HIGH_PRICE_MULTIPLE):
            insert = with_v.label.replace("with ", "")
            r["status"] = "review"
            r["review_reason"] = (f"priced like a copy {with_v.label} ({HIGH_PRICE_MULTIPLE:g}× the typical ¥{med:,}) "
                                  f"— check photos for an unlisted {insert}")
            counts[(r["title_key"], "charted")] -= 1
            counts[(r["title_key"], "review")] += 1
            continue
        if r["status"] == "charted" and med and r["price"] < med * LOW_PRICE_FRACTION:
            r["status"] = "review"
            r["review_reason"] = f"price under {int(LOW_PRICE_FRACTION * 100)}% of the typical sale (¥{med:,}) — check photos"
            counts[(r["title_key"], "charted")] -= 1
            counts[(r["title_key"], "review")] += 1
    out.sort(key=lambda r: r["closed_at"], reverse=True)
    doc = {
        "generated_at": _fmt(now_jst()),
        "timezone": "Asia/Tokyo",
        "fx": fx,
        "last_run": run,
        "titles": [{"key": t.key, "name": t.name, "platform": t.platform,
                    "variants": [{"key": v.key, "label": v.label, "short": v.short, "color": v.color}
                                 for v in t.variants]} for t in TITLES],
        "sales": out,
    }
    DATA.mkdir(exist_ok=True)
    tmp = SALES_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(SALES_PATH)
    return counts


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape vintage game sales into data/sales.json")
    ap.add_argument("--no-yahoo", action="store_true")
    ap.add_argument("--no-mercari", action="store_true")
    ap.add_argument("--full", action="store_true", help="Page Yahoo all the way back instead of stopping at known sales.")
    ap.add_argument("--yahoo-pages", type=int, default=6, help="Max Yahoo pages (50 results) per search term.")
    ap.add_argument("--mercari-pages", type=int, default=5, help="Max Mercari pages (120 results) per search term.")
    ap.add_argument("--offline", action="store_true", help="No fetching; re-classify stored data only.")
    ap.add_argument("--import", dest="imports", action="append", default=[], metavar="FILE",
                    help="Merge an old history.json or collected-sales JSON.")
    ap.add_argument("--add", action="append", default=[], metavar="URL", help="Add one listing by URL.")
    args = ap.parse_args()

    store = load_store()
    log(f"Stored sales before this run: {len(store)}")
    run = {"at": _fmt(now_jst()), "sources": {}}

    for f in args.imports:
        a, u = merge(store, import_legacy(Path(f)))
        log(f"Imported {f}: {a} new, {u} improved")

    if not args.offline:
        terms = [t for spec in TITLES for t in spec.search_terms]

        if not args.no_yahoo:
            yahoo_known = [r["closed_at"] for r in store.values() if r["date_basis"] == "auction_end"]
            stop_before = None if (args.full or not yahoo_known) else max(yahoo_known) - timedelta(days=2)
            fetched = []
            for term in terms:
                rows = fetch_yahoo(term, args.yahoo_pages, stop_before)
                log(f"    Yahoo '{term}': {len(rows)} closed listings")
                fetched.extend(rows)
            a, u = merge(store, fetched)
            by_src = Counter(r["source"] for r in fetched)
            for src in ("Yahoo Auction", "Yahoo Flea"):
                run["sources"][src] = {"fetched": by_src[src]}
            run["yahoo_new"] = a
            if not fetched:
                warn("Yahoo", "0 listings fetched from Yahoo -- possibly blocked from this network")

        if not args.no_mercari:
            rows, status = fetch_mercari(terms, args.mercari_pages, store)
            a, u = merge(store, rows)
            run["sources"]["Mercari"] = {"fetched": len(rows), "new": a, "dates_improved": u, "status": status}
            if not rows:
                warn("Mercari", f"0 sold items fetched (status: {status}) -- Mercari may be blocking this network")

        for url in args.add:
            r = fetch_single(url)
            if r:
                merge(store, [r])
                log(f"  + added {r['title'][:50]} ¥{r['price']:,}")

    overrides = load_overrides()
    rejections: Counter = Counter()
    fx = {} if args.offline else fetch_rates()
    counts = write_sales(store, rejections, run, fx)

    hidden = set(overrides["hidden"])
    log("\nWrote data/sales.json")
    for spec in TITLES:
        log(f"  {spec.name}: {counts[(spec.key, 'charted')]} charted, {counts[(spec.key, 'review')]} need review")
    log(f"  hidden by you on the site: {len(hidden)}")
    for src, info in run["sources"].items():
        log(f"  {src}: {info}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Scrape result\n\n| | charted | needs review |\n|---|---|---|\n")
            for spec in TITLES:
                f.write(f"| {spec.name} | {counts[(spec.key, 'charted')]} | {counts[(spec.key, 'review')]} |\n")
            f.write("\n| Source | this run |\n|---|---|\n")
            for src, info in run["sources"].items():
                f.write(f"| {src} | {info} |\n")
    if rejections:
        log("Not stored (dropped by the classifier):")
        for reason, n in rejections.most_common():
            log(f"  - {reason}: {n}")


if __name__ == "__main__":
    main()
