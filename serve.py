"""Маленький космос: веб-приложение и подбор блюд с Food.ru по продуктам в реальном времени.

Как подбирается меню:
1. По каждому продукту «что есть дома» (и «готова докупить» в режиме докупки) ищем на food.ru.
   Страница поиска отдаёт для каждого рецепта полный список ингредиентов — по нему и считаем,
   чего не хватает (соль, масло, специи и прочую базу не считаем).
2. Отбрасываем рецепты с исключёнными продуктами и скрытые («Не предлагать»).
3. Раскладываем по приёмам пищи и для выбранных блюд читаем страницу рецепта:
   граммовки, шаги и КБЖУ на порцию.
Ответы food.ru кешируются в SQLite, чтобы не дёргать сайт лишний раз.
"""
import concurrent.futures
import functools
import html
import json
import os
import re
import sqlite3
import threading
import time
import zlib
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from ingredients import basic, covers, dish_of, excludes, group, role, searchable, seasoning, snack_portion

ROOT = "https://food.ru"
SEARCH_TTL = 12 * 3600          # результаты поиска
DETAILS_TTL = 14 * 24 * 3600    # страницы рецептов
MAX_TERMS = 10                  # сколько продуктов ищем за раз
SLOTS = ["Завтрак", "Обед", "Полдник", "Ужин"]
SKIP_CATEGORIES = {"соусы и маринады", "заготовки", "напитки", "коктейли", "соусы"}

gate = threading.BoundedSemaphore(3)
workers = concurrent.futures.ThreadPoolExecutor(max_workers=6)
DB = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/liv-web")) / "recipes.sqlite3"


# --- food.ru ------------------------------------------------------------------

def fetch(path):
    request = Request(ROOT + path, headers={"User-Agent": "Mozilla/5.0 (home menu helper)", "Accept-Language": "ru"})
    with urlopen(request, timeout=8) as response:
        data = response.read(4_000_001)
        if len(data) > 4_000_000:
            raise ValueError("large page")
        return data.decode("utf-8")


def search(query, page=1):
    """Рецепты из поиска food.ru: id, название, ссылка, ингредиенты, категории, рейтинг."""
    params = {"query": query}
    if page > 1:
        params["page"] = page
    source = fetch("/search?" + urlencode(params))
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', source, re.S)
    if not match:
        raise ValueError("no data")
    state = json.loads(match[1])["props"]["pageProps"]["__EFFECTOR_NEXTJS_INITIAL_STATE__"]
    block = next((v for v in state.values() if isinstance(v, dict) and isinstance(v.get("materials"), list)), None)
    if block is None:
        raise ValueError("no materials")
    rows = []
    for m in block["materials"]:
        if m.get("material_type") != "recipe" or not m.get("ingredients") or not m.get("url_part"):
            continue
        stats = m.get("stats") or {}
        rows.append({
            "id": m["id"],
            "name": html.unescape(m["main_title"]).strip(),
            "url": f"{ROOT}/recipes/{m['id']}-{m['url_part']}",
            "ingredients": [str(i).strip() for i in m["ingredients"] if str(i).strip()],
            "categories": [b["title"] for b in m.get("breadcrumbs") or [] if b.get("title")],
            "time": m.get("total_cooking_time"),
            "rating": (stats.get("total_rating") or 0, stats.get("rating_count") or 0),
        })
    return rows


AMOUNT = re.compile(r"\s(?:(?:\d|½|¼|¾)(?![\d.,]*\s*%)|по вкусу|на кончике|щепотк|для (?:подачи|смазывания|жарки|украшения|посыпки))")
UNIT_GRAMS = {"г": 1, "гр": 1, "кг": 1000, "мл": 1, "л": 1000}


def number(text):
    text = text.replace(",", ".").strip()
    if "/" in text:
        a, b = text.split("/", 1)
        return float(a) / float(b)
    return float(text)


def parse_ingredient(line):
    """«Репчатый лук 1 шт. = 80 г» → ("Репчатый лук", "1 шт.", 80.0)"""
    line = " ".join(line.split())
    left, _, right = line.partition(" = ")
    grams = None
    if right:
        m = re.fullmatch(r"([\d.,]+)\s*(г|гр|кг|мл|л)\.?", right.strip())
        if m:
            grams = number(m[1]) * UNIT_GRAMS[m[2]]
    m = AMOUNT.search(left)
    if not m:
        return left, "", grams
    name, amount = left[:m.start()].strip(), left[m.start():].strip()
    if grams is None:
        g = re.fullmatch(r"([\d.,]+)\s*(г|гр|кг|мл|л)\.?", amount)
        if g:
            grams = number(g[1]) * UNIT_GRAMS[g[2]]
    if grams is not None and amount and not re.fullmatch(r"[\d.,]+\s*(?:г|гр)\.?", amount):
        amount += f" ≈ {round(grams):g} г"
    return name, amount, grams


def details(url):
    """Полный рецепт со страницы food.ru (данные schema.org/Recipe)."""
    source = fetch(urlsplit(url).path)
    for block in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', source, re.S):
        try:
            r = json.loads(block)
        except ValueError:
            continue
        if not isinstance(r, dict) or r.get("@type") != "Recipe":
            continue
        portions = re.match(r"\s*(\d+)", str(r.get("recipeYield") or ""))
        portions = max(1, int(portions[1])) if portions else 1
        items, total, known = [], 0.0, True
        for line in r.get("recipeIngredient") or []:
            name, amount, grams = parse_ingredient(str(line))
            items.append([name, amount])
            if grams is None:
                known = known and basic(name)  # «соль по вкусу» на КБЖУ не влияет
            else:
                total += grams
        steps = []
        for step in r.get("recipeInstructions") or []:
            text = step.get("text", "") if isinstance(step, dict) else str(step)
            text = " ".join(html.unescape(re.sub("<[^>]+>", " ", text)).split())
            if text:
                steps.append(text)
        values = None
        try:
            per100 = [float(re.search(r"\d+(?:[.,]\d+)?", r["nutrition"][k])[0].replace(",", "."))
                      for k in ("calories", "proteinContent", "fatContent", "carbohydrateContent")]
            if total and known:
                values = [round(v * total / 100 / portions, 1) for v in per100]
        except (KeyError, TypeError, AttributeError):
            pass
        image = r.get("image")
        image = image[0] if isinstance(image, list) and image else image if isinstance(image, str) else None
        return {"name": html.unescape(r.get("name") or "").strip(), "portions": portions, "items": items,
                "steps": steps, "values": values, "image": image}
    raise ValueError("no recipe data")


# --- кеш и настройки ------------------------------------------------------------

@contextmanager
def database():
    connection = sqlite3.connect(DB, timeout=8)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_store():
    with database() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS blocked(name TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS excluded(name TEXT PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'all');
            CREATE TABLE IF NOT EXISTS blocked_urls(url TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE IF NOT EXISTS migrations(id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS search_cache(query TEXT, page INTEGER, data TEXT, updated INTEGER,
                                                    PRIMARY KEY(query, page));
            CREATE TABLE IF NOT EXISTS details_cache(url TEXT PRIMARY KEY, data TEXT, updated INTEGER);
        """)
        if "mode" not in [c[1] for c in db.execute("PRAGMA table_info(excluded)")]:
            db.execute("ALTER TABLE excluded ADD COLUMN mode TEXT NOT NULL DEFAULT 'all'")
        if not db.execute("SELECT 1 FROM migrations WHERE id='live-search'").fetchone():
            # Старый накопительный кеш рецептов из первой версии больше не нужен
            db.execute("DROP TABLE IF EXISTS recipes")
            db.execute("DROP TABLE IF EXISTS searches")
            db.execute("INSERT INTO migrations VALUES ('live-search')")


def restrictions():
    with database() as db:
        return {"excluded": [(r[0], r[1]) for r in db.execute("SELECT name, mode FROM excluded ORDER BY name")],
                "blocked": [r[0] for r in db.execute("SELECT name FROM blocked ORDER BY name")],
                "blocked_urls": {r[0] for r in db.execute("SELECT url FROM blocked_urls")}}


def snapshot():
    state = restrictions()
    return {"excluded": [{"name": n, "mode": m} for n, m in state["excluded"]], "blocked": state["blocked"]}


def set_block(name, block, url=""):
    with database() as db:
        if block:
            db.execute("INSERT OR IGNORE INTO blocked VALUES (?)", (name,))
            if url:
                db.execute("INSERT OR REPLACE INTO blocked_urls VALUES (?,?)", (url, name))
        else:
            db.execute("DELETE FROM blocked WHERE name=?", (name,))
            db.execute("DELETE FROM blocked_urls WHERE name=?", (name,))


def cached(table, key, ttl, load):
    column = "url" if table == "details_cache" else "query"
    where = f"{column}=?" + (" AND page=?" if table == "search_cache" else "")
    with database() as db:
        row = db.execute(f"SELECT data FROM {table} WHERE {where} AND updated>?", (*key, time.time() - ttl)).fetchone()
    if row:
        return json.loads(row[0])
    value = load()
    with database() as db:
        db.execute(f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' * (len(key) + 2))})",
                   (*key, json.dumps(value, ensure_ascii=False), int(time.time())))
    return value


def cached_search(query, page=1):
    return cached("search_cache", (query.lower(), page), SEARCH_TTL, lambda: search(query, page))


def cached_details(url):
    return cached("details_cache", (url,), DETAILS_TTL, lambda: details(url))


# --- подбор меню ---------------------------------------------------------------

SWEET_DISH = re.compile(r"вафл|эклер|панкейк|пончик|круассан|булочк|штрудел|шарлотк|брауни|чизкейк|тирамису|суфле|зефир|пастил|"
                        r"мармелад|щербет|сорбет|молочн|сладк|десерт|каша|ягод|фрукт|морожен|джем|варень|блин|олад|сырник|творожн|пудинг|смузи|"
                        r"выпечк|кекс|маффин|пирожн|торт|печень|мусс|желе|крем|пюре из (?:малин|клубник|ягод|ябл|банан)")
SAVORY = re.compile(r"картоф|кабач|мяс|кур|рыб|гриб|сырн(?:ый|ая|ое|ые|ым)|луков|овощ|печен(?:ь|и|ью)\b|печеночн|фарш|колбас|ветчин|сосис|чеснок")
NOT_A_MEAL = re.compile(r"^как |\b(?:джем|варенье|конфитюр|повидло|сироп|ликер|настойк\w*|наливк\w*|маринад\w*|соус\w*|заправк\w*|"
                        r"масло|компот|морс|лимонад|чай|кофе|коктейл\w*|заготовк\w*|приправ\w*)\b")
PROTEIN = re.compile(r"\b(?:кур|цыпл|индей|говя|телят|свин|фарш|рыб|лосос|семг|форел|треск|минта|тунец|горбуш|кревет|кальмар|"
                     r"нут\b|фасол|чечевиц|тофу|печен(?:ь|и|ью)\b|печеночн|сердечк|котлет)")


STARCH = re.compile(r"\b(?:рис|греч|макарон|спагет|лапш|вермишел|паст[аы]\b|картоф|картош|булгур|пшен|перлов|кускус|киноа|пюре|хлеб|лаваш|тест)")


def protein(r):
    """Есть ли в блюде «основа»: мясо, птица, рыба, бобовые (бульон и приправа «для курицы» не в счёт)."""
    for i in r["ingredients"]:
        i = i.lower().replace("ё", "е")
        if PROTEIN.search(i) and not re.search(r"бульон|кубик|приправ|специ|для (?:курицы|мяса|птицы|рыбы)|\bжир|яйц|яиц", i):
            return True
    return False


def garnish(r):
    """Гарнир сам по себе (рис, пюре, макароны с маслом) — это не ужин, а дополнение к основному блюду."""
    if protein(r):
        return False
    cats = " ".join(r["categories"]).lower()
    return "гарнир" in cats or bool(re.search(r"блюда из круп|блюда из картофеля|мучные вторые", cats)) \
        or any(STARCH.search(u.lower()) for u in r["used"])


def needs_side(r):
    return protein(r) and not any(STARCH.search(i.lower()) for i in r["ingredients"]) and "гарнир" not in " ".join(r["categories"])


def slot_for(recipe):
    title = recipe["name"].lower().replace("ё", "е")
    cats = " ".join(recipe["categories"]).lower().replace("ё", "е")
    text = title + " | " + cats
    if re.search(r"мороженое|десерт|торт|пирожн|мусс|желе|пудинг|конфет|сладост|смузи|суфле", title):
        return "Полдник"
    if re.search(r"молочн.*(?:суп|лапш|вермишел)|(?:суп|лапш).*(?:молочн|на молоке)", title):
        return "Завтрак"
    if re.search(r"завтрак|каша|каши|омлет|яичниц|скрэмбл|сырник|олад|блин|гренк|мюсли|гранол|тост|творожн\w* запеканк", text):
        return "Завтрак"
    if re.search(r"суп|борщ|\bщи\b|бульон|солянк|рассольник|\bуха\b|первые блюда|плов|рагу|гуляш", text):
        return "Обед"
    if re.search(r"десерт|выпечк|пирог|пирож|печень|маффин|кекс|смузи|закуск|бутерброд|пудинг|снек|перекус|сэндвич|"
                 r"брускет|рулет|вафл|хлебц|полдник|сладост|конфет|мусс|желе|фрукт", text):
        return "Полдник"
    if SWEET_DISH.search(title) and not SAVORY.search(title):
        return "Полдник"
    return "Ужин"


# Сушёные специи и травы: без них блюдо всё равно получится, недостающими не считаем
OPTIONAL = re.compile(r"паприк|куркум|карри|тимьян|чабрец|орегано|розмарин|прованск|итальянск\w* трав|сушен\w* (?:трав|базилик|чеснок|укроп|петрушк)"
                      r"|кориандр|зира|мускат|хмели|чесночн\w* порош|молот\w* имбир|кардамон|гвоздик|корица|кунжут|семена")


def cheap(name):
    """Недорогое, что легко докупить по пути: лук, чеснок, морковь."""
    value = name.lower().replace("ё", "е")
    return group(name) in ("лук", "чеснок") or bool(re.search(r"\bморков", value) and "корейск" not in value)


def analyse(recipe, have, buy):
    """Что из ингредиентов есть, чего не хватает (без кухонной базы и сушёных специй)."""
    needed = [i for i in recipe["ingredients"] if not basic(i) and not OPTIONAL.search(i.lower().replace("ё", "е"))]
    used = [i for i in needed if any(covers(h, i) for h in have)]
    missing = [i for i in needed if i not in used]
    still = [i for i in missing if not any(covers(b, i) for b in buy)]
    return used, missing, still


def search_terms(terms, turn):
    """По каким продуктам искать: без приправ и «подачи»; при длинном списке — разное окно на каждый «Подобрать другое меню»."""
    terms = list(dict.fromkeys(terms))
    main = [t for t in terms if searchable(t)] or terms
    if len(main) <= MAX_TERMS:
        return main
    # Мясо/птица/рыба участвуют в поиске всегда — на них держатся обед и ужин
    pinned = [t for t in main if PROTEIN.search(t.lower().replace("ё", "е"))][:MAX_TERMS // 2]
    rest = [t for t in main if t not in pinned]
    size = MAX_TERMS - len(pinned)
    start = (turn * max(1, size // 2)) % len(rest)
    return pinned + (rest[start:] + rest[:start])[:size]


def build_queries(terms, have):
    """Запросы к food.ru: по каждому продукту, по парам и — шире — по мясу/птице/рыбе,
    иначе в выдачу попадают только 2–3 блюда, которые можно приготовить целиком из того, что есть."""
    queries = [(q, 1) for q in terms]
    queries += [(f"{a} {b}", 1) for a, b in zip(terms, terms[1:])][:3]
    starches = [h for h in have if STARCH.search(h.lower().replace("ё", "е")) and searchable(h)][:4]
    for p in [t for t in terms if PROTEIN.search(t.lower().replace("ё", "е"))][:3]:
        queries += [(p, 2), (p, 3), (f"суп {p}", 1), (f"суп {p}", 2), (f"{p} в духовке", 1), (f"{p} на сковороде", 1)]
        queries += [(f"{p} {s}", 1) for s in starches if s.lower() != p.lower()]
    return list(dict.fromkeys(queries))


def candidates(queries):
    """Параллельный поиск по запросам (запрос, страница); возвращает уникальные рецепты."""
    jobs = [workers.submit(cached_search, q, page) for q, page in queries]
    found, failures = {}, 0
    for job in jobs:  # в порядке запросов, а не ответов — иначе порядок блюд «плывёт» между запросами
        try:
            for r in job.result():
                found.setdefault(r["id"], r)
        except (OSError, ValueError, KeyError, TypeError):
            failures += 1
    if queries and failures == len(queries):
        raise OSError("food.ru unavailable")
    return list(found.values())


def sort_into(found, rows, have, buy, allowed, allow_almost=True):
    """Готовые блюда (всё есть) и идеи (не хватает 1–2 продуктов) складываем в found по id."""
    for r in rows:
        if r["id"] in found or not allowed(r) or any(x["name"] == r["name"] for x in found.values()):
            continue
        used, missing, still = analyse(r, have, buy)
        if not [u for u in used if not seasoning(u)]:
            continue  # блюдо должно стоять на продуктах из списка, а не на одном базилике
        if all(group(i) in ("лук", "чеснок") for i in r["ingredients"] if not basic(i)):
            continue  # «Карамелизированный лук» — приправа, а не блюдо
        if not still:
            found[r["id"]] = dict(r, used=used, missing=missing, ready=True, almost=False)
        elif allow_almost and len(still) == 1 and cheap(still[0]):
            # Почти всё есть: не хватает лука/чеснока/моркови — тоже предлагаем, с пометкой «докупить»
            found[r["id"]] = dict(r, used=used, missing=missing, ready=True, almost=True)
        elif len(missing) <= 2:
            found[r["id"]] = dict(r, used=used, missing=missing, ready=False)


def snack_sets(have, allowed_name):
    """Перекусы без готовки из того, что есть: хлебцы с пастой и вареньем, мороженое с ягодами…"""
    by = {}
    for h in have:
        by.setdefault(role(h), []).append(h)
    breads, sweets, fruits = by.get("bread", []), by.get("sweet", []), by.get("fruit", [])
    dairy, cheese, crunch = by.get("dairy", []), by.get("cheese", []), by.get("crunch", [])
    sets = []
    for b in breads:
        nutty = [s for s in sweets if re.search(r"паст|урбеч|нутелл", s.lower())]
        jams = [s for s in sweets if s not in nutty]
        for n in nutty:
            sets += [(b, n, j) for j in jams] or [(b, n)]
        sets += [(b, j) for j in jams] + [(b, c) for c in cheese]
    for d in dairy:
        sets += [(d, f) for f in fruits] + [(d, s) for s in sweets if not re.search(r"паст|урбеч", s.lower())]
    for c in crunch:
        sets += [(c, f) for f in fruits] or [(c,)]
    out = []
    for items in dict.fromkeys(sets):
        name = " + ".join(i.lower() for i in items)
        name = name[0].upper() + name[1:]
        if not allowed_name(name, items):
            continue
        portions = [snack_portion(i) for i in items]
        values = [round(sum(p[1][k] for p in portions), 1) for k in range(4)] if all(portions) else None
        breakfast = role(items[0]) == "bread" or (role(items[0]) == "dairy" and not re.search(r"морожен|пломбир", items[0].lower()))
        steps = (["Намажь " + " и ".join(i.lower() for i in items[1:]) + " на " + items[0].lower() + "."]
                 if role(items[0]) == "bread" else ["Выложи всё в пиалу или на тарелку — готовить ничего не нужно."])
        out.append({"id": "set:" + name, "name": name, "url": None, "combo": True, "breakfast": breakfast,
                    "ingredients": list(items), "used": list(items), "missing": [], "time": None, "portions": 1,
                    "items": [[i, f"≈ {p[0]} г" if p else ""] for i, p in zip(items, portions)], "steps": steps,
                    "values": values})
    return out


def snack_base(r):
    """Основа перекуса — чтобы подряд не шло одно и то же (мороженое, мороженое, мороженое…)."""
    text = r["name"].lower().replace("ё", "е")
    if re.search(r"мороженое\b|мороженым\b|мороженом\b|пломбир", text):
        return "мороженое"
    if r.get("combo"):
        return role(r["ingredients"][0]) or r["ingredients"][0]
    for key, pattern in (("выпечка", r"кекс|маффин|пирог|пирож|печень|вафл|блин|олад|булоч|ватрушк|рулет|торт|печен"),
                         ("ягоды", r"ягод|малин|клубник|смородин|вишн|черник"), ("творог", r"творо|сырник|сырок")):
        if re.search(pattern, text):
            return key
    return "рецепт"


def blend(items, turn=0):
    """Два блюда целиком из своих продуктов, затем одно «докупить лук» — и так по кругу, без повторов.
    Блюд «с докупкой» берём не больше половины от своих, на каждый «Подобрать другое меню» — другие."""
    full = [r for r in items if not r.get("almost")]
    almost = [r for r in items if r.get("almost")]
    if not full or not almost:
        return full + almost
    start = (turn * 3) % len(almost)
    almost = (almost[start:] + almost[:start])[:max(1, (len(full) + 1) // 2)]
    out = []
    while full or almost:
        out += full[:2] + almost[:1]
        full, almost = full[2:], almost[1:]
    return out


def variety(items, turn=0, per_base=3):
    """Чередуем основы по кругу; от каждой — не больше per_base вариантов (свои на каждый «Подобрать другое меню»)."""
    buckets = {}
    for r in items:
        buckets.setdefault(snack_base(r), []).append(r)
    for key, group in buckets.items():
        start = (turn * per_base) % len(group)
        buckets[key] = (group[start:] + group[:start])[:per_base]
    out, queues = [], list(buckets.values())
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out


def serving(slot, recipe, have, index, side=None):
    """Что из «подачи» поставить рядом с блюдом: хлебцы и огурцы к супу, варенье к каше…"""
    if not recipe or recipe.get("combo"):
        return []
    text = (recipe["name"] + " " + " ".join(recipe.get("categories") or [])).lower().replace("ё", "е")
    if SWEET_DISH.search(text) and not SAVORY.search(text):
        wanted = ["sweet", "fruit"]
        if re.search(r"суп|лапш", text):
            wanted = ["fruit"]
    elif slot == "Обед":
        wanted = ["bread", "fresh"]
    elif slot == "Ужин":
        wanted = ["fresh", "sauce"]
    elif slot == "Завтрак":
        wanted = ["bread", "fresh"]
    else:
        wanted = ["fresh"]
    out = []
    for kind in wanted:
        used = recipe["ingredients"] + (side["ingredients"] if side else [])
        options = [h for h in have if role(h) == kind and not any(covers(h, i) for i in used)]
        if options:
            out.append(options[index % len(options)])
    return out


def menu(have, buy, mode, turn, shifts=(0, 0, 0, 0), side_shifts=(0, 0, 0, 0), allow_almost=True):
    state = restrictions()
    whole = [x for x, m in state["excluded"] if m == "all"]
    partial = [x for x, m in state["excluded"] if m == "dish"]
    have = [h for h in have if not any(excludes(h, x) for x in whole)]
    buy = [b for b in buy if not any(excludes(b, x) for x in whole)] if mode == "shop" else []
    if not have:
        return {"menu": [{"slot": s, "recipe": None, "serve": []} for s in SLOTS], "ideas": [], "found": 0}

    def allowed_name(name, ingredients):
        if name in state["blocked"]:
            return False
        if any(dish_of(name, x) for x in partial):
            return False
        return not any(excludes(n, x) for x in whole for n in [name, *ingredients])

    def allowed(r):
        if r["url"] in state["blocked_urls"]:
            return False
        cats = [c.lower() for c in r["categories"]]
        if any(c in SKIP_CATEGORIES or c.startswith(("заготовк", "соус", "напит")) for c in cats):
            return False
        if NOT_A_MEAL.search(r["name"].lower().replace("ё", "е")):
            return False
        return allowed_name(r["name"], r["ingredients"])

    found = {}
    terms = search_terms(have + buy, turn)
    sort_into(found, candidates(build_queries(terms, have + buy)), have, buy, allowed, allow_almost)
    if sum(1 for r in found.values() if r["ready"]) < 8:
        # Мало вариантов — добираем вторую страницу поиска по каждому продукту
        for job in [workers.submit(cached_search, q, 2) for q in terms]:
            try:
                sort_into(found, job.result(), have, buy, allowed, allow_almost)
            except (OSError, ValueError, KeyError, TypeError):
                pass
    ready = [r for r in found.values() if r["ready"]]
    ideas = [r for r in found.values() if not r["ready"]]
    sets = snack_sets(have, allowed_name)

    def order(slot):
        def key(r):
            total, count = r["rating"]
            core = sum(1 for u in r["used"] if not seasoning(u))
            return (-(slot in ("Обед", "Ужин") and protein(r)), -core, len(r["missing"]),
                    -(total / count if count else 0), -count, r["id"])
        return key

    def dish(r):
        # «Морковь на гриле» из одного продукта — только если больше предложить нечего
        return sum(1 for i in r["ingredients"] if not basic(i)) >= 2

    def mix(a, b):
        out = []
        for i in range(max(len(a), len(b))):
            out += a[i:i + 1] + b[i:i + 1]
        return out

    savory = [r for r in ready if slot_for(r) in ("Обед", "Ужин")]
    # Основное блюдо: с курицей/мясом/рыбой; если таких нет — любое не-гарнирное; голый гарнир — в крайнем случае
    mains = sorted([r for r in savory if protein(r)], key=order("Ужин")) \
        or sorted([r for r in savory if not garnish(r) and dish(r)], key=order("Ужин")) \
        or sorted(savory, key=order("Ужин"))
    mains, sides_pool = blend(mains, turn), [r for r in savory if not r.get("almost")]
    sides = sorted([r for r in sides_pool if garnish(r) and not re.search(r"суп|бульон|щи\b|борщ", r["name"].lower())
                    and not (SWEET_DISH.search(r["name"].lower()) and not SAVORY.search(r["name"].lower()))],
                   key=order("Полдник"))
    soups = blend(sorted([r for r in ready if slot_for(r) == "Обед" and not garnish(r)], key=order("Обед")), turn)
    groups = {}
    for s in ("Завтрак", "Полдник"):
        group = sorted([r for r in ready if slot_for(r) == s], key=order(s))
        groups[s] = blend([r for r in group if dish(r)] or group, turn)
    groups["Обед"] = soups or mains
    soup = re.compile(r"суп|борщ|\bщи\b|бульон|солянк|рассольник|\bуха\b|похлебк")
    groups["Ужин"] = [r for r in mains if not soup.search(r["name"].lower())] or mains
    snacks = [r for r in groups["Полдник"]
              if re.search(r"закуск|бутерброд|снек|перекус|сэндвич|брускет|салат",
                           " ".join(r["categories"]).lower() + " " + r["name"].lower())
              or (SWEET_DISH.search(r["name"].lower()) and not STARCH.search(r["name"].lower().replace("пюре из", "")))]
    groups["Полдник"] = variety(sets + snacks, turn)
    groups["Завтрак"] = mix(groups["Завтрак"], [x for x in sets if x["breakfast"]])
    ideas.sort(key=lambda r: (len(r["missing"]), -len(r["used"])))
    near = {s: [r for r in ideas if len(r["missing"]) == 1 and slot_for(r) == s] for s in SLOTS}
    day = int(time.time() // 86400)
    chosen, taken = [], set()
    for i, slot in enumerate(SLOTS):
        # Нет блюда целиком из своих продуктов — берём то, где не хватает одного продукта
        group = [r for r in groups[slot] if r["id"] not in taken] or [r for r in near[slot] if r["id"] not in taken]
        pick = group[(turn + day + shifts[i]) % len(group)] if group else None
        if pick:
            taken.add(pick["id"])
        chosen.append((slot, pick, turn + shifts[i] + i))
    side_for = {}
    for i, (slot, pick, k) in enumerate(chosen):
        if pick and slot in ("Обед", "Ужин") and not pick.get("combo") and needs_side(pick):
            options = [r for r in sides if r["id"] not in taken] or sides
            if options:
                # Свой счётчик у гарнира: «↻» у основного блюда гарнир не трогает и наоборот
                side = options[(zlib.crc32(str(pick["id"]).encode()) + side_shifts[i]) % len(options)]
                taken.add(side["id"])
                side_for[slot] = side
    ideas = [r for r in ideas if r["id"] not in taken]
    ideas = [ideas[(turn * 3 + i) % len(ideas)] for i in range(min(3, len(ideas)))] if ideas else []
    ideas = list({r["id"]: r for r in ideas}.values())

    full = {r["url"]: workers.submit(cached_details, r["url"])
            for r in [p for _, p, _ in chosen if p] + list(side_for.values()) + ideas if r.get("url")}

    def finish(r):
        if r.get("combo"):
            return {k: v for k, v in r.items() if k != "breakfast"}
        out = {"name": r["name"], "url": r["url"], "ingredients": r["ingredients"], "missing": r["missing"],
               "used": r["used"], "time": r["time"], "portions": None, "items": [], "steps": [], "values": None}
        try:
            d = full[r["url"]].result()
            out.update(portions=d["portions"], items=d["items"], steps=d["steps"], values=d["values"])
        except (OSError, ValueError, KeyError, TypeError):
            pass  # показываем хотя бы название, состав и ссылку на оригинал
        return out

    return {"menu": [{"slot": s, "recipe": finish(r) if r else None,
                      "side": finish(side_for[s]) if s in side_for else None,
                      "serve": serving(s, r, have, k, side_for.get(s))} for s, r, k in chosen],
            "ideas": [finish(r) for r in ideas], "found": len(ready) + len(sets)}


# --- HTTP ------------------------------------------------------------------------

class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".webp": "image/webp", ".html": "text/html; charset=utf-8"}

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def list_directory(self, path):
        self.send_error(403)

    def send_json(self, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path not in ("/api/blocked", "/api/excluded"):
            return self.send_error(404)
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return self.send_error(403)
        if self.headers.get_content_type() != "application/json":
            return self.send_error(415)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError()
            body = json.loads(self.rfile.read(size))
            name, block = body["name"], body["block"]
            if not isinstance(name, str) or not 0 < len(name.strip()) <= 500 or type(block) is not bool:
                raise ValueError()
            if self.path == "/api/blocked":
                url = body.get("url", "")
                if not isinstance(url, str) or (url and not re.fullmatch(r"https://food\.ru/recipes/\d+-[\w-]+", url)):
                    raise ValueError()
                set_block(name.strip(), block, url)
            else:
                mode = body.get("mode", "all")
                if mode not in ("all", "dish"):
                    raise ValueError()
                with database() as db:
                    if block:
                        db.execute("INSERT INTO excluded(name, mode) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET mode=?",
                                   (name.strip(), mode, mode))
                    else:
                        db.execute("DELETE FROM excluded WHERE name=?", (name.strip(),))
            self.send_json({"ok": True})
        except (ValueError, TypeError, KeyError):
            self.send_error(400)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/api/recipe-state":
            return self.send_json(snapshot())
        if url.path != "/api/menu":
            return super().do_GET()
        params = parse_qs(url.query)
        clean = lambda values: list(dict.fromkeys(v.strip() for v in values if v.strip()))
        have, buy = clean(params.get("have", [])), clean(params.get("buy", []))
        mode = params.get("mode", ["home"])[0]
        turn = params.get("turn", ["0"])[0]
        shift = params.get("shift", ["0,0,0,0"])[0]
        side_shift = params.get("side_shift", ["0,0,0,0"])[0]
        allow_almost = params.get("almost", ["1"])[0] != "0"
        if not all(re.fullmatch(r"\d{1,6}(?:,\d{1,6}){3}", v) for v in (shift, side_shift)):
            return self.send_error(400)
        if (len(have) + len(buy) > 60 or any(len(v) > 100 for v in have + buy)
                or mode not in ("home", "shop") or not re.fullmatch(r"[0-9]{1,9}", turn)):
            return self.send_error(400)
        if not gate.acquire(blocking=False):
            return self.send_error(503)
        try:
            self.send_json(menu(have, buy, mode, int(turn), tuple(int(x) for x in shift.split(",")),
                                tuple(int(x) for x in side_shift.split(",")), allow_almost))
        except (OSError, ValueError):
            self.send_error(502)
        finally:
            gate.release()


if __name__ == "__main__":
    init_store()
    handler = functools.partial(Handler, directory=str(Path(__file__).resolve().parent / "public"))
    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PORT", "8080"))), handler).serve_forever()
