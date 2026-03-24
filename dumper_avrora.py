import asyncio
import aiohttp
import psycopg2
from psycopg2.extras import execute_values
import os
from dotenv import load_dotenv

load_dotenv()

PG_URL = os.getenv("DATABASE_URL")
SHOP_NUMBER = "988"
MAX_BARCODE = 150000

current_concurrency = 8
current_delay = 1.2

async def fetch_product(session, barcode):
    url = f"https://pt.avrora.ua/api/PriceTag/getInfo?barcode={barcode}&shopNumber={SHOP_NUMBER}"
    try:
        async with session.get(url, timeout=5) as resp:
            if resp.status == 429: return "LIMIT"
            if resp.status != 200: return None
            data = await resp.json()
            if not data.get('status') or "successfully" not in data.get('message', '').lower():
                return None
            
            # Compress photo if exists
            photo_b64 = None
            raw_photo = data.get('data', {}).get('photo') or data.get('photo')
            if raw_photo and "null" not in str(raw_photo).lower():
                try:
                    import base64
                    from io import BytesIO
                    from PIL import Image
                    rawb = raw_photo.split(",")[-1] if "," in raw_photo else raw_photo
                    img_data = base64.b64decode(rawb)
                    img = Image.open(BytesIO(img_data)).convert("RGB")
                    img.thumbnail((100, 100))
                    out = BytesIO()
                    img.save(out, format="WEBP", quality=60)
                    photo_b64 = base64.b64encode(out.getvalue()).decode("utf-8")
                except Exception as e:
                    pass

            return (
                str(barcode), 
                data.get('data', {}).get('name') or data.get('name'), 
                data.get('data', {}).get('kt') or data.get('kt'), 
                float(data.get('data', {}).get('price') or data.get('price', 0)), 
                float(data.get('data', {}).get('priceOld') or data.get('priceOld', 0)),
                photo_b64
            )
    except:
        return None

async def init_db(conn):
    cur = conn.cursor()
    cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS photo_thumb TEXT;")
    conn.commit()
    cur.close()

async def run_dump():
    global current_concurrency, current_delay
    print(f"📡 Запуск адаптивного дампера з компресією фотографій...")
    
    if not PG_URL:
        print("❌ DATABASE_URL не встановлено!")
        return

    conn = psycopg2.connect(PG_URL)
    await init_db(conn)
    cur = conn.cursor()

    async with aiohttp.ClientSession() as session:
        i = 1
        while i <= MAX_BARCODE:
            tasks = [fetch_product(session, bc) for bc in range(i, min(i + current_concurrency, MAX_BARCODE + 1))]
            results = await asyncio.gather(*tasks)
            
            if "LIMIT" in results:
                print("⚠️ Помилка, рейтліміт, ми почекаємо...")
                current_concurrency = 5
                current_delay = 2.5
                await asyncio.sleep(5)
                continue
            
            valid_results = [r for r in results if r and r != "LIMIT"]
            if valid_results:
                query = """
                    INSERT INTO products (barcode, name, kt, price, price_old, photo_thumb)
                    VALUES %s
                    ON CONFLICT (barcode) DO UPDATE SET
                        name = EXCLUDED.name, 
                        price = EXCLUDED.price, 
                        price_old = EXCLUDED.price_old,
                        photo_thumb = COALESCE(EXCLUDED.photo_thumb, products.photo_thumb),
                        last_checked = CURRENT_TIMESTAMP
                    WHERE products.price IS DISTINCT FROM EXCLUDED.price 
                       OR products.price_old IS DISTINCT FROM EXCLUDED.price_old
                       OR products.name IS DISTINCT FROM EXCLUDED.name
                       OR (EXCLUDED.photo_thumb IS NOT NULL AND products.photo_thumb IS DISTINCT FROM EXCLUDED.photo_thumb)
                    RETURNING barcode, name, price, price_old, (xmax = 0) AS is_new
                """
                
                try:
                    cur.execute("BEGIN;")
                    execute_values(cur, query, valid_results)
                    changes = cur.fetchall()
                    conn.commit()

                    if changes:
                        print(f"🔥 Зміни та нові товари (батч {i}):")
                        for barcode, name, price, price_old, is_new in changes:
                            label = "[НОВИЙ]" if is_new else "[ЗМІНА]"
                            print(f"   {label} Артикул: {barcode} | {name[:40]}... | Ціна: {price} (Минула: {price_old})")
                    
                    print(f"✅ Все окей, йдемо далі. (Баркод {i}, швидкість {current_concurrency})")
                    current_concurrency = min(10, current_concurrency + 1)
                    current_delay = max(1.0, current_delay - 0.1)
                except Exception as e:
                    conn.rollback()
                    print(f"❌ Помилка БД: {e}")
            else:
                print(f"🧬 Баркод {i}: товарів не знайдено.")

            i += current_concurrency
            await asyncio.sleep(current_delay)

    cur.close()
    conn.close()
    print("✅ Все, дамп закінчено.")

if __name__ == "__main__":
    asyncio.run(run_dump())
