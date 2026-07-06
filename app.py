#!/usr/bin/env python3
import csv
import functools
import gzip
import hashlib
import html
import io
import json
import os
import re
import ssl
import struct
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from flask import Flask as _Flask, request as _freq, jsonify as _fjson, send_from_directory as _fsend
    _flask_ok = True
except ImportError:
    _flask_ok = False
from pathlib import Path

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        Image,
        KeepTogether,
        ListFlowable,
        ListItem,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
except Exception:
    colors = None


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
OUT = ROOT / "output"
REPORTS = OUT / "reports"
ASSETS = OUT / "assets"
STATE_FILE = ROOT / "data" / "state.json"
JOB_FILE  = ROOT / "data" / "current_job.json"
LOG_FILE  = ROOT / "data" / "errors.log"

# Хранилище фоновых задач: job_id -> {"status": "running"|"done"|"error", "result": ...}
JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


def _read_job_file():
    """Читает статус задачи из файла (для устойчивости к перезапускам)."""
    try:
        if JOB_FILE.exists():
            return json.loads(JOB_FILE.read_text("utf-8"))
    except Exception:
        pass
    return None


def _write_job_file(data):
    """Сохраняет статус задачи в файл."""
    try:
        JOB_FILE.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    except Exception:
        pass


def log_error(context: str, exc: Exception = None) -> None:
    """Пишет ошибку в консоль и в data/errors.log."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {context}"
    if exc:
        line += f": {type(exc).__name__}: {exc}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            if exc:
                traceback.print_exc(file=f)
    except Exception:
        pass


def get_recent_errors(n: int = 20) -> list:
    """Возвращает последние n строк из errors.log."""
    try:
        if LOG_FILE.exists():
            lines = LOG_FILE.read_text("utf-8").splitlines()
            return lines[-n:]
    except Exception:
        pass
    return []


def notify_telegram(message: str) -> None:
    """Отправляет сообщение в Telegram-чат (нужны TG_BOT_TOKEN и TG_CHAT_ID в env)."""
    import urllib.request as _ur
    import json as _json
    token = os.environ.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = _json.dumps({"chat_id": chat_id, "text": message, "parse_mode": "HTML"}).encode()
        req = _ur.Request(url, data=payload, headers={"Content-Type": "application/json"})
        _ur.urlopen(req, timeout=10)
    except Exception as exc:
        log_error("notify_telegram", exc)


COMPETITORS = [
    {
        "key": "checkoffice",
        "name": "CheckOffice",
        "domain": "checkoffice.ru",
        "site": "https://checkoffice.ru",
        "queries": ["check office", "чек офис", "checkoffice", "чекофис"],
        "socials": {
            "telegram": "",          # заполнить если появится
            "vk": "checkoffice",
            "youtube": "",
            "rutube": "",
            "dzen": "",
        },
    },
    {
        "key": "mdaudit",
        "name": "MD Audit",
        "domain": "mdaudit.ru",
        "site": "https://mdaudit.ru",
        "queries": ["мд аудит", "md audit"],
        "socials": {
            "telegram": "mdaudit",
            "vk": "mdaudit",
            "youtube": "",
            "rutube": "",
            "dzen": "",
        },
    },
    {
        "key": "serviceinspector",
        "name": "Service Inspector",
        "domain": "serviceinspector.ru",
        "site": "https://serviceinspector.ru",
        "queries": ["сервис инспектор", "service inspector"],
        "socials": {
            "telegram": "SI_chanel",
            "vk": "",
            "youtube": "",
            "rutube": "",
            "dzen": "",
        },
    },
    {
        "key": "retailiqa",
        "name": "Ритейлика",
        "domain": "retailiqa.ru",
        "site": "https://retailiqa.ru",
        "queries": ["ритейлика"],
        "socials": {
            "telegram": "",
            "vk": "retailiqa",
            "youtube": "",
            "rutube": "",
            "dzen": "",
        },
    },
    {
        "key": "merasoft",
        "name": "Мерасофт",
        "domain": "mera-soft.ru",
        "site": "https://mera-soft.ru",
        "queries": ["мерасофт", "мерасофт чек лист"],
        # Tilda-блог: статьи не попадают в sitemap — берём из Dzen-фида
        "blog_feed": "https://dzen.ru/api/v3/launcher/export?channelName=merasoft&type=rss",
        "socials": {
            "telegram": "",
            "vk": "merasoft",
            "youtube": "",
            "rutube": "69521007",
            "dzen": "merasoft",
        },
    },
    {
        "key": "imredi",
        "name": "Imredi",
        "domain": "imredi.biz",
        "site": "https://imredi.biz",
        "queries": ["имреди", "imredi"],
        "socials": {
            "telegram": "",
            "vk": "imredi",
            "youtube": "",
            "rutube": "",
            "dzen": "",
        },
    },
]


MONTHS = {
    "feb": "Февраль 2026",
    "mar": "Март 2026",
    "apr": "Апрель 2026",
    "may": "Май 2026",
    "jun": "Июнь 2026",
}


@dataclass
class CompetitorData:
    key: str
    name: str
    domain: str
    site: str
    wordstat: dict = field(default_factory=dict)
    spywords: dict = field(default_factory=lambda: {
        "yandex_top50": None,
        "yandex_top10": None,
        "google_top50": None,
        "google_top10": None,
        "search_traffic_yandex": None,
        "search_traffic_google": None,
        "unique_urls_yandex": None,
        "unique_urls_google": None,
    })
    site_summary: str = ""
    content: list = field(default_factory=list)
    external: list = field(default_factory=list)
    social: list = field(default_factory=list)
    media: list = field(default_factory=list)
    emails: list = field(default_factory=list)
    phones: list = field(default_factory=list)
    events: list = field(default_factory=list)
    updates: list = field(default_factory=list)
    jobs: list = field(default_factory=list)
    images: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    social_channels: list = field(default_factory=list)  # структурированные каналы с постами


def ensure_dirs():
    for path in (ROOT / "data", REPORTS, ASSETS):
        path.mkdir(parents=True, exist_ok=True)
    # Если после перезапуска задача осталась в статусе "running" — сбрасываем
    try:
        job = _read_job_file()
        if job and job.get("status") == "running":
            _write_job_file({"status": "error", "step": "Ошибка",
                             "error": "Сервер перезапустился. Попробуйте ещё раз."})
    except Exception:
        pass

# Вызываем сразу при импорте — нужно для gunicorn (main() не вызывается)
ensure_dirs()


def read_state():
    ensure_dirs()
    if not STATE_FILE.exists():
        return {"sources": {}, "runs": {}}
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except Exception:
        return {"sources": {}, "runs": {}}


def write_state(state):
    ensure_dirs()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


def clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def norm(value):
    value = clean_text(value).lower()
    value = re.sub(r"[«»\"'`]", "", value)
    value = re.sub(r"[^a-zа-яё0-9 ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def number_from(value):
    matches = re.findall(r"(\d[\d\s\u00a0.,]*)", value or "")
    if not matches:
        return None
    raw = matches[-1].replace("\u00a0", " ").replace(" ", "")
    raw = raw.replace(",", ".")
    try:
        return int(float(raw))
    except Exception:
        return None


def _is_cert_error(exc):
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLError):
        return True
    return "CERTIFICATE_VERIFY_FAILED" in str(reason) or "certificate" in str(reason).lower()


def urlopen_with_ssl_fallback(req, timeout):
    """Обычная загрузка; если сертификат сайта просрочен/невалиден —
    повторяем без проверки сертификата."""
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as exc:
        if not _is_cert_error(exc):
            raise
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def request_text(url, timeout=12, extra_headers=None):
    headers = {
        "User-Agent": "Mozilla/5.0 MarketingMonitor/1.0 (+local report generator)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if extra_headers:
        headers.update({k: v for k, v in extra_headers.items() if v})
    req = urllib.request.Request(
        url,
        headers=headers,
    )
    with urlopen_with_ssl_fallback(req, timeout=timeout) as resp:
        ct_header = resp.headers.get("Content-Type", "")
        ct_base = ct_header.lower().split(";")[0].strip()
        _BINARY = ("image/", "application/pdf", "application/zip",
                   "application/octet-stream", "audio/", "video/")
        if ct_base and any(ct_base.startswith(b) for b in _BINARY):
            raise ValueError(f"Пропуск бинарного контента ({ct_base}): {url}")
        raw = resp.read(2_500_000)
        ctype = resp.headers.get_content_charset() or "utf-8"
    # Автодекомпрессия gzip: если байты начинаются с магических \x1f\x8b
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    return raw.decode(ctype, errors="replace")


def abs_url(url, base):
    try:
        return urllib.parse.urljoin(base, url)
    except Exception:
        return url


def strip_noise(markup):
    """Убираем скрипты и стили, чтобы CSS/JS не попадал в текст ссылок и контент."""
    markup = re.sub(r"<script\b[^>]*>.*?</script>", " ", markup, flags=re.I | re.S)
    markup = re.sub(r"<style\b[^>]*>.*?</style>", " ", markup, flags=re.I | re.S)
    markup = re.sub(r"<!--.*?-->", " ", markup, flags=re.S)
    return markup


def parse_title_description(markup):
    title = ""
    match = re.search(r"<title[^>]*>(.*?)</title>", markup, re.I | re.S)
    if match:
        title = clean_text(re.sub(r"<[^>]+>", " ", match.group(1)))
    desc = ""
    for pattern in [
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
    ]:
        match = re.search(pattern, markup, re.I | re.S)
        if match:
            desc = clean_text(match.group(1))
            break
    return title, desc


CONTENT_HINT = re.compile(
    r"blog|news|article|articles|stati|statya|post|posts|case|cases|keys|press|media|"
    r"tpost|event|events|webinar|vebinar|efir|update|релиз|release|changelog|nov|новост|"
    r"стат|кейс|обновлен|анонс|материал|guide|how|faq|partner|integration|интеграц",
    re.I,
)
SOCIAL_HINT = re.compile(r"(t\.me|telegram\.me|vk\.com|youtube\.com|youtu\.be|dzen\.ru|zen\.yandex|rutube\.ru|ok\.ru|instagram\.com|facebook\.com|linkedin\.com)", re.I)

JUNK_HREF = re.compile(r"#(popup|menu|submenu|demo|newsletter|question|card|rec|tab|form|b\d)", re.I)
JUNK_PATH = re.compile(r"(personal_data|pd_policy|pd_consent|/policy|/privacy|/oferta|/agreement|/cookie|/sitemap)", re.I)
GENERIC_TEXT = {
    "перезвоните мне", "подписаться на рассылку", "подписаться", "запросить демо",
    "задать вопрос", "получить консультацию", "тестовый доступ", "смотреть кейс",
    "читать кейс", "подробнее", "зарегистрироваться", "скачать", "скачать сейчас",
    "регистрация", "заказать звонок", "узнать больше", "оставить заявку", "связаться",
    "начать", "попробовать", "демо", "войти", "материалы", "чек-листы", "далее",
    "все статьи", "все новости", "все кейсы", "перейти", "читать далее", "читать",
}


def reg_domain(url):
    try:
        netloc = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return ""
    parts = netloc.replace("www.", "").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else netloc


def summarize_post_text(text):
    """Извлекает суть поста — первое осмысленное предложение/фразу."""
    text = text.strip()
    # Берём первый непустой абзац
    paras = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    first = paras[0] if paras else text
    # Берём первое предложение
    sentences = re.split(r'(?<=[.!?])\s+', first)
    sent = sentences[0].strip()
    # Если первое предложение слишком короткое (<20 символов) — берём два
    if len(sent) < 20 and len(sentences) > 1:
        sent = (sentences[0] + " " + sentences[1]).strip()
    # Если всё равно слишком короткое — берём начало первого абзаца
    if len(sent) < 20:
        sent = first
    return sent[:180]


def extract_analytical_description(page_html, comp_name):
    """
    Читает страницу внешнего материала и возвращает аналитическое описание:
    «Комментарий [Имя] о ...» или суть публикации в одном предложении.
    """
    # Автор — ищем по itemprop, class, JSON-LD
    author = ""
    for pat in [
        r'"author"\s*:\s*\{\s*"(?:@type[^}]+,\s*)"?name"?\s*:\s*"([^"]{5,60})"',
        r'"author"\s*:\s*"([А-ЯA-Z][^"]{4,60})"',
        r'itemprop=["\']author["\'][^>]*>\s*(?:<[^>]+>\s*)*([А-ЯA-Zа-яa-z][^\d<\n]{4,50})',
        r'class=["\'][^"\']*author[^"\']*["\'][^>]*>\s*(?:<[^>]+>\s*)*([А-ЯA-Z][^\d<\n]{4,40})',
        r'rel=["\']author["\'][^>]*>([^<]{4,60})<',
    ]:
        m = re.search(pat, page_html, re.I | re.S)
        if m:
            candidate = clean_text(re.sub(r'<[^>]+>', '', m.group(1)))
            # Фильтруем явный мусор
            if candidate and not re.search(r'http|©|www|\.ru|\.com', candidate):
                author = candidate[:60]
                break

    # Тип материала
    mat_type = "Материал"
    if re.search(r'комментар|эксперт|мнение', page_html, re.I):
        mat_type = "Комментарий"
    elif re.search(r'интервью|interview', page_html, re.I):
        mat_type = "Интервью"
    elif re.search(r'обзор|review|аналитика', page_html, re.I):
        mat_type = "Обзор"
    elif re.search(r'колонка|авторская', page_html, re.I):
        mat_type = "Авторская колонка"
    elif re.search(r'пресс.?рели[зс]|пресс.?релиз|press.?release', page_html, re.I):
        mat_type = "Пресс-релиз"

    # Ищем предложение с упоминанием компании в тексте страницы
    body_text = clean_text(re.sub(r'<[^>]+>', ' ', page_html))
    sentences = re.split(r'(?<=[.!?])\s+', body_text)
    comp_lower = comp_name.lower()
    relevant = [
        s.strip() for s in sentences
        if comp_lower in s.lower() and 30 < len(s) < 350
    ]

    if author and relevant:
        return f"{mat_type} {author} — {relevant[0][:180]}"
    elif author:
        return f"{mat_type} {author}"
    elif relevant:
        return relevant[0][:220]

    # Fallback: og:description или title
    _, og_desc = parse_title_description(page_html)
    if og_desc and len(og_desc) > 20:
        return og_desc[:220]
    return ""


_TRANSLIT_MAP = {
    "shh":"щ","sh":"ш","ch":"ч","zh":"ж","ts":"ц","ya":"я","yu":"ю","yo":"ё",
    "ye":"е","yi":"ый","iy":"ий","je":"е","jo":"ё","ju":"ю","ja":"я",
    "sch":"щ","kh":"х","gh":"г",
    "a":"а","b":"б","v":"в","g":"г","d":"д","e":"е","z":"з","i":"и",
    "j":"й","k":"к","l":"л","m":"м","n":"н","o":"о","p":"п","r":"р",
    "s":"с","t":"т","u":"у","f":"ф","h":"х","c":"к","x":"кс","y":"ы","q":"к",
    "jelektronnyj":"электронный","jelektronnyh":"электронных",
    "jeffektivnyj":"эффективный",
}

# Частотные английские слова в URL-слагах, которые не стоит транслитерировать
_ENGLISH_SLUG_WORDS = frozenset({
    "mystery", "shopper", "shoper", "shop", "shopping", "store", "retail",
    "check", "checklist", "case", "study", "guide", "tips", "blog", "post",
    "digital", "smart", "cloud", "data", "api", "release", "feature",
    "the", "and", "for", "how", "why", "what", "top", "best", "free", "new",
    "management", "analytics", "platform", "solution", "integration",
    "mobile", "desktop", "app", "web", "software", "system", "service",
    "about", "contact", "team", "company", "product", "products", "news",
    "market", "marketing", "report", "review", "video", "image", "photo",
})


def _translit_word(word: str) -> str:
    """Пробует перевести транслитерированное слово обратно в кириллицу."""
    # Уже содержит кириллицу — оставляем
    if re.search(r"[а-яёА-ЯЁ]", word):
        return word
    wl = word.lower()
    # Явно английские слова — не транслитерируем
    if wl in _ENGLISH_SLUG_WORDS:
        return word
    # Английские суффиксы, которых нет в русском транслите
    if re.search(r"(tion|sion|ness|ment|ance|ence|ous$|ive$)$", wl):
        return word
    # Применяем транслит (длинные паттерны первыми)
    result = wl
    for lat, cyr in sorted(_TRANSLIT_MAP.items(), key=lambda x: -len(x[0])):
        result = result.replace(lat, cyr)
    # Если остались латинские буквы (напр. 'w') — слово не транслитерируется корректно
    if re.search(r"[a-z]", result):
        return word
    return result


def slug_title(href):
    path = urllib.parse.urlparse(href).path.rstrip("/")
    seg = path.split("/")[-1] if path else ""
    seg = re.sub(r"\.(html?|php|aspx?|pdf)$", "", seg, flags=re.I)
    seg = re.sub(r"[-_]+", " ", seg).strip()
    if not seg:
        return ""
    words = seg.split()
    # UUID-паттерн: более половины сегментов — hex-строки 4+ символов С буквами a-f
    hex_count = sum(
        1 for w in words
        if re.match(r'^[0-9a-f]{4,}$', w.lower()) and re.search(r'[a-f]', w.lower())
    )
    if len(words) >= 3 and hex_count >= len(words) // 2:
        return ""
    # Чисто числовой ID (Telegram post ID и т.п.) → не транслитерируем
    if len(words) == 1 and re.match(r'^\d+$', words[0]):
        return ""
    decoded_words = [_translit_word(w) for w in words]
    result = " ".join(decoded_words).strip()
    # Склеиваем версионные номера: "6 7 0" → "6.7.0", "23 1592" → "23.1592"
    result = re.sub(r'\b\d{1,4}(?:\s+\d{1,4}){1,4}\b', lambda m: m.group(0).replace(" ", "."), result)
    return result[:1].upper() + result[1:] if result else ""


def parse_links(markup, base):
    """Возвращает {'content': [...], 'external': [...]} — свой контент и сторонние ресурсы."""
    markup = strip_noise(markup)
    base_dom = reg_domain(base)
    content, external = [], []
    seen = set()
    for match in re.finditer(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', markup, re.I | re.S):
        raw = match.group(1)
        href = abs_url(raw, base)
        if not href.startswith("http"):
            continue
        if JUNK_HREF.search(href) or JUNK_PATH.search(href):
            continue
        bare = href.split("#")[0]
        text = clean_text(re.sub(r"<[^>]+>", " ", match.group(2)))
        if "{" in text or "}" in text or ("px" in text and ":" in text):
            continue  # просочившийся CSS
        ntext = text.lower()
        if len(text) < 8 or ntext in GENERIC_TEXT:
            title = slug_title(bare)
            if not title or len(title) < 6:
                continue  # кнопка без смысла — пропускаем
        else:
            title = text[:200]
        if bare in seen:
            continue
        seen.add(bare)
        dom = reg_domain(bare)
        if SOCIAL_HINT.search(bare):
            continue  # соцсети собираются отдельно
        item = {"title": title, "url": bare}
        if dom and dom == base_dom:
            # пропускаем голую главную и совсем короткие пути
            path = urllib.parse.urlparse(bare).path.strip("/")
            if len(path) < 2:
                continue
            content.append(item)
        else:
            external.append(item)
    return {"content": content[:70], "external": external[:30]}


def parse_social_links(markup):
    found = {}
    for match in re.finditer(r'href=["\']([^"\']+)["\']', markup, re.I):
        url = match.group(1)
        m = SOCIAL_HINT.search(url)
        if not m:
            continue
        domain = m.group(1).lower().replace("www.", "")
        # короткое имя площадки
        name = {
            "t.me": "Telegram", "telegram.me": "Telegram", "vk.com": "VK",
            "youtube.com": "YouTube", "youtu.be": "YouTube", "dzen.ru": "Дзен",
            "zen.yandex": "Дзен", "rutube.ru": "RuTube", "ok.ru": "Одноклассники",
            "instagram.com": "Instagram", "facebook.com": "Facebook", "linkedin.com": "LinkedIn",
        }.get(domain, domain)
        clean = url.split("?")[0].rstrip("/")
        if name not in found:
            found[name] = clean
    return [f"{name}: {url}" for name, url in found.items()]


def parse_emails(markup):
    emails = set()
    for match in re.finditer(r"mailto:([^\"'?>\s]+)", markup, re.I):
        emails.add(match.group(1).strip().lower())
    for match in re.finditer(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", markup):
        addr = match.group(0).lower()
        if not re.search(r"\.(png|jpg|jpeg|gif|svg|webp|js|css)$", addr):
            if not addr.startswith(("example@", "name@", "mail@example", "test@")):
                emails.add(addr)
    return sorted(emails)[:10]


def parse_phones(markup):
    phones = set()
    # 1) Надёжнее всего — ссылки tel:
    for match in re.finditer(r'tel:([+\d\s\-\(\)]{10,})', markup, re.I):
        digits = re.sub(r"\D", "", match.group(1))
        if len(digits) == 11 and digits[0] in "78":
            phones.add("+7" + digits[1:])
    # 2) Номера в тексте — но только с разделителями (иначе ловим ID из вёрстки)
    pat = re.compile(r"(?<!\d)(?:\+7|8)[\s\-]*\(?\d{3}\)?[\s\-]+\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
    for match in pat.finditer(markup):
        raw = match.group(0)
        if not re.search(r"[\s\-()]", raw):
            continue
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 11 and digits[0] in "78":
            phones.add("+7" + digits[1:])
    # убираем явные плейсхолдеры вида +79999999999 / +70000000000
    phones = {p for p in phones if len(set(p[2:])) > 2}
    return sorted(phones)[:6]


def image_dimensions(data):
    """Достаём ширину/высоту картинки прямо из байтов, без сторонних библиотек."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            return w, h
        if data[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", data[6:10])
            return w, h
        if data[:2] == b"\xff\xd8":  # JPEG
            i, n = 2, len(data)
            while i < n - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                seg = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + seg
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X":
                w = (data[24] | (data[25] << 8) | (data[26] << 16)) + 1
                h = (data[27] | (data[28] << 8) | (data[29] << 16)) + 1
                return w, h
            if chunk == b"VP8 ":
                w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
                h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
                return w, h
            if chunk == b"VP8L":
                b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
                w = ((b1 & 0x3F) << 8 | b0) + 1
                h = ((b3 & 0x0F) << 10 | b2 << 2 | (b1 & 0xC0) >> 6) + 1
                return w, h
    except Exception:
        return None
    return None


def good_image(data):
    """Оставляем только нормальные картинки: не иконки, не тонкие полоски-баннеры."""
    dims = image_dimensions(data)
    if not dims:
        return False
    w, h = dims
    if w < 240 or h < 140:
        return False  # иконка/логотип
    ratio = w / h if h else 99
    if ratio > 4.5 or ratio < 0.22:
        return False  # тонкий баннер/разделитель
    return True


def parse_images(markup, base, key):
    markup = strip_noise(markup)
    items = []
    seen = set()
    for match in re.finditer(r'<img\s+[^>]*(?:data-src|data-lazy-src|src)=["\']([^"\']+)["\']', markup, re.I):
        src = abs_url(match.group(1), base)
        if not src.startswith("http") or src in seen:
            continue
        if re.search(
            r"sprite|icon|favicon|logo|blank|placeholder|loader|spacer|pixel|badge|qr|"
            r"app-?store|google-?play|play-?market|/vk|telegram|youtube|rutube|dzen|"
            r"whats|viber|facebook|/fb|/ok|button|/btn|arrow|\.svg($|\?)|data:image",
            src, re.I,
        ):
            continue
        seen.add(src)
        items.append(src)
    local = []
    for url in items[:16]:
        path = download_asset(url, key)
        if path:
            local.append(path)
        if len(local) >= 8:
            break
    return local


def download_asset(url, key):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen_with_ssl_fallback(req, timeout=8) as resp:
            ctype = resp.headers.get("content-type", "")
            if not ctype.startswith("image/"):
                return None
            data = resp.read(3_000_000)
        if not good_image(data):
            return None  # отсеиваем иконки, логотипы и баннеры-полоски
        ext = ".jpg"
        if "png" in ctype:
            ext = ".png"
        elif "webp" in ctype:
            ext = ".webp"
        elif "gif" in ctype:
            ext = ".gif"
        # стабильное имя по содержимому — не плодим дубликаты при повторных прогонах
        digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
        filename = f"{key}-{digest}{ext}"
        path = ASSETS / filename
        path.write_bytes(data)
        return str(path.relative_to(ROOT))
    except Exception:
        return None


# ─── Внешние упоминания: поиск в интернете ──────────────────────────────────

# Категоризация по домену источника
_MENTION_DOMAINS = {
    "forbes.ru":       ("Комментарий в СМИ", "#c62828"),
    "vc.ru":           ("Статья/обсуждение", "#1565c0"),
    "habr.com":        ("Статья на Хабре",   "#e65100"),
    "comnews.ru":      ("Комментарий в СМИ", "#c62828"),
    "cnews.ru":        ("Новость в СМИ",     "#ad1457"),
    "iksmedia.ru":     ("Новость в СМИ",     "#ad1457"),
    "hi-tech.mail.ru": ("Комментарий в СМИ", "#c62828"),
    "plusworld.ru":    ("Авторская колонка", "#6a1b9a"),
    "softadvisor.ru":  ("Обзор продукта",    "#00695c"),
    "tadviser.ru":     ("Обзор продукта",    "#00695c"),
    "rusbase.com":     ("Новость в СМИ",     "#ad1457"),
    "rb.ru":           ("Новость в СМИ",     "#ad1457"),
    "cio.ru":          ("Новость в СМИ",     "#ad1457"),
    "anti-malware.ru": ("Обзор продукта",    "#00695c"),
    "it-world.ru":     ("Новость в СМИ",     "#ad1457"),
    "retail.ru":       ("Новость в СМИ",     "#ad1457"),
    "retailer.ru":     ("Новость в СМИ",     "#ad1457"),
    "retail-loyalty.org": ("Новость в СМИ",  "#ad1457"),
    "executive.ru":    ("Авторская колонка", "#6a1b9a"),
    "therunet.com":    ("Новость в СМИ",     "#ad1457"),
    "prodmag.ru":      ("Новость в СМИ",     "#ad1457"),
}

# Ключевые слова в тексте статьи → уточняем категорию
_MENTION_KW = [
    (r"комментари[й|я]|сказал|заявил|отметил|рассказал|пояснил|добавил", "Комментарий эксперта", "#c62828"),
    (r"обзор|сравнени[е|я]|тестирован|review",                            "Обзор продукта",       "#00695c"),
    (r"рейтинг|топ\s*\d+|топ-\d+|best\s+\d+",                            "Рейтинг/Подборка",     "#00838f"),
    (r"кейс|успешн|внедрен|проект",                                        "Кейс/Внедрение",       "#2e7d32"),
    (r"вакансия|вакансии|ищем|join\s+us",                                  "Вакансия",             "#e53935"),
    (r"партнёр|партнер|интеграц",                                           "Партнёрство",          "#00897b"),
    (r"мероприят|конференц|вебинар|выставка|форум",                        "Мероприятие",          "#fb8c00"),
]

_RSS_MONTHS = {
    "Jan":"01","Feb":"02","Mar":"03","Apr":"04","May":"05","Jun":"06",
    "Jul":"07","Aug":"08","Sep":"09","Oct":"10","Nov":"11","Dec":"12",
}


def parse_rss_date(date_str):
    """'Mon, 15 Jun 2026 10:00:00 GMT' → '2026-06-15'."""
    m = re.search(r"(\d{1,2})\s+(\w{3})\s+(\d{4})", date_str or "")
    if m:
        d, mon, y = m.group(1).zfill(2), _RSS_MONTHS.get(m.group(2), "??"), m.group(3)
        return f"{y}-{mon}-{d}"
    return ""


def classify_mention(url, snippet=""):
    """Возвращает (label, color) для упоминания."""
    dom = reg_domain(url) or ""
    for d, (label, color) in _MENTION_DOMAINS.items():
        if dom.endswith(d):
            # Дополнительно уточняем по тексту сниппета
            for pat, lbl2, clr2 in _MENTION_KW:
                if re.search(pat, snippet, re.I):
                    return lbl2, clr2
            return label, color
    for pat, lbl, clr in _MENTION_KW:
        if re.search(pat, snippet, re.I):
            return lbl, clr
    return "Упоминание в СМИ", "#5c6bc0"


def collect_web_mentions(comp, month):
    """
    Ищет упоминания компании в интернете за нужный месяц через:
      1. Google News RSS
      2. Яндекс.Новости RSS
      3. Прямой поиск на ключевых отраслевых сайтах
    Возвращает список {'title', 'url', 'lastmod', 'snippet'}.
    """
    mm = MONTH_NUM.get(month, "") if month else ""
    current_year = time.strftime("%Y")
    results = []
    seen_urls = set()

    def add(item):
        url = item.get("url", "")
        if url in seen_urls or not url.startswith("http"):
            return
        # Фильтр по месяцу
        lm = item.get("lastmod", "")
        if mm and lm:
            parts = lm.split("-")
            if len(parts) >= 2 and parts[1] != mm:
                return
        # Если дата неизвестна или нет описания — читаем страницу
        needs_fetch = (mm and not lm) or not item.get("snippet")
        if needs_fetch:
            try:
                page = request_text(url, timeout=7)
                if mm and not lm:
                    lm = extract_visible_date(page)
                    if lm:
                        item["lastmod"] = lm
                        parts = lm.split("-")
                        if len(parts) >= 2 and parts[1] != mm:
                            return
                    elif mm:
                        return  # дата неизвестна → пропускаем
                ptitle, _ = parse_title_description(page)
                if ptitle and not item.get("title"):
                    item["title"] = ptitle[:180]
                # Генерируем аналитическое описание
                analytical = extract_analytical_description(page, comp["name"])
                if analytical:
                    item["snippet"] = analytical
            except Exception:
                if mm and not lm:
                    return  # без даты — пропускаем
        seen_urls.add(url)
        results.append(item)

    queries = [comp["name"]] + [q for q in comp.get("queries", []) if len(q) > 4]

    # ── 1. Google News RSS ─────────────────────────────────────────────────
    for query in queries[:3]:
        try:
            q = urllib.parse.quote(f'"{query}"')
            url = f"https://news.google.com/rss/search?q={q}&hl=ru&gl=RU&ceid=RU:ru"
            xml = request_text(url, timeout=12)
            for block in re.findall(r"<item>(.*?)</item>", xml, re.S | re.I):
                t = re.search(r"<title>(.*?)</title>", block, re.S)
                l = re.search(r"<link>(.*?)</link>", block, re.S)
                d = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
                desc = re.search(r"<description>(.*?)</description>", block, re.S)
                if not (t and l):
                    continue
                title = clean_text(re.sub(r"<[^>]+>", "", t.group(1)))
                link  = clean_text(l.group(1))
                pub   = parse_rss_date(d.group(1)) if d else ""
                snip  = clean_text(re.sub(r"<[^>]+>", " ", desc.group(1)))[:300] if desc else ""
                add({"title": title, "url": link, "lastmod": pub, "snippet": snip})
        except Exception as exc:
            log_error(f"[web_mentions] Google News query={query}", exc)

    # ── 2. Яндекс.Новости RSS ─────────────────────────────────────────────
    for query in queries[:2]:
        try:
            q = urllib.parse.quote(query)
            url = f"https://news.yandex.ru/search.rss?text={q}&lang=ru"
            xml = request_text(url, timeout=12)
            for block in re.findall(r"<item>(.*?)</item>", xml, re.S | re.I):
                t = re.search(r"<title>(.*?)</title>", block, re.S)
                l = re.search(r"<link>(.*?)</link>", block, re.S)
                d = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
                desc = re.search(r"<description>(.*?)</description>", block, re.S)
                if not (t and l):
                    continue
                title = clean_text(re.sub(r"<[^>]+>", "", t.group(1)))
                link  = clean_text(l.group(1))
                pub   = parse_rss_date(d.group(1)) if d else ""
                snip  = clean_text(re.sub(r"<[^>]+>", " ", desc.group(1)))[:300] if desc else ""
                add({"title": title, "url": link, "lastmod": pub, "snippet": snip})
        except Exception as exc:
            log_error(f"[web_mentions] Yandex News query={query}", exc)

    # ── 3. Прямой поиск на отраслевых сайтах ─────────────────────────────
    INDUSTRY_SITES = [
        ("vc.ru",          f"https://vc.ru/search?q={urllib.parse.quote(comp['name'])}"),
        ("habr.com",       f"https://habr.com/ru/search/?q={urllib.parse.quote(comp['name'])}&target_type=posts"),
        ("cnews.ru",       f"https://www.cnews.ru/search?q={urllib.parse.quote(comp['name'])}"),
        ("comnews.ru",     f"https://www.comnews.ru/search/node/{urllib.parse.quote(comp['name'])}"),
        ("tadviser.ru",    f"https://www.tadviser.ru/index.php?s={urllib.parse.quote(comp['name'])}"),
        ("softadvisor.ru", f"https://softadvisor.ru/catalog?q={urllib.parse.quote(comp['name'])}"),
        ("retail.ru",      f"https://www.retail.ru/search/?q={urllib.parse.quote(comp['name'])}"),
    ]
    # Навигационные слова, которые точно не являются статьями
    _NAV_TEXTS = re.compile(
        r"^(все\s+(потоки|статьи|новости|темы|публикации|материалы|записи|теги)|"
        r"редактировать|войти|регистрация|подписаться|загрузить|ещё|показать ещё|"
        r"следующая|предыдущая|назад|далее|главная|в начало|поиск|сортировка|"
        r"хабы|компании|пользователи|авторы|все темы|написать|новый пост)$",
        re.I | re.U,
    )
    # Параллельно загружаем страницы всех отраслевых сайтов
    def _fetch_industry_page(args):
        _, search_url = args
        try:
            return search_url, request_text(search_url, timeout=10)
        except Exception:
            return search_url, None

    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
    _pages = {}
    with _TPE(max_workers=5) as _pool:
        _futs = {_pool.submit(_fetch_industry_page, item): item for item in INDUSTRY_SITES}
        for _fut in _ac(_futs, timeout=35):
            try:
                _url, _html = _fut.result()
                if _html:
                    _pages[_url] = _html
            except Exception:
                pass

    for site_name, search_url in INDUSTRY_SITES:
        page = _pages.get(search_url)
        if not page:
            continue
        try:
            for m in re.finditer(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page, re.I | re.S):
                href = abs_url(m.group(1), search_url)
                text = clean_text(re.sub(r"<[^>]+>", " ", m.group(2)))
                if not href.startswith("http"):
                    continue
                # Минимум 3 слова и 15 символов в заголовке
                if len(text) < 15 or len(text.split()) < 3:
                    continue
                # Навигационный текст — пропускаем
                if _NAV_TEXTS.match(text.strip()):
                    continue
                # Только тот же домен
                if reg_domain(href) != reg_domain(search_url):
                    continue
                # Служебные пути — пропускаем
                if re.search(r"/search|/tag|/author|/category|/page|/sandbox|/hub|/user", href, re.I):
                    continue
                # Заголовок ДОЛЖЕН содержать упоминание компании
                comp_check = any(
                    q.lower() in text.lower() or q.lower() in href.lower()
                    for q in [comp["name"]] + comp.get("queries", [])
                )
                if comp_check:
                    add({"title": text[:180], "url": href, "lastmod": "", "snippet": ""})
        except Exception:
            pass

    # ── 4. Поиск мероприятий через Google News RSS ────────────────────────
    event_suffixes = ["вебинар", "конференция", "спикер", "выступление"]
    for suffix in event_suffixes[:2]:
        try:
            q = urllib.parse.quote(f'"{comp["name"]}" {suffix}')
            url = f"https://news.google.com/rss/search?q={q}&hl=ru&gl=RU&ceid=RU:ru"
            xml = request_text(url, timeout=10)
            for block in re.findall(r"<item>(.*?)</item>", xml, re.S | re.I):
                t = re.search(r"<title>(.*?)</title>", block, re.S)
                l = re.search(r"<link>(.*?)</link>", block, re.S)
                d = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
                desc = re.search(r"<description>(.*?)</description>", block, re.S)
                if not (t and l):
                    continue
                title = clean_text(re.sub(r"<[^>]+>", "", t.group(1)))
                link  = clean_text(l.group(1))
                pub   = parse_rss_date(d.group(1)) if d else ""
                snip  = clean_text(re.sub(r"<[^>]+>", " ", desc.group(1)))[:300] if desc else ""
                add({"title": title, "url": link, "lastmod": pub, "snippet": snip})
        except Exception as exc:
            log_error(f"[web_mentions] events query={suffix}", exc)

    return results[:40]


_RU_MONTHS = {
    "январ": "01", "феврал": "02", "март": "03", "апрел": "04",
    "ма": "05",  # май/мая — короткий префикс, проверяем отдельно
    "июн": "06", "июл": "07", "август": "08",
    "сентябр": "09", "октябр": "10", "ноябр": "11", "декабр": "12",
}
_RU_MONTH_PAT = re.compile(
    r"(\d{1,2})\s*(январ[яь]?|феврал[яь]?|март[аe]?|апрел[яь]?|ма[йя]|"
    r"июн[яь]?|июл[яь]?|август[аe]?|сентябр[яь]?|октябр[яь]?|ноябр[яь]?|декабр[яь]?)\s*(\d{4})",
    re.I | re.U,
)
_NUM_DATE_PAT = re.compile(r"(\d{2})[.\-/](\d{2})[.\-/](\d{4})")
_ISO_DATE_PAT = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def extract_meta_date(markup):
    """
    Ищет дату публикации в машиночитаемых тегах — рендерятся сервер-сайд
    даже на Tilda и других JS-фреймворках.
    Приоритет: JSON-LD > <time datetime> > <meta property/name>.
    Возвращает 'YYYY-MM-DD' или ''.
    """
    # 1. JSON-LD: {"datePublished": "2026-06-03"} — самый надёжный источник
    for jld_match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        markup, re.S | re.I,
    ):
        try:
            data = json.loads(jld_match.group(1))
            if isinstance(data, list):
                data = data[0] if data else {}
            # Обходим вложенные @graph
            if "@graph" in data:
                for node in data["@graph"]:
                    dp = node.get("datePublished") or node.get("dateCreated") or ""
                    if dp and re.match(r"\d{4}-\d{2}-\d{2}", str(dp)):
                        return str(dp)[:10]
            dp = data.get("datePublished") or data.get("dateCreated") or data.get("uploadDate") or ""
            if dp and re.match(r"\d{4}-\d{2}-\d{2}", str(dp)):
                return str(dp)[:10]
        except Exception:
            pass

    # 2. <time datetime="2026-06-03"> — HTML5 стандарт, Tilda его использует
    m = re.search(r'<time[^>]+datetime=["\'](\d{4}-\d{2}-\d{2})[T"\']', markup, re.I)
    if m:
        return m.group(1)

    # 3. <meta property="article:published_time" content="2026-06-03T...">
    _meta_pats = [
        r'property=["\']article:published_time["\'][^>]+content=["\'](\d{4}-\d{2}-\d{2})',
        r'content=["\'](\d{4}-\d{2}-\d{2})[^"\']*["\'][^>]+property=["\']article:published_time["\']',
        r'name=["\'](?:date|pubdate|publish[_-]date|publication[_-]date)["\'][^>]+content=["\'](\d{4}-\d{2}-\d{2})',
        r'property=["\']og:article:published_time["\'][^>]+content=["\'](\d{4}-\d{2}-\d{2})',
        r'itemprop=["\']datePublished["\'][^>]+content=["\'](\d{4}-\d{2}-\d{2})',
        r'content=["\'](\d{4}-\d{2}-\d{2})[^"\']*["\'][^>]+itemprop=["\']datePublished["\']',
    ]
    for pat in _meta_pats:
        m = re.search(pat, markup, re.I)
        if m:
            return m.group(1)

    # 4. Tilda: data-record-type="101" + ищем дату в data-атрибутах
    m = re.search(r'data-(?:date|published)[^=]*=["\'](\d{4}-\d{2}-\d{2})', markup, re.I)
    if m:
        return m.group(1)

    return ""


def extract_visible_date(markup):
    """
    Ищет дату публикации. Приоритет:
    1. Машиночитаемые теги (meta, JSON-LD, time datetime) — надёжнее всего
    2. Видимый текст страницы (русские и числовые форматы)
    Возвращает 'YYYY-MM-DD' или ''.
    """
    # Приоритет 1: meta-теги и JSON-LD (работают даже на Tilda)
    meta = extract_meta_date(markup)
    if meta:
        return meta

    # Приоритет 2: видимый текст
    clean = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)
    clean = re.sub(r"<[^>]+>", " ", clean)
    # Русская дата: «15 июня 2026»
    m = _RU_MONTH_PAT.search(clean)
    if m:
        day, month_word, year = m.group(1), m.group(2).lower(), m.group(3)
        mm = None
        for prefix, num in _RU_MONTHS.items():
            if month_word.startswith(prefix):
                mm = num
                break
        if not mm and month_word.startswith("ма"):
            mm = "05"
        if mm:
            return f"{year}-{mm}-{day.zfill(2)}"
    # Числовая дата: «15.06.2026»
    m = _NUM_DATE_PAT.search(clean)
    if m:
        d, mo, y = m.group(1), m.group(2), m.group(3)
        if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            return f"{y}-{mo}-{d}"
    # ISO-дата в тексте: «2026-06-15»
    m = _ISO_DATE_PAT.search(clean)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


@functools.lru_cache(maxsize=32)
def parse_sitemap(site_url, timeout=10):
    """Возвращает {url: lastmod} из sitemap.xml / sitemap_index.xml."""
    result = {}
    base = site_url.rstrip("/")
    for path in ["/sitemap.xml", "/sitemap_index.xml", "/sitemap.xml.gz", "/sitemap/"]:
        try:
            xml = request_text(base + path, timeout=timeout)
            if not xml.strip():
                continue
            # Если это индекс — вытащим первые 5 дочерних sitemap (включая feeds)
            for loc in re.findall(r"<sitemap>\s*<loc>(.*?)</loc>", xml, re.S | re.I)[:5]:
                try:
                    sub = request_text(clean_text(loc), timeout=timeout)
                    xml += sub
                except Exception:
                    pass
            # Парсим все <url> блоки
            for block in re.findall(r"<url>(.*?)</url>", xml, re.S | re.I):
                loc_m = re.search(r"<loc>(.*?)</loc>", block, re.S | re.I)
                lm_m = re.search(r"<lastmod>(.*?)</lastmod>", block, re.S | re.I)
                if loc_m:
                    url = clean_text(loc_m.group(1))
                    lastmod = clean_text(lm_m.group(1)) if lm_m else ""
                    result[url] = lastmod
            if result:
                break
        except Exception:
            continue
    return result


def _url_month(url, mm):
    """True если URL содержит номер месяца в типичных паттернах дат."""
    return bool(
        re.search(rf"[/\-]{mm}[/\-]", url) or
        re.search(rf"2026{mm}", url) or
        re.search(rf"/{mm}/", url)
    )


def item_matches_month(item, mm):
    """True если статья опубликована в нужном месяце (по lastmod, URL или тексту страницы)."""
    if not mm:
        return True
    lastmod = item.get("lastmod", "")
    url = item.get("url", "")
    if lastmod:
        # lastmod формат YYYY-MM-DD или YYYY-MM
        parts = lastmod.split("-")
        if len(parts) >= 2 and parts[1] == mm:
            return True
        return False  # lastmod есть, но не тот месяц
    if _url_month(url, mm):
        return True
    # Видимая дата читается при fetch страницы — делается снаружи
    return None  # None = неизвестно, нужно зайти на страницу


def collect_site(comp, month=None):
    """month — трёхбуквенный код ('jun', 'jul' и т.д.) или None (без фильтра)."""
    result = {"summary": "", "content": [], "external": [], "images": [],
              "social": [], "emails": [], "phones": [], "errors": []}
    mm = MONTH_NUM.get(month, "") if month else ""
    try:
        markup = request_text(comp["site"])
        title, desc = parse_title_description(markup)
        result["summary"] = clean_text(" - ".join([x for x in [title, desc] if x]))[:450]
        result["images"] = parse_images(markup, comp["site"], comp["key"])
        result["social"] = parse_social_links(markup)
        result["emails"] = parse_emails(markup)
        result["phones"] = parse_phones(markup)

        # --- Sitemap: основной источник статей с датами ---
        # Служебные страницы, которые точно не являются статьями
        _SERVICE_PATHS = re.compile(
            r"^/?(about|o-nas|o-kompanii|contacts?|kontakt|pricing|tarif|features?|"
            r"funktsii|product|demo|partners?|team|komanda|career|karera|jobs?|"
            r"vakansii|faq|help|support|privacy|policy|terms|legal|sitemap|"
            r"404|login|register|signup|cabinet|personal|profile|search|tag|"
            r"category|rubrika|author|feed|rss|api)(/|$)",
            re.I,
        )

        sitemap = parse_sitemap(comp["site"])
        sitemap_content = []
        _sitemap_unknown = []  # URL без даты — проверим параллельно
        for url, lastmod in sitemap.items():
            path = urllib.parse.urlparse(url).path.strip("/")

            # Пропускаем служебные страницы по первому сегменту пути
            first_seg = path.split("/")[0] if path else ""
            if _SERVICE_PATHS.match("/" + first_seg):
                continue

            # Пропускаем слишком короткие пути (главная, /about, /en/ и т.п.)
            if len(path) < 5:
                continue

            # Если есть lastmod → это явный кандидат, берём без проверки URL-паттерна
            # Если нет lastmod → смотрим: URL содержит слово про контент ИЛИ slug длинный (≥3 слова)
            if not lastmod:
                url_is_content = bool(re.search(
                    r"blog|news|article|post|case|keysy|events|press|tpost|"
                    r"update|release|стат|кейс|новост|material|publication",
                    url, re.I,
                ))
                slug_words = len(re.findall(r"[a-zA-Zа-яА-Я]{3,}", path.split("/")[-1]))
                if not url_is_content and slug_words < 3:
                    continue  # короткий безымянный slug — скорее всего не статья

            item = {"url": url, "title": slug_title(url), "lastmod": lastmod}
            check = item_matches_month(item, mm)
            if check is True:
                sitemap_content.append(item)
            elif check is None and mm:
                _sitemap_unknown.append(item)  # отложим на параллельный fetch
            elif not mm:
                sitemap_content.append(item)

        # Параллельный fetch для URL без известной даты (cap=20, 8 воркеров, 5с на URL)
        if _sitemap_unknown and mm:
            def _fetch_sitemap_date(item):
                try:
                    page = request_text(item["url"], timeout=5)
                    visible_date = extract_visible_date(page)
                    if visible_date:
                        item = dict(item)
                        item["lastmod"] = visible_date
                        if item_matches_month(item, mm) is True:
                            ptitle, _ = parse_title_description(page)
                            if ptitle:
                                item["title"] = ptitle[:180]
                            return item
                except Exception:
                    pass
                return None
            from concurrent.futures import ThreadPoolExecutor as _TPE2, as_completed as _ac2
            with _TPE2(max_workers=8) as _p2:
                _futs2 = {_p2.submit(_fetch_sitemap_date, it): it for it in _sitemap_unknown[:20]}
                for _f2 in _ac2(_futs2, timeout=40):
                    try:
                        _r2 = _f2.result()
                        if _r2:
                            sitemap_content.append(_r2)
                    except Exception:
                        pass

        # --- Blog feed fallback (для Tilda и других JS-блогов без статей в sitemap) ---
        if not sitemap_content and comp.get("blog_feed"):
            try:
                feed_raw = request_text(comp["blog_feed"], timeout=15)
                feed_items = []
                # Попытка 1: JSON (Dzen API возвращает application/json)
                try:
                    feed_data = json.loads(feed_raw)
                    for key in ("items", "publications", "articles", "entries", "posts"):
                        if isinstance(feed_data.get(key), list):
                            feed_items = feed_data[key]
                            break
                    if not feed_items and isinstance(feed_data, list):
                        feed_items = feed_data
                except (ValueError, TypeError):
                    pass
                # Попытка 2: XML/RSS (стандартный Atom/RSS-формат)
                if not feed_items:
                    for block in re.findall(r"<item>(.*?)</item>", feed_raw, re.S | re.I):
                        t_m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
                        l_m = re.search(r"<(?:origLink|link)>(.*?)</(?:origLink|link)>", block, re.S)
                        d_m = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
                        if t_m and l_m:
                            feed_items.append({
                                "title":   clean_text(t_m.group(1)),
                                "link":    clean_text(l_m.group(1)),
                                "pubDate": d_m.group(1).strip() if d_m else "",
                            })

                _comp_domain = comp.get("domain", "")
                _feed_unknown = []
                for fi in feed_items:
                    # Предпочитаем оригинальную ссылку на сайт конкурента
                    # (Dzen кросс-постинг: origLink / sourceUrl / originalUrl → mera-soft.ru/tpost/...)
                    url = (fi.get("origLink") or fi.get("sourceUrl") or fi.get("originalUrl") or "")
                    if not url:
                        # Если нет поля с оригинальным URL — fallback на link/url
                        url = fi.get("link") or fi.get("url") or ""
                    if not url or not url.startswith("http"):
                        continue
                    # Берём только статьи с домена самого конкурента — не Dzen, не сторонние
                    if _comp_domain and _comp_domain not in url:
                        continue
                    title = (fi.get("title") or fi.get("name") or slug_title(url) or "")
                    if isinstance(title, str):
                        title = title[:180]
                    raw_date = (fi.get("pubDate") or fi.get("publishedDate") or
                                fi.get("isoDate") or fi.get("date") or fi.get("published") or "")
                    lastmod = parse_rss_date(raw_date) if isinstance(raw_date, str) and raw_date else ""
                    item = {"url": url, "title": title, "lastmod": lastmod}
                    check = item_matches_month(item, mm)
                    if check is True:
                        sitemap_content.append(item)
                    elif check is None and mm:
                        _feed_unknown.append(item)
                    elif not mm:
                        sitemap_content.append(item)

                # Для статей без известной даты — пробуем fetchить страницу
                if _feed_unknown and mm:
                    def _fetch_feed_date(item):
                        try:
                            page = request_text(item["url"], timeout=6)
                            visible_date = extract_visible_date(page)
                            if visible_date:
                                item = dict(item)
                                item["lastmod"] = visible_date
                                if item_matches_month(item, mm) is True:
                                    ptitle, _ = parse_title_description(page)
                                    if ptitle:
                                        item["title"] = ptitle[:180]
                                    return item
                        except Exception:
                            pass
                        return None
                    from concurrent.futures import ThreadPoolExecutor as _TPEF, as_completed as _acF
                    with _TPEF(max_workers=6) as _pF:
                        _futsF = {_pF.submit(_fetch_feed_date, it): it for it in _feed_unknown[:15]}
                        for _fF in _acF(_futsF, timeout=30):
                            try:
                                _rF = _fF.result()
                                if _rF:
                                    sitemap_content.append(_rF)
                            except Exception:
                                pass
            except Exception as exc:
                log_error(f"[collect_site] blog_feed {comp.get('blog_feed')}", exc)

        # --- HTML-парсинг главной + раздела блога (параллельно, до 3 доп. страниц) ---
        html_content = []
        html_external = []
        pages_to_check = discover_extra_public_pages(markup, comp["site"])[:3]

        def _fetch_extra_page(extra):
            try:
                page = request_text(extra, timeout=6) if extra != comp["site"] else markup
                return extra, page
            except Exception:
                return extra, None

        from concurrent.futures import ThreadPoolExecutor as _TPE3, as_completed as _ac3
        with _TPE3(max_workers=4) as _p3:
            _futs3 = {_p3.submit(_fetch_extra_page, u): u for u in [comp["site"]] + pages_to_check}
            for _f3 in _ac3(_futs3, timeout=20):
                try:
                    _url3, _page3 = _f3.result()
                    if not _page3:
                        continue
                    more = parse_links(_page3, _url3)
                    html_content.extend(more["content"])
                    html_external.extend(more["external"])
                    if len(result["images"]) < 8:
                        result["images"].extend(parse_images(_page3, _url3, comp["key"])[:3])
                    result["social"].extend(parse_social_links(_page3))
                    result["emails"].extend(parse_emails(_page3))
                    result["phones"].extend(parse_phones(_page3))
                except Exception:
                    pass

        # Если есть данные из sitemap — используем их как контент,
        # HTML-ссылки используем только как дополнение для статей без sitemap.
        if sitemap_content:
            result["content"] = sitemap_content
        else:
            # Sitemap недоступен — фильтруем HTML-ссылки по дате (параллельно)
            _html_known = []
            _html_unknown = []
            for item in unique_items(html_content, "url"):
                check = item_matches_month(item, mm)
                if check is True:
                    _html_known.append(item)
                elif check is None and mm:
                    _html_unknown.append(item)
                elif not mm:
                    _html_known.append(item)

            if _html_unknown and mm:
                def _fetch_html_date(item):
                    try:
                        page = request_text(item["url"], timeout=5)
                        visible_date = extract_visible_date(page)
                        if visible_date:
                            item = dict(item)
                            item["lastmod"] = visible_date
                            if item_matches_month(item, mm) is True:
                                ptitle, _ = parse_title_description(page)
                                if ptitle:
                                    item["title"] = ptitle[:180]
                                return item
                    except Exception:
                        pass
                    return None
                from concurrent.futures import ThreadPoolExecutor as _TPE4, as_completed as _ac4
                with _TPE4(max_workers=8) as _p4:
                    _futs4 = {_p4.submit(_fetch_html_date, it): it for it in _html_unknown[:15]}
                    for _f4 in _ac4(_futs4, timeout=30):
                        try:
                            _r4 = _f4.result()
                            if _r4:
                                _html_known.append(_r4)
                        except Exception:
                            pass
            result["content"] = _html_known

        # --- Фильтрация внешних публикаций по дате (параллельно, cap=15) ---
        _ext_unique = unique_items(html_external, "url")[:15]
        filtered_external = []

        def _check_ext(ext_item):
            if not mm:
                return ext_item
            if _url_month(ext_item.get("url", ""), mm):
                return ext_item
            try:
                ext_page = request_text(ext_item["url"], timeout=5)
                visible_date = extract_visible_date(ext_page)
                if visible_date:
                    ext_item = dict(ext_item)
                    ext_item["lastmod"] = visible_date
                    parts = visible_date.split("-")
                    if len(parts) >= 2 and parts[1] == mm:
                        ptitle, _ = parse_title_description(ext_page)
                        if ptitle:
                            ext_item["title"] = ptitle[:180]
                        return ext_item
            except Exception:
                pass
            return None

        from concurrent.futures import ThreadPoolExecutor as _TPE5, as_completed as _ac5
        with _TPE5(max_workers=8) as _p5:
            _futs5 = {_p5.submit(_check_ext, it): it for it in _ext_unique}
            for _f5 in _ac5(_futs5, timeout=25):
                try:
                    _r5 = _f5.result()
                    if _r5:
                        filtered_external.append(_r5)
                except Exception:
                    pass

        result["external"] = filtered_external[:30]
        result["content"] = unique_items(result["content"], "url")[:50]
        result["images"] = list(dict.fromkeys(result["images"]))[:14]
        result["social"] = list(dict.fromkeys(result["social"]))[:12]
        result["emails"] = list(dict.fromkeys(result["emails"]))[:10]
        result["phones"] = list(dict.fromkeys(result["phones"]))[:6]
    except Exception as exc:
        result["errors"].append(f"Сайт не собрался: {exc}")
    return result


def _strip_tracking_params(url: str) -> str:
    """Убирает UTM и другие трекинговые параметры из URL для дедупликации."""
    try:
        parsed = urllib.parse.urlparse(url)
        _TRACKING = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content",
                     "yclid","gclid","fbclid","ref","from","source","medium"}
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        clean_qs = {k: v for k, v in qs.items() if k.lower() not in _TRACKING}
        clean_query = urllib.parse.urlencode(clean_qs, doseq=True)
        return urllib.parse.urlunparse(parsed._replace(query=clean_query))
    except Exception:
        return url


def unique_items(items, key):
    seen = set()
    out = []
    for item in items:
        raw_value = item.get(key) if isinstance(item, dict) else item
        # Для URL-ключей дедуплицируем без трекинговых параметров
        value = _strip_tracking_params(raw_value) if key == "url" and raw_value else raw_value
        if value in seen:
            continue
        seen.add(value)
        out.append(item)
    return out


def discover_extra_public_pages(markup, base):
    urls = []
    candidates = [
        "/sitemap.xml", "/sitemap_index.xml", "/rss", "/rss.xml", "/feed",
        "/blog/", "/blog", "/news/", "/news", "/events/", "/events",
        "/case/", "/cases/", "/cases", "/keysy/", "/press/", "/media/",
        "/updates/", "/release-notes/", "/changelog/", "/webinars/",
        "/about/", "/about", "/career/", "/vacancy/", "/jobs/", "/contacts/",
    ]
    for c in candidates:
        urls.append(abs_url(c, base))
    # ссылки на разделы прямо со страницы
    for match in re.finditer(r'<a\s+[^>]*href=["\']([^"\']+)["\']', markup, re.I):
        u = abs_url(match.group(1), base)
        if u.startswith("http") and re.search(r"blog|news|case|keys|event|press|media|update|release|changelog|стат|кейс|новост", u, re.I):
            urls.append(u.split("#")[0])
    # из sitemap
    for match in re.finditer(r"<loc>(.*?)</loc>", markup, re.I | re.S):
        u = clean_text(match.group(1))
        if re.search(r"blog|news|article|post|case|events|press|media|tpost|update|release", u, re.I):
            urls.append(u)
    return list(dict.fromkeys(urls))


_EVENT_KW = re.compile(
    r"вебинар|webinar|конференц|форум|выставк|кейс.чемпион|митап|meetup|"
    r"выступ[аеёи]|спикер|speaker|эфир|онлайн.встреч|приглашаем|регистрац|"
    r"join\s+us|участвуем|участие|будем\s+на|приходите|запись\s+на",
    re.I | re.U,
)
_EVENT_ROLE = [
    (re.compile(r"организу[её]м|проводим|мы\s+проводим|наш\s+вебинар", re.I | re.U), "Организатор"),
    (re.compile(r"спикер|выступ[аеё]|будем\s+рассказыв|наш\s+эксперт",  re.I | re.U), "Спикер"),
    (re.compile(r"партнёр|партнер|генеральный\s+партнер|спонсор",          re.I | re.U), "Партнёр"),
    (re.compile(r"участву[её]м|участие|принима[её]м\s+участие",           re.I | re.U), "Участник"),
]
_EVENT_FORMAT = [
    (re.compile(r"вебинар|webinar|онлайн.встреч|онлайн\s+мероприят", re.I), "Вебинар"),
    (re.compile(r"конференц|forum|форум",                              re.I), "Конференция"),
    (re.compile(r"выставк|expo|экспо",                                 re.I), "Выставка"),
    (re.compile(r"митап|meetup",                                        re.I), "Митап"),
    (re.compile(r"кейс.чемпион|case.champ",                            re.I), "Кейс-чемпионат"),
    (re.compile(r"эфир",                                                re.I), "Онлайн-эфир"),
]


def _extract_event_date(text):
    """Ищет дату мероприятия в тексте Telegram-поста. Возвращает 'YYYY-MM-DD' или ''."""
    # «15 июня», «15.06», «15 июня 2026»
    m = _RU_MONTH_PAT.search(text)
    if m:
        day, month_word, year = m.group(1), m.group(2).lower(), m.group(3)
        mm = None
        for prefix, num in _RU_MONTHS.items():
            if month_word.startswith(prefix):
                mm = num
                break
        if not mm and month_word.startswith("ма"):
            mm = "05"
        if mm:
            return f"{year}-{mm}-{day.zfill(2)}"
    # «15.06.2026» или «15.06»
    m = re.search(r"(\d{1,2})[./](\d{2})(?:[./](\d{4}))?", text)
    if m:
        d, mo = m.group(1).zfill(2), m.group(2)
        y = m.group(3) or time.strftime("%Y")
        if 1 <= int(mo) <= 12:
            return f"{y}-{mo}-{d}"
    return ""


def _extract_links(raw_html):
    """Вытаскивает все http-ссылки из HTML-блока Telegram-поста."""
    links = []
    for m in re.finditer(r'href=["\']([^"\']+)["\']', raw_html, re.I):
        href = m.group(1)
        if href.startswith("http") and "t.me" not in href:
            links.append(href.split("?")[0])
        elif href.startswith("http"):
            links.append(href)
    return list(dict.fromkeys(links))


def _build_event_summary(text, fmt_label, role):
    """Формирует одно предложение-описание мероприятия из текста поста."""
    # Берём первые 2 предложения
    sentences = re.split(r"[.!?]\s+", text.strip())
    summary = " ".join(s.strip() for s in sentences[:2] if s.strip())[:220]
    if not summary:
        summary = text[:200]
    role_str = f", роль: {role}" if role else ""
    fmt_str = f"{fmt_label}{role_str}. " if fmt_label else ""
    return f"{fmt_str}{summary}"


def parse_event_from_tg_post(raw_html, channel_url, month=None):
    """
    Из одного Telegram-поста пытается извлечь мероприятие.
    Возвращает dict или None.
    """
    text_clean = clean_text(re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.I))
    text_clean = clean_text(re.sub(r"<[^>]+>", " ", text_clean))

    if not _EVENT_KW.search(text_clean):
        return None

    # Формат мероприятия
    fmt_label = "Мероприятие"
    for pat, label in _EVENT_FORMAT:
        if pat.search(text_clean):
            fmt_label = label
            break

    # Роль компании
    role = ""
    for pat, r in _EVENT_ROLE:
        if pat.search(text_clean):
            role = r
            break
    if not role:
        role = "Участник"  # по умолчанию

    # Дата события
    event_date = _extract_event_date(text_clean)

    # Фильтр по месяцу анализа
    mm = MONTH_NUM.get(month, "") if month else ""
    if mm and event_date:
        parts = event_date.split("-")
        if len(parts) >= 2 and parts[1] != mm:
            return None  # мероприятие в другом месяце
    # Если дата не найдена и месяц задан — включаем (дата могла быть в предыдущем посте)

    # Ссылки из поста
    links = _extract_links(raw_html)
    # Ищем ссылки на регистрацию / запись
    reg_link = ""
    rec_link = ""
    for lnk in links:
        if re.search(r"timepad|eventbrite|meetup|luma|reg|ticket|bilet|event", lnk, re.I):
            reg_link = lnk
        elif re.search(r"record|запись|video|youtube|youtu\.be|rutube|vk\.com/video", lnk, re.I):
            rec_link = lnk
    link = reg_link or rec_link or (links[0] if links else channel_url)

    summary = _build_event_summary(text_clean, fmt_label, role)

    return {
        "title": summary[:180],
        "url": link,
        "lastmod": event_date,
        "format": fmt_label,
        "role": role,
        "reg_link": reg_link,
        "rec_link": rec_link,
        "snippet": text_clean[:300],
    }


def collect_telegram(channel, month=None):
    if not channel:
        return {"posts": [], "events": [], "subscribers": None, "errors": []}
    channel = channel.strip().replace("@", "")
    channel = re.sub(r"^https?://(t\.me|telegram\.me)/(s/)?", "", channel).split("/")[0]
    if not channel:
        return {"posts": [], "events": [], "subscribers": None, "errors": []}
    url = f"https://t.me/s/{channel}"
    result = {"posts": [], "events": [], "subscribers": None, "errors": []}
    try:
        markup = request_text(url)
        sub_match = re.search(
            r'class="tgme_channel_info_counter[^"]*"[^>]*>.*?class="counter_value"[^>]*>(.*?)</',
            markup, re.I | re.S,
        )
        if sub_match:
            result["subscribers"] = clean_text(sub_match.group(1))

        # Парсим каждое сообщение целиком (включая ссылки для извлечения)
        for msg_match in re.finditer(
            r'class="tgme_widget_message_wrap[^"]*"[^>]*>(.*?)</div>\s*</div>\s*</div>',
            markup, re.I | re.S,
        ):
            msg_html = msg_match.group(1)
            # Текст поста
            txt_match = re.search(
                r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                msg_html, re.I | re.S,
            )
            if not txt_match:
                continue
            post_html = txt_match.group(1)
            text = clean_text(re.sub(r"<br\s*/?>", "\n", post_html, flags=re.I))
            text = clean_text(re.sub(r"<[^>]+>", " ", text))

            if not text:
                continue

            # Пробуем распознать мероприятие
            event = parse_event_from_tg_post(post_html, url, month=month)
            if event:
                result["events"].append(event)
            else:
                essence = summarize_post_text(text)
                result["posts"].append({"title": essence, "url": url})

        result["posts"] = result["posts"][:10]
        result["events"] = result["events"][:15]
    except Exception as exc:
        result["errors"].append(f"Telegram не собрался: {exc}")
    return result


def parse_import_text(text):
    parsed = {comp["key"]: {"wordstat": {}, "spywords": {}, "social": {}} for comp in COMPETITORS}
    for raw_line in (text or "").splitlines():
        line = clean_text(raw_line)
        if not line:
            continue
        value = number_from(line)
        if value is None:
            continue
        nline = norm(line)
        metric_line = bool(re.search(r"top\s*\d+|топ\s*\d+|top-\d+|топ-\d+|telegram|телеграм|tg|vk|вк|вконтакте", nline))
        for comp in COMPETITORS:
            ckey = comp["key"]
            cname = norm(comp["name"])
            domain = norm(comp["domain"])
            mentions_comp = cname in nline or domain in nline or ckey in nline
            for query in comp["queries"]:
                nq = norm(query)
                if not metric_line and nq and (nline.startswith(nq) or f" {nq} " in f" {nline} "):
                    parsed[ckey]["wordstat"][query] = value
            if mentions_comp:
                is_yandex = bool(re.search(r"янд|yandex", nline))
                is_google = bool(re.search(r"google|гугл", nline))
                is_top50 = bool(re.search(r"top\s*50|топ\s*50|top-50|топ-50", nline))
                is_top10 = bool(re.search(r"top\s*10|топ\s*10|top-10|топ-10", nline))
                if is_yandex and is_top50:
                    parsed[ckey]["spywords"]["yandex_top50"] = value
                elif is_yandex and is_top10:
                    parsed[ckey]["spywords"]["yandex_top10"] = value
                elif is_google and is_top50:
                    parsed[ckey]["spywords"]["google_top50"] = value
                elif is_google and is_top10:
                    parsed[ckey]["spywords"]["google_top10"] = value
                elif re.search(r"telegram|телеграм|tg", nline):
                    parsed[ckey]["social"]["telegram"] = value
                elif re.search(r"vk|вк|вконтакте", nline):
                    parsed[ckey]["social"]["vk"] = value
    return parsed


def parse_manual_metrics(manual_metrics):
    parsed = {comp["key"]: {"spywords": {}, "social": {}} for comp in COMPETITORS}
    manual_metrics = manual_metrics or {}
    for comp in COMPETITORS:
        row = manual_metrics.get(comp["key"], {}) or {}
        for key in ["yandex_top50", "yandex_top10", "google_top50", "google_top10", "search_traffic_yandex", "search_traffic_google", "unique_urls_yandex", "unique_urls_google"]:
            if row.get(key) not in (None, ""):
                try:
                    parsed[comp["key"]]["spywords"][key] = int(row[key])
                except Exception:
                    pass
        for key in ["telegram", "vk"]:
            if row.get(key) not in (None, ""):
                try:
                    parsed[comp["key"]]["social"][key] = int(row[key])
                except Exception:
                    pass
    return parsed


def collect_authorized_text(auth):
    auth = auth or {}
    chunks = []
    errors = []
    targets = [
        ("SpyWords", auth.get("spywordsUrl"), auth.get("spywordsCookie")),
        ("Вордстат", auth.get("wordstatUrl"), auth.get("wordstatCookie")),
    ]
    for label, url, cookie in targets:
        if not url:
            continue
        headers = {}
        if cookie:
            headers["Cookie"] = cookie
        if auth.get("apiToken"):
            headers["Authorization"] = auth["apiToken"] if auth["apiToken"].lower().startswith("bearer ") else "Bearer " + auth["apiToken"]
        try:
            text = request_text(url, timeout=25, extra_headers=headers)
            chunks.append(f"\n\n### {label} authorized page {url}\n{text}")
        except Exception as exc:
            errors.append(f"{label}: авторизованный сбор не удался: {exc}")
    for url in auth.get("extraUrls", []) or []:
        try:
            text = request_text(url, timeout=20)
            chunks.append(f"\n\n### Extra URL {url}\n{text}")
        except Exception as exc:
            errors.append(f"Доп. URL {url}: не удалось собрать: {exc}")
    return "\n".join(chunks), errors


# ─── Соц. сети: категоризация и сбор ─────────────────────────────────────────

_POST_CATS = [
    ("Мероприятие",   re.compile(r"вебинар|webinar|конференц|форум|выставк|кейс.чемпион|митап|эфир|регистрац|спикер|выступ[аеёи]", re.I | re.U)),
    ("Кейс клиента",  re.compile(r"кейс|клиент|внедрен|результат|партнёр|партнер|сеть магазин|розниц", re.I | re.U)),
    ("Обновление",    re.compile(r"обновлен|релиз|версия|новый функц|улучшен|выпустил|запустил|feature|release", re.I | re.U)),
    ("Статья/блог",   re.compile(r"читайте|статья|публикац|материал|блог|разобрали|рассказыва", re.I | re.U)),
    ("Экспертный",    re.compile(r"совет|эксперт|практика|опыт|тренд|исследован|рейтинг|топ\s*\d", re.I | re.U)),
    ("Промо",         re.compile(r"скидк|акция|специальн|предложен|бесплатн|подпиш|попробуй", re.I | re.U)),
    ("Праздник/общее",re.compile(r"поздравля|праздник|день\s+\w+|выходн|пятниц", re.I | re.U)),
]


def categorize_post(text):
    """Возвращает категорию поста по тексту."""
    for label, pat in _POST_CATS:
        if pat.search(text):
            return label
    return "Другое"


def generate_social_summary(platform, handle, posts_by_cat, total, month_label):
    """Одно предложение-вывод об активности в соцсети за месяц."""
    if not total:
        return f"В {month_label} активности в {platform} не зафиксировано."
    # Находим топ-2 категории
    sorted_cats = sorted(posts_by_cat.items(), key=lambda x: x[1], reverse=True)
    top = [f"{cat} ({cnt})" for cat, cnt in sorted_cats[:2] if cnt]
    top_str = " и ".join(top) if top else "разные темы"
    freq = round(total / 4.3, 1)  # ~4.3 недель в месяце
    return (
        f"В {month_label} {platform} опубликовал {total} пост{'а' if 2 <= total <= 4 else 'ов' if total >= 5 else ''} "
        f"(~{freq:.0f}/нед), акцент на {top_str}."
    )


def collect_vk(group_slug, month=None):
    """
    Парсит публичную страницу ВКонтакте: подписчики + посты за месяц.
    Возвращает {'subscribers': str, 'posts': [...], 'errors': []}.
    """
    if not group_slug:
        return {"subscribers": None, "posts": [], "errors": []}
    slug = group_slug.strip().lstrip("@").replace("https://vk.com/", "").replace("vk.com/", "")
    result = {"subscribers": None, "posts": [], "errors": []}
    mm = MONTH_NUM.get(month, "") if month else ""

    try:
        url = f"https://vk.com/{slug}"
        markup = request_text(url, timeout=12)

        # Подписчики
        sub_m = re.search(
            r'(?:подписчик|follower)[^<]{0,60}?(\d[\d\s]+)\b|'
            r'"members_count"\s*:\s*(\d+)|'
            r'class="[^"]*followers[^"]*"[^>]*>.*?(\d[\d\s]+)',
            markup, re.I | re.S,
        )
        if sub_m:
            raw = (sub_m.group(1) or sub_m.group(2) or sub_m.group(3) or "").strip()
            result["subscribers"] = raw.replace(" ", "")

        # Посты: ищем блоки с датой и текстом
        # VK встраивает данные в JSON внутри скрипта
        json_m = re.search(r'"wall"\s*:\s*(\{.*?"items"\s*:\s*\[.*?\]\s*\})', markup, re.S)
        posts_raw = []
        if json_m:
            try:
                wall = json.loads(json_m.group(1))
                for p in wall.get("items", [])[:30]:
                    text = p.get("text", "")
                    date_ts = p.get("date", 0)
                    post_id = p.get("id", "")
                    if date_ts:
                        dt = time.gmtime(int(date_ts))
                        lastmod = f"{dt.tm_year}-{dt.tm_mon:02d}-{dt.tm_mday:02d}"
                    else:
                        lastmod = ""
                    post_url = f"https://vk.com/{slug}?w=wall-{abs(p.get('owner_id',0))}_{post_id}"
                    posts_raw.append({"title": text[:200], "url": post_url, "lastmod": lastmod})
            except Exception:
                pass

        # Фолбэк: HTML-парсинг блоков постов
        if not posts_raw:
            for block in re.finditer(
                r'class="[^"]*post__text[^"]*"[^>]*>(.*?)</div>',
                markup, re.I | re.S,
            ):
                text = clean_text(re.sub(r"<[^>]+>", " ", block.group(1)))
                if len(text) < 10:
                    continue
                # Ищем дату рядом (в ~500 символах до блока)
                start = max(0, block.start() - 500)
                ctx = markup[start:block.start()]
                date_vis = extract_visible_date(ctx)
                posts_raw.append({"title": text[:200], "url": url, "lastmod": date_vis})

        # Фильтруем по месяцу
        for p in posts_raw:
            lm = p.get("lastmod", "")
            if mm:
                if lm:
                    parts = lm.split("-")
                    if len(parts) >= 2 and parts[1] == mm:
                        result["posts"].append(p)
                # Без даты — не включаем (в VK даты почти всегда есть)
            else:
                result["posts"].append(p)

        result["posts"] = result["posts"][:20]

    except Exception as exc:
        result["errors"].append(f"VK не собрался: {exc}")
    return result


# ─── Обновления в сервисе ─────────────────────────────────────────────────────

_UPDATE_URL_PAT = re.compile(
    r"obnovlen|reliz|release|changelog|update|versiy|what.?s.?new|"
    r"новост.*версии|новые.функц|что.нового",
    re.I | re.U,
)
_UPDATE_TITLE_PAT = re.compile(
    r"обновлени|релиз|release|changelog|новые\s+функц|что\s+нового|"
    r"версия\s+v|v\.\d|новые\s+возможн",
    re.I | re.U,
)
_VERSION_PAT = re.compile(r"v\.?\s*(\d+[\d.]+\d)", re.I)


def extract_update_content(markup, url=""):
    """
    Извлекает структурированный текст обновлений из HTML-страницы.
    Возвращает dict: {version, date, items: [str], raw: str}
    """
    # Дата публикации
    pub_date = extract_visible_date(markup)

    # Версия продукта
    version = ""
    vm = _VERSION_PAT.search(markup)
    if vm:
        version = "v." + vm.group(1)

    # Удаляем скрипты, стили, навигацию
    body = re.sub(r"<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)

    # Ищем основной контентный блок (article, main, .content, .post-body и т.п.)
    content_block = ""
    for pat in [
        r'<article[^>]*>(.*?)</article>',
        r'<main[^>]*>(.*?)</main>',
        r'<div[^>]*class="[^"]*(?:post|article|content|entry|text|body)[^"]*"[^>]*>(.*?)</div>',
    ]:
        m = re.search(pat, body, re.S | re.I)
        if m and len(m.group(1)) > 200:
            content_block = m.group(1)
            break
    if not content_block:
        content_block = body

    # Извлекаем пункты из нумерованных и маркированных списков
    items = []
    for li in re.findall(r"<li[^>]*>(.*?)</li>", content_block, re.S | re.I):
        text = clean_text(re.sub(r"<[^>]+>", " ", li))
        text = re.sub(r"\s{2,}", " ", text).strip()
        if 15 < len(text) < 600 and not re.search(r"^(главная|блог|статьи|контакт|о нас)", text, re.I):
            items.append(text)

    # Если списков нет — берём абзацы, которые похожи на описание фич
    if not items:
        for p in re.findall(r"<p[^>]*>(.*?)</p>", content_block, re.S | re.I):
            text = clean_text(re.sub(r"<[^>]+>", " ", p))
            text = re.sub(r"\s{2,}", " ", text).strip()
            if 30 < len(text) < 600:
                items.append(text)

    # Ограничиваем количество пунктов
    items = items[:20]

    # Заголовок страницы
    title_m = re.search(r"<h1[^>]*>(.*?)</h1>", content_block, re.S | re.I)
    title = clean_text(re.sub(r"<[^>]+>", " ", title_m.group(1))) if title_m else slug_title(url)

    return {
        "version": version,
        "date": pub_date,
        "title": title[:180],
        "url": url,
        "items": items,
    }


def extract_update_from_tg_post(text, url=""):
    """
    Извлекает описание обновления из текста Telegram-поста.
    Возвращает dict или None.
    """
    if not _UPDATE_TITLE_PAT.search(text):
        return None

    version = ""
    vm = _VERSION_PAT.search(text)
    if vm:
        version = "v." + vm.group(1)

    # Дата из текста поста
    date = _extract_event_date(text)

    # Разбиваем на пункты по переносам строк
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    # Убираем слишком короткие строки (эмодзи, заголовки)
    items = [l for l in lines if len(l) > 20]

    if not items:
        return None

    title = lines[0][:180] if lines else (version or "Обновление")

    return {
        "version": version,
        "date": date,
        "title": title,
        "url": url,
        "items": items[:15],
    }


def collect_updates(comp, sitemap_data, tg_posts, month=None):
    """
    Собирает обновления сервиса из:
    1. Sitemap — ищет URL с признаками страницы обновлений, заходит и читает
    2. Telegram-постов — ищет посты про обновления, разбирает их текст
    Возвращает список обновлений за нужный месяц.
    """
    mm = MONTH_NUM.get(month, "") if month else ""
    updates = []
    seen = set()

    def add_update(upd):
        key = upd.get("url") or upd.get("title", "")[:60]
        if key in seen:
            return
        seen.add(key)
        # Фильтр по месяцу
        dt = upd.get("date", "")
        if mm and dt:
            parts = dt.split("-")
            if len(parts) >= 2 and parts[1] != mm:
                return
        updates.append(upd)

    # ── 1. Sitemap: ищем URL про обновления ───────────────────────────────
    for url, lastmod in sitemap_data.items():
        if not _UPDATE_URL_PAT.search(url):
            continue
        # Проверяем месяц по lastmod или идём на страницу
        if mm and lastmod:
            parts = lastmod.split("-")
            if len(parts) >= 2 and parts[1] != mm:
                continue
        try:
            page = request_text(url, timeout=10)
            # Проверяем заголовок страницы
            title_m = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S | re.I)
            page_title = clean_text(re.sub(r"<[^>]+>", " ", title_m.group(1))) if title_m else ""
            # Страница точно про обновления?
            if not _UPDATE_TITLE_PAT.search(url + " " + page_title):
                continue
            upd = extract_update_content(page, url)
            if not upd.get("date") and lastmod:
                upd["date"] = lastmod[:10]
            if upd.get("items"):
                add_update(upd)
        except Exception:
            pass

    # ── 2. Telegram-посты про обновления ──────────────────────────────────
    for post in tg_posts:
        text = post.get("title", "")
        url  = post.get("url", "")
        upd  = extract_update_from_tg_post(text, url)
        if upd:
            if not upd.get("date"):
                upd["date"] = post.get("lastmod", "")
            add_update(upd)

    return updates[:5]  # максимум 5 обновлений за месяц


def collect_dzen(slug, month=None):
    """
    Собирает данные с Дзен-канала через официальный API.
    Возвращает {'subscribers', 'posts': [...], 'errors': [...]}.
    """
    result = {"subscribers": "", "posts": [], "errors": []}
    mm = MONTH_NUM.get(month, "") if month else ""
    try:
        api_url = f"https://dzen.ru/api/v3/launcher/export?channelName={slug}&type=rss"
        raw = request_text(api_url, timeout=15)
        if not raw:
            result["errors"].append("Dzen API вернул пустой ответ")
            return result

        items = []
        subscribers = ""

        # Попытка 1: JSON
        try:
            data = json.loads(raw)
            # Подписчики
            subs_val = (data.get("subscribers") or
                        (data.get("channel") or {}).get("subscribers") or
                        (data.get("channel") or {}).get("followersCount") or "")
            if subs_val:
                subscribers = str(subs_val)
            # Список статей
            for key in ("items", "publications", "articles", "entries", "posts"):
                if isinstance(data.get(key), list):
                    items = data[key]
                    break
            if not items and isinstance(data, list):
                items = data
        except (ValueError, TypeError):
            pass

        # Попытка 2: XML/RSS (Dzen может вернуть RSS-обёрнутый JSON или XML)
        if not items:
            # Подписчики из RSS-расширения
            sm = re.search(r"<(?:dz:)?followers[^>]*>(\d+)</", raw, re.I)
            if sm:
                subscribers = sm.group(1)
            for block in re.findall(r"<item>(.*?)</item>", raw, re.S | re.I):
                t_m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
                l_m = re.search(r"<link>(.*?)</link>", block, re.S)
                d_m = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
                v_m = re.search(r"<(?:dz:)?views[^>]*>(\d+)</", block, re.I)
                if t_m and l_m:
                    items.append({
                        "title":   clean_text(t_m.group(1)),
                        "link":    clean_text(l_m.group(1)),
                        "pubDate": d_m.group(1).strip() if d_m else "",
                        "views":   int(v_m.group(1)) if v_m else 0,
                    })

        # Фильтруем и формируем посты
        total_views = 0
        view_count = 0
        for fi in items:
            url   = fi.get("link") or fi.get("url") or ""
            title = (fi.get("title") or fi.get("name") or "")[:200]
            raw_date = (fi.get("pubDate") or fi.get("publishedDate") or
                        fi.get("isoDate") or fi.get("date") or "")
            lastmod = parse_rss_date(raw_date) if raw_date else ""
            views = 0
            try:
                views = int(fi.get("views") or fi.get("viewsCount") or 0)
            except (TypeError, ValueError):
                pass

            if mm and lastmod:
                parts = lastmod.split("-")
                if len(parts) < 2 or parts[1] != mm:
                    continue
            elif mm and not lastmod:
                continue  # без даты пропускаем

            if views:
                total_views += views
                view_count += 1
            result["posts"].append({
                "title": title,
                "url": url,
                "lastmod": lastmod,
                "views": views,
                "category": "Статья",
            })

        result["subscribers"] = subscribers
        # Средние просмотры — сохраняем в поле avg_views для отображения
        if view_count:
            result["avg_views"] = total_views // view_count
        result["posts"] = result["posts"][:30]

    except Exception as exc:
        result["errors"].append(f"Dzen не собрался: {exc}")
    return result


def collect_social_channels(comp, month=None):
    """
    Собирает данные по всем соцсетям конкурента.
    Возвращает список каналов:
    [{'platform': 'Telegram', 'handle': '@mdaudit', 'url': ...,
      'subscribers': '356', 'posts': [...], 'summary': '...'}]
    """
    socials = comp.get("socials", {})
    channels = []
    mm = MONTH_NUM.get(month, "") if month else ""
    month_label = MONTHS.get(month, month) if month else "месяц"

    # ── Telegram ──────────────────────────────────────────────────────────
    tg = socials.get("telegram", "")
    if tg:
        tg_data = collect_telegram(tg, month=month)
        posts = []
        for p in tg_data.get("posts", []):
            p["category"] = categorize_post(p.get("title", ""))
            posts.append(p)
        # события тоже показываем как посты с категорией «Мероприятие»
        for ev in tg_data.get("events", []):
            posts.append({
                "title": ev.get("title", ""),
                "url": ev.get("url", "") or ev.get("reg_link", ""),
                "lastmod": ev.get("lastmod", ""),
                "category": "Мероприятие",
                "reg_link": ev.get("reg_link", ""),
                "rec_link": ev.get("rec_link", ""),
            })
        cats = {}
        for p in posts:
            cats[p["category"]] = cats.get(p["category"], 0) + 1
        channels.append({
            "platform": "Telegram",
            "handle": f"@{tg}",
            "url": f"https://t.me/{tg}",
            "subscribers": tg_data.get("subscribers") or "—",
            "posts": posts[:20],
            "cats": cats,
            "summary": generate_social_summary("Telegram", tg, cats, len(posts), month_label),
            "errors": tg_data.get("errors", []),
        })

    # ── VK ────────────────────────────────────────────────────────────────
    vk = socials.get("vk", "")
    if vk:
        vk_data = collect_vk(vk, month=month)
        posts = []
        for p in vk_data.get("posts", []):
            p["category"] = categorize_post(p.get("title", ""))
            posts.append(p)
        cats = {}
        for p in posts:
            cats[p["category"]] = cats.get(p["category"], 0) + 1
        channels.append({
            "platform": "ВКонтакте",
            "handle": f"vk.com/{vk}",
            "url": f"https://vk.com/{vk}",
            "subscribers": vk_data.get("subscribers") or "—",
            "posts": posts[:20],
            "cats": cats,
            "summary": generate_social_summary("ВКонтакте", vk, cats, len(posts), month_label),
            "errors": vk_data.get("errors", []),
        })

    # ── YouTube ───────────────────────────────────────────────────────────
    yt = socials.get("youtube", "")
    if yt:
        try:
            # YouTube RSS не требует API-ключа если знаем channel_id
            # Пробуем получить channel_id со страницы
            yt_url = yt if yt.startswith("http") else f"https://www.youtube.com/@{yt}"
            yt_page = request_text(yt_url, timeout=10)
            cid_m = re.search(r'"channelId"\s*:\s*"([^"]+)"', yt_page)
            posts = []
            if cid_m:
                rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid_m.group(1)}"
                rss = request_text(rss_url, timeout=10)
                for entry in re.findall(r"<entry>(.*?)</entry>", rss, re.S):
                    t = re.search(r"<title>(.*?)</title>", entry, re.S)
                    l = re.search(r"<link[^>]+href=[\"']([^\"']+)[\"']", entry, re.S)
                    d = re.search(r"<published>(.*?)</published>", entry, re.S)
                    if not (t and l):
                        continue
                    lastmod = (d.group(1) or "")[:10] if d else ""
                    if mm and lastmod:
                        parts = lastmod.split("-")
                        if len(parts) >= 2 and parts[1] != mm:
                            continue
                    title = clean_text(re.sub(r"<[^>]+>", "", t.group(1)))
                    posts.append({"title": title, "url": clean_text(l.group(1)), "lastmod": lastmod, "category": "Видео"})
            sub_m = re.search(r'"subscriberCountText"\s*:\s*\{"simpleText"\s*:\s*"([^"]+)"', yt_page)
            subs = sub_m.group(1) if sub_m else "—"
            cats = {"Видео": len(posts)}
            channels.append({
                "platform": "YouTube",
                "handle": yt,
                "url": yt_url,
                "subscribers": subs,
                "posts": posts[:10],
                "cats": cats,
                "summary": generate_social_summary("YouTube", yt, cats, len(posts), month_label),
                "errors": [],
            })
        except Exception as exc:
            pass  # YouTube необязателен, молча пропускаем

    # ── Дзен ──────────────────────────────────────────────────────────────
    dz = socials.get("dzen", "")
    if dz:
        dz_data = collect_dzen(dz, month=month)
        posts = dz_data.get("posts", [])
        avg_views = dz_data.get("avg_views", 0)
        cats = {"Статья": len(posts)}
        subs_str = dz_data.get("subscribers") or "—"
        summary_extra = f", ср. {avg_views} просм." if avg_views else ""
        channels.append({
            "platform": "Дзен",
            "handle": dz,
            "url": f"https://dzen.ru/{dz}",
            "subscribers": subs_str,
            "posts": posts[:20],
            "cats": cats,
            "summary": generate_social_summary("Дзен", dz, cats, len(posts), month_label) + summary_extra,
            "errors": dz_data.get("errors", []),
        })

    # ── RuTube ────────────────────────────────────────────────────────────
    rt = socials.get("rutube", "")
    if rt:
        try:
            rt_url = rt if rt.startswith("http") else f"https://rutube.ru/channel/{rt}/"
            rt_page = request_text(rt_url, timeout=10)
            posts = []
            for m in re.finditer(
                r'"title"\s*:\s*"([^"]+)".*?"link"\s*:\s*"([^"]+)".*?"created_ts"\s*:\s*"([^"]+)"',
                rt_page, re.S,
            ):
                lastmod = m.group(3)[:10]
                if mm:
                    parts = lastmod.split("-")
                    if len(parts) >= 2 and parts[1] != mm:
                        continue
                posts.append({"title": m.group(1), "url": "https://rutube.ru" + m.group(2) if m.group(2).startswith("/") else m.group(2), "lastmod": lastmod, "category": "Видео"})
            cats = {"Видео": len(posts)}
            channels.append({
                "platform": "RuTube",
                "handle": rt,
                "url": rt_url,
                "subscribers": "—",
                "posts": posts[:10],
                "cats": cats,
                "summary": generate_social_summary("RuTube", rt, cats, len(posts), month_label),
                "errors": [],
            })
        except Exception:
            pass

    return channels


def _collect_one(comp, source, month=None):
    """Сетевой сбор по одному конкуренту (выполняется в отдельном потоке)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    comp = dict(comp)
    if source.get("site"):
        comp["site"] = source["site"]
    merged_comp = dict(comp)
    merged_comp["socials"] = {**comp.get("socials", {}), **source.get("socials", {})}

    # Запускаем три тяжёлые функции параллельно вместо последовательно
    with ThreadPoolExecutor(max_workers=3) as inner_pool:
        fut_site    = inner_pool.submit(collect_site,            merged_comp, month)
        fut_social  = inner_pool.submit(collect_social_channels, merged_comp, month)
        fut_web     = inner_pool.submit(collect_web_mentions,    merged_comp, month)
        site_data       = fut_site.result()
        social_channels = fut_social.result()
        web_mentions    = fut_web.result()

    # parse_sitemap кэшируется — второй вызов мгновенный (уже выполнен в collect_site)
    tg_posts = []
    for ch in social_channels:
        if ch.get("platform") == "Telegram":
            tg_posts = ch.get("posts", [])
            break
    sitemap_data = parse_sitemap(merged_comp["site"])
    updates = collect_updates(merged_comp, sitemap_data, tg_posts, month=month)

    return merged_comp, site_data, social_channels, web_mentions, updates


def merge_real_data(month, import_text, sources, manual_metrics=None, auth=None, on_progress=None):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    auth_text, auth_errors = collect_authorized_text(auth)
    parsed = parse_import_text((import_text or "") + "\n" + auth_text)
    manual = parse_manual_metrics(manual_metrics)

    # параллельно тянем сеть по всем конкурентам — это в разы быстрее, чем по очереди
    n_total = len(COMPETITORS)
    results_map = {}
    with ThreadPoolExecutor(max_workers=n_total) as pool:
        futures = {
            pool.submit(_collect_one, comp, sources.get(comp["key"], {}), month=month): comp
            for comp in COMPETITORS
        }
        done = 0
        for fut in as_completed(futures):
            comp = futures[fut]
            try:
                results_map[comp["key"]] = fut.result()
            except Exception as exc:
                results_map[comp["key"]] = (comp, {}, [], [], [])
                log_error(f"[collect_one] {comp['name']}", exc)
            done += 1
            if on_progress:
                try:
                    on_progress(f"Собираю данные... {done}/{n_total} конкурентов")
                except Exception:
                    pass
    results = [results_map[comp["key"]] for comp in COMPETITORS]

    items = []
    for comp, site_data, social_channels, web_mentions, updates in results:
        key = comp["key"]
        data = CompetitorData(key, comp["name"], comp["domain"], comp["site"])
        data.wordstat.update(parsed[key]["wordstat"])
        data.spywords.update(parsed[key]["spywords"])
        data.spywords.update(manual[key]["spywords"])
        # Ручные метрики соцсетей (если вставлены вручную)
        manual_social = parsed[key]["social"]
        manual_social.update(manual[key]["social"])

        data.site_summary = site_data["summary"]
        data.content.extend(site_data["content"])
        # Сторонние публикации: интернет-поиск + ссылки с сайта
        data.external.extend(web_mentions)
        data.external.extend(site_data.get("external", []))
        seen_ext = set()
        dedup_ext = []
        for ex in data.external:
            u = ex.get("url", "")
            if u not in seen_ext:
                seen_ext.add(u)
                dedup_ext.append(ex)
        data.external = dedup_ext[:50]
        data.images.extend(site_data["images"])
        data.emails.extend(site_data.get("emails", []))
        data.phones.extend(site_data.get("phones", []))
        data.errors.extend(site_data["errors"])

        # Соцсети: структурированные каналы
        data.social_channels = social_channels  # список каналов с постами
        # Краткий список для совместимости
        for ch in social_channels:
            subs = ch.get("subscribers", "")
            if subs and subs != "—":
                data.social.append(f"{ch['platform']} {ch['handle']}: {subs} подписчиков")
            # События из Telegram (с дедупликацией по URL и заголовку)
            for p in ch.get("posts", []):
                if p.get("category") == "Мероприятие":
                    _key = (p.get("url") or p.get("title", "")[:60]).lower().strip()
                    if not any(
                        (ev.get("url") or ev.get("title", "")[:60]).lower().strip() == _key
                        for ev in data.events
                    ):
                        data.events.append(p)
        # Ручные подписчики (из import text)
        if manual_social.get("telegram"):
            data.social.append(f"Telegram: {manual_social['telegram']} подписчиков (вручную)")
        if manual_social.get("vk"):
            data.social.append(f"VK: {manual_social['vk']} подписчиков (вручную)")

        # Обновления сервиса
        data.updates = updates

        data.errors.extend(auth_errors)
        items.append(data)
    return items


def asdict_data(items):
    out = []
    for item in items:
        out.append({
            "key": item.key,
            "name": item.name,
            "domain": item.domain,
            "site": item.site,
            "wordstat": item.wordstat,
            "spywords": item.spywords,
            "site_summary": item.site_summary,
            "content": item.content,
            "external": item.external,
            "social": item.social,
            "media": item.media,
            "emails": item.emails,
            "phones": item.phones,
            "events": item.events,
            "updates": item.updates,
            "jobs": item.jobs,
            "images": item.images,
            "errors": item.errors,
        })
    return out


def fmt(value):
    if value is None or value == "":
        return "-"
    try:
        return f"{int(value):,}".replace(",", " ")
    except Exception:
        return str(value)


def e(value):
    return html.escape(str(value or ""))


_RU_MONTH_NAMES = ["", "янв", "фев", "мар", "апр", "май", "июн",
                   "июл", "авг", "сен", "окт", "ноя", "дек"]


def format_date(lastmod):
    """'2026-06-15' → '15 июн 2026'. Пустая строка → ''."""
    if not lastmod:
        return ""
    parts = lastmod.split("-")
    if len(parts) >= 3:
        try:
            y, m, d = parts[0], int(parts[1]), int(parts[2].split("T")[0])
            mon = _RU_MONTH_NAMES[m] if 1 <= m <= 12 else parts[1]
            return f"{d} {mon} {y}"
        except Exception:
            pass
    if len(parts) == 2:
        try:
            y, m = parts[0], int(parts[1])
            mon = _RU_MONTH_NAMES[m] if 1 <= m <= 12 else parts[1]
            return f"{mon} {y}"
        except Exception:
            pass
    return lastmod


_PAGE_TYPES = [
    (r"blog|article|post|стат|публик",           "Статья",       "#2196a8"),
    (r"news|новост|пресс|press",                  "Новость",      "#5c6bc0"),
    (r"case|кейс|keysy|success",                  "Кейс",         "#43a047"),
    (r"event|вебинар|webinar|конфер|мероприят",   "Мероприятие",  "#fb8c00"),
    (r"update|release|changelog|обновлени",        "Обновление",   "#8e24aa"),
    (r"vacancy|вакансия|career|job|jobs",          "Вакансия",     "#e53935"),
    (r"tpost",                                     "Статья",       "#2196a8"),
    (r"partner|интеграц|integration",              "Партнёрство",  "#00897b"),
]


def classify_page(url):
    """Возвращает (label, color) по URL."""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    full = (parsed.netloc + parsed.path).lower()
    # Dzen articles (dzen.ru/a/...) — всегда Статья
    if "dzen.ru/a/" in full:
        return "Статья", "#2196a8"
    for pattern, label, color in _PAGE_TYPES:
        if re.search(pattern, path, re.I):
            return label, color
    return "Страница", "#90a4ae"


def content_item_html(x):
    """Форматирует запись контента с сайта: категория + заголовок + дата."""
    url = x.get("url", "")
    title = x.get("title", "") or slug_title(url) or url
    date_str = format_date(x.get("lastmod", ""))
    label, color = classify_page(url)
    badge = f'<span style="font-size:10px;padding:1px 6px;border-radius:3px;background:{color};color:#fff;margin-right:5px">{e(label)}</span>'
    date_tag = f' <span style="font-size:11px;color:#888;margin-left:6px">{e(date_str)}</span>' if date_str else ""
    return f'<li>{badge}<a href="{e(url)}" target="_blank">{e(title)}</a>{date_tag}</li>'


def clean_snippet(raw: str) -> str:
    """Убирает HTML-теги и экранированные символы из сниппета."""
    import html as _html
    text = _html.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)          # strip HTML tags
    text = re.sub(r"&[a-z]+;", " ", text)          # strip remaining entities
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def is_relevant_mention(item: dict, comp_name: str) -> bool:
    """Проверяет что упоминание реально про конкурента."""
    name_lower = comp_name.lower()
    # Берём ключевые слова из имени (убираем короткие)
    name_words = [w for w in re.split(r"\s+|-", name_lower) if len(w) >= 3]
    title_lower   = (item.get("title", "") or "").lower()
    snippet_lower = (item.get("snippet", "") or "").lower()
    domain_lower  = (item.get("url", "") or "").lower()
    combined = title_lower + " " + snippet_lower + " " + domain_lower
    return any(w in combined for w in name_words)


def mention_item_html(x):
    """Форматирует внешнее упоминание: категория + источник + заголовок + дата + сниппет."""
    url = x.get("url", "")
    title = x.get("title", "") or slug_title(url) or url
    date_str = format_date(x.get("lastmod", ""))
    snippet = clean_snippet(x.get("snippet", ""))
    label, color = classify_mention(url, snippet)
    dom = reg_domain(url) or url
    badge = f'<span style="font-size:10px;padding:1px 6px;border-radius:3px;background:{color};color:#fff;margin-right:5px">{e(label)}</span>'
    source_tag = f'<span style="font-size:10px;color:#666;margin-right:4px">[{e(dom)}]</span>'
    date_tag = f' <span style="font-size:11px;color:#888;margin-left:6px">{e(date_str)}</span>' if date_str else ""
    snip_tag = f'<div style="font-size:11px;color:#555;margin:2px 0 0 0;padding-left:8px">{e(snippet[:200])}</div>' if snippet else ""
    return f'<li>{badge}{source_tag}<a href="{e(url)}" target="_blank">{e(title)}</a>{date_tag}{snip_tag}</li>'


def _is_uuid_url(url: str) -> bool:
    """True если URL содержит UUID-слаг (Kontur.Stream и подобные)."""
    path = urllib.parse.urlparse(url or "").path
    return bool(re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}', path, re.I))


def clean_event_title(ev: dict):
    """Извлекает чистый заголовок и роль из сырого TG-текста мероприятия.
    Возвращает (title_str, role_str).
    """
    raw = (ev.get("title", "") or "").strip()
    url = ev.get("url", "") or ev.get("reg_link", "") or ev.get("rec_link", "")

    # Убираем эмодзи-восклицательные знаки из начала (❗️❗️❗️ВАЖНО:...)
    _clean_raw = re.sub(r'^[❗️⚠️🔴🟡⭐]+\s*', '', raw).strip()

    # Извлекаем роль: "роль: Спикер" / "роль: Участник"
    role = ""
    m_role = re.search(r"роль[:\s]+([А-Яа-яЁёa-zA-Z][^\s.,;/]{1,30})", raw, re.I)
    if m_role:
        role = m_role.group(1).strip()

    # Шаблонный TG-пост: "Вебинар, роль: X. [основной текст]"
    if re.match(r"[Вв]ебинар[,\s]", raw):
        # Убираем преамбулу "Вебинар, роль: Участник."
        after_prefix = re.sub(r"^[Вв]ебинар,?\s*(?:роль[^.]+\.?\s*)?", "", raw).strip()
        # Убираем "На этой неделе мы:" и всё после (это недельный дайджест, не тема вебинара)
        after_prefix = re.split(r"[Нн]а этой неделе|[Нн]а прошлой неделе", after_prefix)[0].strip()
        # Убираем emoji-артефакты
        after_prefix = re.sub(r"[✅✔️🔥⚡📌➡️❗️⚠️]\s*", "", after_prefix).strip().rstrip(".,;:")
        after_prefix = re.sub(r"\s{2,}", " ", after_prefix).strip()

        if after_prefix and len(after_prefix) > 5:
            return after_prefix[:180], role

        # Если текст после префикса пустой — пробуем URL (только не UUID)
        if url and not _is_uuid_url(url):
            slug = slug_title(url)
            if slug and len(slug) > 5:
                return slug[:120], role

        return "Вебинар", role

    # Обычный заголовок: убираем шаблонные хвосты и emoji
    clean = re.sub(r'^[❗️⚠️🔴⭐]+\s*(?:[Вв][Аа][Жж][Нн][Оо][!:.]?\s*)?', '', raw).strip()
    clean = re.split(r"[Нн]а этой неделе|[Нн]а прошлой неделе", clean)[0]
    clean = re.sub(r"[✅✔️🔥⚡📌➡️❗️]\s*", "", clean).strip().rstrip(".,;:")
    clean = re.sub(r"\s{2,}", " ", clean).strip()
    return (clean[:180] or raw[:180]), role


def event_item_html(ev):
    """Форматирует мероприятие как строку нумерованного списка: дата — тема [ссылка]."""
    date_str  = format_date(ev.get("lastmod", ""))
    reg_link  = ev.get("reg_link", "")
    rec_link  = ev.get("rec_link", "")
    url       = ev.get("url", "")
    main_link = reg_link or rec_link or url

    clean_title, role = clean_event_title(ev)
    if not clean_title:
        clean_title = "Мероприятие"

    # Дата
    date_part = (
        f'<span style="color:#e65100;font-size:11px;font-weight:600;margin-right:6px">'
        f'{e(date_str)}</span>— '
        if date_str else ""
    )
    # Роль-бейдж
    role_badge = (
        f'<span style="font-size:10px;color:#fff;background:#7b1fa2;'
        f'padding:1px 6px;border-radius:3px;margin-right:6px;white-space:nowrap">'
        f'{e(role)}</span>'
        if role else ""
    )
    # Заголовок
    title_html = (
        f'<a href="{e(main_link)}" target="_blank" '
        f'style="color:#1a1a1a;font-weight:500;font-size:13px">'
        f'{e(clean_title)}</a>'
        if main_link else
        f'<span style="font-size:13px">{e(clean_title)}</span>'
    )
    # Доп. ссылки
    links = ""
    if reg_link and reg_link == main_link:
        links = (
            f' <a href="{e(reg_link)}" target="_blank" '
            f'style="font-size:11px;color:#1565c0;white-space:nowrap">[Регистрация]</a>'
        )
    if rec_link and rec_link != main_link:
        links += (
            f' <a href="{e(rec_link)}" target="_blank" '
            f'style="font-size:11px;color:#1565c0;white-space:nowrap">[Запись]</a>'
        )

    return (
        f'<li style="padding:7px 0;border-bottom:1px solid #f5f5f5;font-size:13px;line-height:1.5">'
        f'{date_part}{role_badge}{title_html}{links}'
        f'</li>'
    )


def _clean_update_item(text: str) -> str:
    """Убирает артефакты '| N. text |' из пунктов обновлений."""
    text = text.strip()
    # Убираем "|  1. text  |" → "text"
    text = re.sub(r"^\|\s*\d+\.\s*", "", text)
    text = re.sub(r"\s*\|$", "", text)
    # Убираем чисто разделительные строки типа "| --- |"
    if re.match(r"^[\|\-\s]+$", text):
        return ""
    return text.strip()


def update_block_html(updates):
    """Рендерит блок обновлений: версия + дата + список фич."""
    if not updates:
        return ""
    html_out = ""
    for upd in updates:
        version  = upd.get("version", "")
        date_str = format_date(upd.get("date", ""))
        title    = upd.get("title", "")
        url      = upd.get("url", "")
        raw_items = upd.get("items", [])

        # Очищаем артефакты и дедуплицируем
        items = []
        seen_items = set()
        for it in raw_items:
            cleaned = _clean_update_item(it)
            if cleaned and cleaned.lower() not in seen_items:
                seen_items.add(cleaned.lower())
                items.append(cleaned)

        # Заголовок обновления
        ver_badge = (
            f'<span style="display:inline-block;background:#ede7f6;color:#6a1b9a;'
            f'font-size:11px;font-weight:700;padding:2px 8px;border-radius:4px;'
            f'margin-right:6px">{e(version)}</span>'
            if version else ""
        )
        date_badge = (
            f'<span style="color:#999;font-size:11px">{e(date_str)}</span>'
            if date_str else ""
        )
        # Если нет title — берём первый пункт как краткое описание
        if not title and items:
            title = items[0][:80]
        title_text = e(title) if (title and title not in version) else ""
        src_link = (
            f'<a href="{e(url)}" target="_blank" style="font-size:11px;color:#1565c0;margin-left:8px">↗ источник</a>'
            if url else ""
        )

        header_inner = " ".join(x for x in [ver_badge, date_badge] if x)
        if title_text:
            header_inner += f'<span style="font-size:13px;font-weight:600;color:#1a1a1a;display:block;margin-top:3px">{title_text}</span>'
        header_inner += src_link

        if items:
            items_html = "".join(
                f'<li style="padding:3px 0;border-bottom:1px solid #f0f0f0;font-size:13px;color:#333">{e(it)}</li>'
                for it in items
            )
            body = f'<ul style="margin:8px 0 0;padding-left:18px;list-style:disc">{items_html}</ul>'
        else:
            body = ""

        html_out += (
            f'<div style="border:1px solid #ede7f6;border-radius:8px;padding:10px 14px;margin-bottom:10px;background:#faf8ff">'
            f'<div>{header_inner}</div>{body}</div>'
        )
    return html_out


def bar_chart(title, pairs, color="#4f77ff", unit=""):
    """Простой горизонтальный bar-chart на чистом SVG, без зависимостей."""
    pairs = [(label, val) for label, val in pairs if val]
    if not pairs:
        return ""
    maxv = max(v for _, v in pairs) or 1
    row_h, label_w, chart_w = 30, 150, 540
    bar_max = chart_w - label_w - 60
    height = len(pairs) * row_h + 16
    rows = []
    y = 8
    for label, v in pairs:
        w = int(bar_max * v / maxv) if maxv else 0
        rows.append(f'<text x="0" y="{y+15}" font-size="12" fill="#333">{e(str(label)[:22])}</text>')
        rows.append(f'<rect x="{label_w}" y="{y+3}" width="{max(w,2)}" height="18" rx="3" fill="{color}"/>')
        rows.append(f'<text x="{label_w+max(w,2)+6}" y="{y+16}" font-size="11" fill="#555">{fmt(v)}{e(unit)}</text>')
        y += row_h
    return (f'<div class="chart"><b>{e(title)}</b>'
            f'<svg viewBox="0 0 {chart_w} {height}" width="100%" preserveAspectRatio="xMinYMin meet">'
            f'{"".join(rows)}</svg></div>')


MONTH_NUM = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}

def filter_by_month(items_list, month):
    """Страховочная фильтрация после collect_site. Строгая: без подтверждённой даты — не включаем."""
    mm = MONTH_NUM.get(month, "")
    if not mm:
        return items_list
    result = []
    for item in items_list:
        url = item.get("url", "")
        lastmod = item.get("lastmod", "")
        if lastmod:
            parts = lastmod.split("-")
            if len(parts) >= 2 and parts[1] == mm:
                result.append(item)
            # иначе дата есть, но другой месяц — не включаем
        elif _url_month(url, mm):
            result.append(item)
        # Нет ни lastmod, ни даты в URL — не включаем
    return result


def render_analytics_html(items):
    content_pairs = [(i.name, len(i.content)) for i in items]
    external_pairs = [(i.name, len(i.external)) for i in items]
    social_pairs = [(i.name, len(list(dict.fromkeys(i.social)))) for i in items]
    wordstat_pairs = sorted(
        [(k, v) for i in items for k, v in i.wordstat.items()],
        key=lambda x: x[1], reverse=True,
    )[:12]
    visibility_pairs = sorted(
        [(i.name, (i.spywords.get("yandex_top50") or 0) + (i.spywords.get("google_top50") or 0)) for i in items],
        key=lambda x: x[1], reverse=True,
    )
    charts = [
        bar_chart("Контент на сайте (статей найдено)", content_pairs, "#4f77ff", " шт"),
        bar_chart("Сторонние публикации (упоминаний найдено)", external_pairs, "#8d4bd6", " шт"),
        bar_chart("Соцсети и каналы (площадок найдено)", social_pairs, "#7a5300", " шт"),
        bar_chart("Бренд-трафик (Вордстат, показы/мес)", wordstat_pairs, "#e51b1b"),
        bar_chart("Видимость TOP-50 (Яндекс+Google)", visibility_pairs, "#4285f4"),
    ]
    charts = [c for c in charts if c]
    if not charts:
        return ""
    return (
        '<div class="analytics-section" id="analytics">'
        '<p class="analytics-title">Аналитика</p>'
        f'<div class="charts">{"".join(charts)}</div>'
        '</div>'
    )


def render_report_html(month, items):
    # Фильтруем контент по месяцу анализа
    for item in items:
        item.content = filter_by_month(item.content, month)
        item.external = filter_by_month(item.external, month)
    month_label = MONTHS.get(month, month)
    date = time.strftime("%d.%m.%Y, %H:%M")

    # Цвета заголовков конкурентов
    COMP_COLORS = [
        "#1a1a2e", "#1b2838", "#0d2137", "#1c2340", "#162032",
    ]

    toc_items = "".join(
        f'<a href="#{e(item.key)}" style="display:block;color:#4f77ff;text-decoration:none;'
        f'font-size:13px;padding:4px 0;border-bottom:1px solid #f0f0f0;line-height:1.4">'
        f'{e(item.name)}</a>'
        for item in items
    )

    CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#1a1a1a;background:#f8f9fb;margin:0;font-size:14px;line-height:1.6}
.page{max-width:1200px;margin:0 auto;padding:32px 36px}
/* Шапка */
.report-header{background:#fff;border:1px solid #e2e5ee;border-radius:10px;padding:20px 24px 16px;margin-bottom:24px;display:flex;align-items:flex-start;justify-content:space-between}
.report-title{font-size:22px;font-weight:600;color:#1a1a1a;margin:0 0 4px}
.report-meta{font-size:12px;color:#888}
.toc{background:#fff;border:1px solid #e2e5ee;border-radius:10px;padding:16px 20px;width:220px;flex-shrink:0}
.toc-label{font-size:11px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:.5px;margin:0 0 8px}
/* Карточка конкурента */
.comp-card{background:#fff;border:1px solid #e2e5ee;border-radius:12px;overflow:hidden;margin-bottom:28px}
.comp-header{padding:16px 22px;display:flex;align-items:center;gap:14px}
.comp-avatar{width:36px;height:36px;border-radius:8px;background:rgba(255,255,255,.15);display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;color:#fff;flex-shrink:0}
.comp-name{font-size:18px;font-weight:600;color:#fff;margin:0}
.comp-domain{font-size:11px;color:rgba(255,255,255,.55);margin:0}
.comp-stats-bar{display:flex;gap:0;border-bottom:1px solid #e8eaf2}
.comp-stat{flex:1;padding:10px 14px;text-align:center;border-right:1px solid #e8eaf2}
.comp-stat:last-child{border-right:none}
.comp-stat-num{font-size:20px;font-weight:600;color:#1a1a1a;line-height:1.1}
.comp-stat-label{font-size:10px;color:#999;margin-top:1px}
.comp-stat-num.blue{color:#1565c0}
.comp-stat-num.green{color:#2e7d32}
.comp-stat-num.purple{color:#6a1b9a}
.comp-stat-num.orange{color:#e65100}
/* Секции */
.comp-body{padding:0 22px 18px}
.section{margin-top:18px}
.section-title{font-size:11px;font-weight:600;color:#4f77ff;text-transform:uppercase;letter-spacing:.6px;border-left:3px solid #4f77ff;padding-left:8px;margin:0 0 10px;line-height:1.3}
/* Таблица показателей */
table{border-collapse:collapse;width:100%;font-size:12px;margin:0 0 4px}
th{background:#f5f6fa;color:#888;font-weight:500;font-size:11px;text-align:center;padding:7px 8px;border:1px solid #e8eaf2}
td{text-align:center;padding:7px 8px;border:1px solid #e8eaf2;color:#1a1a1a}
td.left{text-align:left}
.ya{color:#e30000;font-weight:600}
.gl{color:#1a73e8;font-weight:600}
/* Wordstat строки */
.ws-row{display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid #f5f5f5;font-size:13px}
.ws-row:last-child{border-bottom:none}
.ws-query{flex:1;color:#333}
.ws-val{font-weight:600;color:#1a1a1a;font-size:13px}
/* Список материалов */
.items-list{list-style:none;padding:0;margin:0}
.items-list li{padding:6px 0;border-bottom:1px solid #f5f5f5;display:flex;align-items:flex-start;gap:7px;font-size:13px}
.items-list li:last-child{border-bottom:none}
.item-date{color:#aaa;font-size:11px;min-width:40px;flex-shrink:0;padding-top:2px}
.item-badge{font-size:10px;padding:2px 7px;border-radius:3px;color:#fff;flex-shrink:0;white-space:nowrap;margin-top:1px}
.item-text{flex:1;line-height:1.4}
.item-text a{color:#246bff;text-decoration:none}
.item-text a:hover{text-decoration:underline}
.item-snippet{font-size:11px;color:#666;margin-top:2px;line-height:1.4}
.empty-note{font-size:12px;color:#bbb;padding:6px 0;font-style:italic}
/* Соцсети */
.social-card{border:1px solid #e8eaf2;border-radius:8px;overflow:hidden;margin-bottom:10px}
.social-header{padding:9px 14px;display:flex;align-items:center;gap:10px}
.platform-badge{font-size:11px;font-weight:600;color:#fff;padding:3px 9px;border-radius:4px}
.social-handle{font-size:12px;color:#555;text-decoration:none}
.social-meta{font-size:11px;color:#aaa;margin-left:auto}
.social-body{padding:8px 14px 10px}
.social-cats{font-size:11px;color:#777;margin-bottom:7px}
.social-posts{list-style:none;padding:0;margin:0 0 8px}
.social-posts li{padding:4px 0;border-bottom:1px solid #f7f7f7;display:flex;align-items:flex-start;gap:6px;font-size:12px}
.social-posts li:last-child{border-bottom:none}
.social-summary{font-size:12px;color:#444;font-style:italic;border-top:1px solid #f0f0f0;padding-top:7px;margin-top:4px}
/* Мероприятия */
.events-list{list-style:none;padding:0;margin:0}
.events-list li{padding:7px 0;border-bottom:1px solid #f5f5f5;font-size:13px}
.events-list li:last-child{border-bottom:none}
/* Обновления */
.update-item{padding:7px 0;border-bottom:1px solid #f5f5f5}
.update-item:last-child{border-bottom:none}
.update-header{font-size:13px;font-weight:600;color:#1a1a1a;margin-bottom:3px}
.update-list{margin:4px 0 0 14px;padding:0;font-size:12px;color:#333;line-height:1.7}
/* Ошибки */
.errors-block{background:#fff8f8;border:1px solid #fdd;border-radius:6px;padding:8px 12px;margin-top:10px;font-size:11px;color:#c00}
/* Аналитика */
.analytics-section{background:#fff;border:1px solid #e2e5ee;border-radius:12px;padding:20px 24px;margin-bottom:28px}
.analytics-title{font-size:15px;font-weight:600;color:#1a1a1a;margin:0 0 16px}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.chart{border:1px solid #e8eaf2;border-radius:8px;padding:14px 16px;background:#fcfcff}
.chart b{display:block;margin-bottom:8px;font-size:12px;color:#555;font-weight:500}
/* Итоговая секция */
.summary-section{background:#fff;border:1px solid #e2e5ee;border-radius:12px;padding:20px 24px;margin-bottom:28px}
.summary-title{font-size:17px;font-weight:600;color:#1a1a1a;margin:0 0 16px;border-bottom:2px solid #f0f1f7;padding-bottom:12px}
/* Executive Summary */
.exec-summary{background:#fff;border:1px solid #c7d7f5;border-radius:12px;padding:20px 24px;margin-bottom:24px;border-left:4px solid #4f77ff}
.exec-title{font-size:16px;font-weight:700;color:#1a1a1a;margin:0 0 14px;letter-spacing:.2px}
.exec-row{display:flex;align-items:flex-start;gap:10px;padding:6px 0;border-bottom:1px solid #f0f3fb;font-size:13px;line-height:1.5;color:#333}
.exec-row:last-child{border-bottom:none}
.exec-icon{font-size:16px;flex-shrink:0;min-width:22px;line-height:1.4}
@media print{
  .page{padding:0}
  @page{margin:12mm 16mm}
  /* Каждый конкурент — с новой страницы */
  .comp-card+.comp-card{page-break-before:always}
  /* Executive summary и аналитика — не разрывать */
  .exec-summary,.analytics-section,.summary-section{break-inside:avoid}
  /* Секции карточки — стараемся не разрывать */
  .section{break-inside:avoid}
  /* Графики в 2 колонки */
  .charts{grid-template-columns:1fr 1fr}
  /* Убираем фоновые цвета компонентов при печати */
  .comp-header{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  /* Ссылки — чёрные в печати */
  a{color:#000!important;text-decoration:none!important}
  /* Убираем тень */
  .comp-card,.exec-summary,.analytics-section,.summary-section{box-shadow:none!important}
  /* TOC скрываем при печати */
  .toc{display:none}
  .report-header{flex-direction:column}
}
@media(max-width:760px){.charts{grid-template-columns:1fr}.report-header{flex-direction:column}.toc{width:auto}}
"""

    body = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>{e(month_label)} — мониторинг конкурентов</title>
<style>{CSS}</style></head><body>
<div class="page">
<div class="report-header">
  <div>
    <p class="report-title">{e(month_label)} — мониторинг активности конкурентов</p>
    <p class="report-meta">Сформирован: {e(date)}</p>
  </div>
  <div class="toc">
    <p class="toc-label">Содержание</p>
    <a href="#exec-summary" style="display:block;color:#4f77ff;text-decoration:none;font-size:13px;padding:4px 0;border-bottom:1px solid #f0f0f0;font-weight:600">Ключевые события</a>
    <a href="#analytics" style="display:block;color:#4f77ff;text-decoration:none;font-size:13px;padding:4px 0;border-bottom:1px solid #f0f0f0">Аналитика</a>
    {toc_items}
    <a href="#summary" style="display:block;color:#4f77ff;text-decoration:none;font-size:13px;padding:4px 0">Сводная таблица</a>
  </div>
</div>
"""
    body += render_executive_summary_html(items, month_label)
    body += render_analytics_html(items)
    body += render_summary_html(items)

    for idx, item in enumerate(items):
        hdr_color = COMP_COLORS[idx % len(COMP_COLORS)]
        initials = "".join(w[0].upper() for w in item.name.split()[:2])

        y50 = item.spywords.get("yandex_top50")
        y10 = item.spywords.get("yandex_top10")
        g50 = item.spywords.get("google_top50")
        g10 = item.spywords.get("google_top10")
        sy  = item.spywords.get("search_traffic_yandex")
        sg  = item.spywords.get("search_traffic_google")
        uy  = item.spywords.get("unique_urls_yandex")
        ug  = item.spywords.get("unique_urls_google")

        # Счётчики активности (предварительные; ряд будет уточнён после фильтрации)
        n_content  = len(item.content)
        n_external = len(item.external)
        n_events   = len(item.events)
        n_updates  = len(item.updates)
        n_channels = len(item.social_channels)

        # Фильтр упоминаний — нужен до stat bar
        relevant_external = [x for x in item.external if is_relevant_mention(x, item.name)]
        n_external = len(relevant_external)

        # Считаем активные каналы и суммарные посты (до рендера соцсетей)
        active_channels = [ch for ch in item.social_channels if len(ch.get("posts", [])) > 0]
        total_social_posts = sum(len(ch.get("posts", [])) for ch in item.social_channels)

        stat_color = lambda n, c: f'class="comp-stat-num {c}"' if n else 'class="comp-stat-num"'

        # ── Показатели сайта (только если есть данные SpyWords) ──────
        has_spywords = any(v is not None for v in [y50, y10, g50, g10, sy, sg, uy, ug])
        if has_spywords:
            spywords_html = f"""<div class="section">
      <p class="section-title">Показатели сайта</p>
      <table>
        <thead><tr><th style="text-align:left">Поисковая система</th><th>TOP-50</th><th>TOP-10</th><th>Трафик</th><th>Уник. URL</th></tr></thead>
        <tbody>
          <tr><td class="left"><span class="ya">Яндекс</span></td><td>{fmt(y50)}</td><td>{fmt(y10)}</td><td>{fmt(sy)}</td><td>{fmt(uy)}</td></tr>
          <tr><td class="left"><span class="gl">Google</span></td><td>{fmt(g50)}</td><td>{fmt(g10)}</td><td>{fmt(sg)}</td><td>{fmt(ug)}</td></tr>
        </tbody>
      </table>
    </div>"""
        else:
            spywords_html = ""  # скрываем пустую таблицу

        # ── Вордстат (показываем только если есть данные) ────────────
        if item.wordstat:
            ws_html = (
                '<div class="section"><p class="section-title">Вордстат — брендовые запросы</p>'
                '<div class="items-list">'
                + "".join(
                    f'<div class="ws-row"><span class="ws-query">«{e(k)}»</span>'
                    f'<span class="ws-val">{fmt(v)} показов/мес</span></div>'
                    for k, v in item.wordstat.items()
                )
                + "</div></div>"
            )
        else:
            ws_html = ""  # скрываем пустой вордстат

        # ── Контент на сайте (блог отдельно от страниц) ──────────────
        _EDITORIAL_LABELS = {"Статья", "Кейс", "Новость", "Мероприятие", "Обновление", "Партнёрство"}
        editorial = [x for x in item.content if classify_page(x.get("url",""))[0] in _EDITORIAL_LABELS]
        pages_only = [x for x in item.content if classify_page(x.get("url",""))[0] not in _EDITORIAL_LABELS]

        if editorial or pages_only:
            parts = []
            if editorial:
                parts.append(
                    f'<p style="font-size:12px;color:#555;margin:0 0 8px">'
                    f'Публикации за {e(month_label.lower())}: <b>{len(editorial)}</b></p>'
                    + '<ul class="items-list">'
                    + "".join(content_item_html(x) for x in editorial[:8])
                    + "</ul>"
                )
            if pages_only:
                extra_count = len(pages_only)
                pages_id = f"pages_{e(item.key)}"
                pages_li = "".join(content_item_html(x) for x in pages_only)
                parts.append(
                    f'<p style="font-size:12px;color:#aaa;margin:6px 0 0">'
                    f'<a href="javascript:void(0)" onclick="'
                    f'var el=document.getElementById(\'{pages_id}\');'
                    f'var btn=document.getElementById(\'{pages_id}_btn\');'
                    f'if(el.style.display===\'none\'){{el.style.display=\'block\';btn.textContent=\'▲ скрыть\';}}'
                    f'else{{el.style.display=\'none\';btn.textContent=\'▼ показать\';}}" '
                    f'style="color:#aaa;text-decoration:none">'
                    f'+ {extra_count} стр. сайта обновлено '
                    f'<span id="{pages_id}_btn" style="font-size:10px;background:#f0f0f0;'
                    f'padding:1px 6px;border-radius:3px;color:#888">▼ показать</span>'
                    f'</a></p>'
                    f'<div id="{pages_id}" style="display:none;margin-top:6px">'
                    f'<ul class="items-list">{pages_li}</ul>'
                    f'</div>'
                )
            cnt_html = "".join(parts)
        else:
            cnt_html = '<p class="empty-note">Публикаций за этот месяц не найдено.</p>'

        # Пересчитываем n_content по реальным редакционным публикациям
        n_content = len(editorial)

        # ── Сторонние публикации (фильтр уже применён выше) ─────────
        if relevant_external:
            ext_html = '<ul class="items-list">' + "".join(
                mention_item_html(x) for x in relevant_external
            ) + "</ul>"
        else:
            ext_html = '<p class="empty-note">Упоминаний в СМИ за этот месяц не найдено.</p>'

        # ── Соцсети ──────────────────────────────────────────────────
        CAT_COLORS = {
            "Мероприятие": "#1565c0", "Кейс клиента": "#2e7d32",
            "Обновление": "#8e24aa", "Статья/блог": "#2196a8",
            "Экспертный": "#e65100", "Промо": "#c62828",
            "Праздник/общее": "#78909c", "Другое": "#90a4ae", "Видео": "#e30000",
        }
        PLATFORM_COLORS = {
            "Telegram": "#0088cc", "ВКонтакте": "#4a76a8",
            "YouTube": "#e30000", "RuTube": "#e65100", "Дзен": "#333",
        }

        social_html = ""
        for ch in item.social_channels:
            platform = ch.get("platform", "")
            handle   = ch.get("handle", "")
            ch_url   = ch.get("url", "")
            subs     = ch.get("subscribers", "—")
            posts    = ch.get("posts", [])
            cats     = ch.get("cats", {})
            summary  = ch.get("summary", "")
            total    = len(posts)
            # Б2: скрываем каналы с 0 постов за месяц
            if total == 0:
                continue
            pc = PLATFORM_COLORS.get(platform, "#555")

            cats_str = " · ".join(
                f"{k} ({v})" for k, v in sorted(cats.items(), key=lambda x: -x[1]) if v
            ) or "—"

            posts_li = ""
            for p in posts[:15]:
                cat   = p.get("category", "Другое")
                cc    = CAT_COLORS.get(cat, "#90a4ae")
                dt    = format_date(p.get("lastmod", ""))
                title = p.get("title", "")[:140]
                purl  = p.get("url", "")
                reg   = p.get("reg_link", "")
                rec   = p.get("rec_link", "")
                dt_tag    = f'<span class="item-date">{e(dt)}</span>' if dt else '<span class="item-date"></span>'
                cat_badge = f'<span class="item-badge" style="background:{cc}">{e(cat)}</span>'
                links_ex  = ""
                if reg:
                    links_ex += f' <a href="{e(reg)}" target="_blank" style="font-size:10px;color:#1565c0">[Регистрация]</a>'
                if rec:
                    links_ex += f' <a href="{e(rec)}" target="_blank" style="font-size:10px;color:#1565c0">[Запись]</a>'
                posts_li += (
                    f'<li>{dt_tag}{cat_badge}'
                    f'<span class="item-text"><a href="{e(purl)}" target="_blank">{e(title)}</a>{links_ex}</span></li>'
                )

            no_posts = '<p class="empty-note">Постов за этот месяц не найдено.</p>' if not posts_li else ""
            social_html += f"""
<div class="social-card">
  <div class="social-header" style="background:{pc}18;border-bottom:1px solid {pc}30">
    <span class="platform-badge" style="background:{pc}">{e(platform)}</span>
    <a href="{e(ch_url)}" target="_blank" class="social-handle">{e(handle)}</a>
    <span class="social-meta">{e(str(subs))} подписчиков · {total} постов за месяц</span>
  </div>
  <div class="social-body">
    <div class="social-cats"><b>Темы:</b> {e(cats_str)}</div>
    <ul class="social-posts">{posts_li}</ul>
    {no_posts}
    {'<div class="social-summary">' + e(summary) + '</div>' if summary else ''}
  </div>
</div>"""

        if not social_html:
            if item.social_channels:
                social_html = '<p class="empty-note">Постов за этот месяц не найдено ни в одном канале.</p>'
            else:
                social_html = '<p class="empty-note">Соцсети не определены или данных нет.</p>'

        # ── Мероприятия ──────────────────────────────────────────────
        if item.events:
            events_html = "".join(event_item_html(ev) for ev in item.events)
            events_block = f'<ol style="padding-left:22px;margin:0">{events_html}</ol>'
        else:
            events_block = ""

        # ── Обновления ───────────────────────────────────────────────
        upd_block = update_block_html(item.updates)

        # ── Ошибки ───────────────────────────────────────────────────
        errors_block = ""
        if item.errors:
            err_li = "".join(f"<div>— {e(x)}</div>" for x in item.errors)
            errors_block = f'<div class="errors-block"><b>Ошибки сбора:</b><br>{err_li}</div>'

        # ── Б1: Краткий вывод ────────────────────────────────────────
        _summary_parts = []
        if n_content > 0:
            _word = "статья" if n_content == 1 else ("статьи" if 2 <= n_content <= 4 else "статей")
            _summary_parts.append(f"{n_content} {_word}")
        if total_social_posts > 0:
            _word = "пост" if total_social_posts == 1 else ("поста" if 2 <= total_social_posts <= 4 else "постов")
            _summary_parts.append(f"{total_social_posts} {_word} в соцсетях")
        if n_events > 0:
            _word = "мероприятие" if n_events == 1 else ("мероприятия" if 2 <= n_events <= 4 else "мероприятий")
            _summary_parts.append(f"{n_events} {_word}")
        if n_updates > 0:
            _word = "обновление" if n_updates == 1 else ("обновления" if 2 <= n_updates <= 4 else "обновлений")
            _summary_parts.append(f"{n_updates} {_word} продукта")

        if _summary_parts:
            _brief_text = e(item.name) + ": " + e(", ".join(_summary_parts)) + "."
            _brief_color = "#1a5c35"
            _brief_bg = "#f0fdf4"
            _brief_border = "#bbf7d0"
        else:
            _brief_text = e(item.name) + ": активности за этот месяц не обнаружено."
            _brief_color = "#6b7280"
            _brief_bg = "#f9fafb"
            _brief_border = "#e5e7eb"

        brief_summary_html = (
            f'<div style="margin:14px 0 4px;padding:10px 16px;border-radius:8px;'
            f'background:{_brief_bg};border:1px solid {_brief_border};'
            f'font-size:13px;color:{_brief_color};font-weight:500;line-height:1.5">'
            f'{_brief_text}</div>'
        )

        # ── Б5: Условный рендер секций (не показываем пустые) ────────
        events_section = (
            f'<div class="section"><p class="section-title">🎙 Мероприятия</p>{events_block}</div>'
            if events_block else ""
        )
        upd_section = (
            f'<div class="section"><p class="section-title">🔄 Обновления в продукте</p>{upd_block}</div>'
            if upd_block else ""
        )

        body += f"""
<div class="comp-card" id="{e(item.key)}">
  <div class="comp-header" style="background:{hdr_color}">
    <div class="comp-avatar">{e(initials)}</div>
    <div>
      <p class="comp-name">{e(item.name)}</p>
      <p class="comp-domain">{e(item.domain)}</p>
    </div>
  </div>
  <div class="comp-stats-bar">
    <div class="comp-stat">
      <div {stat_color(n_content, 'blue')}>{n_content}</div>
      <div class="comp-stat-label">статей</div>
    </div>
    <div class="comp-stat">
      <div {stat_color(total_social_posts, 'orange')}>{total_social_posts}</div>
      <div class="comp-stat-label">постов</div>
    </div>
    <div class="comp-stat">
      <div {stat_color(n_events, 'green')}>{n_events}</div>
      <div class="comp-stat-label">мероприятий</div>
    </div>
    <div class="comp-stat">
      <div {stat_color(n_updates, 'purple')}>{n_updates}</div>
      <div class="comp-stat-label">обновлений</div>
    </div>
    <div class="comp-stat">
      <div {stat_color(n_external, 'purple')}>{n_external}</div>
      <div class="comp-stat-label">упоминаний</div>
    </div>
  </div>
  <div class="comp-body">
    {brief_summary_html}
    {spywords_html}
    {ws_html}
    <div class="section">
      <p class="section-title">📝 Контент на сайте</p>
      {cnt_html}
    </div>
    <div class="section">
      <p class="section-title">📣 Соц. сети</p>
      {social_html}
    </div>
    {events_section}
    {upd_section}
    <div class="section">
      <p class="section-title">📰 Упоминания в СМИ</p>
      {ext_html}
    </div>
    {errors_block}
  </div>
</div>"""

    body += "</div></body></html>"
    return body


def _item_activity_stats(item):
    """Вычисляет статистику активности конкурента. Используется в summary и executive summary."""
    _EDITORIAL_LABELS = {"Статья", "Кейс", "Новость", "Мероприятие", "Обновление", "Партнёрство"}
    editorial = [x for x in item.content if classify_page(x.get("url", ""))[0] in _EDITORIAL_LABELS]
    n_blog    = len(editorial)
    n_posts   = sum(len(ch.get("posts", [])) for ch in item.social_channels)
    n_events  = len(item.events)
    n_updates = len(item.updates)
    relevant_ext = [x for x in item.external if is_relevant_mention(x, item.name)]
    n_ext     = len(relevant_ext)
    total     = n_blog + n_posts + n_events + n_updates
    return {
        "blog": n_blog, "posts": n_posts, "events": n_events,
        "updates": n_updates, "ext": n_ext, "total": total,
        "editorial": editorial,
    }


def render_executive_summary_html(items, month_label):
    """Блок «Ключевые события месяца» — вставляется перед карточками конкурентов."""
    stats = [{"item": it, **_item_activity_stats(it)} for it in items]
    by_total = sorted(stats, key=lambda x: x["total"], reverse=True)

    def _num(n, singular, few, many):
        if n % 10 == 1 and n % 100 != 11:       return f"{n} {singular}"
        if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14): return f"{n} {few}"
        return f"{n} {many}"

    # ── Самый активный ───────────────────────────────────────────
    top1 = by_total[0] if by_total and by_total[0]["total"] > 0 else None
    top2 = by_total[1] if len(by_total) > 1 and by_total[1]["total"] > 0 else None
    if top1:
        top_parts = []
        if top1["blog"]:    top_parts.append(_num(top1["blog"],    "статья",      "статьи",      "статей"))
        if top1["posts"]:   top_parts.append(_num(top1["posts"],   "TG-пост",     "TG-поста",    "TG-постов"))
        if top1["events"]:  top_parts.append(_num(top1["events"],  "мероприятие", "мероприятия", "мероприятий"))
        if top1["updates"]: top_parts.append(_num(top1["updates"], "обновление",  "обновления",  "обновлений"))
        top_text = f'<b>{e(top1["item"].name)}</b> — {e(", ".join(top_parts))}'
        if top2:
            top_parts2 = []
            if top2["blog"]:    top_parts2.append(_num(top2["blog"],    "статья",      "статьи",      "статей"))
            if top2["posts"]:   top_parts2.append(_num(top2["posts"],   "TG-пост",     "TG-поста",    "TG-постов"))
            if top2["events"]:  top_parts2.append(_num(top2["events"],  "мероприятие", "мероприятия", "мероприятий"))
            if top2["updates"]: top_parts2.append(_num(top2["updates"], "обновление",  "обновления",  "обновлений"))
            top_text += f'; <b>{e(top2["item"].name)}</b> — {e(", ".join(top_parts2))}'
        leader_html = f'<div class="exec-row"><span class="exec-icon">🏆</span><span><b>Самый активный:</b> {top_text}</span></div>'
    else:
        leader_html = '<div class="exec-row"><span class="exec-icon">🏆</span><span>Ни один конкурент не проявил активности за этот месяц.</span></div>'

    # ── Новые обновления продукта ────────────────────────────────
    updates_parts = []
    for s in stats:
        if s["updates"]:
            for upd in s["item"].updates[:2]:
                ver   = upd.get("version", "")
                title = upd.get("title", "")
                label = ver or title or "обновление"
                items_sample = [_clean_update_item(it) for it in upd.get("items", [])[:2]]
                items_sample = [x for x in items_sample if x]
                detail = " · ".join(items_sample[:2]) if items_sample else ""
                updates_parts.append(
                    f'<b>{e(s["item"].name)}</b> {e(label)}'
                    + (f' — {e(detail)}' if detail else '')
                )
    if updates_parts:
        product_html = (
            '<div class="exec-row"><span class="exec-icon">🆕</span>'
            '<span><b>Новое в продуктах:</b> '
            + ' &nbsp;|&nbsp; '.join(updates_parts)
            + '</span></div>'
        )
    else:
        product_html = ""

    # ── Блог ─────────────────────────────────────────────────────
    blog_parts = [(s["item"].name, s["blog"]) for s in by_total if s["blog"] > 0]
    if blog_parts:
        blog_text = " · ".join(
            f'<b>{e(nm)}</b> {_num(n, "статья", "статьи", "статей")}'
            for nm, n in blog_parts
        )
        blog_html = f'<div class="exec-row"><span class="exec-icon">📝</span><span><b>Блог:</b> {blog_text}</span></div>'
    else:
        blog_html = ""

    # ── Telegram ─────────────────────────────────────────────────
    tg_parts = [(s["item"].name, s["posts"]) for s in by_total if s["posts"] > 0]
    if tg_parts:
        tg_text = " · ".join(
            f'<b>{e(nm)}</b> {_num(n, "пост", "поста", "постов")}'
            for nm, n in tg_parts
        )
        tg_html = f'<div class="exec-row"><span class="exec-icon">📣</span><span><b>Telegram:</b> {tg_text}</span></div>'
    else:
        tg_html = ""

    # ── Вебинары / мероприятия ───────────────────────────────────
    ev_parts = [(s["item"].name, s["events"]) for s in stats if s["events"] > 0]
    if ev_parts:
        ev_text = " · ".join(
            f'<b>{e(nm)}</b> {_num(n, "мероприятие", "мероприятия", "мероприятий")}'
            for nm, n in ev_parts
        )
        ev_html = f'<div class="exec-row"><span class="exec-icon">🎙</span><span><b>Мероприятия:</b> {ev_text}</span></div>'
    else:
        ev_html = ""

    # ── Неактивные ───────────────────────────────────────────────
    inactive = [s["item"].name for s in stats if s["total"] == 0]
    inactive_html = (
        f'<div class="exec-row"><span class="exec-icon">😴</span>'
        f'<span><b>Неактивны:</b> {e(", ".join(inactive))}</span></div>'
        if inactive else ""
    )

    rows_html = leader_html + product_html + tg_html + blog_html + ev_html + inactive_html

    return f"""
<div class="exec-summary" id="exec-summary">
  <p class="exec-title">{e(month_label)} — ключевые события</p>
  {rows_html}
</div>
"""


def render_summary_html(items):
    """Итоговая сводная таблица активности всех конкурентов."""
    stats = [{"item": it, **_item_activity_stats(it)} for it in items]
    by_total = sorted(stats, key=lambda x: x["total"], reverse=True)

    def _cell(n):
        if n == 0:
            return '<td style="color:#ddd;text-align:center">—</td>'
        return f'<td style="text-align:center;font-weight:600">{n}</td>'

    rows_html = ""
    for s in by_total:
        total = s["total"]
        total_cell = (
            f'<td style="text-align:center;font-weight:700;color:#1a5c35">{total}</td>'
            if total > 0 else
            '<td style="text-align:center;color:#aaa">0</td>'
        )
        rows_html += (
            f'<tr><td style="font-weight:500">{e(s["item"].name)}</td>'
            f'{_cell(s["blog"])}{_cell(s["posts"])}{_cell(s["events"])}'
            f'{_cell(s["updates"])}{_cell(s["ext"])}{total_cell}</tr>'
        )

    return f"""
<div class="summary-section" id="summary">
  <p class="summary-title">Сводная таблица активности</p>
  <table>
    <thead><tr>
      <th style="text-align:left">Компания</th>
      <th>📝 Статьи</th>
      <th>📣 TG-посты</th>
      <th>🎙 Вебинары</th>
      <th>🔄 Обновления</th>
      <th>📰 Упоминания</th>
      <th style="color:#1a5c35">Итого</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>
"""


def setup_fonts():
    if colors is None:
        return "Helvetica"
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                pdfmetrics.registerFont(TTFont("MonitorFont", path))
                return "MonitorFont"
            except Exception:
                pass
    return "Helvetica"


def render_pdf(month, items, pdf_path):
    if colors is None:
        raise RuntimeError("reportlab is not installed")
    font = setup_fonts()
    month_label = MONTHS.get(month, month)
    doc = SimpleDocTemplate(
        str(pdf_path), pagesize=A4,
        leftMargin=16 * mm, rightMargin=16 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm,
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="RuTitle",  parent=styles["Title"],    fontName=font, fontSize=18, leading=22, alignment=0))
    styles.add(ParagraphStyle(name="RuH2",     parent=styles["Heading2"], fontName=font, fontSize=14, leading=18, textColor=colors.HexColor("#1a1a2e"), spaceBefore=14))
    styles.add(ParagraphStyle(name="RuH3",     parent=styles["Heading3"], fontName=font, fontSize=11, leading=14, textColor=colors.HexColor("#4f77ff"), spaceBefore=8))
    styles.add(ParagraphStyle(name="RuBody",   parent=styles["BodyText"], fontName=font, fontSize=10,  leading=14))
    styles.add(ParagraphStyle(name="RuSmall",  parent=styles["BodyText"], fontName=font, fontSize=8.5, leading=11, textColor=colors.HexColor("#666666")))
    styles.add(ParagraphStyle(name="RuBrief",  parent=styles["BodyText"], fontName=font, fontSize=10,  leading=14, textColor=colors.HexColor("#1a5c35"), backColor=colors.HexColor("#f0fdf4"), borderPadding=6))

    _TS = lambda: TableStyle([
        ("FONTNAME",    (0, 0), (-1, -1), font),
        ("FONTSIZE",    (0, 0), (-1, -1), 9),
        ("BACKGROUND",  (0, 0), (-1, 0),  colors.HexColor("#f3f5fb")),
        ("GRID",        (0, 0), (-1, -1), 0.4, colors.HexColor("#dfe3ec")),
        ("ALIGN",       (1, 1), (-1, -1), "CENTER"),
        ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafbff")]),
    ])
    _EDITORIAL_LABELS = {"Статья", "Кейс", "Новость", "Мероприятие", "Обновление", "Партнёрство"}

    story = [
        Paragraph(f"{month_label} — Мониторинг активности конкурентов", styles["RuTitle"]),
        Spacer(1, 6),
        Paragraph("Данные собраны автоматически. SpyWords и Вордстат показываются только при наличии реальных данных.", styles["RuSmall"]),
        Spacer(1, 10),
    ]

    # ── Сводная таблица ──────────────────────────────────────────────────────
    story.append(Paragraph("Сводная таблица активности", styles["RuH2"]))
    tbl_data = [["Компания", "Статьи", "TG-посты", "Вебинары", "Обновления", "Упоминания", "Итого"]]
    stats_all = [{"item": it, **_item_activity_stats(it)} for it in items]
    for s in sorted(stats_all, key=lambda x: x["total"], reverse=True):
        tbl_data.append([
            s["item"].name,
            str(s["blog"]) if s["blog"] else "—",
            str(s["posts"]) if s["posts"] else "—",
            str(s["events"]) if s["events"] else "—",
            str(s["updates"]) if s["updates"] else "—",
            str(s["ext"]) if s["ext"] else "—",
            str(s["total"]) if s["total"] else "0",
        ])
    tbl = Table(tbl_data, repeatRows=1, hAlign="LEFT")
    tbl.setStyle(_TS())
    story.extend([tbl, Spacer(1, 16), PageBreak()])

    # ── Карточка каждого конкурента ──────────────────────────────────────────
    for s in stats_all:
        item = s["item"]
        editorial = s["editorial"]
        relevant_ext = [x for x in item.external if is_relevant_mention(x, item.name)]

        # Заголовок
        story.append(Paragraph(f"{item.name}  ·  {item.domain}", styles["RuH2"]))

        # Краткий вывод
        brief_parts = []
        if s["blog"]:    brief_parts.append(f"{s['blog']} статей")
        if s["posts"]:   brief_parts.append(f"{s['posts']} TG-постов")
        if s["events"]:  brief_parts.append(f"{s['events']} мероприятий")
        if s["updates"]: brief_parts.append(f"{s['updates']} обновлений")
        brief_text = (", ".join(brief_parts) + ".") if brief_parts else "Активности не обнаружено."
        story.append(Paragraph(brief_text, styles["RuBrief"]))
        story.append(Spacer(1, 8))

        # SpyWords (только если есть)
        y50 = item.spywords.get("yandex_top50")
        g50 = item.spywords.get("google_top50")
        if any(v is not None for v in [y50, item.spywords.get("yandex_top10"), g50, item.spywords.get("google_top10")]):
            story.append(Paragraph("Показатели сайта", styles["RuH3"]))
            spy_data = [
                ["Система", "TOP-50", "TOP-10", "Трафик", "Уник. URL"],
                ["Яндекс", fmt(item.spywords.get("yandex_top50")), fmt(item.spywords.get("yandex_top10")), fmt(item.spywords.get("search_traffic_yandex")), fmt(item.spywords.get("unique_urls_yandex"))],
                ["Google", fmt(item.spywords.get("google_top50")), fmt(item.spywords.get("google_top10")), fmt(item.spywords.get("search_traffic_google")), fmt(item.spywords.get("unique_urls_google"))],
            ]
            spy_tbl = Table(spy_data, repeatRows=1, hAlign="LEFT")
            spy_tbl.setStyle(_TS())
            story.extend([spy_tbl, Spacer(1, 6)])

        # Контент
        if editorial:
            story.append(Paragraph(f"📝 Контент на сайте ({len(editorial)} публикаций)", styles["RuH3"]))
            story.append(ListFlowable(
                [ListItem(Paragraph(
                    f"{x.get('title', '') or slug_title(x.get('url',''))}  [{format_date(x.get('lastmod',''))}]",
                    styles["RuBody"]
                )) for x in editorial[:8]],
                bulletType="bullet", leftIndent=12,
            ))
            story.append(Spacer(1, 4))

        # Соцсети
        active_chs = [ch for ch in item.social_channels if len(ch.get("posts", [])) > 0]
        if active_chs:
            story.append(Paragraph("📣 Социальные сети", styles["RuH3"]))
            for ch in active_chs:
                n = len(ch.get("posts", []))
                story.append(Paragraph(
                    f"{ch.get('platform','')} @{ch.get('handle','')} — {n} постов",
                    styles["RuBody"]
                ))
            story.append(Spacer(1, 4))

        # Мероприятия
        if item.events:
            story.append(Paragraph(f"🎙 Мероприятия ({len(item.events)})", styles["RuH3"]))
            for ev in item.events:
                clean_t, role = clean_event_title(ev)
                date_str = format_date(ev.get("lastmod", ""))
                label = f"{date_str} — {clean_t}" if date_str else clean_t
                if role: label += f" [{role}]"
                story.append(Paragraph(f"• {label}", styles["RuBody"]))
            story.append(Spacer(1, 4))

        # Обновления
        if item.updates:
            story.append(Paragraph(f"🔄 Обновления в продукте ({len(item.updates)})", styles["RuH3"]))
            for upd in item.updates:
                ver   = upd.get("version", "")
                title = upd.get("title", "")
                date  = format_date(upd.get("date", ""))
                hdr   = " ".join(x for x in [ver, date, title] if x)
                story.append(Paragraph(f"• {hdr}", styles["RuBody"]))
                clean_items = [_clean_update_item(it) for it in upd.get("items", [])[:4]]
                for ci in [x for x in clean_items if x]:
                    story.append(Paragraph(f"    — {ci}", styles["RuSmall"]))
            story.append(Spacer(1, 4))

        # Упоминания
        if relevant_ext:
            story.append(Paragraph(f"📰 Упоминания в СМИ ({len(relevant_ext)})", styles["RuH3"]))
            story.append(ListFlowable(
                [ListItem(Paragraph(
                    f"{x.get('title', '') or x.get('url','')}",
                    styles["RuBody"]
                )) for x in relevant_ext[:6]],
                bulletType="bullet", leftIndent=12,
            ))

        story.extend([Spacer(1, 14), PageBreak()])

    # Убираем последний PageBreak
    if story and isinstance(story[-1], PageBreak):
        story.pop()

    doc.build(story)


# ---------------------------------------------------------------------------
# Flask WSGI-приложение (используется и локально, и на Railway)
# ---------------------------------------------------------------------------

flask_app = None

if _flask_ok:
    flask_app = _Flask(__name__, static_folder=None)

    @flask_app.after_request
    def _no_cache(resp):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp

    # ── Статус фоновой задачи ──────────────────────────────────────────────
    @flask_app.route("/api/job/<job_id>")
    def _api_job(job_id):
        # Сначала проверяем память (самое актуальное)
        job = JOBS.get(job_id)
        if job is None:
            # Fallback: читаем из файла (сохраняется между перезапусками)
            job = _read_job_file()
        if job is None:
            return _fjson({"status": "not_found"}), 404
        return _fjson(job)

    # ── Запуск сбора данных ────────────────────────────────────────────────
    @flask_app.route("/api/run", methods=["POST"])
    def _api_run():
        import threading
        payload = _freq.get_json(force=True) or {}
        job_id = "current"
        _initial = {"status": "running", "step": "Запускаю сбор данных..."}
        JOBS[job_id] = _initial
        _write_job_file(_initial)

        def run_job():
            def _set(d):
                with _JOBS_LOCK:
                    JOBS[job_id] = d
                _write_job_file(d)
            def _update_step(step):
                with _JOBS_LOCK:
                    cur = dict(JOBS.get(job_id, {}))
                    cur["step"] = step
                    JOBS[job_id] = cur
                _write_job_file(cur)

            try:
                print(f"[JOB {job_id}] Старт", flush=True)
                # Сбрасываем кеш sitemap — иначе данные прошлого запуска попадут в новый
                parse_sitemap.cache_clear()
                month          = payload.get("month", "jun")
                import_text    = payload.get("importText", "")
                sources        = payload.get("sources", {})
                manual_metrics = payload.get("manualMetrics", {})
                auth           = payload.get("auth", {})

                state = read_state()
                state["sources"] = sources
                write_state(state)

                _update_step("Собираю данные с сайтов и соцсетей...")
                print(f"[JOB {job_id}] merge_real_data...", flush=True)
                import concurrent.futures as _cf
                with _cf.ThreadPoolExecutor(max_workers=1) as _exc:
                    _fut = _exc.submit(merge_real_data, month, import_text, sources, manual_metrics, auth, _update_step)
                    try:
                        items = _fut.result(timeout=480)  # 8 минут максимум
                    except _cf.TimeoutError:
                        _fut.cancel()
                        raise RuntimeError("Сбор занял более 8 минут и был прерван. Попробуйте ещё раз.")
                print(f"[JOB {job_id}] merge_real_data готово, рендерю отчёт...", flush=True)

                _update_step("Формирую отчёт...")
                stamp     = time.strftime("%Y%m%d-%H%M%S")
                base      = f"{month}-{stamp}"
                html_report = render_report_html(month, items)
                html_path = REPORTS / f"{base}.html"
                pdf_path  = REPORTS / f"{base}.pdf"
                json_path = REPORTS / f"{base}.json"
                html_path.write_text(html_report, "utf-8")
                json_path.write_text(
                    json.dumps(asdict_data(items), ensure_ascii=False, indent=2), "utf-8"
                )

                pdf_error = None
                try:
                    render_pdf(month, items, pdf_path)
                except Exception as exc:
                    pdf_error = str(exc)
                    log_error("render_pdf", exc)

                state = read_state()
                run_info = {
                    "month":   month,
                    "html":    str(html_path.relative_to(ROOT)),
                    "pdf":     str(pdf_path.relative_to(ROOT)) if pdf_path.exists() else None,
                    "json":    str(json_path.relative_to(ROOT)),
                    "created": stamp,
                }
                state.setdefault("runs", {})[base] = run_info
                write_state(state)

                _set({
                    "status":   "done",
                    "step":     "Готово!",
                    "report":   run_info,
                    "pdfError": pdf_error,
                    "data":     asdict_data(items),
                })
                print(f"[JOB {job_id}] Готово!", flush=True)
            except Exception as exc:
                log_error(f"[JOB {job_id}] Сбор упал", exc)
                _set({"status": "error", "step": "Ошибка", "error": str(exc)})
                notify_telegram(f"⚠️ <b>marketing-monitor</b>: сбор за {month} упал\n<code>{type(exc).__name__}: {exc}</code>")

        threading.Thread(target=run_job, daemon=True).start()
        return _fjson({"ok": True, "jobId": job_id})

    # ── Статус сервиса ────────────────────────────────────────────────────
    @flask_app.route("/status")
    def _api_status():
        with _JOBS_LOCK:
            job_status = JOBS.get("current", {}).get("status", "idle")
            job_step   = JOBS.get("current", {}).get("step", "")
        state = read_state()
        runs  = state.get("runs", {})
        last_run = runs[max(runs)] if runs else None
        return _fjson({
            "ok":          True,
            "time":        time.strftime("%Y-%m-%d %H:%M:%S"),
            "job_status":  job_status,
            "job_step":    job_step,
            "last_run":    last_run,
            "recent_errors": get_recent_errors(10),
        })

    # ── Состояние (список прошлых отчётов) ────────────────────────────────
    @flask_app.route("/api/state", methods=["GET", "POST"])
    def _api_state():
        return _fjson(read_state())

    # ── Файлы из папки output (HTML/PDF/JSON отчёты) ──────────────────────
    @flask_app.route("/output/<path:filename>")
    def _serve_output(filename):
        return _fsend(ROOT / "output", filename)

    # ── Статические файлы (index.html, CSS, JS …) ─────────────────────────
    @flask_app.route("/")
    def _serve_index():
        return _fsend(STATIC, "index.html")

    @flask_app.route("/<path:filename>")
    def _serve_static(filename):
        p = STATIC / filename
        if p.exists():
            return _fsend(STATIC, filename)
        p2 = ROOT / filename
        if p2.exists():
            return _fsend(ROOT, filename)
        return "Not found", 404


# ---------------------------------------------------------------------------
# Запасной Handler (используется локально, если Flask не установлен)
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # клиент отключился — фоновое задание всё равно работает

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        # Статус фоновой задачи
        if self.path.startswith("/api/job/"):
            job_id = self.path[len("/api/job/"):]
            job = JOBS.get(job_id)
            if job is None:
                self.send_json({"status": "not_found"}, 404)
            else:
                self.send_json(job)
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/":
            path = "/index.html"
        if path.startswith("/output/"):
            file_path = ROOT / path.lstrip("/")
        else:
            file_path = STATIC / path.lstrip("/")
        if file_path.is_dir():
            file_path = file_path / "index.html"
        if not file_path.exists():
            self.send_error(404)
            return
        ctype = "text/html; charset=utf-8"
        if file_path.suffix == ".pdf":
            ctype = "application/pdf"
        elif file_path.suffix in (".jpg", ".jpeg"):
            ctype = "image/jpeg"
        elif file_path.suffix == ".png":
            ctype = "image/png"
        elif file_path.suffix == ".json":
            ctype = "application/json; charset=utf-8"
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # запрещаем кеширование, чтобы браузер не показывал старую версию страницы
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        try:
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self):
        try:
            if self.path == "/api/run":
                import threading
                payload = self.read_json()
                # Фиксированный ключ — клиент всегда опрашивает /api/job/current
                job_id = "current"
                JOBS[job_id] = {"status": "running", "step": "Запускаю сбор данных..."}

                def run_job():
                    try:
                        month        = payload.get("month", "jun")
                        import_text  = payload.get("importText", "")
                        sources      = payload.get("sources", {})
                        manual_metrics = payload.get("manualMetrics", {})
                        auth         = payload.get("auth", {})

                        state = read_state()
                        state["sources"] = sources
                        write_state(state)

                        JOBS[job_id]["step"] = "Собираю данные с сайтов и соцсетей..."
                        items = merge_real_data(month, import_text, sources, manual_metrics, auth)

                        JOBS[job_id]["step"] = "Формирую отчёт..."
                        stamp = time.strftime("%Y%m%d-%H%M%S")
                        base  = f"{month}-{stamp}"
                        html_report = render_report_html(month, items)
                        html_path = REPORTS / f"{base}.html"
                        pdf_path  = REPORTS / f"{base}.pdf"
                        json_path = REPORTS / f"{base}.json"
                        html_path.write_text(html_report, "utf-8")
                        json_path.write_text(json.dumps(asdict_data(items), ensure_ascii=False, indent=2), "utf-8")

                        pdf_error = None
                        try:
                            render_pdf(month, items, pdf_path)
                        except Exception as exc:
                            pdf_error = str(exc)

                        state = read_state()
                        run_info = {
                            "month":   month,
                            "html":    str(html_path.relative_to(ROOT)),
                            "pdf":     str(pdf_path.relative_to(ROOT)) if pdf_path.exists() else None,
                            "json":    str(json_path.relative_to(ROOT)),
                            "created": stamp,
                        }
                        state.setdefault("runs", {})[base] = run_info
                        write_state(state)

                        JOBS[job_id] = {
                            "status":   "done",
                            "step":     "Готово!",
                            "report":   run_info,
                            "pdfError": pdf_error,
                            "data":     asdict_data(items),
                        }
                    except Exception as exc:
                        traceback.print_exc()
                        JOBS[job_id] = {"status": "error", "step": "Ошибка", "error": str(exc)}

                threading.Thread(target=run_job, daemon=True).start()
                self.send_json({"ok": True, "jobId": job_id})
                return

            if self.path == "/api/state":
                self.send_json(read_state())
                return
            self.send_error(404)
        except Exception as exc:
            traceback.print_exc()
            self.send_json({"ok": False, "error": str(exc)}, 500)



def main():
    ensure_dirs()
    import threading
    import webbrowser

    cloud_mode = "RAILWAY_ENVIRONMENT" in os.environ or "RENDER" in os.environ
    host       = "0.0.0.0" if cloud_mode else "127.0.0.1"
    start_port = int(os.environ.get("PORT", "8787"))

    # ── Flask-путь (предпочтительный) ─────────────────────────────────────
    if flask_app is not None:
        port = start_port
        print(f"Marketing monitor: http://{host}:{port}")

        if not cloud_mode:
            threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()

        # Waitress — production WSGI-сервер (Railway/локально)
        try:
            from waitress import serve as _wsrv
            _wsrv(flask_app, host=host, port=port, threads=8)
        except ImportError:
            # Фоллбэк: Flask dev-сервер
            flask_app.run(host=host, port=port, threaded=True, use_reloader=False)
        return

    # ── Запасной путь: встроенный HTTP-сервер (если Flask не установлен) ──
    server = None
    port   = start_port
    if cloud_mode:
        server = ThreadingHTTPServer((host, start_port), Handler)
    else:
        for candidate in range(start_port, start_port + 20):
            try:
                server = ThreadingHTTPServer((host, candidate), Handler)
                port   = candidate
                break
            except OSError:
                continue
        if server is None:
            print("Не удалось занять ни один порт. Закройте старое окно программы.")
            return

    print(f"Marketing monitor: http://{host}:{port}")
    if not cloud_mode:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()
