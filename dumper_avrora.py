import asyncio
import aiohttp
import asyncpg
import aioboto3
import hashlib
import os
import io
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
from PIL import Image
try:
    import pillow_avif  # Реєструє AVIF плагін
except ImportError:
    pass

load_dotenv()

# --- Config ---
PG_URL = os.getenv("DATABASE_URL")
R2_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET = os.getenv("R2_SECRET_KEY")
R2_ENDPOINT = os.getenv("R2_ENDPOINT")
R2_BUCKET = os.getenv("R2_BUCKET")
CDN_URL = os.getenv("CDN_URL")

SHOP_NUMBER = "988"
MAX_BARCODE = 150000
CONCURRENCY = 15
RECENT_SKIP_HOURS = 24

class AvroraDumper:
    def __init__(self):
        self.session = None
        self.db_pool = None
        self.r2_session = aioboto3.Session()
        self.recent_barcodes = set()
        
        # Статистика для батчу
        self.batch_stats = {
            'new': 0,
            'updated': 0,
            'unchanged': 0,
            'batch_start': 0,
            'batch_end': 0
        }

    async def init(self):
        self.session = aiohttp.ClientSession(headers={"User-Agent": "AvroraParser/2.0"})
        self.db_pool = await asyncpg.create_pool(PG_URL)
        
        async with self.db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    barcode TEXT PRIMARY KEY,
                    name TEXT,
                    kt TEXT,
                    price NUMERIC(12,2),
                    price_old NUMERIC(12,2),
                    image_hash TEXT,
                    image_base_url TEXT,
                    last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_products_image_hash ON products(image_hash);
            """)

    async def get_image_data(self, raw_data):
        """Витягує байти картинки з Base64 або URL."""
        if not raw_data or str(raw_data).lower() == "null":
            return None
        try:
            if "data:image" in raw_data:
                import base64
                return base64.b64decode(raw_data.split(",")[-1])
            # Якщо раптом прийде посилання - скачаємо
            async with self.session.get(raw_data, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.read()
        except:
            pass
        return None

    def process_image(self, img_bytes, size, quality):
        """Ресайз в AVIF без метаданих."""
        buf = io.BytesIO()
        with Image.open(io.BytesIO(img_bytes)) as img:
            img = img.convert("RGB")
            img.thumbnail((size, size))
            img.save(buf, format="AVIF", quality=quality, speed=6)
        return buf.getvalue()

    async def upload_to_r2(self, s3_client, barcode, img_bytes, mode):
        """Заливка в Cloudflare R2."""
        key = f"products/{barcode}/{mode}.avif"
        await s3_client.put_object(
            Bucket=R2_BUCKET,
            Key=key,
            Body=img_bytes,
            ContentType="image/avif",
            CacheControl="public, max-age=31536000, immutable"
        )
        return f"{CDN_URL}/{key}"

    async def process_product(self, barcode, s3_client):
        url = f"https://pt.avrora.ua/api/PriceTag/getInfo?barcode={barcode}&shopNumber={SHOP_NUMBER}"
        try:
            async with self.session.get(url, timeout=5) as resp:
                if resp.status != 200: 
                    return None
                data = await resp.json()
                if not data.get('status') or "successfully" not in data.get('message', '').lower():
                    return None

                prod = data.get('data') or data
                name = str(prod.get('name', '')).strip()
                kt = str(prod.get('kt', '')).strip()
                price = float(prod.get('price', 0))
                price_old = float(prod.get('priceOld', 0))
                raw_photo = prod.get('photo') or prod.get('main_photo')

                img_hash, img_base_url = None, None
                img_bytes = await self.get_image_data(raw_photo)

                if img_bytes:
                    img_hash = hashlib.sha256(img_bytes).hexdigest()
                    
                    # Перевіряємо дублікати картинки в базі
                    async with self.db_pool.acquire() as conn:
                        existing = await conn.fetchrow(
                            "SELECT image_base_url FROM products WHERE image_hash = $1 LIMIT 1", 
                            img_hash
                        )
                        if existing:
                            img_base_url = existing['image_base_url']
                        else:
                            # Нова картинка - генеримо AVIF і заливаємо
                            t_bytes = self.process_image(img_bytes, 256, 35)
                            p_bytes = self.process_image(img_bytes, 512, 45)
                            await self.upload_to_r2(s3_client, barcode, t_bytes, "thumb")
                            await self.upload_to_r2(s3_client, barcode, p_bytes, "preview")
                            img_base_url = f"{CDN_URL}/products/{barcode}"

                # Перевіряємо, чи щось змінилось
                async with self.db_pool.acquire() as conn:
                    old = await conn.fetchrow(
                        "SELECT name, kt, price, price_old, image_hash FROM products WHERE barcode = $1",
                        str(barcode)
                    )
                    
                    # Якщо існує і нічого не змінилось — тихо оновлюємо last_checked
                    if old:
                        changed = (
                            old['name'] != name or
                            old['kt'] != kt or
                            old['price'] != price or
                            old['price_old'] != price_old or
                            old['image_hash'] != img_hash
                        )
                        if not changed:
                            await conn.execute(
                                "UPDATE products SET last_checked = CURRENT_TIMESTAMP WHERE barcode = $1",
                                str(barcode)
                            )
                            self.batch_stats['unchanged'] += 1
                            return None  # Нічого не змінилось — не друкуємо
                    
                    # Якщо новий або змінився — робимо UPSERT
                    row = await conn.fetchrow("""
                        INSERT INTO products (barcode, name, kt, price, price_old, image_hash, image_base_url, last_checked)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, CURRENT_TIMESTAMP)
                        ON CONFLICT (barcode) DO UPDATE SET
                            name = EXCLUDED.name,
                            kt = EXCLUDED.kt,
                            price = EXCLUDED.price,
                            price_old = EXCLUDED.price_old,
                            image_hash = COALESCE(EXCLUDED.image_hash, products.image_hash),
                            image_base_url = COALESCE(EXCLUDED.image_base_url, products.image_base_url),
                            last_checked = CURRENT_TIMESTAMP
                        RETURNING (xmax = 0) as is_new
                    """, str(barcode), name, kt, price, price_old, img_hash, img_base_url)

                is_new = row['is_new']
                if is_new:
                    self.batch_stats['new'] += 1
                else:
                    self.batch_stats['updated'] += 1
                
                status = "🆕 NEW" if is_new else "🔄 UPD"
                
                # Друкуємо тільки якщо щось змінилось
                print(json.dumps({
                    "status": status,
                    "barcode": barcode,
                    "name": name[:30],
                    "price": price,
                    "thumb": f"{img_base_url}/thumb.avif" if img_base_url else None
                }, ensure_ascii=False))
                
                return status

        except Exception as e:
            # print(f"Error {barcode}: {e}")
            return None

    def print_batch_summary(self):
        """Друкує підсумок по батчу, якщо не було змін."""
        start = self.batch_stats['batch_start']
        end = self.batch_stats['batch_end']
        new = self.batch_stats['new']
        upd = self.batch_stats['updated']
        unch = self.batch_stats['unchanged']
        
        if new == 0 and upd == 0 and unch > 0:
            print(f"💤 Змін у баркодах {start}-{end} не виявлено ({unch} без змін)")
        elif unch > 0:
            print(f"📊 Батч {start}-{end}: 🆕 {new} | 🔄 {upd} | 💤 {unch}")
        
        # Скидаємо статистику
        self.batch_stats = {'new': 0, 'updated': 0, 'unchanged': 0, 'batch_start': 0, 'batch_end': 0}

    async def run(self):
        await self.init()
        
        print("\n📦 АВРОРА ДАМПЕР 2.0 (AVIF + R2 Edition)")
        print("1. Повний перебор (почати з 1-го баркоду)")
        print("2. Резюмувати (продовжити з останнього місця)")
        choice = input("> ").strip()

        async with self.db_pool.acquire() as conn:
            if choice == "2":
                last_done = await conn.fetchval(
                    "SELECT MAX(barcode::int) FROM products WHERE last_checked >= NOW() - ($1 * INTERVAL '1 hour') AND barcode ~ '^[0-9]+$'",
                    RECENT_SKIP_HOURS
                )
                start_i = (last_done or 0) + 1
                
                # Завантажуємо список нещодавно перевірених, щоб скіпати дирки
                rows = await conn.fetch(
                    "SELECT barcode FROM products WHERE last_checked >= NOW() - ($1 * INTERVAL '1 hour')",
                    RECENT_SKIP_HOURS
                )
                self.recent_barcodes = {str(r['barcode']) for r in rows}
                print(f"⏩ Резюмуємо з {start_i}. Знайдено {len(self.recent_barcodes)} актуальних баркодів.")
            else:
                start_i = 1
                self.recent_barcodes = set()
                print("🔄 Починаємо повний перебор з 1-го баркоду...")

        print(f"🚀 Запуск! Concurrency: {CONCURRENCY}\n")

        async with self.r2_session.client(
            's3', endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_KEY,
            aws_secret_access_key=R2_SECRET
        ) as s3:
            for i in range(start_i, MAX_BARCODE + 1, CONCURRENCY):
                batch = [bc for bc in range(i, min(i + CONCURRENCY, MAX_BARCODE + 1)) 
                         if str(bc) not in self.recent_barcodes]
                if not batch: 
                    continue
                
                # Запам'ятовуємо межі батчу
                self.batch_stats['batch_start'] = batch[0]
                self.batch_stats['batch_end'] = batch[-1]
                
                # Обробляємо батч
                await asyncio.gather(*(self.process_product(bc, s3) for bc in batch))
                
                # Друкуємо підсумок, якщо не було змін
                self.print_batch_summary()
                
                await asyncio.sleep(0.5)  # Anti-rate-limit

    async def close(self):
        if self.session: 
            await self.session.close()
        if self.db_pool: 
            await self.db_pool.close()

if __name__ == "__main__":
    dumper = AvroraDumper()
    try:
        asyncio.run(dumper.run())
    except KeyboardInterrupt:
        print("\n\n⚠️ Зупинено користувачем")
    finally:
        asyncio.run(dumper.close())