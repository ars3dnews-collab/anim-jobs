# -*- coding: utf-8 -*-
"""
Бот вакансий 3D-анимации.

Читает публичную таблицу "Animation/VFX/Game Industry Job Postings",
выбирает вакансии по анимации, переводит на русский и публикует в канал.

Устройство намеренно простое: запуск читает таблицу, публикует то, что
нашлось нового, и завершается. Внутри запуска бот не ждёт и не спит
дольше нескольких секунд между постами.

Так сделано не из эстетики. Прошлая версия держала запуск живым почти
шесть часов, отсыпаясь между публикациями, и за сутки набегало 24 часа
машинного времени GitHub. Это нарушение их правил — Actions не хостинг, —
и репозиторий отключили. Здесь то же самое достигается частыми короткими
запусками, которые расходуют минуты в сутки вместо часов.

От потери памяти бот тоже защищён: список уже опубликованного он сверяет
не только с posted.json, но и с самой историей канала. Если файл памяти
исчезнет, повторов всё равно не будет.
"""

import os
import re
import io
import csv
import json
import time
import html
import hashlib
import datetime as dt

import requests

import config

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "posted.json")

TG_API = "https://api.telegram.org/bot{token}/{method}"
SHEET_CSV = ("https://docs.google.com/spreadsheets/d/{sid}/gviz/tq"
             "?tqx=out:csv&gid={gid}")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def log(msg):
    print("[{}] {}".format(dt.datetime.now().strftime("%H:%M:%S"), msg),
          flush=True)


# ------------------------------------------------------------------ память

def load_state():
    base = {"posted": [], "links": [], "glossary": {},
            "channel_scanned": False}
    if not os.path.exists(STATE_FILE):
        return base
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in base.items():
            data.setdefault(k, v)
        return data
    except Exception as e:
        log("posted.json не читается ({}) — начинаю с чистого листа".format(e))
        return base


def save_state(state):
    state["posted"] = state["posted"][-4000:]
    state["links"] = state["links"][-4000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    push_state()


def push_state():
    """Сохранить память в репозиторий сразу, не дожидаясь конца запуска."""
    if os.environ.get("GIT_AUTOSAVE", "").lower() not in ("1", "true", "yes"):
        return
    import subprocess
    try:
        subprocess.run(["git", "add", "posted.json"], cwd=HERE,
                       check=False, capture_output=True)
        changed = subprocess.run(["git", "diff", "--staged", "--quiet"],
                                 cwd=HERE, capture_output=True).returncode != 0
        if not changed:
            return
        subprocess.run(["git", "commit", "-m", "update posted jobs"],
                       cwd=HERE, check=False, capture_output=True)
        subprocess.run(["git", "pull", "--rebase", "--autostash"],
                       cwd=HERE, check=False, capture_output=True)
        r = subprocess.run(["git", "push"], cwd=HERE, capture_output=True)
        if r.returncode != 0:
            log("  ! память не запушилась: {}".format(
                r.stderr.decode("utf-8", "replace")[:150]))
    except Exception as e:
        log("  ! память не сохранилась: {}".format(e))


# ------------------------------------------------- что уже вышло в канале

def norm_link(url):
    """Адрес без схемы, www, хвостового слэша и параметров."""
    url = (url or "").strip().lower()
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    url = url.split("?")[0].split("#")[0]
    return url.rstrip("/")


def channel_username():
    name = CHANNEL_ID.lstrip("@")
    return name if re.fullmatch(r"[A-Za-z0-9_]{3,64}", name) else ""


_POST_ID = re.compile(r'data-post="[^"/]+/(\d+)"')
_HREF = re.compile(r'<a href="(https?://[^"]+)"')


def _page_links(page):
    """Ссылки на отклики и номера постов с одной страницы веб-версии."""
    ids = [int(x) for x in _POST_ID.findall(page)]
    links = [u for u in _HREF.findall(page)
             if "t.me/" not in u and "telegram.org" not in u]
    return ids, links


def load_channel(state):
    """Подтянуть в память ссылки, которые уже были в канале.

    Первый раз проходим всю историю, дальше смотрим только свежую
    страницу: новые посты всегда в конце. Это и есть страховка на случай
    потери posted.json — канал сам помнит, что в нём было.
    """
    user = channel_username()
    if not user:
        log("  канал задан не именем — сверку с историей пропускаю")
        return

    known = set(state["links"])
    pages = int(getattr(config, "CHANNEL_SCAN_PAGES", 40))
    deep = not state.get("channel_scanned")
    before, seen_pages = "", 0

    while seen_pages < (pages if deep else 1):
        url = "https://t.me/s/{}{}".format(
            user, "?before={}".format(before) if before else "")
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            if not r.ok:
                break
            page = r.text
        except Exception as e:
            log("  ! история канала недоступна: {}".format(str(e)[:90]))
            break

        ids, links = _page_links(page)
        if not ids:
            break
        for u in links:
            known.add(norm_link(u))
        seen_pages += 1
        before = str(min(ids))
        if int(before) <= 1:
            break

    state["links"] = sorted(known)
    if deep:
        state["channel_scanned"] = True
        log("  история канала прочитана: {} ссылок с {} страниц".format(
            len(known), seen_pages))
    else:
        log("  в канале уже {} ссылок".format(len(known)))


# ------------------------------------------------------------------ таблица

MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
          "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
          "november": 11, "december": 12}

RU_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
             "августа", "сентября", "октября", "ноября", "декабря")


def parse_date(text):
    """"August 28, 2026" -> date. Пусто или мусор -> None."""
    m = re.match(r"\s*([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", text or "")
    if not m:
        return None
    month = MONTHS.get(m.group(1).lower())
    if not month:
        return None
    try:
        return dt.date(int(m.group(3)), month, int(m.group(2)))
    except ValueError:
        return None


def job_id(row):
    key = "|".join([
        (row.get("studio") or "").strip().lower(),
        (row.get("title") or "").strip().lower(),
        norm_link(row.get("link")),
    ])
    return hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()[:16]


def wanted(title):
    """Подходит ли должность. Пунктуация и порядок слов не важны."""
    low = (title or "").lower()
    if not low:
        return False
    if any(s in low for s in config.SKIP):
        return False
    words = set(re.findall(r"[a-z0-9]+", low))
    return any(set(w.split()) <= words for w in config.WANT)


def download_sheet():
    """Скачать CSV листа, переживая временные отказы Google."""
    url = SHEET_CSV.format(sid=config.SHEET_ID, gid=config.SHEET_GID)
    tries = int(getattr(config, "SHEET_TRIES", 4))
    last = None
    for attempt in range(1, tries + 1):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
            if r.ok:
                r.encoding = "utf-8"
                if attempt > 1:
                    log("  таблица прочиталась с {}-й попытки".format(attempt))
                return r.text
            last = "HTTP {}".format(r.status_code)
            if r.status_code not in (429, 500, 502, 503, 504):
                raise RuntimeError("таблица: " + last)
        except requests.RequestException as e:
            last = str(e)[:120]
        if attempt < tries:
            pause = min(30, 5 * 2 ** (attempt - 1))
            log("  таблица не далась ({}), жду {} с".format(last, pause))
            time.sleep(pause)
    raise RuntimeError("таблица недоступна: {}".format(last))


def fetch_sheet():
    """Отобранные вакансии, свежие сначала."""
    rows = list(csv.reader(io.StringIO(download_sheet())))
    log("  в таблице строк: {}".format(len(rows)))

    col = config.COLUMNS
    today = dt.date.today()
    min_date = dt.date.fromisoformat(getattr(config, "MIN_DATE", "2000-01-01"))
    out = []
    for raw in rows[1:]:
        def cell(name):
            i = col[name]
            return raw[i].strip() if i < len(raw) else ""

        studio, title, link = cell("studio"), cell("title"), cell("link")
        if not studio or not title or not wanted(title):
            continue

        when = parse_date(cell("date"))
        age = (today - when).days if when else 999
        if age > int(getattr(config, "MAX_AGE_DAYS", 45)):
            continue
        if when and when < min_date:
            continue

        out.append({
            "studio": studio, "title": title, "city": cell("city"),
            "state": cell("state"), "country": cell("country"),
            "level": cell("level"), "mode": cell("mode"),
            "software": cell("software"), "notes": cell("notes"),
            "link": link, "date": when, "age": age,
        })

    out.sort(key=lambda j: j["age"])
    log("  подходящих вакансий с {}: {}".format(min_date, len(out)))
    return out


def merge_twins(jobs):
    """Схлопнуть строки, описывающие одну вакансию.

    Студии подают один набор сразу на несколько городов, и в таблице это
    несколько строк с одинаковой ссылкой. Города не теряются: они
    собираются в одну строку и попадают в пост целиком.
    """
    order, groups = [], {}
    for job in jobs:
        jid = job_id(job)
        if jid not in groups:
            groups[jid] = job
            order.append(jid)
            job["places"] = []
        spot = ", ".join(p for p in (job.get("city"), job.get("state"),
                                     job.get("country")) if p)
        places = groups[jid].setdefault("places", [])
        if spot and spot not in places:
            places.append(spot)
    return [groups[j] for j in order]


# ------------------------------------------------------------------ словарь
# Уровень и формат работы повторяются из строки в строку, поэтому
# переводим их таблицей, а не моделью: формулировки в канале одинаковые
# и не зависят от настроения ИИ.

LEVELS = {
    "trainee": "стажёр", "internship": "стажировка", "junior": "junior",
    "mid": "middle", "senior": "senior", "lead": "lead",
    "director": "директор", "manager / supervisor": "супервайзер",
    "head": "head",
}

MODES = {
    "remote": "удалённо", "hybrid": "гибрид", "on-site": "в офисе",
    "on-sitel": "в офисе", "on-site or remote": "офис или удалённо",
    "all options": "офис, гибрид или удалённо",
}

MODE_TAGS = {
    "удалённо": "#удалёнка", "гибрид": "#гибрид", "в офисе": "#офис",
    "офис или удалённо": "#удалёнка",
    "офис, гибрид или удалённо": "#удалёнка",
}


def ru_level(value):
    parts = [p.strip().lower() for p in (value or "").split(",") if p.strip()]
    return ", ".join(LEVELS.get(p, p) for p in parts)


def ru_mode(value):
    return MODES.get((value or "").strip().lower(), (value or "").strip())


def ru_date(when):
    if not when:
        return ""
    return "{} {} {}".format(when.day, RU_MONTHS[when.month - 1], when.year)


# ------------------------------------------------------------------ Gemini

GEMINI_URL = ("https://generativelanguage.googleapis.com/{ver}/models/"
              "{model}:generateContent")
_MODELS = []


def _safe(msg):
    text = str(msg)
    if GEMINI_KEY:
        text = text.replace(GEMINI_KEY, "***")
    return re.sub(r"key=[^&\s]+", "key=***", text)


def available_models():
    if _MODELS:
        return _MODELS
    for ver in ("v1beta", "v1"):
        try:
            r = requests.get(
                "https://generativelanguage.googleapis.com/{}/models".format(ver),
                params={"key": GEMINI_KEY, "pageSize": 200}, timeout=30)
            if not r.ok:
                continue
            names = [(ver, m["name"].split("/")[-1])
                     for m in r.json().get("models", [])
                     if "generateContent" in (m.get("supportedGenerationMethods") or [])]
            if names:
                _MODELS.extend(names)
                log("  доступно моделей: {}".format(len(names)))
                return _MODELS
        except Exception as e:
            log("  ! список моделей: {}".format(_safe(e)[:110]))
    return []


def model_queue():
    found = available_models()
    if not found:
        return ([("v1beta", config.GEMINI_MODEL)] +
                [("v1beta", m) for m in config.GEMINI_FALLBACK_MODELS])
    bad = ("tts", "image", "embedding", "live", "audio", "native", "vision",
           "thinking", "learnlm", "aqa")
    usable = [(v, n) for v, n in found if not any(b in n for b in bad)] or found
    prefs = [config.GEMINI_MODEL] + list(config.GEMINI_FALLBACK_MODELS)

    def rank(item):
        _, name = item
        alias = 1 if ("latest" in name or name.endswith("-preview")) else 0
        if name in prefs:
            return (0, alias, prefs.index(name))
        for i, mark in enumerate(("flash-lite", "flash", "gemma", "pro")):
            if mark in name:
                return (i + 1, alias, len(name))
        return (5, alias, len(name))

    return sorted(usable, key=rank)[:5]


def ask_ai(prompt, timeout=50):
    """Спросить модель, перебирая доступные, пока какая-нибудь не ответит."""
    if not GEMINI_KEY:
        return ""
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1200}}
    for ver, model in model_queue():
        try:
            r = requests.post(GEMINI_URL.format(ver=ver, model=model),
                              params={"key": GEMINI_KEY}, json=body,
                              timeout=timeout)
            if not r.ok:
                continue
            cand = (r.json().get("candidates") or [{}])[0]
            parts = (cand.get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts).strip()
            if text:
                return text
        except Exception as e:
            log("    ! {}: {}".format(model, _safe(e)[:90]))
    return ""


TRANSLATE_PROMPT = """Ты редактор русскоязычного телеграм-канала с вакансиями \
для 3D-аниматоров. Переведи данные вакансии на русский язык.

Ответь ТОЛЬКО JSON-объектом без markdown-обёртки, с полями:
  "title"  — должность по-русски, коротко и по-профессиональному
             ("Animator, 3D" -> "3D-аниматор", "Technical Animator" -> \
"технический аниматор", "Animation Supervisor" -> "супервайзер анимации").
             Без названия студии, без слова "вакансия".
  "place"  — место одной строкой: город, регион, страна по-русски
             ("Madison, Wisconsin, United States" -> "Мэдисон, Висконсин, США").
             Общепринятые русские названия. Если места нет — "".
             Несколько мест разделены знаком "|" — это одна вакансия сразу
             на несколько городов: перечисли их через запятую, не повторяя
             страну ("Гуанчжоу, Ханчжоу, Шанхай (Китай), Франция").
  "notes"  — перевод примечания живым русским языком, 1-3 предложения.
             Ничего не выдумывай, только то, что есть в оригинале.
             Если примечания нет или оно бессодержательное — "".

Данные:
Должность: {title}
Студия: {studio}
Место: {place}
Примечание: {notes}
"""


def translate(job, state):
    """Перевести вакансию. Место и должность копятся в словаре.

    Одинаковые города и должности встречаются десятки раз, и гонять ради
    них модель заново — тратить лимиты и рисковать разнобоем.
    """
    places = job.get("places") or []
    if not places:
        one = ", ".join(p for p in (job.get("city"), job.get("state"),
                                    job.get("country")) if p)
        places = [one] if one else []
    place_en = " | ".join(places[:6])

    gloss = state.setdefault("glossary", {})
    title_key = "t:" + job["title"].lower()
    place_key = "p:" + place_en.lower()

    known_title = gloss.get(title_key)
    known_place = gloss.get(place_key) if place_en else ""
    need_notes = bool((job["notes"] or "").strip())

    if known_title and (known_place or not place_en) and not need_notes:
        return {"title": known_title, "place": known_place or "", "notes": ""}

    raw = ask_ai(TRANSLATE_PROMPT.format(
        title=job["title"], studio=job["studio"],
        place=place_en or "—", notes=(job["notes"] or "—")[:900]))

    data = {}
    if raw:
        cut = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        m = re.search(r"\{.*\}", cut, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = {}

    title_ru = (data.get("title") or "").strip() or known_title or job["title"]
    place_ru = (data.get("place") or "").strip() or known_place or place_en
    notes_ru = (data.get("notes") or "").strip()

    if title_ru and len(title_ru) < 80:
        gloss[title_key] = title_ru
    if place_ru and len(place_ru) < 160:
        gloss[place_key] = place_ru

    return {"title": title_ru, "place": place_ru, "notes": notes_ru}


# ------------------------------------------------------------------ пост

_EMPTY_NOTE = re.compile(r"^(нет|отсутству|информац\w+ нет|не указан|—|-)\b", re.I)

COUNTRY_TAGS = {
    "united states": "#сша", "canada": "#канада", "england": "#британия",
    "united kingdom": "#британия", "scotland": "#британия",
    "spain": "#испания", "ireland": "#ирландия", "france": "#франция",
    "germany": "#германия", "sweden": "#швеция", "poland": "#польша",
    "india": "#индия", "australia": "#австралия", "japan": "#япония",
    "china": "#китай", "new zealand": "#новаязеландия",
    "netherlands": "#нидерланды", "czech republic": "#чехия",
    "hungary": "#венгрия", "italy": "#италия", "brazil": "#бразилия",
    "malta": "#мальта", "cyprus": "#кипр", "south korea": "#корея",
    "vietnam": "#вьетнам", "philippines": "#филиппины",
    "malaysia": "#малайзия", "thailand": "#таиланд",
    "indonesia": "#индонезия", "singapore": "#сингапур",
    "israel": "#израиль", "türkiye": "#турция", "turkey": "#турция",
    "serbia": "#сербия", "colombia": "#колумбия", "peru": "#перу",
    "uruguay": "#уругвай", "luxembourg": "#люксембург",
    "mexico": "#мексика", "argentina": "#аргентина", "finland": "#финляндия",
    "denmark": "#дания", "norway": "#норвегия", "belgium": "#бельгия",
    "austria": "#австрия", "switzerland": "#швейцария",
    "portugal": "#португалия", "romania": "#румыния",
}


def build_post(job, ru):
    e = html.escape
    lines = ["<b>{}</b> — {}".format(e(ru["title"] or job["title"]),
                                     e(job["studio"])), ""]

    if ru["place"]:
        lines.append("📍 {}".format(e(ru["place"])))

    mode = ru_mode(job["mode"])
    if mode:
        lines.append("💼 {}".format(e(mode)))

    level = ru_level(job["level"])
    if level:
        lines.append("🎚 Уровень: {}".format(e(level)))

    if job["software"]:
        lines.append("🛠 Софт: {}".format(e(job["software"])))

    when = ru_date(job["date"])
    if when:
        lines.append("📅 Опубликовано: {}".format(e(when)))

    note = (ru["notes"] or "").strip()
    if note and not _EMPTY_NOTE.match(note):
        lines += ["", e(note)]

    if job["link"]:
        label = ("Заполнить форму" if "docs.google.com/forms" in job["link"]
                 else "Откликнуться")
        lines += ["", '🔗 <a href="{}">{} →</a>'.format(
            e(job["link"], quote=True), label)]

    tags = ["#вакансия"]
    low = job["title"].lower()
    if "technical" in low:
        tags.append("#techanim")
    elif "director" in low or "supervisor" in low or "head" in low:
        tags.append("#lead")
    else:
        tags.append("#аниматор")
    tag = MODE_TAGS.get(mode)
    if tag:
        tags.append(tag)
    for place in (job.get("places") or [job.get("country") or ""]):
        ct = COUNTRY_TAGS.get(place.split(",")[-1].strip().lower())
        if ct and ct not in tags:
            tags.append(ct)
    lines += ["", " ".join(tags)]

    return "\n".join(lines)


# ------------------------------------------------------------------ Telegram

def tg(method, data=None):
    r = requests.post(TG_API.format(token=BOT_TOKEN, method=method),
                      data=data, timeout=45)
    try:
        payload = r.json()
    except Exception:
        raise RuntimeError("Telegram вернул не-JSON: {}".format(r.text[:200]))
    if not payload.get("ok"):
        raise RuntimeError("Telegram: {}".format(payload.get("description")))
    return payload["result"]


def publish(job, state):
    ru = translate(job, state)
    tg("sendMessage", {
        "chat_id": CHANNEL_ID,
        "text": build_post(job, ru),
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })
    log("  ✓ {} — {}".format(ru["title"] or job["title"], job["studio"]))


# ------------------------------------------------------------------ запуск

def main():
    started = time.time()
    force = os.environ.get("FORCE", "").lower() in ("1", "true", "yes")
    dry = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

    if not dry and not (BOT_TOKEN and CHANNEL_ID):
        raise SystemExit("нет BOT_TOKEN или CHANNEL_ID")

    state = load_state()
    log("Бот вакансий на связи. В памяти {} вакансий".format(
        len(state["posted"])))

    load_channel(state)

    log("Читаю таблицу...")
    try:
        jobs = fetch_sheet()
    except Exception as e:
        # Отказ Google — не повод падать: следующий запуск через 15 минут.
        log("! таблица не прочиталась: {}".format(str(e)[:150]))
        log("Выхожу без публикаций, попробую в следующий раз.")
        return

    known = set(state["posted"])
    seen_links = set(state["links"])
    fresh = []
    skipped_channel = 0
    for job in jobs:
        if job_id(job) in known:
            continue
        if norm_link(job["link"]) in seen_links:
            skipped_channel += 1
            continue
        fresh.append(job)

    fresh = merge_twins(fresh)
    fresh.sort(key=lambda j: j["age"])
    if skipped_channel:
        log("  уже были в канале, пропускаю: {}".format(skipped_channel))
    log("Новых к публикации: {}".format(len(fresh)))

    if dry:
        for j in fresh[:15]:
            log("  · [{} дн] {} — {} · {}".format(
                j["age"], j["title"], j["studio"], j["country"]))
        return

    if not fresh:
        log("Новых вакансий нет. Запуск занял {} с.".format(
            int(time.time() - started)))
        return

    gap = int(getattr(config, "POST_GAP_SECONDS", 40))
    limit = int(getattr(config, "MAX_POSTS_PER_RUN", 6))
    posted = 0

    for job in fresh[:limit]:
        if posted and not force:
            time.sleep(gap)
        try:
            publish(job, state)
        except Exception as e:
            log("  ! не опубликовалось: {}".format(str(e)[:200]))
            continue
        state["posted"].append(job_id(job))
        state["links"].append(norm_link(job["link"]))
        save_state(state)
        posted += 1

    left = len(fresh) - posted
    log("Готово: {} постов за {} с.{}".format(
        posted, int(time.time() - started),
        "  Осталось на следующий запуск: {}".format(left) if left > 0 else ""))


if __name__ == "__main__":
    main()
