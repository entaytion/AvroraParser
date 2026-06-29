"""
Копійочка Дампер V3 — Obscura (anti-detection) + aiohttp (AJAX) + PostgreSQL + R2
Замінює scrapling на Obscura CLI для кращого обходу Wordfence.
"""

import asyncio
import hashlib
import io
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
import asyncpg
import aioboto3
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

# ====================== CONFIG ======================
PG_URL = os.getenv("DATABASE_URL")
R2_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET = os.getenv("R2_SECRET_KEY")
R2_ENDPOINT = os.getenv("R2_ENDPOINT")
R2_BUCKET = os.getenv("R2_BUCKET")
CDN_URL = os.getenv("CDN_URL")

CITY_NAME = "Хмельницький"
CONCURRENCY = 2
RECENT_SKIP_HOURS = 24
MAX_RETRIES = 5
RETRY_BASE_DELAY = 2.0
OBSCURA_TIMEOUT = 20
REQUEST_DELAY = (1.5, 3.0)  # random delay between requests (min, max seconds)

AJAX_URL = "https://www.kopiyochka.ua/user-pannel/admin-ajax.php"
SITEMAP_INDEX = "https://www.kopiyochka.ua/sitemap_index.xml"
BASE_URL = "https://www.kopiyochka.ua"

# Path to obscura binary — check common locations
def find_obscura():
    candidates = [
        Path(__file__).parent / "obscura" / "obscura.exe",
        Path(__file__).parent / "obscura" / "obscura",
        Path("obscura.exe"),
        Path("obscura"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    # Try PATH
    import shutil as _shutil
    found = _shutil.which("obscura")
    if found:
        return found
    return None

OBSCURA_BIN = find_obscura()

# ====================== LOGGING ======================
logging.basicConfig(
    level=logging.WARNING,
    format="%(message)s",
)
log = logging.getLogger("kopiyochka")


def is_access_limited(status_code: int, body_text: str) -> bool:
    if status_code in (429, 503):
        return True
    lowered = (body_text or "").lower()
    return any(x in lowered for x in [
        "your access to this site has been limited",
        "wordfence",
        "temporarily limited for security reasons",
    ])


# ====================== SQL ======================

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS kopiyochka_products (
    product_code TEXT PRIMARY KEY,
    title TEXT,
    url TEXT,
    price NUMERIC(12,2),
    price_old NUMERIC(12,2),
    stock INTEGER DEFAULT 0,
    category TEXT,
    image_url TEXT,
    image_hash TEXT,
    image_base_url TEXT,
    city_name TEXT,
    last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_kopi_image_hash ON kopiyochka_products(image_hash);
CREATE INDEX IF NOT EXISTS idx_kopi_city ON kopiyochka_products(city_name);
"""

UPSERT_SQL = """
INSERT INTO kopiyochka_products (
    product_code, title, url, price, price_old, stock,
    category, image_url, image_hash, image_base_url, city_name, last_checked
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, CURRENT_TIMESTAMP)
ON CONFLICT (product_code) DO UPDATE SET
    title = EXCLUDED.title,
    url = EXCLUDED.url,
    price = EXCLUDED.price,
    price_old = EXCLUDED.price_old,
    stock = EXCLUDED.stock,
    category = EXCLUDED.category,
    image_url = EXCLUDED.image_url,
    image_hash = COALESCE(EXCLUDED.image_hash, kopiyochka_products.image_hash),
    image_base_url = COALESCE(EXCLUDED.image_base_url, kopiyochka_products.image_base_url),
    city_name = EXCLUDED.city_name,
    last_checked = CURRENT_TIMESTAMP
RETURNING (xmax = 0) as is_new
"""


# ====================== OBSCURA CLIENT ======================

class ObscuraFetcher:
    """Wraps Obscura CLI for anti-detection HTTP fetching."""

    def __init__(self, bin_path: str, timeout: int = OBSCURA_TIMEOUT):
        self.bin = bin_path
        self.timeout = timeout
        self.warmed_up = False

    def _warmup_sync(self):
        """Visit homepage + catalog to establish session cookies."""
        for url in ["https://www.kopiyochka.ua/", "https://www.kopiyochka.ua/catalog/"]:
            try:
                subprocess.run(
                    [self.bin, "fetch", url, "--dump", "original", "--timeout", "15", "--quiet"],
                    capture_output=True, timeout=25,
                )
                time.sleep(1.0)
            except Exception:
                pass
        self.warmed_up = True

    async def warmup(self):
        """Warm up session cookies via Obscura."""
        if not self.warmed_up:
            print("🔥 Прогріваю сесію (homepage + catalog)...")
            await asyncio.to_thread(self._warmup_sync)
            print("✅ Сесія прогріта")

    def _fetch_sync(self, url: str) -> tuple[int, str]:
        """Synchronous fetch via Obscura --dump original (lightweight, no V8)."""
        try:
            r = subprocess.run(
                [self.bin, "fetch", url,
                 "--dump", "original",
                 "--timeout", str(self.timeout),
                 "--quiet"],
                capture_output=True, text=True,
                timeout=self.timeout + 15,
            )
            html = r.stdout
            if is_access_limited(200, html):
                return 429, html
            if not html or len(html) < 100:
                return 500, ""
            return 200, html
        except subprocess.TimeoutExpired:
            return 408, ""
        except Exception:
            return 500, ""

    async def fetch_html(self, url: str, retries: int = MAX_RETRIES) -> tuple[int, str]:
        """Fetch a page via Obscura browser. Returns (status_code, html)."""
        for attempt in range(retries + 1):
            status, html = await asyncio.to_thread(self._fetch_sync, url)

            if status == 429:
                if attempt < retries:
                    delay = min(20.0, RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0.2, 1.0))
                    log.warning(f"⚠️ Wordfence для {url.split('/')[-1]}, ретрай {attempt + 1}/{retries}, {delay:.1f}с")
                    await asyncio.sleep(delay)
                    continue
                return 429, html

            if status in (408, 500) and attempt < retries:
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                continue

            return status, html

        return 500, ""

    def _fetch_original_sync(self, url: str, headers: dict = None) -> tuple[int, bytes]:
        """Synchronous raw fetch (runs in thread)."""
        cmd = [self.bin, "fetch", url, "--dump", "original", "--timeout", str(self.timeout), "--quiet"]
        if headers:
            for k, v in headers.items():
                cmd.extend(["--header", f"{k}: {v}"])
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=self.timeout + 15)
            return 200, r.stdout
        except Exception:
            return 500, b""

    async def fetch_original(self, url: str, headers: dict = None, retries: int = 3) -> tuple[int, bytes]:
        """Fetch raw bytes via Obscura --dump original."""
        for attempt in range(retries + 1):
            status, data = await asyncio.to_thread(self._fetch_original_sync, url, headers)
            if status == 200 and data:
                return status, data
            if attempt < retries:
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
        return 500, b""


# ====================== AIOHTTP CLIENT ======================

class HttpFetcher:
    """Simple aiohttp-based fetcher for AJAX and image downloads."""

    def __init__(self):
        self.session = None

    async def ensure_session(self):
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(limit=CONCURRENCY, force_close=True)
            self.session = aiohttp.ClientSession(
                connector=connector,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
                    "X-Requested-With": "XMLHttpRequest",
                }
            )
        return self.session

    async def post_ajax(self, data: dict, referer: str, retries: int = MAX_RETRIES) -> dict | None:
        """POST to AJAX endpoint for pricing."""
        session = await self.ensure_session()
        for attempt in range(retries + 1):
            try:
                async with session.post(
                    AJAX_URL,
                    data=data,
                    headers={"Referer": referer},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 429:
                        if attempt < retries:
                            delay = min(20.0, RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0.2, 1.0))
                            await asyncio.sleep(delay)
                            continue
                        return None
                    if resp.status != 200:
                        return None
                    text = await resp.text()
                    if is_access_limited(resp.status, text):
                        if attempt < retries:
                            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                            continue
                        return None
                    return json.loads(text)
            except Exception:
                if attempt < retries:
                    await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                return None
        return None

    async def fetch_bytes(self, url: str, retries: int = 3) -> bytes | None:
        """Download raw bytes (for images)."""
        session = await self.ensure_session()
        for attempt in range(retries + 1):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    if resp.status == 429 and attempt < retries:
                        await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                        continue
                    return None
            except Exception:
                if attempt < retries:
                    await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                return None
        return None

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()


# ====================== IMAGE HELPERS ======================

def process_image(img_bytes: bytes, size: int, quality: int) -> bytes:
    buf = io.BytesIO()
    with Image.open(io.BytesIO(img_bytes)) as img:
        img = img.convert("RGB")
        img.thumbnail((size, size), Image.Resampling.LANCZOS)
        img.save(buf, format="AVIF", quality=quality, speed=6)
    return buf.getvalue()


def parse_price_str(text: str) -> float:
    cleaned = re.sub(r'<[^>]+>', '', str(text)).strip()
    cleaned = re.sub(r'[^\d,.]', '', cleaned)
    cleaned = cleaned.replace(',', '.')
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return 0.0


# ====================== SITEMAP ======================

async def fetch_product_urls_http(session: aiohttp.ClientSession) -> list[str]:
    """Collect product URLs from sitemap via aiohttp."""
    print("📦 Завантажую sitemap_index.xml...")

    product_urls = set()
    try:
        async with session.get(SITEMAP_INDEX, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                print(f"❌ Sitemap index HTTP {resp.status}")
                return []
            text = await resp.text()
    except Exception as e:
        print(f"❌ Помилка завантаження sitemap: {e}")
        return []

    sitemap_urls = re.findall(r'<loc>(https://www\.kopiyochka\.ua/[^<]*\.xml)</loc>', text)
    catalog_sitemaps = [u for u in sitemap_urls if 'catalog' in u or 'product' in u]
    if not catalog_sitemaps:
        catalog_sitemaps = sitemap_urls

    print(f"📄 Знайдено {len(catalog_sitemaps)} sitemap-файлів")

    for sitemap_url in catalog_sitemaps:
        try:
            async with session.get(sitemap_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    continue
                sm_text = await resp.text()
        except Exception:
            continue

        urls = re.findall(r'<loc>(https://www\.kopiyochka\.ua/[^<]*)</loc>', sm_text)
        new_urls = [
            u for u in urls
            if "/catalog/" in u and ".xml" not in u and not u.rstrip("/").endswith("/catalog")
        ]
        product_urls.update(new_urls)
        if new_urls:
            print(f"  → {sitemap_url.split('/')[-1]}: {len(new_urls)} товарів")
        await asyncio.sleep(0.15)

    print(f"✅ Всього зібрано {len(product_urls)} унікальних URL товарів")
    return sorted(product_urls)


# ====================== PARSING ======================

def parse_product_page(html: str, url: str) -> dict | None:
    """Parse product page HTML → extract code, title, image, category."""
    if not html or len(html) < 100:
        return None

    # Product code
    code_match = re.search(r'productCode\s*=\s*["\']?(\d+)', html, re.IGNORECASE)
    if not code_match:
        code_match = re.search(r'product_code\s*[:=]\s*["\']?(\d+)', html, re.IGNORECASE)
    if not code_match:
        soup = BeautifulSoup(html, 'html.parser')
        sku_el = soup.find(string=re.compile(r'Артикул', re.IGNORECASE))
        if sku_el:
            num_match = re.search(r'(\d{4,})', sku_el.parent.get_text() if sku_el.parent else str(sku_el))
            if num_match:
                code_match = num_match
    if not code_match:
        return None

    product_code = code_match.group(1) if hasattr(code_match, 'group') else str(code_match)

    soup = BeautifulSoup(html, 'html.parser')

    # Title
    h1 = soup.find('h1')
    title = h1.get_text(strip=True) if h1 else f"Товар {product_code}"

    # Image
    image_url = None
    og_img = soup.find('meta', property='og:image')
    if og_img and og_img.get('content'):
        image_url = og_img['content']
    if not image_url:
        for img in soup.find_all('img'):
            src = img.get('src', '') or img.get('data-src', '')
            if src and ('product' in src.lower() or 'upload' in src.lower() or 'wp-content' in src.lower()):
                image_url = src
                break

    # Category from breadcrumbs
    category = ""
    breadcrumbs = soup.find(class_=re.compile(r'breadcrumb', re.IGNORECASE))
    if breadcrumbs:
        crumbs = breadcrumbs.find_all('a')
        if len(crumbs) >= 2:
            category = crumbs[-1].get_text(strip=True)

    return {
        "product_code": product_code,
        "title": title,
        "url": url,
        "image_url": image_url,
        "category": category,
    }


def parse_price_response(data: dict) -> tuple[float, float, int]:
    """Parse AJAX pricing response → (price, price_old, total_stock)."""
    price = 0.0
    price_old = 0.0
    total_stock = 0

    if data and 'city_items' in data and data['city_items']:
        items = data['city_items']
        total_stock = sum(int(item.get('stock_available', 0) or 0) for item in items)
        for item in items:
            price_html = item.get('formatted_price', '')
            price_soup = BeautifulSoup(str(price_html), 'html.parser')
            ins = price_soup.find('ins')
            del_tag = price_soup.find('del')
            if ins and del_tag:
                price = parse_price_str(ins.get_text())
                price_old = parse_price_str(del_tag.get_text())
            else:
                price = parse_price_str(price_html)
            if price > 0:
                break

    elif data and 'current_place' in data:
        cp = data['current_place']
        total_stock = int(cp.get('stock_available', 0) or 0)
        price_html = cp.get('formatted_price', '')
        price_soup = BeautifulSoup(str(price_html), 'html.parser')
        ins = price_soup.find('ins')
        del_tag = price_soup.find('del')
        if ins and del_tag:
            price = parse_price_str(ins.get_text())
            price_old = parse_price_str(del_tag.get_text())
        else:
            price = parse_price_str(price_html)

    return price, price_old, total_stock


# ====================== DUMPER ======================

class KopiyochkaDumper:
    def __init__(self, city: str = CITY_NAME, skip_images: bool = False, skip_recent_hours: int = 0):
        self.city = city
        self.skip_images = skip_images
        self.skip_recent = skip_recent_hours
        self.db_pool = None
        self.r2_session = aioboto3.Session()
        self.obscura = None
        self.http = HttpFetcher()
        self.recent_codes = set()
        self.stats = {
            'new': 0, 'updated': 0, 'unchanged': 0,
            'images': 0, 'errors': 0, 'discounts': 0,
            'rate_limited': 0, 'retried': 0,
        }
        self.processed = 0
        self.total = 0
        self.started_at = None

    async def run(self):
        if not OBSCURA_BIN:
            print("❌ Obscura binary not found! Place obscura.exe in avrora1/obscura/")
            return

        self.obscura = ObscuraFetcher(OBSCURA_BIN)
        self.started_at = time.monotonic()

        # 0. Warm up session
        await self.obscura.warmup()

        # 1. DB setup
        self.db_pool = await asyncpg.create_pool(PG_URL)
        async with self.db_pool.acquire() as conn:
            await conn.execute(CREATE_TABLE_SQL)
            if self.skip_recent > 0:
                rows = await conn.fetch(
                    "SELECT product_code FROM kopiyochka_products "
                    "WHERE last_checked >= NOW() - ($1 * INTERVAL '1 hour')",
                    self.skip_recent
                )
                self.recent_codes = {r['product_code'] for r in rows}
                if self.recent_codes:
                    print(f"⏩ Пропускаємо {len(self.recent_codes)} товарів (оновлено < {self.skip_recent} год)")

        # 2. Get URLs from sitemap
        session = await self.http.ensure_session()
        product_urls = await fetch_product_urls_http(session)
        self.total = len(product_urls)
        if not product_urls:
            print("❌ Немає URL для обробки")
            return

        print(f"🚀 Старт: {self.total} товарів, місто: {self.city}")

        # 3. Process in batches
        semaphore = asyncio.Semaphore(CONCURRENCY)
        tasks = [self._process_one(url, semaphore) for url in product_urls]
        await asyncio.gather(*tasks, return_exceptions=True)

        # 4. Summary
        elapsed = time.monotonic() - self.started_at
        print(f"\n\n📊 Підсумок ({elapsed:.0f}с): "
              f"нові={self.stats['new']}, оновлено={self.stats['updated']}, "
              f"без змін={self.stats['unchanged']}, фото={self.stats['images']}, "
              f"знижки={self.stats['discounts']}, помилки={self.stats['errors']}")

        await self.http.close()
        if self.db_pool:
            await self.db_pool.close()

    async def _process_one(self, url: str, sem: asyncio.Semaphore):
        async with sem:
            # Random delay between requests to avoid rate limiting
            await asyncio.sleep(random.uniform(*REQUEST_DELAY))
            try:
                await self._handle_product(url)
            except Exception as e:
                self.stats['errors'] += 1
                print(f"❌ {url.split('/')[-1]}: {e}")
            self.processed += 1
            if self.processed % 25 == 0 or self.processed == self.total:
                elapsed = time.monotonic() - self.started_at
                rate = self.processed / max(elapsed, 0.001)
                pct = self.processed / max(self.total, 1) * 100
                eta = (self.total - self.processed) / rate if rate > 0 else 0
                print(f"  [{self.processed}/{self.total}] {pct:.0f}% — {rate:.1f}/с — "
                      f"нові:{self.stats['new']} онов:{self.stats['updated']} без змін:{self.stats['unchanged']} "
                      f"ETA:{int(eta//60)}х{int(eta%60):02d}с")

    async def _handle_product(self, url: str):
        # Fetch page via Obscura
        status, html = await self.obscura.fetch_html(url)
        if status != 200:
            self.stats['errors'] += 1
            return

        info = parse_product_page(html, url)
        if not info:
            self.stats['errors'] += 1
            return

        code = info["product_code"]
        if code in self.recent_codes:
            self.stats['unchanged'] += 1
            return

        # Get price via AJAX
        ajax_data = {
            'action': 'get_price_and_remain',
            'product_code': code,
            'city_name': self.city,
            'load_all': '1',
        }
        price_resp = await self.http.post_ajax(ajax_data, referer=url)
        if not price_resp:
            self.stats['errors'] += 1
            return

        price, price_old, stock = parse_price_response(price_resp)

        # Log big discounts
        if price_old > 0 and price > 0 and price < price_old:
            disc = int((1 - price / price_old) * 100)
            if disc >= 50:
                self.stats['discounts'] += 1
                print(f"🔥 -{disc}% {info['title'][:48]} | {price} грн (було {price_old})")

        # Handle image
        img_hash = None
        img_base_url = None
        if not self.skip_images and info.get("image_url"):
            img_hash, img_base_url = await self._handle_image(code, info["image_url"])
            if img_base_url:
                self.stats['images'] += 1

        # Save to DB
        await self._save(code, info, price, price_old, stock, img_hash, img_base_url)

    async def _handle_image(self, code: str, image_url: str) -> tuple[str | None, str | None]:
        if not image_url or not image_url.startswith("http"):
            return None, None

        img_bytes = await self.http.fetch_bytes(image_url)
        if not img_bytes:
            return None, None

        img_hash = hashlib.sha256(img_bytes).hexdigest()

        # Check dedup
        async with self.db_pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT image_base_url FROM kopiyochka_products WHERE image_hash = $1 LIMIT 1",
                img_hash
            )
            if existing and existing['image_base_url']:
                return img_hash, existing['image_base_url']

        # Process + upload
        t_bytes = await asyncio.to_thread(process_image, img_bytes, 256, 35)
        p_bytes = await asyncio.to_thread(process_image, img_bytes, 512, 45)

        async with self.r2_session.client(
            's3',
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_KEY,
            aws_secret_access_key=R2_SECRET
        ) as s3:
            await s3.put_object(
                Bucket=R2_BUCKET, Key=f"kopiyochka/{code}/thumb.avif",
                Body=t_bytes, ContentType="image/avif",
                CacheControl="public, max-age=31536000, immutable"
            )
            await s3.put_object(
                Bucket=R2_BUCKET, Key=f"kopiyochka/{code}/preview.avif",
                Body=p_bytes, ContentType="image/avif",
                CacheControl="public, max-age=31536000, immutable"
            )

        return img_hash, f"{CDN_URL}/kopiyochka/{code}"

    async def _save(self, code, info, price, price_old, stock, img_hash, img_base_url):
        try:
            async with self.db_pool.acquire() as conn:
                old = await conn.fetchrow(
                    "SELECT title, price, price_old, stock, category, image_hash "
                    "FROM kopiyochka_products WHERE product_code = $1", code
                )

                if old:
                    changed = []
                    if str(old["title"] or "") != info["title"]:
                        changed.append("назва")
                    if abs(float(old["price"] or 0) - price) > 0.001:
                        changed.append(f"ціна")
                    if abs(float(old["price_old"] or 0) - price_old) > 0.001:
                        changed.append("стара ціна")
                    if int(old["stock"] or 0) != stock:
                        changed.append("залишок")
                    if not changed:
                        await conn.execute(
                            "UPDATE kopiyochka_products SET last_checked = CURRENT_TIMESTAMP WHERE product_code = $1",
                            code
                        )
                        self.stats['unchanged'] += 1
                        return

                row = await conn.fetchrow(
                    UPSERT_SQL,
                    code, info["title"], info["url"], price, price_old, stock,
                    info["category"], info.get("image_url"), img_hash, img_base_url, self.city
                )

                if row and row.get('is_new'):
                    self.stats['new'] += 1
                else:
                    self.stats['updated'] += 1

        except Exception as e:
            self.stats['errors'] += 1
            print(f"❌ DB [{code}]: {e}")


# ====================== MAIN ======================

if __name__ == "__main__":
    os.system("cls" if os.name == "nt" else "clear")

    print("\n📦 КОПІЙОЧКА ДАМПЕР V3 (Obscura)")
    print(f"Місто: {CITY_NAME}")
    print()

    if not OBSCURA_BIN:
        print("⚠️  Obscura не знайдено! Покладіть obscura.exe в avrora1/obscura/")
        print(f"   Шукали: {[str(p) for p in [Path(__file__).parent / 'obscura' / 'obscura.exe']]}")
        sys.exit(1)

    print(f"✅ Obscura: {OBSCURA_BIN}")
    print()
    print("1. Повний дамп (з фото)")
    print("2. Повний дамп (без фото)")
    print(f"3. Швидкий дамп (пропустити < {RECENT_SKIP_HOURS} год)")
    print("0. Вихід")

    choice = input("\n> ").strip()

    skip_images = False
    skip_recent = 0

    if choice == "1":
        skip_images = False
    elif choice == "2":
        skip_images = True
    elif choice == "3":
        skip_images = True
        skip_recent = RECENT_SKIP_HOURS
    elif choice == "0":
        sys.exit(0)
    else:
        print("Невірний вибір")
        sys.exit(1)

    dumper = KopiyochkaDumper(
        city=CITY_NAME,
        skip_images=skip_images,
        skip_recent_hours=skip_recent,
    )
    asyncio.run(dumper.run())
