"""صنع بطاقة خبر مصمّمة (صورة مربعة) لكل منشور."""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

ROOT = Path(__file__).parent
FONT_BOLD = ROOT / "fonts" / "Tajawal-ExtraBold.ttf"
FONT_MED = ROOT / "fonts" / "Tajawal-Bold.ttf"
LOGO = ROOT / "assets" / "logo.png"  # اختياري: ضع شعار الصفحة هنا

SIZE = 1080
GREEN_TOP = (0, 98, 51)      # أخضر العلم
GREEN_BOT = (0, 48, 26)
RED = (210, 16, 52)          # أحمر العلم
WHITE = (255, 255, 255)
SOFT = (210, 230, 218)

HAS_RAQM = features.check("raqm")
AR_MONTHS = ["جانفي", "فيفري", "مارس", "أفريل", "ماي", "جوان", "جويلية",
             "أوت", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]


def _shape(text: str) -> str:
    return text


def _kw():
    return {"direction": "rtl", "language": "ar"}


def _width(draw, text, font) -> float:
    return draw.textlength(_shape(text), font=font, **_kw())


def _draw_rtl(draw, right_x, y, text, font, fill):
    """يرسم نصًا محاذيًا لليمين تنتهي حافته عند right_x."""
    w = _width(draw, text, font)
    draw.text((right_x - w, y), _shape(text), font=font, fill=fill, **_kw())
    return w


def _wrap(draw, text, font, max_w) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if _width(draw, trial, font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _fit_headline(draw, text, max_w, max_h):
    for size in range(92, 40, -4):
        font = ImageFont.truetype(str(FONT_BOLD), size)
        lines = _wrap(draw, text, font, max_w)
        line_h = int(size * 1.45)
        if len(lines) * line_h <= max_h:
            return font, lines, line_h
    font = ImageFont.truetype(str(FONT_BOLD), 40)
    lines = _wrap(draw, text, font, max_w)
    return font, lines[: max_h // 58], 58


def _ar_date(dt: datetime) -> str:
    return f"{dt.day} {AR_MONTHS[dt.month - 1]} {dt.year}"


def make_card(headline: str, source: str, page_name: str, when: datetime | None = None) -> bytes:
    if not HAS_RAQM:
        # بدون مكتبة raqm تظهر الحروف العربية مقطّعة، فالأفضل النشر بدون بطاقة
        raise RuntimeError("مكتبة raqm غير متوفرة لتشكيل الحروف العربية")
    when = (when or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=1)))
    img = Image.new("RGB", (SIZE, SIZE), GREEN_BOT)
    draw = ImageDraw.Draw(img)

    # خلفية متدرجة
    for y in range(SIZE):
        t = y / SIZE
        c = tuple(int(GREEN_TOP[i] * (1 - t) + GREEN_BOT[i] * t) for i in range(3))
        draw.line([(0, y), (SIZE, y)], fill=c)

    margin = 80
    right = SIZE - margin

    # الشريط العلوي: اسم الصفحة + الشعار
    x = right
    if LOGO.exists():
        logo = Image.open(LOGO).convert("RGBA")
        logo.thumbnail((96, 96))
        img.paste(logo, (right - logo.width, 70), logo)
        x = right - logo.width - 20
    _draw_rtl(draw, x, 88, page_name, ImageFont.truetype(str(FONT_BOLD), 44), WHITE)

    # شارة "أخبار الجزائر"
    tag_font = ImageFont.truetype(str(FONT_MED), 32)
    tag = "أخبار الجزائر"
    tw = _width(draw, tag, tag_font)
    draw.rounded_rectangle((right - tw - 48, 190, right, 250), radius=30, fill=RED)
    _draw_rtl(draw, right - 24, 202, tag, tag_font, WHITE)

    # العنوان
    top, bottom = 300, 850
    font, lines, lh = _fit_headline(draw, headline, SIZE - 2 * margin, bottom - top)
    y = top + ((bottom - top) - len(lines) * lh) // 2
    for line in lines:
        _draw_rtl(draw, right, y, line, font, WHITE)
        y += lh

    # خط أحمر فاصل ثم المصدر والتاريخ
    draw.rectangle((margin, 880, right, 886), fill=RED)
    small = ImageFont.truetype(str(FONT_MED), 34)
    _draw_rtl(draw, right, 910, f"المصدر: {source}", small, SOFT)
    date_txt = _ar_date(when)
    draw.text((margin, 910), _shape(date_txt), font=small, fill=SOFT, **_kw())

    # شريط سفلي بألوان العلم
    draw.rectangle((0, SIZE - 22, SIZE // 2, SIZE), fill=GREEN_TOP)
    draw.rectangle((SIZE // 2, SIZE - 22, SIZE, SIZE), fill=WHITE)
    draw.rectangle((SIZE // 2 - 60, SIZE - 22, SIZE // 2 + 60, SIZE), fill=RED)

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


if __name__ == "__main__":
    import sys
    data = make_card(
        sys.argv[1] if len(sys.argv) > 1 else "رئيس الجمهورية يستقبل وزير الشؤون الخارجية ويبحث معه ملفات التعاون الثنائي",
        "وكالة الأنباء الجزائرية", "حكايات 58 ولاية")
    Path(sys.argv[2] if len(sys.argv) > 2 else "card.jpg").write_bytes(data)
