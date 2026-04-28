import os
import logging
import re
import json
import httpx
import asyncio
import time
from datetime import datetime, timedelta
from functools import lru_cache
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from bs4 import BeautifulSoup

#база аудиторий
from cabinets_db import CABINETS_MAP, HOUSING_ID

#логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="расписание", version="1")

#CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
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

#кэш
class ScheduleCache:
    def __init__(self, ttl_seconds=300):  # 5 минут кэш
        self.cache = {}
        self.ttl = ttl_seconds
    
    def get(self, key):
        if key in self.cache:
            data, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                return data
            else:
                del self.cache[key]
        return None
    
    def set(self, key, data):
        self.cache[key] = (data, time.time())
    
    def clear(self):
        self.cache.clear()

schedule_cache = ScheduleCache(ttl_seconds=300)

def extract_token(html_text):
    """Извлекает CSRF токен из HTML разными способами"""
    
    # Способ 1: Стандартный meta тег
    match = re.search(r'<meta[^>]*name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\']', html_text, re.IGNORECASE)
    if match:
        return match.group(1)
    
    # Способ 2: В JavaScript переменной
    match = re.search(r'name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']', html_text)
    if match:
        return match.group(1)
    
    # Способ 3: Поиск в скрытом input поле
    match = re.search(r'<input[^>]*name=["\']_token["\'][^>]*value=["\']([^"\']+)["\']', html_text, re.IGNORECASE)
    if match:
        return match.group(1)
    
    # Способ 4: В JavaScript коде (другой формат)
    match = re.search(r'["\']csrf-token["\'][^}]*content["\']:\s*["\']([^"\']+)["\']', html_text)
    if match:
        return match.group(1)
    
    # Способ 5: Поиск в атрибутах Angular
    match = re.search(r'ng-init="[^"]*[\'"]csrf[\'"]\s*:\s*[\'"]([^\'"]+)[\'"]', html_text, re.IGNORECASE)
    if match:
        return match.group(1)
    
    return None

def format_subgroup(subgroup_data):
    """Форматирует информацию о подгруппе"""
    if not subgroup_data:
        return ""
    
    if isinstance(subgroup_data, dict):
        subgroup_number = subgroup_data.get('subgroup_numbers', subgroup_data.get('number', ''))
        if subgroup_number and subgroup_number != False and subgroup_number != 'False':
            return f" (п/г {subgroup_number})"
        else:
            return ""
    
    if isinstance(subgroup_data, (str, int, float)):
        subgroup_str = str(subgroup_data).strip()
        if subgroup_str and subgroup_str.lower() not in ['false', 'none', '0', '']:
            return f" (п/г {subgroup_str})"
    
    return ""

async def fetch_schedule(cabinet_id: str, target_date: datetime):
    """Получение расписания для аудитории"""
    target_date_iso = target_date.strftime("%Y-%m-%d")
    target_date_dmy = target_date.strftime("%d.%m.%Y")
    
    async with httpx.AsyncClient(verify=False, timeout=30.0, follow_redirects=True) as client:
        try:
            init_res = await client.get("https://ya.mininuniver.ru/shedule", headers=HEADERS)
            
            token = extract_token(init_res.text)
            
            if not token:
                if 'XSRF-TOKEN' in init_res.cookies:
                    token = init_res.cookies['XSRF-TOKEN']
                elif 'csrf_token' in init_res.cookies:
                    token = init_res.cookies['csrf_token']
                else:
                    logger.error("Токен не найден")
                    return []
            
            post_headers = HEADERS.copy()
            post_headers.update({
                "X-CSRF-TOKEN": token,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://ya.mininuniver.ru/shedule",
                "Origin": "https://ya.mininuniver.ru"
            })
            
            if hasattr(init_res, 'cookies'):
                client.cookies.update(init_res.cookies)
            
            payload = {
                "_token": token,
                "searchType": "2",
                "housing": HOUSING_ID,
                "cabinet": cabinet_id
            }
            
            res = await client.post("https://ya.mininuniver.ru/shedule", data=payload, headers=post_headers)
            
            if not res.text or len(res.text) < 100:
                logger.error(f"Пустой ответ: {len(res.text) if res.text else 0} символов")
                return []
            
            match = re.search(r'window\.CalendarData\s*=\s*(\[.*?\]|\{.*?\});', res.text, re.DOTALL)
            if not match:
                return []
            
            data = json.loads(match.group(1))
            
            if not isinstance(data, list):
                logger.warning(f"Неожиданный формат данных: {type(data)}")
                return []
            
            target_day_data = None
            for day_obj in data:
                if isinstance(day_obj, dict):
                    day_date = day_obj.get('date', '')
                    if day_date == target_date_iso or day_date == target_date_dmy:
                        target_day_data = day_obj
                        break
            
            if not target_day_data:
                logger.info(f"Расписание на {target_date_dmy} не найдено")
                return []
            
            final = []
            title = target_day_data.get('title', {})
            
            if isinstance(title, dict):
                for couple_num, couple_data in title.items():
                    if not isinstance(couple_data, dict):
                        continue
                    
                    lessons = couple_data.get('lessons', [])
                    
                    for lesson in lessons:
                        if not isinstance(lesson, dict):
                            continue
                        
                        groups = lesson.get('groups', '')
                        if isinstance(groups, list):
                            group_str = ", ".join([str(g) for g in groups if g and g != 'False'])
                        else:
                            group_str = str(groups) if groups and groups != 'False' else ''
                        
                        if not group_str:
                            continue
                        
                        subgroup = lesson.get('subgroup', {})
                        if isinstance(subgroup, dict):
                            subgroup_nums = subgroup.get('subgroup_numbers', False)
                            if subgroup_nums and subgroup_nums != False:
                                group_str += f" (п/г {subgroup_nums})"
                        
                        couple = lesson.get('couple', {})
                        time_range = couple.get('time', '00:00 - 00:00') if isinstance(couple, dict) else '00:00 - 00:00'
                        t_parts = str(time_range).split(' - ')
                        
                        teacher = lesson.get('teacher', {})
                        teacher_name = teacher.get('name', '—') if isinstance(teacher, dict) else str(teacher) if teacher else '—'
                        
                        couple_type = couple.get('couple_type', '') if isinstance(couple, dict) else ''
                        
                        discipline = lesson.get('discipline', '—')
                        if couple_type:
                            discipline = f"{couple_type} {discipline}"
                        
                        final.append({
                            "start": t_parts[0].strip() if len(t_parts) > 0 else "00:00",
                            "end": t_parts[1].strip() if len(t_parts) > 1 else "00:00",
                            "subject": discipline,
                            "group": group_str,
                            "teacher": teacher_name
                        })
            
            unique_results = []
            seen = set()
            for item in sorted(final, key=lambda x: x['start']):
                key = f"{item['start']}-{item['subject']}-{item['group']}"
                if key not in seen:
                    unique_results.append(item)
                    seen.add(key)
            
            logger.info(f"✅ {cabinet_id}: найдено {len(unique_results)} занятий на {target_date_dmy}")
            return unique_results
            
        except Exception as e:
            logger.error(f"Ошибка в fetch_schedule: {str(e)}", exc_info=True)
            return []

@app.get("/")
async def root():
    return {
        "name": "Smart Schedule Board API",
        "version": "1.0.0",
        "status": "running",
        "endpoints": [
            "/api/all_rooms",
            "/api/details/{room_name}",
            "/api/health",
            "/api/debug/{room_name}"
        ]
    }

@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "cabinets_count": len(CABINETS_MAP)
    }

@app.get("/api/all_rooms")
async def get_rooms():
    rooms = sorted(list(CABINETS_MAP.keys()))
    logger.info(f"Returning {len(rooms)} rooms")
    return rooms

@app.get("/api/debug/{room_name}")
async def debug_room(room_name: str, date: str = None):
    cab_id = CABINETS_MAP.get(room_name)
    if not cab_id:
        raise HTTPException(status_code=404, detail=f"Аудитория {room_name} не найдена")
    
    if date:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d")
        except:
            target_date = datetime.now()
    else:
        target_date = datetime.now()
    
    today_schedule = await fetch_schedule(cab_id, target_date)
    tomorrow_schedule = await fetch_schedule(cab_id, target_date + timedelta(days=1))
    
    return {
        "room": room_name,
        "cabinet_id": cab_id,
        "now": datetime.now().isoformat(),
        "target_date": target_date.strftime("%d.%m.%Y"),
        "today_schedule": today_schedule,
        "tomorrow_schedule": tomorrow_schedule,
        "today_count": len(today_schedule) if today_schedule else 0,
        "tomorrow_count": len(tomorrow_schedule) if tomorrow_schedule else 0
    }

@app.get("/api/details/{room_name}")
async def get_details(room_name: str):
    cab_id = CABINETS_MAP.get(room_name)
    if not cab_id:
        logger.warning(f"Room not found: {room_name}")
        raise HTTPException(status_code=404, detail=f"Аудитория {room_name} не найдена")
    
    try:
        now = datetime.now()
        
        cache_key_today = f"{cab_id}_{now.strftime('%Y%m%d')}_today"
        cache_key_tomorrow = f"{cab_id}_{(now + timedelta(days=1)).strftime('%Y%m%d')}_tomorrow"
        
        today_schedule = schedule_cache.get(cache_key_today)
        if today_schedule is None:
            logger.info(f"Fetching schedule for today: {room_name} ({cab_id})")
            today_schedule = await fetch_schedule(cab_id, now)
            schedule_cache.set(cache_key_today, today_schedule)
        else:
            logger.info(f"Using cached schedule for today: {room_name}")
        
        tomorrow_schedule = schedule_cache.get(cache_key_tomorrow)
        if tomorrow_schedule is None:
            logger.info(f"Fetching schedule for tomorrow: {room_name} ({cab_id})")
            tomorrow_schedule = await fetch_schedule(cab_id, now + timedelta(days=1))
            schedule_cache.set(cache_key_tomorrow, tomorrow_schedule)
        else:
            logger.info(f"Using cached schedule for tomorrow: {room_name}")
        
        return {
            "room": room_name,
            "cabinet_id": cab_id,
            "now_iso": now.isoformat(),
            "today": {
                "date": now.strftime("%d.%m.%Y"),
                "schedule": today_schedule
            },
            "tomorrow": {
                "date": (now + timedelta(days=1)).strftime("%d.%m.%Y"),
                "schedule": tomorrow_schedule
            }
        }
        
    except Exception as e:
        logger.error(f"Error getting details for room {room_name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"ошибка получения расписания: {str(e)}")

@app.get("/api/search")
async def search_rooms(query: str = ""):
    if not query:
        return list(CABINETS_MAP.keys())
    
    query_lower = query.lower()
    results = [
        room for room in CABINETS_MAP.keys()
        if query_lower in room.lower()
    ]
    return sorted(results)

if __name__ == "__main__":
    import uvicorn
    
    logger.info("Starting Smart Schedule Board API server...")
    logger.info(f"Loaded {len(CABINETS_MAP)} cabinets")
    logger.info(f"Housing ID: {HOUSING_ID}")
    
    port = int(os.environ.get("PORT", 8000))
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_level="info"
    )