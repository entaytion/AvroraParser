import requests
import json
import os
import time
import asyncio
import aiohttp
from colorama import init, Fore, Style
import base64
from PIL import Image
import pytesseract
import io
from playwright.sync_api import sync_playwright
from playwright.async_api import async_playwright
import re
import sys

CONFIG_FILE = "config.json"
REMOVED_FILE = "removed_products.txt"

init(autoreset=True)

removed_ids_set = set()

def get_stores():
    url = "https://avrora.ua/index.php?dispatch=pwa.store_locations&is_ajax=1"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    stores_dict = data.get("objects", {})
    stores = list(stores_dict.values())
    return stores


def filter_stores(stores, query):
    query = query.lower()
    filtered = []
    for store in stores:
        if not isinstance(store, dict):
            continue
        city = store.get("city", "").lower()
        if query in city:
            filtered.append(store)
    return filtered


def extract_english_words(text):
    return ' '.join(re.findall(r'[A-Za-z0-9]+', text))


def parse_kt_filter(kt_input):
    if kt_input.strip().lower() == 'all':
        return None  # None означає всі категорії
    return set(x.strip() for x in kt_input.split(',') if x.strip())


def search_product(shop_number, kt_filter=None):
    config = load_config() or {}
    show_removed_message = config.get("show_removed_message", True)
    while True:
        barcode = input("\nВведіть баркод товару (або 'exit' для виходу): ").strip()
        if barcode.lower() == 'exit':
            print("Вихід з пошуку товарів.")
            break
        if not barcode.isdigit():
            print("Баркод має містити лише цифри!")
            continue
        url = f"https://pt.avrora.ua/api/PriceTag/getInfo?barcode={barcode}&shopNumber={shop_number}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        try:
            resp = requests.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            # Якщо відповідь містить 'name' (або 'code'), це і є товар
            if isinstance(data, dict) and (data.get('name') or data.get('code')):
                product = data
            elif data.get('success') and data.get('data'):
                product = data['data']
            else:
                print("Товар не знайдено або немає даних.")
                print("Відповідь API:", data)
                continue
            # --- KT фільтр ---
            if kt_filter is not None:
                kt_val = str(product.get('kt', '')).strip()
                if kt_val not in kt_filter:
                    continue
            # Перевірка "Тільки на сайті" через Playwright
            eng_name = extract_english_words(product.get('name', ''))
            if eng_name and is_online_only_product(eng_name):
                print(Fore.YELLOW + f"Товар '{product.get('name', '')}' доступний тільки на сайті!")
                continue
            # Діагностика: вивід поля photo
            img_base64 = product.get('photo')
            if img_base64:
                print("[photo є, пробую розпізнати текст...]")
                text = recognize_text_from_base64(img_base64)
                print(f"[Розпізнаний текст з фото]: {text}")
                if "зняли з продажу" in text.lower():
                    save_removed_id(product.get('code', barcode))
                    if show_removed_message:
                        print(Fore.YELLOW + f"Товар {product.get('name', 'Немає назви')} (код: {product.get('code', barcode)}) знятий з продажу!")
                    continue
            print(f"\nНазва: {product.get('name', 'Немає даних')}")
            print(f"Ціна: {product.get('price', 'Немає даних')}")
            print(f"Баркод: {product.get('code', 'Немає даних')}")
            print(f"Кількість: {product.get('remQty', 'Немає даних')}")
            print(f"Опис: {product.get('description', 'Немає даних')}")
        except Exception as e:
            print(f"Помилка при запиті: {e}")


def save_config(city, shop_number, shop_name, show_removed_message=True, kt_filter_str='all'):
    config = {
        "city": city,
        "shop_number": shop_number,
        "shop_name": shop_name,
        "show_removed_message": show_removed_message,
        "kt_filter": kt_filter_str
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def check_discounts(shop_number, start=1, end=150000):
    print(f"\nПочинаю перевірку знижок у магазині {shop_number} (баркоди {start}-{end})...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    found = 0
    for barcode in range(start, end + 1):
        url = f"https://pt.avrora.ua/api/PriceTag/getInfo?barcode={barcode}&shopNumber={shop_number}"
        try:
            resp = requests.get(url, headers=headers, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            # Обробка обох типів відповіді
            if isinstance(data, dict) and (data.get('name') or data.get('code')):
                product = data
            elif data.get('success') and data.get('data'):
                product = data['data']
            else:
                continue
            remqty = float(product.get('remQty', 0) or 0)
            price = float(product.get('price', 0) or 0)
            priceold = float(product.get('priceOld', 0) or 0)
            if remqty >= 1 and priceold > price:
                found += 1
                print(f"\nЗНИЖКА! {product.get('name', 'Немає назви')}")
                print(f"Ціна: {price} (Стара: {priceold}) | Баркод: {product.get('code', '-')}")
                print(f"Залишок: {remqty}")
        except Exception:
            continue
        time.sleep(0.05)  # невелика затримка, щоб не забанили
    print(f"\nПеревірка завершена. Знайдено {found} товарів зі знижкою та залишком >= 1.")


async def fetch_product(session, url):
    try:
        async with session.get(url, timeout=5) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception:
        return None

# Функція для розпізнавання тексту з base64-зображення
def recognize_text_from_base64(base64_str):
    try:
        if base64_str.startswith('data:image'):
            base64_str = base64_str.split(',')[1]
        image_data = base64.b64decode(base64_str)
        image = Image.open(io.BytesIO(image_data))
        # Зберігаємо зображення для ручної перевірки
        image.save('temp_photo.png')
        # Покращуємо контрастність: переводимо у чорно-біле
        gray = image.convert('L')
        bw = gray.point(lambda x: 0 if x < 180 else 255, '1')
        bw.save('temp_photo_bw.png')
        text = pytesseract.image_to_string(bw, lang='ukr+eng')
        return text
    except Exception as e:
        print(f"[Помилка OCR]: {e}")
        return ""

def load_removed_ids():
    if os.path.exists(REMOVED_FILE):
        with open(REMOVED_FILE, "r", encoding="utf-8") as f:
            for line in f:
                removed_ids_set.add(line.strip())

def save_removed_id(product_id):
    product_id = str(product_id)
    if product_id in removed_ids_set:
        return
    removed_ids_set.add(product_id)
    with open(REMOVED_FILE, "a", encoding="utf-8") as f:
        f.write(product_id + "\n")

async def process_barcode(session, shop_number, barcode, results, kt_filter=None):
    config = load_config() or {}
    show_removed_message = config.get("show_removed_message", True)
    url = f"https://pt.avrora.ua/api/PriceTag/getInfo?barcode={barcode}&shopNumber={shop_number}"
    print(Fore.LIGHTBLACK_EX + f"Перевіряю баркод: {barcode}", end='\r')
    data = await fetch_product(session, url)
    if not data:
        return
    # Обробка обох типів відповіді
    if isinstance(data, dict) and (data.get('name') or data.get('code')):
        product = data
    elif data.get('success') and data.get('data'):
        product = data['data']
    else:
        return
    # --- KT фільтр ---
    if kt_filter is not None:
        kt_val = str(product.get('kt', '')).strip()
        if kt_val not in kt_filter:
            return
    # Перевірка наявності зображення з base64
    img_base64 = product.get('photo')
    if img_base64:
        text = recognize_text_from_base64(img_base64)
        if "зняли з продажу" in text.lower():
            save_removed_id(product.get('code', barcode))
            if show_removed_message:
                print(Fore.YELLOW + f"\nТовар {product.get('name', 'Немає назви')} (код: {product.get('code', barcode)}) знятий з продажу!")
            return
    remqty = float(product.get('remQty', 0) or 0)
    price = float(product.get('price', 0) or 0)
    priceold = float(product.get('priceOld', 0) or 0)
    # Якщо є знижка, але remqty == 0, перевіряємо "Тільки на сайті"
    if priceold > price and remqty == 0:
        eng_name = extract_english_words(product.get('name', ''))
        if eng_name and await is_online_only_product(eng_name):
            print(Fore.YELLOW + f"\nТовар '{product.get('name', '')}' зі знижкою, але доступний тільки на сайті!")
            return
    if remqty >= 1 and priceold > price:
        results.append({
            'name': product.get('name', 'Немає назви'),
            'price': price,
            'priceOld': priceold,
            'code': product.get('code', '-'),
            'remQty': remqty
        })
        print(Fore.GREEN + f"\nЗНИЖКА! {product.get('name', 'Немає назви')}")
        print(Fore.GREEN + f"Ціна: {price} (Стара: {priceold}) | Баркод: {product.get('code', '-')}")
        print(Fore.GREEN + f"Залишок: {remqty}")
    elif remqty >= 1:
        print(Fore.RED + f"\nБез знижки: {product.get('name', 'Немає назви')} | Ціна: {price} | Баркод: {product.get('code', '-')}")

async def check_discounts_async(shop_number, start=1, end=150000, workers=15, kt_filter=None):
    print(f"\n[ASYNC] Починаю перевірку знижок у магазині {shop_number} (баркоди {start}-{end}) у {workers} потоків...")
    connector = aiohttp.TCPConnector(limit=workers)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    results = []
    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        sem = asyncio.Semaphore(workers)
        async def sem_task(barcode):
            async with sem:
                await process_barcode(session, shop_number, barcode, results, kt_filter=kt_filter)
        tasks = [sem_task(barcode) for barcode in range(start, end + 1)]
        for i in range(0, len(tasks), 500):
            await asyncio.gather(*tasks[i:i+500])  # по 500 задач за раз, щоб не перевантажити пам'ять
    print(f"\nПеревірка завершена. Знайдено {len(results)} товарів зі знижкою та залишком >= 1.")
    for product in results:
        print(f"\nЗНИЖКА! {product['name']}")
        print(f"Ціна: {product['price']} (Стара: {product['priceOld']}) | Баркод: {product['code']}")
        print(f"Залишок: {product['remQty']}")


async def is_online_only_product(product_name):
    search_url = f"https://avrora.ua/?subcats=Y&status=A&pshort=Y&pfull=Y&pname=Y&pkeywords=Y&pcode_from_q=Y&search_performed=Y&q={product_name}&dispatch=products.search"
    print(f"[Playwright] Відкриваю: {search_url}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(search_url, timeout=30000)
        # Спочатку шукаємо бейдж
        badge = await page.query_selector('span.labels-item.variant_3520')
        if badge:
            text = await badge.inner_text()
            if 'Тільки на сайті' in text:
                await browser.close()
                return True
        # Якщо бейдж не знайдено, чекаємо на результати пошуку
        try:
            await page.wait_for_selector('div.ty-product-list__item', timeout=10000)
        except Exception as e:
            print(f"[Playwright] Не знайдено селектор 'div.ty-product-list__item'. Припускаю, що товару нема.")
            await browser.close()
            return False
        await browser.close()
        return False

def clear_terminal():
    os.system('cls' if os.name == 'nt' else 'clear')

def main():
    load_removed_ids()
    config = load_config()
    shop_number = None
    shop_name = None
    show_removed_message = False
    kt_filter_str = 'all'
    if config:
        shop_number = config.get('shop_number')
        shop_name = config.get('shop_name')
        show_removed_message = config.get('show_removed_message', False)
        kt_filter_str = config.get('kt_filter', 'all')
    if not shop_number or not shop_name:
        print("Завантажую список магазинів...")
        stores = get_stores()
        print(f"Знайдено {len(stores)} магазинів у базі.")
        city = input("Введіть місто для пошуку: ").strip()
        filtered = filter_stores(stores, city)
        print(f"\nЗнайдено {len(filtered)} магазинів у місті '{city}':")
        for idx, store in enumerate(filtered, 1):
            print(f"{idx}. ID: {store.get('shopNumber')} | Адреса: {store.get('name')}")
        if not filtered:
            print("Магазинів не знайдено.")
            return
        while True:
            try:
                selected = int(input("\nВведіть номер магазину зі списку для вибору: "))
                if 1 <= selected <= len(filtered):
                    chosen_store = filtered[selected - 1]
                    break
                else:
                    print(f"Введіть число від 1 до {len(filtered)}")
            except ValueError:
                print("Введіть коректний номер!")
        shop_number = chosen_store.get('shopNumber')
        shop_name = chosen_store.get('name')
        save_config(city, shop_number, shop_name, show_removed_message=show_removed_message, kt_filter_str=kt_filter_str)
        print(f"\nВи обрали магазин: {shop_name} (ID: {shop_number})")
    # --- Меню режимів з налаштуваннями ---
    while True:
        check = Fore.GREEN + '[✓]' if show_removed_message else '[ ]'
        kt_filter = parse_kt_filter(kt_filter_str)
        print(f"\nПоточна адреса: {shop_name} (ID: {shop_number})")
        print(f"KT-фільтр: [{kt_filter_str}]")
        print("Оберіть режим роботи:")
        print("1. Налаштування")
        print("2. Пошук товару по баркоду")
        print("3. Перевірити всі знижки (баркоди 1-150000, повільно)")
        print("4. Перевірити всі знижки (баркоди 1-150000, 15 потоків, швидко)")
        print("Введіть номер режиму:")
        mode = input().strip()
        if mode == '1':
            # Меню налаштувань
            while True:
                clear_terminal()
                check = Fore.GREEN + '[✓]' if show_removed_message else '[ ]'
                print(f"=== Налаштування ===\nПоточна адреса: {shop_name} (ID: {shop_number})")
                print(f"1. Виводити повідомлення про зняті з продажу товари: {check}")
                print("2. Змінити адресу (місто, магазин)")
                print(f"3. Змінити KT-фільтр (зараз: [{kt_filter_str}])")
                print("b. Назад")
                print("Введіть 1 для перемикання, 2 для зміни адреси, 3 для KT або 'b'/Enter для повернення:")
                ch = input().strip().lower()
                if ch == '1':
                    show_removed_message = not show_removed_message
                    config = load_config() or {}
                    save_config(config.get('city', ''), config.get('shop_number', ''), config.get('shop_name', ''), show_removed_message=show_removed_message, kt_filter_str=kt_filter_str)
                elif ch == '2':
                    print("Завантажую список магазинів...")
                    stores = get_stores()
                    print(f"Знайдено {len(stores)} магазинів у базі.")
                    city = input("Введіть місто для пошуку: ").strip()
                    filtered = filter_stores(stores, city)
                    print(f"\nЗнайдено {len(filtered)} магазинів у місті '{city}':")
                    for idx, store in enumerate(filtered, 1):
                        print(f"{idx}. ID: {store.get('shopNumber')} | Адреса: {store.get('name')}")
                    if not filtered:
                        print("Магазинів не знайдено.")
                        continue
                    while True:
                        try:
                            selected = int(input("\nВведіть номер магазину зі списку для вибору: "))
                            if 1 <= selected <= len(filtered):
                                chosen_store = filtered[selected - 1]
                                break
                            else:
                                print(f"Введіть число від 1 до {len(filtered)}")
                        except ValueError:
                            print("Введіть коректний номер!")
                    shop_number = chosen_store.get('shopNumber')
                    shop_name = chosen_store.get('name')
                    save_config(city, shop_number, shop_name, show_removed_message=show_removed_message, kt_filter_str=kt_filter_str)
                    print(f"\nВи обрали магазин: {shop_name} (ID: {shop_number})")
                elif ch == '3':
                    print("Введіть KT через кому (наприклад: 2,3,4) або all для всіх категорій:")
                    kt_filter_str = input().strip() or 'all'
                    save_config(shop_name, shop_number, shop_name, show_removed_message=show_removed_message, kt_filter_str=kt_filter_str)
                elif ch == 'b' or ch == '':
                    clear_terminal()
                    break
        elif mode == '2':
            search_product(shop_number, kt_filter=kt_filter)
            break
        elif mode == '3':
            check_discounts(shop_number)
            break
        elif mode == '4':
            asyncio.run(check_discounts_async(shop_number, kt_filter=kt_filter))
            break
        else:
            print("Введіть коректний номер режиму!")


if __name__ == "__main__":
    main() 