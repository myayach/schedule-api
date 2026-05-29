import os
import logging
import re
import json
import httpx
import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from bs4 import BeautifulSoup

from cabinets_db import CABINETS_MAP, HOUSING_ID

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ==================== ПРИЛОЖЕНИЕ ====================
app = FastAPI(
    title="Smart Schedule Board API",
    version="2.0.0",
    description="API расписания аудиторий Мининского университета"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== КОНСТАНТЫ ====================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/tmp/schedule_cache"))
MEMORY_CACHE_TTL = 300  # 5 минут в памяти
DISK_CACHE_TTL = 86400  # 24 часа на диске (fallback)

# ==================== КЭШИ ====================

class MemoryCache:
    """Быстрый in-memory кэш с TTL"""
    def __init__(self, ttl: int = MEMORY_CACHE_TTL):
        self._cache: dict = {}
        self.ttl = ttl

    def get(self, key: str) -> Optional[list]:
        entry = self._cache.get(key)
        if entry and time.time() - entry["ts"] < self.ttl:
            return entry["data"]
        if entry:
            del self._cache[key]
        return None

    def set(self, key: str, data: list) -> None:
        self._cache[key] = {"data": data, "ts": time.time()}

    def clear(self) -> None:
        self._cache.clear()

    @property
    def size(self) -> int:
        return len(self._cache)


class DiskCache:
    """Персистентный кэш на диске — работает как fallback при недоступности сайта"""
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Disk cache initialized at {self.cache_dir}")

    def _path(self, key: str) -> Path:
        safe = re.sub(r'[^a-zA-Z0-9_\-]', '_', key)
        return self.cache_dir / f"{safe}.json"

    def get(self, key: str, max_age: int = DISK_CACHE_TTL) -> Optional[list]:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                entry = json.load(f)
            if time.time() - entry["ts"] < max_age:
                return entry["data"]
        except Exception as e:
            logger.warning(f"Disk cache read error for {key}: {e}")
        return None

    def set(self, key: str, data: list) -> None:
        p = self._path(key)
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"data": data, "ts": time.time(), "saved_at": datetime.now().isoformat()}, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Disk cache write error for {key}: {e}")

    def get_stale(self, key: str) -> Optional[list]:
        """Вернуть данные любой давности — используется как последний fallback"""
        p = self._path(key)
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                entry = json.load(f)
            age_h = (time.time() - entry["ts"]) / 3600
            logger.info(f"Serving stale disk cache for {key} (age: {age_h:.1f}h)")
            return entry["data"]
        except Exception:
            return None

    def get_meta(self, key: str) -> Optional[dict]:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                entry = json.load(f)
            return {"saved_at": entry.get("saved_at"), "age_hours": round((time.time() - entry["ts"]) / 3600, 1)}
        except Exception:
            return None


memory_cache = MemoryCache()
disk_cache = DiskCache(CACHE_DIR)


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def extract_token(html_text: str) -> Optional[str]:
    patterns = [
        r'<meta[^>]*name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\']',
        r'name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']',
        r'<input[^>]*name=["\']_token["\'][^>]*value=["\']([^"\']+)["\']',
        r'["\']csrf-token["\'][^}]*content["\']:\s*["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        m = re.search(pattern, html_text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def cache_key(cabinet_id: str, date: datetime) -> str:
    return f"{cabinet_id}_{date.strftime('%Y%m%d')}"


# ==================== ПАРСИНГ РАСПИСАНИЯ ====================

async def fetch_schedule_from_source(cabinet_id: str, target_date: datetime) -> list:
    """Делает запрос к сайту и возвращает расписание"""
    target_date_iso = target_date.strftime("%Y-%m-%d")
    target_date_dmy = target_date.strftime("%d.%m.%Y")

    async with httpx.AsyncClient(verify=False, timeout=30.0, follow_redirects=True) as client:
        init_res = await client.get("https://ya.mininuniver.ru/shedule", headers=HEADERS)

        token = extract_token(init_res.text)
        if not token:
            for cookie_name in ("XSRF-TOKEN", "csrf_token"):
                if cookie_name in init_res.cookies:
                    token = init_res.cookies[cookie_name]
                    break

        if not token:
            logger.error("CSRF token not found")
            return []

        post_headers = {**HEADERS, "X-CSRF-TOKEN": token, "X-Requested-With": "XMLHttpRequest",
                        "Referer": "https://ya.mininuniver.ru/shedule", "Origin": "https://ya.mininuniver.ru"}
        client.cookies.update(init_res.cookies)

        payload = {"_token": token, "searchType": "2", "housing": HOUSING_ID, "cabinet": cabinet_id}
        res = await client.post("https://ya.mininuniver.ru/shedule", data=payload, headers=post_headers)

        if not res.text or len(res.text) < 100:
            return []

        match = re.search(r'window\.CalendarData\s*=\s*(\[.*?\]|\{.*?\});', res.text, re.DOTALL)
        if not match:
            return []

        data = json.loads(match.group(1))
        if not isinstance(data, list):
            return []

        target_day = next(
            (d for d in data if isinstance(d, dict) and d.get("date") in (target_date_iso, target_date_dmy)),
            None
        )
        if not target_day:
            return []

        results = []
        title = target_day.get("title", {})

        if isinstance(title, dict):
            for _, couple_data in title.items():
                if not isinstance(couple_data, dict):
                    continue
                for lesson in couple_data.get("lessons", []):
                    if not isinstance(lesson, dict):
                        continue

                    groups = lesson.get("groups", "")
                    if isinstance(groups, list):
                        group_str = ", ".join(str(g) for g in groups if g and g != "False")
                    else:
                        group_str = str(groups) if groups and groups != "False" else ""

                    if not group_str:
                        continue

                    subgroup = lesson.get("subgroup", {})
                    if isinstance(subgroup, dict):
                        sg_num = subgroup.get("subgroup_numbers")
                        if sg_num and sg_num is not False:
                            group_str += f" (п/г {sg_num})"

                    couple = lesson.get("couple", {})
                    time_range = couple.get("time", "00:00 - 00:00") if isinstance(couple, dict) else "00:00 - 00:00"
                    t_parts = str(time_range).split(" - ")

                    teacher = lesson.get("teacher", {})
                    teacher_name = teacher.get("name", "—") if isinstance(teacher, dict) else str(teacher or "—")

                    couple_type = couple.get("couple_type", "") if isinstance(couple, dict) else ""
                    discipline = lesson.get("discipline", "—")
                    if couple_type:
                        discipline = f"{couple_type} {discipline}"

                    results.append({
                        "start": t_parts[0].strip() if len(t_parts) > 0 else "00:00",
                        "end": t_parts[1].strip() if len(t_parts) > 1 else "00:00",
                        "subject": discipline,
                        "group": group_str,
                        "teacher": teacher_name,
                    })

        # Дедупликация и сортировка
        seen = set()
        unique = []
        for item in sorted(results, key=lambda x: x["start"]):
            key = f"{item['start']}-{item['subject']}-{item['group']}"
            if key not in seen:
                unique.append(item)
                seen.add(key)

        logger.info(f"✅ cabinet {cabinet_id}: {len(unique)} lessons on {target_date_dmy}")
        return unique


async def get_schedule(cabinet_id: str, target_date: datetime) -> tuple[list, bool]:
    """
    Возвращает (schedule, is_from_cache).
    Приоритет: memory → disk (свежий) → source → disk (stale fallback)
    """
    key = cache_key(cabinet_id, target_date)

    # 1. Memory cache
    cached = memory_cache.get(key)
    if cached is not None:
        return cached, True

    # 2. Disk cache (свежий, до 24ч)
    disk_data = disk_cache.get(key)
    if disk_data is not None:
        memory_cache.set(key, disk_data)
        return disk_data, True

    # 3. Live fetch
    try:
        data = await fetch_schedule_from_source(cabinet_id, target_date)
        memory_cache.set(key, data)
        disk_cache.set(key, data)
        return data, False
    except Exception as e:
        logger.error(f"Live fetch failed for {cabinet_id} on {target_date.date()}: {e}")

        # 4. Stale disk fallback
        stale = disk_cache.get_stale(key)
        if stale is not None:
            return stale, True

        return [], False


# ==================== РОУТЫ ====================

@app.get("/")
async def root():
    return {
        "name": "Smart Schedule Board API",
        "version": "2.0.0",
        "status": "running",
        "cache": {
            "memory_entries": memory_cache.size,
            "cache_dir": str(CACHE_DIR),
        },
        "endpoints": ["/api/all_rooms", "/api/details/{room_name}", "/api/health", "/api/debug/{room_name}"],
    }


@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "cabinets_count": len(CABINETS_MAP),
        "memory_cache_entries": memory_cache.size,
    }


@app.get("/api/all_rooms")
async def get_rooms():
    return sorted(list(CABINETS_MAP.keys()))


@app.get("/api/details/{room_name}")
async def get_details(room_name: str):
    cab_id = CABINETS_MAP.get(room_name)
    if not cab_id:
        raise HTTPException(status_code=404, detail=f"Аудитория {room_name} не найдена")

    now = datetime.now()
    tomorrow = now + timedelta(days=1)

    try:
        today_schedule, today_cached = await get_schedule(cab_id, now)
        tomorrow_schedule, tomorrow_cached = await get_schedule(cab_id, tomorrow)

        # Мета о кэше (для отладки и честности)
        today_meta = disk_cache.get_meta(cache_key(cab_id, now))
        tomorrow_meta = disk_cache.get_meta(cache_key(cab_id, tomorrow))

        return {
            "room": room_name,
            "cabinet_id": cab_id,
            "now_iso": now.isoformat(),
            "today": {
                "date": now.strftime("%d.%m.%Y"),
                "schedule": today_schedule,
                "from_cache": today_cached,
                "cache_meta": today_meta,
            },
            "tomorrow": {
                "date": tomorrow.strftime("%d.%m.%Y"),
                "schedule": tomorrow_schedule,
                "from_cache": tomorrow_cached,
                "cache_meta": tomorrow_meta,
            },
        }
    except Exception as e:
        logger.error(f"Error in get_details({room_name}): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка получения расписания: {str(e)}")


@app.get("/api/debug/{room_name}")
async def debug_room(room_name: str, date: str = None):
    cab_id = CABINETS_MAP.get(room_name)
    if not cab_id:
        raise HTTPException(status_code=404, detail=f"Аудитория {room_name} не найдена")

    try:
        target_date = datetime.strptime(date, "%Y-%m-%d") if date else datetime.now()
    except ValueError:
        target_date = datetime.now()

    today_schedule, _ = await get_schedule(cab_id, target_date)
    tomorrow_schedule, _ = await get_schedule(cab_id, target_date + timedelta(days=1))

    return {
        "room": room_name,
        "cabinet_id": cab_id,
        "now": datetime.now().isoformat(),
        "target_date": target_date.strftime("%d.%m.%Y"),
        "today_schedule": today_schedule,
        "tomorrow_schedule": tomorrow_schedule,
        "today_count": len(today_schedule),
        "tomorrow_count": len(tomorrow_schedule),
    }


@app.get("/api/search")
async def search_rooms(query: str = ""):
    if not query:
        return sorted(list(CABINETS_MAP.keys()))
    q = query.lower()
    return sorted(r for r in CABINETS_MAP if q in r.lower())


@app.delete("/api/cache")
async def clear_cache():
    """Очистить memory cache (admin endpoint)"""
    memory_cache.clear()
    return {"status": "cleared", "timestamp": datetime.now().isoformat()}


# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    import uvicorn

    logger.info("=" * 50)
    logger.info("Starting Smart Schedule Board API v2.0.0")
    logger.info(f"Loaded {len(CABINETS_MAP)} cabinets, housing ID: {HOUSING_ID}")
    logger.info(f"Disk cache: {CACHE_DIR}")
    logger.info("=" * 50)

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
