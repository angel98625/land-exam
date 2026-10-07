"""每日抓取房市、土地、建築與法規相關資訊，輸出 news/data/ 下的 JSON。

由 .github/workflows/news.yml 每天台灣時間 10:00 執行。只用 Python 標準函式庫。
"""
import datetime as dt
import email.utils
import html
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

TW = dt.timezone(dt.timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "news" / "data"
KEEP_DAYS = 30
WINDOW_HOURS = 36  # 只收最近 36 小時內發布的項目
UA = "Mozilla/5.0 (compatible; land-exam-news/1.0; +https://angel98625.github.io/land-exam/news/)"

# 內政部、央行等綜合來源需用關鍵字過濾，只留跟土地、房市、建築有關的
KEYWORDS = [
    "房", "不動產", "土地", "地政", "地價", "地籍", "登記", "實價", "預售", "租賃", "租屋",
    "建築", "建物", "營建", "都市", "都更", "國土", "徵收", "重劃", "公設", "社宅", "社會住宅",
    "住宅", "信用管制", "平均地權", "囤房", "地權", "容積",
]

GOOGLE_QUERIES = [
    "房市", "房價", "實價登錄", "預售屋", "平均地權條例", "囤房稅",
    "央行 信用管制", "房貸 利率", "土地 法規 修正", "地政 內政部",
    "國土管理署", "都市更新", "危老重建", "建築法 修正", "國土計畫",
    "土地徵收", "社會住宅", "租屋 補貼", "地價稅", "土地增值稅",
]

FEEDS = [
    # (來源名稱, RSS 網址, 是否需要關鍵字過濾)
    ("內政部 新聞發布", "https://www.moi.gov.tw/OpenData.aspx?SN=76F358C679FAD4CF", True),
    ("內政部 行政公告", "https://www.moi.gov.tw/OpenData.aspx?SN=EB24A8E71E37C079", True),
    ("內政部 法規命令發布", "https://www.moi.gov.tw/OpenData.aspx?SN=B55985D8C9CDC53A", True),
    ("內政部 草案預告", "https://www.moi.gov.tw/OpenData.aspx?SN=3148769B8A76FD66", True),
    ("中央銀行 新聞稿", "https://www.cbc.gov.tw/tw/rss-302-1.xml", True),
    # 媒體 RSS 帶有前導段落，放在 Google 新聞之前，重複時優先保留這裡的版本
    ("經濟日報 房市", "https://money.udn.com/rssfeed/news/1001/5591", False),
    ("ETtoday 房產雲", "https://feeds.feedburner.com/ettoday/house", False),
    ("自由時報 財經", "https://news.ltn.com.tw/rss/business.xml", True),
    ("中央社 財經", "https://feeds.feedburner.com/rsscna/finance", True),
] + [
    ("Google 新聞", "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{q} when:1d", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"}), False)
    for q in GOOGLE_QUERIES
]


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(text, limit=None):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        d = None
    if d is None:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                d = dt.datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=TW)
    return d


def child(el, *names):
    for n in names:
        for c in el:
            tag = c.tag.split("}")[-1]
            if tag == n:
                return c
    return None


def parse_feed(raw, source):
    root = ET.fromstring(raw)
    items = [e for e in root.iter() if e.tag.split("}")[-1] in ("item", "entry")]
    out = []
    for it in items:
        t = child(it, "title")
        l = child(it, "link")
        d = child(it, "description", "summary", "content")
        p = child(it, "pubDate", "published", "updated", "date")
        s = child(it, "source")
        link = (l.text or l.get("href") or "").strip() if l is not None else ""
        title = clean(t.text if t is not None else "")
        src = source
        if source == "Google 新聞":
            # Google 新聞標題格式為「標題 - 媒體名稱」
            media = clean(s.text) if s is not None and s.text else ""
            if not media and " - " in title:
                media = title.rsplit(" - ", 1)[1]
            if media and title.endswith(" - " + media):
                title = title[: -len(media) - 3]
            src = media or "Google 新聞"
            summary = ""  # Google 新聞的描述只是標題連結，不當摘要
        else:
            summary = clean(d.text if d is not None else "", 160)
        if title and link:
            out.append({
                "title": title,
                "link": link,
                "source": src,
                "summary": summary,
                "published": parse_date(p.text if p is not None else ""),
                "official": source.startswith(("內政部", "中央銀行")),
                "google": source == "Google 新聞",
            })
    return out


def meta_description(url):
    """沒有摘要的項目，到原網頁抓 og:description 或 meta description 當前導段落。"""
    try:
        raw = get(url, timeout=12)[:400_000].decode("utf-8", "ignore")
    except Exception:
        return ""
    for pat in (r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description',
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)'):
        m = re.search(pat, raw, re.I)
        if m:
            return clean(m.group(1), 160)
    return ""


def norm(title):
    return re.sub(r"[\s\W_]+", "", title).lower()


def main():
    now = dt.datetime.now(TW)
    today = now.strftime("%Y-%m-%d")
    DATA.mkdir(parents=True, exist_ok=True)

    # 過去 30 天已收錄的標題，避免同一則新聞每天重複出現
    seen = set()
    for f in DATA.glob("20*.json"):
        if f.stem == today:
            continue
        try:
            for it in json.loads(f.read_text("utf-8"))["items"]:
                seen.add(norm(it["title"]))
        except Exception:
            pass

    collected, status = [], []
    for name, url, need_filter in FEEDS:
        try:
            items = parse_feed(get(url), name)
        except Exception as e:  # 單一來源失敗不影響其他來源
            status.append({"source": name, "ok": False, "error": str(e)[:200]})
            print(f"[skip] {name}: {e}", file=sys.stderr)
            continue
        kept = 0
        for it in items:
            if it["published"] and now - it["published"] > dt.timedelta(hours=WINDOW_HOURS):
                continue
            if need_filter and not any(k in it["title"] + it["summary"] for k in KEYWORDS):
                continue
            key = norm(it["title"])
            if key in seen:
                continue
            seen.add(key)
            collected.append(it)
            kept += 1
        status.append({"source": name, "ok": True, "fetched": len(items), "kept": kept})
        print(f"[ok] {name}: {len(items)} fetched, {kept} kept")

    for it in collected:
        if not it["google"] and not it["summary"]:
            it["summary"] = meta_description(it["link"])

    epoch = dt.datetime(1970, 1, 1, tzinfo=TW)
    collected.sort(key=lambda it: it["published"] or epoch, reverse=True)
    for it in collected:
        it["published"] = it["published"].astimezone(TW).strftime("%Y-%m-%d %H:%M") if it["published"] else ""

    (DATA / f"{today}.json").write_text(json.dumps({
        "date": today,
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "count": len(collected),
        "items": collected,
        "sources": status,
    }, ensure_ascii=False, indent=1), "utf-8")

    # 只保留近 30 天
    cutoff = (now - dt.timedelta(days=KEEP_DAYS - 1)).strftime("%Y-%m-%d")
    for f in DATA.glob("20*.json"):
        if f.stem < cutoff:
            f.unlink()
    days = sorted((f.stem for f in DATA.glob("20*.json")), reverse=True)
    counts = {d: json.loads((DATA / f"{d}.json").read_text("utf-8"))["count"] for d in days}
    (DATA / "index.json").write_text(json.dumps({
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "days": [{"date": d, "count": counts[d]} for d in days],
    }, ensure_ascii=False, indent=1), "utf-8")
    print(f"{today}: {len(collected)} items")


if __name__ == "__main__":
    main()
