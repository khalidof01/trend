"""بوت أخبار الجزائر: يجمع أخبار المصادر الرسمية وينشرها تلقائيًا على صفحة فيسبوك.

يعمل كل مرة ثم يتوقف (مصمم لـ GitHub Actions). الحالة تُحفظ في state/state.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state" / "state.json"
CONFIG_FILE = ROOT / "config.yaml"

FB_PAGE_ID = os.getenv("FB_PAGE_ID", "")
FB_PAGE_TOKEN = os.getenv("FB_PAGE_TOKEN", "")
FB_API_VERSION = os.getenv("FB_API_VERSION", "v27.0")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DRY_RUN = os.getenv("DRY_RUN", "") == "1"

ARABIC_RE = re.compile(r"[؀-ۿ]")
UA = {"User-Agent": "Mozilla/5.0 (dz-news-bot)"}
NOW = datetime.now(timezone.utc)


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- الحالة
def load_state() -> dict:
    if STATE_FILE.exists():
        s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    else:
        s = {}
    s.setdefault("seen", {})        # بصمة الخبر -> وقت رؤيته
    s.setdefault("posted_at", [])   # أوقات النشر التلقائي (للحدود)
    return s


def save_state(s: dict):
    cutoff = (NOW - timedelta(days=4)).timestamp()
    s["seen"] = {k: v for k, v in s["seen"].items() if v > cutoff}
    s["posted_at"] = [t for t in s["posted_at"] if t > (NOW - timedelta(days=1)).timestamp()]
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


def fingerprint(text: str) -> str:
    norm = re.sub(r"[^\w]+", "", text.lower())
    norm = re.sub(r"[ًٌٍَُِّْـ]", "", norm)  # إزالة التشكيل والتطويل
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- أدوات
def domain_of(url: str) -> str:
    host = urllib.parse.urlparse(url).hostname or ""
    return host.lower().removeprefix("www.")


def domain_matches(host: str, allowed: str) -> bool:
    return host == allowed or host.endswith("." + allowed)


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def fetch(url: str) -> bytes | None:
    try:
        r = requests.get(url, headers=UA, timeout=25)
        r.raise_for_status()
        return r.content
    except Exception as e:
        log(f"  ! تعذر جلب {url}: {e}")
        return None


def normalize_ar(t: str) -> str:
    t = re.sub(r"[أإآ]", "ا", t)
    t = t.replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"[ًٌٍَُِّْـ]", "", t)


def is_algerian(title: str, keywords: list[str]) -> bool:
    nt = normalize_ar(title)
    return any(normalize_ar(k) in nt for k in keywords)


COUNTRY_OF = {"التركية": "تركيا", "السويسرية": "سويسرا", "النمساوية": "النمسا",
              "البلجيكية": "بلجيكا", "الهولندية": "هولندا", "الفرنسية": "فرنسا",
              "الروسية": "روسيا", "الأذربيجانية": "أذربيجان", "الكازاخستانية": "كازاخستان",
              "النيجرية": "النيجر", "المالية": "مالي", "الموريتانية": "موريتانيا",
              "السنغالية": "السنغال", "النيجيرية": "نيجيريا", "الإثيوبية": "إثيوبيا",
              "الكينية": "كينيا", "الرواندية": "رواندا", "الأنغولية": "أنغولا",
              "الموزمبيقية": "الموزمبيق", "المصرية": "مصر", "السعودية": "السعودية"}


def clarify_places(title: str, notes: dict) -> str:
    """يضيف اسم البلد بعد المدن الأجنبية: «بأنطاليا» -> «بأنطاليا التركية»."""
    for city, adj in (notes or {}).items():
        country = COUNTRY_OF.get(adj, "")
        nt = normalize_ar(title)
        if normalize_ar(adj) in nt or (country and normalize_ar(country) in nt):
            continue
        pattern = "".join("[اأإآ]" if ch in "اأإآ" else re.escape(ch) for ch in city)
        title, n = re.subn(rf"({pattern})(?![\u0600-\u06FF])", rf"\1 {adj}", title, count=1)
    return title


# ---------------------------------------------------------------- الأخبار الرسمية
def google_news_url(domain: str) -> str:
    q = urllib.parse.quote(f"site:{domain} when:1d")
    return f"https://news.google.com/rss/search?q={q}&hl=ar&gl=DZ&ceid=DZ:ar"


def parse_rss_items(xml_bytes: bytes) -> list[dict]:
    items = []
    root = ET.fromstring(xml_bytes)
    for it in root.iter("item"):
        src = it.find("source")
        items.append({
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "date": parse_date(it.findtext("pubDate")),
            "source_url": src.get("url", "") if src is not None else "",
            "source_name": (src.text or "").strip() if src is not None else "",
        })
    return items


def collect_official(cfg: dict) -> list[dict]:
    post_cfg = cfg["posting"]
    max_age = timedelta(hours=post_cfg["max_age_hours"])
    sources = cfg["official_sources"]
    out = []

    feeds = [(google_news_url(s["domain"]), s) for s in sources]
    feeds += [(f["url"], {"domain": domain_of(f["url"]), "name": f["name"]})
              for f in cfg.get("direct_feeds") or []]

    for url, src in feeds:
        data = fetch(url)
        if not data:
            continue
        try:
            items = parse_rss_items(data)
        except ET.ParseError as e:
            log(f"  ! RSS غير صالح من {src['name']}: {e}")
            continue
        for it in items:
            # التحقق: يجب أن يكون الخبر فعلًا من النطاق الرسمي
            real = domain_of(it["source_url"] or it["link"])
            if not domain_matches(real, src["domain"]):
                continue
            title = it["title"]
            # Google News يضيف " - اسم المصدر" في آخر العنوان
            if it["source_name"] and title.endswith(" - " + it["source_name"]):
                title = title[: -len(it["source_name"]) - 3].strip()
            if post_cfg.get("arabic_only") and not ARABIC_RE.search(title):
                continue
            if post_cfg.get("algeria_only") and not is_algerian(title, cfg.get("algeria_keywords") or []):
                continue
            if it["date"] and NOW - it["date"] > max_age:
                continue
            title = clarify_places(title, cfg.get("place_notes"))
            out.append({"title": title, "link": it["link"], "source": src["name"],
                        "date": it["date"]})
    out.sort(key=lambda x: x["date"] or NOW)  # الأقدم أولًا
    return out


# ---------------------------------------------------------------- الصياغة
SYSTEM_PROMPT = (
    "أنت محرر في صفحة أخبار جزائرية على فيسبوك. اكتب منشورًا قصيرًا بالعربية الفصحى "
    "(سطران إلى ثلاثة) اعتمادًا فقط على المعلومات المعطاة. ممنوع منعًا باتًا إضافة أي "
    "معلومة أو رقم أو اسم أو تاريخ غير موجود في النص المعطى. لا تبالغ ولا تستعمل عناوين "
    "مثيرة. ابدأ برمز تعبيري واحد مناسب. لا تضع روابط ولا وسوم ولا ذكر المصدر؛ ستضاف لاحقًا. "
    "أعد نص المنشور فقط."
)


def ai_rewrite(cfg: dict, facts: str) -> str | None:
    if not (cfg["ai"].get("enabled") and ANTHROPIC_KEY):
        return None
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": cfg["ai"]["model"], "max_tokens": 300, "system": SYSTEM_PROMPT,
                  "messages": [{"role": "user", "content": facts}]},
            timeout=60,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
        return text or None
    except Exception as e:
        log(f"  ! فشلت الصياغة بالذكاء الاصطناعي، سيُستعمل القالب: {e}")
        return None


def compose(cfg: dict, headline: str, source: str, link: str) -> str:
    body = ai_rewrite(cfg, f"العنوان: {headline}") or f"📰 {headline}"
    tags = cfg["posting"].get("hashtags", "")
    return f"{body}\n\nالمصدر: {source}\n{link}\n\n{tags}".strip()


# ---------------------------------------------------------------- فيسبوك
def fb_post(message: str, link: str | None = None, image: bytes | None = None) -> str | None:
    if DRY_RUN:
        log("  [تجربة] كان سيُنشر على فيسبوك" + (" مع بطاقة" if image else "") + ":\n" + message)
        return "dry-run"
    if not (FB_PAGE_ID and FB_PAGE_TOKEN):
        log("  ! FB_PAGE_ID أو FB_PAGE_TOKEN غير مضبوط")
        return None
    base = f"https://graph.facebook.com/{FB_API_VERSION}/{FB_PAGE_ID}"
    if image:
        r = requests.post(f"{base}/photos",
                          data={"caption": message, "access_token": FB_PAGE_TOKEN},
                          files={"source": ("card.jpg", image, "image/jpeg")}, timeout=60)
        if r.ok:
            pid = r.json().get("post_id") or r.json().get("id")
            log(f"  ✓ نُشر على فيسبوك مع بطاقة: {pid}")
            return pid
        log(f"  ! فشل نشر الصورة ({r.status_code} {r.text[:200]})، سيُنشر كرابط")
    data = {"message": message, "access_token": FB_PAGE_TOKEN}
    if link:
        data["link"] = link
    r = requests.post(f"{base}/feed", data=data, timeout=30)
    if r.ok:
        pid = r.json().get("id")
        log(f"  ✓ نُشر على فيسبوك: {pid}")
        return pid
    log(f"  ! خطأ من فيسبوك: {r.status_code} {r.text[:300]}")
    return None


# ---------------------------------------------------------------- التشغيل
def can_auto_post(cfg: dict, state: dict, posted_this_run: int) -> bool:
    p = cfg["posting"]
    if posted_this_run >= p["max_posts_per_run"]:
        return False
    if len(state["posted_at"]) >= p["max_posts_per_day"]:
        return False
    if state["posted_at"] and NOW.timestamp() - max(state["posted_at"]) < p["min_gap_minutes"] * 60:
        return False
    return True


def run_official(cfg: dict, state: dict):
    log("— الأخبار الرسمية")
    post_cfg = cfg["posting"]
    posted = 0
    for item in collect_official(cfg):
        fp = fingerprint(item["title"])
        if fp in state["seen"]:
            continue
        if not can_auto_post(cfg, state, posted):
            log("  · تم بلوغ حد النشر، الباقي في التشغيل القادم")
            break
        msg = compose(cfg, item["title"], item["source"], item["link"])
        image = None
        if post_cfg.get("image_cards"):
            try:
                from card import make_card
                image = make_card(item["title"], item["source"],
                                  post_cfg.get("page_name", ""), item["date"])
            except Exception as e:
                log(f"  ! تعذر صنع البطاقة: {e}")
        if fb_post(msg, item["link"], image):
            state["seen"][fp] = NOW.timestamp()
            state["posted_at"].append(NOW.timestamp())
            posted += 1
        else:
            break  # لا نعلّم الخبر كمقروء حتى ينجح النشر


def main():
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    state = load_state()
    if cfg["posting"].get("image_cards"):
        from PIL import features
        log("البطاقات المصممة: " + ("جاهزة ✓" if features.check("raqm") else "غير متاحة (raqm مفقود)"))
    try:
        run_official(cfg, state)
    finally:
        save_state(state)
    log("انتهى.")


if __name__ == "__main__":
    sys.exit(main())
