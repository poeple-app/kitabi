#!/usr/bin/env python3
"""Regenerate mockups/yeralt-notlar.html — the example Dostoyevski reading
journal that GitHub README links to. Run from repo root:

    python tools/regenerate_mockup.py

Requires only Jinja2 + Pillow (already in dev deps).

Inlines the logo as base64 + journal.css as a <style> block so the resulting
HTML is fully self-contained (drop it anywhere, opens in any browser)."""
import base64, enum, pathlib, sys
from datetime import datetime, date
from types import SimpleNamespace
from jinja2 import Environment, FileSystemLoader, select_autoescape

# Repo root = parent of tools/
ROOT = pathlib.Path(__file__).resolve().parent.parent
MOCK_DIR = ROOT / "mockups"
MOCK_DIR.mkdir(exist_ok=True)

env = Environment(
    loader=FileSystemLoader(str(ROOT / "templates")),
    autoescape=select_autoescape(["html"]),
)

# Mirror kitabi.data's locale-independent Turkish date filter so the mockup
# renders the same Turkish month names the production PDF does. Both %B and
# %b resolve to the full Turkish name — see _tr_date in kitabi/data.py.
_TR_MONTHS_FULL = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
                   "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


def _tr_date(dt, fmt="%d %B %Y"):
    if dt is None:
        return ""
    name = _TR_MONTHS_FULL[dt.month - 1]
    resolved = fmt.replace("%B", name).replace("%b", name)
    return dt.strftime(resolved)


env.filters["tr_date"] = _tr_date


class Category(enum.Enum):
    QUOTE = "Alıntı"
    IDEA = "Fikir"
    NEW_INFO = "Yeni Bilgi"
    WORD = "Kelime"
    CONCEPT = "Kavram"
    SUMMARY = "Özet"


def D(**kw):
    return SimpleNamespace(**kw)


def _file_uri(p: pathlib.Path) -> str | None:
    if not p.exists():
        return None
    return f"data:image/png;base64,{base64.b64encode(p.read_bytes()).decode('ascii')}"


# ─── Sample data — Yeraltından Notlar ──────────────────────────────────
book = D(
    id=1, short_code="YAN",
    title="Yeraltından Notlar", author="Fyodor Dostoyevski",
    publisher="İletişim Yayınları", publication_year=2017,
    isbn="9789750507380",
    genre="Roman", subgenre="Felsefi Roman",
    total_pages=188, read_pages=188,
    icon="📕", cover_url="",
    bought_from="İdefix", price_tl=67,
    bought_at=date(2026, 3, 8),
    tags=["klasik", "19yy", "rus", "varoluş"],
    personal_note="Bir arkadaşımın ısrarla önerdiği — modernlik tartışmalarına temel kitap.",
    rating=5,
    one_line_review="Modernliğin temellerini sarsan kısa bir başyapıt.",
    would_recommend=True, goodreads_url=None,
    extra_fields={"Önerildi": "Defne (Mart 2026)", "İlk okuma": "Hayır (3. okuma)"},
)

sessions = []
for i, (start, end, sp, ep) in enumerate([
    (datetime(2026, 3, 12, 21, 30), datetime(2026, 3, 12, 23, 5),  1,  32),
    (datetime(2026, 3, 18, 22, 0),  datetime(2026, 3, 18, 23, 45), 32, 64),
    (datetime(2026, 3, 28, 20, 15), datetime(2026, 3, 28, 22, 30), 64, 92),
    (datetime(2026, 4, 5,  19, 0),  datetime(2026, 4, 5,  20, 50), 92, 120),
    (datetime(2026, 5, 14, 21, 0),  datetime(2026, 5, 14, 23, 0),  120, 156),
    (datetime(2026, 5, 28, 20, 0),  datetime(2026, 5, 28, 22, 30), 156, 188),
], 1):
    sessions.append(D(
        id=i, code=f"YAN-S{i:02d}",
        started_at=start, ended_at=end,
        start_page=sp, end_page=ep,
        duration_min=int((end - start).total_seconds() / 60),
        notes=[],
    ))


def N(idx, sid, cat, page, body, *, definition=None, explanation=None, fav=False):
    s = sessions[sid - 1]
    return D(
        id=idx, code=f"YAN{idx:03d}", book_id=1, session_id=sid,
        category=cat, page=page, transcript=body,
        definition=definition, explanation=explanation,
        is_favorite=fav, from_qa=False,
        photo_file_id=None, is_orphan_photo=False, category_label=None,
        created_at=s.started_at,
    )


notes = [
    N( 1, 1, Category.QUOTE, 12, "Ben hasta bir adamım… Kötü bir adamım. Sevimsiz bir adamım.", fav=True),
    N( 2, 1, Category.IDEA, 18, "Anlatıcı kendini baştan dezavantajlı pozisyonda sunuyor — bu bir alçakgönüllülük değil, okuyucuyu silahsızlandırma taktiği."),
    N( 3, 1, Category.NEW_INFO, 24, "1864'te yazıldı, varoluşçuluğun habercisi sayılıyor; Sartre ve Camus'tan yarım yüzyıl önce."),
    N( 4, 2, Category.CONCEPT, 34, "Aşırı bilinç",
       definition="Yeraltı adamı için aşırı bilinç hastalık niteliğinde. Eyleme geçeceğine sonsuza dek kendini izler; bu da onu felç eder."),
    N( 5, 2, Category.QUOTE, 41, "İki kere iki dört eder, gene de iki kere iki beş eder bazen, hoş bir şeycik olabilir.", fav=True),
    N( 6, 2, Category.IDEA, 47, "Rasyonel akıl matematik gibi katı; ama insan tam da bu katılığa karşı 'beş' der. Özgürlük matematik dışıdır."),
    N( 7, 2, Category.WORD, 52, "Determinizm",
       definition="Her olayın önceki nedenler tarafından zorunlu olarak belirlendiği görüş."),
    N( 8, 3, Category.QUOTE, 71, "İnsan zaman zaman kendi yararına tam ters şeyleri sevebilir, severek de tercih edebilir.", fav=True),
    N( 9, 3, Category.IDEA, 78, "Modernite 'mutluluğu' standart bir hedef olarak sunar. Yeraltı adamı bunu reddederek özgürlüğünü ilan eder."),
    N(10, 3, Category.CONCEPT, 82, "İrade", definition="Eylemi seçen iç güç. Yeraltı adamı için irade rasyonel hesaba indirgenemez."),
    N(11, 3, Category.NEW_INFO, 88, "Kitap iki bölümlü: 1. felsefi monolog, 2. olay anlatımı (Liza'yla karşılaşma). Bağlam ikinci bölümde somutlaşır."),
    N(12, 4, Category.QUOTE, 95, "Beni hor görenleri seviyorum çünkü hor görmek için yeterli sebepleri var."),
    N(13, 4, Category.IDEA, 102, "Kendine acıma + kendinden tiksinme birlikte. Modern psikolojinin temel paradokslarından biri."),
    N(14, 4, Category.WORD, 108, "Kinik",
       definition="Toplumsal normlara, hatta erdeme inanmayan, alaycı tutumdaki kişi."),
    N(15, 4, Category.CONCEPT, 115, "Determinizm karşıtlığı",
       definition="Yeraltı adamının 'iki kere iki beş' itirazı — bilimsel determinist akla karşı özgür iradenin son siperi."),
    N(16, 5, Category.QUOTE, 128, "Sevgili efendim, bilemezsin gönlüm nasıl da ağladı."),
    N(17, 5, Category.IDEA, 134, "Liza'yla karşılaşma yeraltı adamının soyut felsefesinin canlı bir teste tabi tutulduğu an. Felsefi monolog gerçek hayata çarptığında ne kalır?"),
    N(18, 5, Category.SUMMARY, 145, "İkinci bölümün ilk yarısı: Yeraltı adamı bir sokağa çıkar, eski okul arkadaşlarıyla yemekte yarı-tartışmalı bir akşam geçirir. Tüm bilinçaltı çatışmaları yüzeye çıkıyor."),
    N(19, 6, Category.IDEA, 162, "Liza'ya 'kurtarıcı' rolü oynaması, kendi acılığını dindirmek için — ama Liza onu görüp gerçekten anladığında bunu kaldıramaz."),
    N(20, 6, Category.CONCEPT, 171, "Yeraltı",
       definition="Toplumun yüzeyine çıkmamış, kendi içine kapalı bilincin sembolik mekânı."),
    N(21, 6, Category.QUOTE, 178, "Hepimiz, kendimizden, gerçek hayattan koparıldık.", fav=True),
    N(22, 6, Category.SUMMARY, 185, "Kitabın son sayfaları: Yeraltı adamı kapanışını yapar — 'sizin de hepiniz benim gibi yarım yamalaksınız, sadece ben söylemeye cesaret ediyorum'. Romanın merkezi tezini açıkça koyar."),
    N(23, 6, Category.NEW_INFO, 188, "Çeviri notu: 'çömez' kelimesi Bourdieu okumalarında da geçiyordu — kurum tarafından dışlanmış, yine kurumla ilişki kuran kişi."),
]

for n in notes:
    sessions[n.session_id - 1].notes.append(n)
book.sessions = sessions
book.notes = notes


# ─── Stats / calendar / glossary cloud ──────────────────────────────────
import calendar as _cal
tr_months = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
             "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


def build_cal(sl):
    months = {}
    for s in sl:
        k = (s.started_at.year, s.started_at.month)
        if k not in months:
            fw, dim = _cal.monthrange(*k)
            months[k] = dict(
                label=f"{tr_months[k[1] - 1]} {k[0]}",
                year=k[0], month=k[1],
                first_weekday=fw, days_in_month=dim,
                active_days=set(), minutes_per_day={},
            )
        m = months[k]
        m["active_days"].add(s.started_at.day)
        m["minutes_per_day"][s.started_at.day] = (
            m["minutes_per_day"].get(s.started_at.day, 0) + s.duration_min
        )
    out = []
    for k in sorted(months):
        m = months[k]; m["active_days"] = sorted(m["active_days"])
        out.append(m)
    return out


total_min = sum(s.duration_min for s in sessions)
cat_counts = {c.value: 0 for c in Category}
for n in notes:
    cat_counts[n.category.value] += 1
stats = dict(
    session_count=len(sessions),
    total_minutes=total_min,
    hours=total_min // 60, minutes_remainder=total_min % 60,
    note_count=len(notes), category_counts=cat_counts,
    pages_read=188,
    word_count=sum(1 for n in notes if n.category == Category.WORD),
    concept_count=sum(1 for n in notes if n.category == Category.CONCEPT),
    quote_count=sum(1 for n in notes if n.category == Category.QUOTE),
    idea_count=sum(1 for n in notes if n.category == Category.IDEA),
    started_at=sessions[0].started_at, ended_at=sessions[-1].ended_at,
    calendar=build_cal(sessions),
)

glossary_notes = [n for n in notes if n.category in (Category.WORD, Category.CONCEPT)]
glossary_cloud = []
for n in glossary_notes:
    term = (n.transcript or "").strip()
    if not term or len(term) > 30 or len(term.split()) > 3:
        continue
    glossary_cloud.append({"term": term, "category": n.category.value, "note_code": n.code})

favorites = [n for n in notes if n.is_favorite]
summaries = [n for n in notes if n.category == Category.SUMMARY]


# ─── Render ─────────────────────────────────────────────────────────────
tpl = env.get_template("journal.html")
html = tpl.render(
    book=book, sessions=sessions, notes=notes, Category=Category,
    glossary=glossary_notes, glossary_cloud=glossary_cloud,
    summaries=summaries, favorites=favorites, stats=stats,
    author_other_books=["Suç ve Ceza", "Karamazov Kardeşler", "Budala"],
    extra_fields=book.extra_fields, photo_data_uris={},
    kitabi_logo_uri=_file_uri(ROOT / "kitabilogo.png"),
    kitabi_logo_white_uri=_file_uri(ROOT / "kitabilogo-white.png"),
    generated_at=datetime(2026, 5, 28, 22, 35),
    to_local=lambda d: d,
)

# Inline the CSS so the file is self-contained
css = (ROOT / "templates" / "journal.css").read_text(encoding="utf-8")
html = html.replace(
    '<link rel="stylesheet" href="journal.css" />',
    f"<style>\n{css}\n</style>",
)

out = MOCK_DIR / "yeralt-notlar.html"
out.write_text(html, encoding="utf-8")
print(f"OK — {out.relative_to(ROOT)}, {out.stat().st_size:,} bytes")
