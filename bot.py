"""بوت أخبار الجزائر: يجمع الأخبار الرسمية والترندات وينشرها على صفحة فيسبوك.

- أخبار المصادر الرسمية  -> نشر تلقائي.
- الترندات               -> ترسل إلى تيليغرام وتنتظر موافقتك.

يعمل كل مرة ثم يتوقف (مصمم لـ GitHub Actions). الحالة تُحفظ في state/state.json.
"""

from __future__ import annotations

import hashlib
import html
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
FB_API_VERSION = os.getenv("FB_API_VERSION", "v26.0")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DRY_RUN = os.getenv("DRY_RUN", "") == "1"

HT_NS = {"ht": "https://trends.google.com/trending/rss"}
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
    s.setdefault("pending", {})     # منشورات تنتظر الموافقة
    s.setdefault("posted_at", [])   # أوقات النشر التلقائي (للحدود)
    s.setdefault("tg_offset", 0)
    return s


def save_state(s: dict):
    cutoff = (NOW - timedelta(days=4)).timestamp()
    s["seen"] = {k: v for k, v in s["seen"].items() if v > cutoff}
    s["pending"] = {k: v for k, v in s["pending"].items() if v.get("ts", 0) > cutoff}
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
            if it["date"] and NOW - it["date"] > max_age:
                continue
            out.append({"title": title, "link": it["link"], "source": src["name"],
                        "date": it["date"]})
    out.sort(key=lambda x: x["date"] or NOW)  # الأقدم أولًا
    return out


# ---------------------------------------------------------------- الترندات
def collect_trends(cfg: dict) -> list[dict]:
    tcfg = cfg["trends"]
    data = fetch(f"https://trends.google.com/trending/rss?geo={tcfg['geo']}")
    if not data:
        return []
    root = ET.fromstring(data)
    trends = []
    for it in root.iter("item"):
        news = []
        for n in it.findall("ht:news_item", HT_NS):
            url = n.findtext("ht:news_item_url", "", HT_NS)
            news.append({
                "title": html.unescape(n.findtext("ht:news_item_title", "", HT_NS)),
                "url": url,
                "source": n.findtext("ht:news_item_source", "", HT_NS),
                "known": any(domain_matches(domain_of(url), d) for d in tcfg["known_media"]),
            })
        trends.append({
            "term": (it.findtext("title") or "").strip(),
            "traffic": it.findtext("ht:approx_traffic", "", HT_NS),
            "news": news,
        })
    return trends


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


def compose(cfg: dict, headline: str, source: str, link: str, extra: str = "") -> str:
    body = ai_rewrite(cfg, f"العنوان: {headline}\n{extra}".strip()) or f"📰 {headline}"
    tags = cfg["posting"].get("hashtags", "")
    return f"{body}\n\nالمصدر: {source}\n{link}\n\n{tags}".strip()


# ---------------------------------------------------------------- فيسبوك
def fb_post(message: str, link: str | None = None) -> str | None:
    if DRY_RUN:
        log("  [تجربة] كان سيُنشر على فيسبوك:\n" + message)
        return "dry-run"
    if not (FB_PAGE_ID and FB_PAGE_TOKEN):
        log("  ! FB_PAGE_ID أو FB_PAGE_TOKEN غير مضبوط")
        return None
    data = {"message": message, "access_token": FB_PAGE_TOKEN}
    if link:
        data["link"] = link
    r = requests.post(f"https://graph.facebook.com/{FB_API_VERSION}/{FB_PAGE_ID}/feed",
                      data=data, timeout=30)
    if r.ok:
        pid = r.json().get("id")
        log(f"  ✓ نُشر على فيسبوك: {pid}")
        return pid
    log(f"  ! خطأ من فيسبوك: {r.status_code} {r.text[:300]}")
    return None


# ---------------------------------------------------------------- تيليغرام
def tg(method: str, **params) -> dict:
    if not TG_TOKEN:
        return {}
    if DRY_RUN and method != "getUpdates":
        log(f"  [تجربة] تيليغرام {method}: {str(params)[:400]}")
        return {"ok": True}
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                          json=params, timeout=30)
        return r.json()
    except Exception as e:
        log(f"  ! خطأ تيليغرام: {e}")
        return {}


def tg_send_for_approval(key: str, text: str):
    kb = {"inline_keyboard": [[{"text": "✅ انشر", "callback_data": f"ok:{key}"},
                               {"text": "❌ تجاهل", "callback_data": f"no:{key}"}]]}
    tg("sendMessage", chat_id=TG_CHAT_ID, text=text[:4000], reply_markup=kb,
       disable_web_page_preview=True)


def process_approvals(state: dict):
    """يقرأ ضغطات الأزرار في تيليغرام منذ آخر تشغيل وينفذها."""
    if not (TG_TOKEN and TG_CHAT_ID):
        return
    res = tg("getUpdates", offset=state["tg_offset"], timeout=0,
             allowed_updates=["callback_query"])
    for upd in res.get("result", []):
        state["tg_offset"] = upd["update_id"] + 1
        cq = upd.get("callback_query")
        if not cq:
            continue
        chat = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        if chat != str(TG_CHAT_ID):
            continue  # تجاهل أي شخص آخر
        action, _, key = (cq.get("data") or "").partition(":")
        item = state["pending"].pop(key, None)
        msg = cq.get("message", {})
        if not item:
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="انتهت صلاحية هذا المنشور")
            continue
        if action == "ok":
            ok = fb_post(item["message"], item.get("link"))
            note = "✅ تم النشر" if ok else "⚠️ فشل النشر، راجع سجل GitHub Actions"
        else:
            note = "❌ تم التجاهل"
        tg("answerCallbackQuery", callback_query_id=cq["id"], text=note)
        tg("editMessageText", chat_id=chat, message_id=msg.get("message_id"),
           text=(msg.get("text") or "")[:3900] + f"\n\n{note}")


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
    posted = 0
    for item in collect_official(cfg):
        fp = fingerprint(item["title"])
        if fp in state["seen"]:
            continue
        if not can_auto_post(cfg, state, posted):
            log("  · تم بلوغ حد النشر، الباقي في التشغيل القادم")
            break
        msg = compose(cfg, item["title"], item["source"], item["link"])
        if fb_post(msg, item["link"]):
            state["seen"][fp] = NOW.timestamp()
            state["posted_at"].append(NOW.timestamp())
            posted += 1
        else:
            break  # لا نعلّم الخبر كمقروء حتى ينجح النشر


def run_trends(cfg: dict, state: dict):
    if not cfg["trends"].get("enabled"):
        return
    if not (TG_TOKEN and TG_CHAT_ID):
        log("— الترندات: تيليغرام غير مضبوط، تم التخطي")
        return
    log("— الترندات")
    sent = 0
    for t in collect_trends(cfg):
        if sent >= cfg["trends"]["max_per_run"]:
            break
        fp = "trend:" + fingerprint(t["term"])
        if fp in state["seen"]:
            continue
        state["seen"][fp] = NOW.timestamp()
        if not t["news"]:
            continue  # ترند بلا خبر مرتبط: لا شيء يمكن التحقق منه
        best = sorted(t["news"], key=lambda n: not n["known"])[0]
        extra = "\n".join(f"- {n['title']} ({n['source']})" for n in t["news"][:3])
        msg = compose(cfg, best["title"], best["source"], best["url"],
                      extra=f"عناوين مرتبطة:\n{extra}")
        key = fp.split(":")[1]
        state["pending"][key] = {"message": msg, "link": best["url"], "ts": NOW.timestamp()}
        sources = "\n".join(f"{'✅' if n['known'] else '⚠️'} {n['source']}: {n['title']}"
                            for n in t["news"][:3])
        tg_send_for_approval(
            key,
            f"🔥 ترند: {t['term']} ({t['traffic']} بحث)\n\nالمصادر:\n{sources}\n\n"
            f"— المنشور المقترح —\n{msg}",
        )
        sent += 1


def main():
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    state = load_state()
    try:
        process_approvals(state)
        run_official(cfg, state)
        run_trends(cfg, state)
    finally:
        save_state(state)
    log("انتهى.")


if __name__ == "__main__":
    sys.exit(main())
