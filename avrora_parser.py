"""
Avrora Parser — швидкий CLI-інструмент для пошуку знижок.
Без Playwright, без OCR, без зайвої х...ні.
"""

import asyncio
import aiohttp
import requests
import json
import os
import re
import sys
from selectolax.parser import HTMLParser

CONFIG_FILE = "config.json"

# ── Кольори (ANSI, без colorama) ─────────────────────────────────────────────
class C:
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    DIM = "\033[90m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_config(**kwargs):
    config = load_config()
    config.update(kwargs)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

# ── Stores ────────────────────────────────────────────────────────────────────

def get_stores():
    url = "https://avrora.ua/index.php?dispatch=pwa.store_locations&is_ajax=1"
    resp = requests.get(url, headers={"User-Agent": UA})
    resp.raise_for_status()
    objects = resp.json().get("objects", [])
    if isinstance(objects, dict):
        return list(objects.values())
    return list(objects) if isinstance(objects, list) else []

def filter_stores(stores, query):
    q = query.lower()
    return [s for s in stores if isinstance(s, dict) and q in s.get("city", "").lower()]

# ── API parse helper ─────────────────────────────────────────────────────────

def extract_product(data):
    """Витягує product dict з будь-якого формату відповіді API."""
    if isinstance(data, dict):
        if data.get("name") or data.get("code"):
            return data
        if data.get("data") and isinstance(data["data"], dict):
            return data["data"]
    return None

def parse_kt_filter(kt_input):
    if kt_input.strip().lower() == "all":
        return None
    return set(x.strip() for x in kt_input.split(",") if x.strip())

# ── Online-only check (selectolax замість Playwright) ─────────────────────────

def is_online_only(product_name: str) -> bool:
    """Перевіряє 'Тільки на сайті' через HTTP + selectolax. Без браузера."""
    eng = " ".join(re.findall(r"[A-Za-z0-9]+", product_name))
    if not eng:
        return False
    try:
        url = f"https://avrora.ua/?q={requests.utils.quote(eng)}&dispatch=products.search"
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=10)
        tree = HTMLParser(resp.text)
        badge = tree.css_first("span.labels-item.variant_3520")
        if badge and "Тільки на сайті" in badge.text():
            return True
    except Exception:
        pass
    return False

# ── Single product search ────────────────────────────────────────────────────

def search_product(shop_number, kt_filter=None):
    while True:
        barcode = input("\nВведіть баркод товару (або 'exit'): ").strip()
        if barcode.lower() == "exit":
            break
        if not barcode.isdigit():
            print("Баркод має містити лише цифри!")
            continue

        url = f"https://pt.avrora.ua/api/PriceTag/getInfo?barcode={barcode}&shopNumber={shop_number}"
        try:
            resp = requests.get(url, headers={"User-Agent": UA}, timeout=10)
            resp.raise_for_status()
            product = extract_product(resp.json())
            if not product:
                print("Товар не знайдено.")
                continue

            # KT фільтр
            if kt_filter and str(product.get("kt", "")).strip() not in kt_filter:
                print(f"{C.DIM}Пропущено (KT не підходить){C.RESET}")
                continue

            # Перевірка "тільки на сайті"
            remqty = float(product.get("remQty", 0) or 0)
            if remqty == 0 and is_online_only(product.get("name", "")):
                print(f"{C.YELLOW}Товар доступний тільки на сайті!{C.RESET}")
                continue

            price = float(product.get("price", 0) or 0)
            price_old = float(product.get("priceOld", 0) or 0)
            discount = ""
            if price_old > price and price_old > 0:
                pct = int((1 - price / price_old) * 100)
                discount = f" {C.GREEN}(-{pct}%, було {price_old}){C.RESET}"

            print(f"\n{C.BOLD}{product.get('name', '?')}{C.RESET}")
            print(f"  Ціна: {price}{discount}")
            print(f"  Артикул: {product.get('code', '-')}")
            print(f"  Залишок: {remqty}")
            print(f"  KT: {product.get('kt', '-')}")

        except Exception as e:
            print(f"Помилка: {e}")

# ── Async discount scanner ───────────────────────────────────────────────────

async def fetch_json(session, url):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                return await resp.json()
            if resp.status == 429:
                return "LIMIT"
    except Exception:
        pass
    return None

async def scan_discounts(shop_number, start=1, end=150000, workers=15, kt_filter=None):
    print(f"\n{C.BOLD}[SCAN] Баркоди {start}–{end}, {workers} потоків{C.RESET}")

    connector = aiohttp.TCPConnector(limit=workers)
    found = []
    checked = 0
    skipped_online = 0

    async with aiohttp.ClientSession(
        connector=connector,
        headers={"User-Agent": UA}
    ) as session:
        sem = asyncio.Semaphore(workers)

        async def process(barcode):
            nonlocal checked, skipped_online
            async with sem:
                url = f"https://pt.avrora.ua/api/PriceTag/getInfo?barcode={barcode}&shopNumber={shop_number}"
                data = await fetch_json(session, url)

                if data == "LIMIT":
                    print(f"{C.YELLOW}⚠️ Rate limit, чекаю 5с...{C.RESET}")
                    await asyncio.sleep(5)
                    return

                if not data:
                    return

                product = extract_product(data)
                if not product:
                    return

                checked += 1

                # KT фільтр
                if kt_filter and str(product.get("kt", "")).strip() not in kt_filter:
                    return

                remqty = float(product.get("remQty", 0) or 0)
                price = float(product.get("price", 0) or 0)
                price_old = float(product.get("priceOld", 0) or 0)

                if remqty < 1 or price_old <= price:
                    return

                pct = int((1 - price / price_old) * 100)
                found.append({
                    "name": product.get("name", "?"),
                    "price": price,
                    "priceOld": price_old,
                    "code": product.get("code", "-"),
                    "remQty": remqty,
                    "discount": pct,
                })
                print(f"{C.GREEN}🔥 -{pct}% {product.get('name', '?')} | {price} (було {price_old}) | Залишок: {remqty}{C.RESET}")

            # Прогрес кожні 1000
            if checked % 1000 == 0:
                print(f"{C.DIM}  ...перевірено {checked} товарів, знайдено {len(found)} знижок{C.RESET}")

        # Батчами по 500
        barcodes = list(range(start, end + 1))
        for i in range(0, len(barcodes), 500):
            batch = barcodes[i:i+500]
            await asyncio.gather(*[process(bc) for bc in batch])

    print(f"\n{C.BOLD}═══ Результат ═══{C.RESET}")
    print(f"Перевірено: {checked} | Знижок: {len(found)}")

    if found:
        found.sort(key=lambda x: x["discount"], reverse=True)
        print(f"\n{C.BOLD}Топ знижки:{C.RESET}")
        for p in found[:20]:
            print(f"  {C.GREEN}-{p['discount']}%{C.RESET} {p['name']} | {p['price']} грн (було {p['priceOld']}) | Код: {p['code']}")

# ── Menu ──────────────────────────────────────────────────────────────────────

def clear():
    os.system("cls" if os.name == "nt" else "clear")

def select_store():
    print("Завантажую список магазинів...")
    stores = get_stores()
    print(f"Знайдено {len(stores)} магазинів.")

    city = input("Введіть місто: ").strip()
    filtered = filter_stores(stores, city)
    if not filtered:
        print("Магазинів не знайдено.")
        return None, None, None

    print(f"\nМагазини в '{city}':")
    for i, s in enumerate(filtered, 1):
        print(f"  {i}. {s.get('shopNumber')} — {s.get('name')}")

    while True:
        try:
            n = int(input("\nОберіть номер: "))
            if 1 <= n <= len(filtered):
                store = filtered[n - 1]
                return city, store.get("shopNumber"), store.get("name")
        except ValueError:
            pass
        print("Спробуйте ще раз.")

def main():
    config = load_config()
    shop_number = config.get("shop_number")
    shop_name = config.get("shop_name")
    kt_filter_str = config.get("kt_filter", "all")

    if not shop_number:
        city, shop_number, shop_name = select_store()
        if not shop_number:
            return
        save_config(city=city, shop_number=shop_number, shop_name=shop_name, kt_filter=kt_filter_str)

    while True:
        kt_filter = parse_kt_filter(kt_filter_str)
        print(f"\n{C.BOLD}═══ Аврора Парсер ═══{C.RESET}")
        print(f"Магазин: {shop_name} ({shop_number})")
        print(f"KT-фільтр: [{kt_filter_str}]")
        print()
        print("1. Пошук товару по баркоду")
        print("2. Сканувати знижки (async, швидко)")
        print("3. Змінити магазин")
        print(f"4. Змінити KT-фільтр")
        print("0. Вихід")

        mode = input("\n> ").strip()

        if mode == "1":
            search_product(shop_number, kt_filter=kt_filter)
        elif mode == "2":
            asyncio.run(scan_discounts(shop_number, kt_filter=kt_filter))
        elif mode == "3":
            city, sn, sname = select_store()
            if sn:
                shop_number, shop_name = sn, sname
                save_config(city=city, shop_number=shop_number, shop_name=shop_name, kt_filter=kt_filter_str)
        elif mode == "4":
            print("Введіть KT через кому або 'all':")
            kt_filter_str = input().strip() or "all"
            save_config(kt_filter=kt_filter_str)
        elif mode == "0":
            break
        else:
            print("Хуйня, спробуй ще раз.")


if __name__ == "__main__":
    main()