import logging
import sqlite3
import requests
import time
import re
import os
import io
import psycopg2
import sys
import pytz
import schedule
import pandas as pd
from datetime import datetime
from collections import defaultdict
from urllib.parse import urlparse
from threading import Thread

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN:
    print("❌ BOT_TOKEN не установлен!")
    sys.exit(1)

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL and os.environ.get('RAILWAY_ENVIRONMENT'):
    print("❌ DATABASE_URL не установлен в Railway!")
    sys.exit(1)

ADMINS = [admin.strip() for admin in os.environ.get('ADMINS', 'r1kuza,nadya_yakovleva01,Priikalist').split(',') if admin.strip()]
WEATHER_API_KEY = os.environ.get('WEATHER_API_KEY')
SAMARA_TIMEZONE = pytz.timezone('Europe/Samara')

MAX_MESSAGE_LENGTH = 4000
MAX_USERS_PER_CLASS = 30
MAX_REQUESTS_PER_MINUTE = 20
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(name)

class DatabaseManager:
    def init(self):
        self.conn = None
        self.db_type = None
        self.connect()

    def connect(self):
        if DATABASE_URL:
            try:
                url = urlparse(DATABASE_URL)
                self.conn = psycopg2.connect(
                    database=url.path[1:], user=url.username, password=url.password,
                    host=url.hostname, port=url.port, sslmode='require'
                )
                self.db_type = 'postgresql'
            except Exception as e:
                logger.error(f"❌ Ошибка PostgreSQL: {e}")
                self.fallback_to_sqlite()
        else:
            self.fallback_to_sqlite()
    
    def fallback_to_sqlite(self):
        try:
            db_path = os.path.join(os.path.dirname(os.path.abspath(file)), "school_bot.db")
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
            self.db_type = 'sqlite'
        except Exception as e:
            logger.error(f"❌ Ошибка SQLite: {e}")
            raise
    
    def execute(self, query, params=None):
        if self.db_type == 'postgresql':
            query = query.replace('?', '%s')
        cursor = self.conn.cursor()
        try:
            cursor.execute(query, params) if params else cursor.execute(query)
            self.conn.commit()
            return cursor
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Ошибка запроса: {e}")
            raise e
    
    def fetchone(self, query, params=None):
        return self.execute(query, params).fetchone()
    
    def fetchall(self, query, params=None):
        return self.execute(query, params).fetchall()
    
    def close(self):
        if self.conn:
            self.conn.close()

    def create_tables(self):
        try:
            tables_query = """
                CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, full_name TEXT NOT NULL, class TEXT NOT NULL, username TEXT, registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
[26.04.2026 11:42] 𓆰♱𓆪: CREATE TABLE IF NOT EXISTS schedule (id SERIAL PRIMARY KEY, class TEXT NOT NULL, day TEXT NOT NULL, lesson_number INTEGER, subject TEXT, teacher TEXT, room TEXT, UNIQUE(class, day, lesson_number));
                CREATE TABLE IF NOT EXISTS bell_schedule (lesson_number INTEGER PRIMARY KEY, start_time TEXT NOT NULL, end_time TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notification_settings (user_id BIGINT PRIMARY KEY, weather_notifications BOOLEAN DEFAULT FALSE, news_notifications BOOLEAN DEFAULT TRUE, achievement_notifications BOOLEAN DEFAULT TRUE);
                CREATE TABLE IF NOT EXISTS school_news (id SERIAL PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL, author TEXT NOT NULL, target_audience TEXT DEFAULT 'all', publish_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP, is_published BOOLEAN DEFAULT TRUE);
                CREATE TABLE IF NOT EXISTS achievements (id SERIAL PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, icon TEXT NOT NULL, condition_type TEXT NOT NULL, condition_value INTEGER);
                CREATE TABLE IF NOT EXISTS user_achievements (user_id BIGINT, achievement_id INTEGER, achieved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (user_id, achievement_id));
                CREATE TABLE IF NOT EXISTS user_activity (id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, action_type TEXT NOT NULL, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, details TEXT);
                CREATE TABLE IF NOT EXISTS broadcast_messages (id SERIAL PRIMARY KEY, admin_username TEXT NOT NULL, message_text TEXT NOT NULL, target_audience TEXT DEFAULT 'all', sent_count INTEGER DEFAULT 0, failed_count INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, status TEXT DEFAULT 'pending');
                CREATE TABLE IF NOT EXISTS class_rosters (id SERIAL PRIMARY KEY, class TEXT NOT NULL, full_name TEXT NOT NULL, UNIQUE(class, full_name));
            """
            for q in tables_query.split(';'):
                if q.strip(): self.execute(q)
            
            if not self.fetchone("SELECT COUNT(*) FROM bell_schedule")[0]:
                for bell in [(1, '8:00', '8:40'), (2, '8:50', '9:30'), (3, '9:40', '10:20'), (4, '10:30', '11:10'), (5, '11:25', '12:05'), (6, '12:10', '12:50'), (7, '13:00', '13:40')]:
                    self.execute("INSERT INTO bell_schedule (lesson_number, start_time, end_time) VALUES (?, ?, ?)", bell)
            
            self._create_default_achievements()
            self._cleanup_duplicate_achievements()
        except Exception as e:
            logger.error(f"Ошибка создания таблиц: {e}")

    def _create_default_achievements(self):
        defaults = [
            ("🎓 Первые шаги", "Зарегистрировался в системе", "🎓", "registration", 1),
            ("📚 Любознательный", "Посмотрел расписание 10 раз", "📚", "schedule_views", 10),
            ("⭐ Активный ученик", "Использовал бота 50 раз", "⭐", "total_actions", 50),
            ("📰 Информированный", "Прочитал 5 новостей", "📰", "news_read", 5),
            ("🌦️ Метеоролог", "Включил уведомления о погоде", "🌦️", "weather_enabled", 1)
        ]
        for name, desc, icon, c_type, c_val in defaults:
            if not self.fetchone("SELECT 1 FROM achievements WHERE condition_type = ?", (c_type,)):
                self.execute("INSERT INTO achievements (name, description, icon, condition_type, condition_value) VALUES (?, ?, ?, ?, ?)", (name, desc, icon, c_type, c_val))
    
    def _cleanup_duplicate_achievements(self):
        try:
            if self.db_type == 'postgresql':
                self.execute("DELETE FROM achievements a1 USING achievements a2 WHERE a1.condition_type = a2.condition_type AND a1.id > a2.id")
            else:
                self.execute("DELETE FROM achievements WHERE id NOT IN (SELECT MIN(id) FROM achievements GROUP BY condition_type)")
        except Exception as e:
            logger.error(f"Ошибка очистки дубликатов: {e}")
    def add_student_to_roster(self, class_name, full_name):
        try:
            self.execute("INSERT INTO class_rosters (class, full_name) VALUES (?, ?) ON CONFLICT (class, full_name) DO NOTHING", (class_name.upper(), full_name.strip()))
            return True
        except Exception as e:
            logger.error(f"Ошибка добавления ученика: {e}")
            return False

    def remove_student_from_roster(self, class_name, full_name):
        try:
            self.execute("DELETE FROM class_rosters WHERE class = ? AND full_name = ?", (class_name.upper(), full_name.strip()))
            return True
        except Exception:
            return False

    def get_students_by_class(self, class_name):
        try:
            return [row[0] for row in self.fetchall("SELECT full_name FROM class_rosters WHERE class = ? ORDER BY full_name", (class_name.upper(),))]
        except Exception:
            return []

    def check_student_in_roster(self, class_name, full_name):
        try:
            return bool(self.fetchone("SELECT 1 FROM class_rosters WHERE class = ? AND full_name = ?", (class_name.upper(), full_name.strip())))
        except Exception:
            return False

    def import_roster_from_excel(self, file_content):
        try:
            df = pd.read_excel(io.BytesIO(file_content))
            added, errors = 0, 0
            for _, row in df.iterrows():
                if len(row) >= 2:
                    class_name, full_name = str(row[0]).strip().upper(), str(row[1]).strip()
                    if class_name and full_name:
                        if self.add_student_to_roster(class_name, full_name):
                            added += 1
                        else:
                            errors += 1
            return True, f"Импортировано {added} учеников, ошибок: {errors}"
        except Exception as e:
            return False, f"Ошибка импорта: {str(e)}"

class RateLimiter:
    def init(self, max_requests=MAX_REQUESTS_PER_MINUTE, window=60):
        self.requests = defaultdict(list)
        self.max_requests = max_requests
        self.window = window
    
    def is_limited(self, user_id):
        now = time.time()
        user_requests = [req for req in self.requests[user_id] if now - req < self.window]
        if len(user_requests) >= self.max_requests: return True
        user_requests.append(now)
        self.requests[user_id] = user_requests[-self.max_requests:]
        return False

class SimpleSchoolBot:
    def init(self):
        self.last_update_id = 0
        self.admin_states = {}
        self.user_states = {}
        self.processed_updates = set()
        self.rate_limiter = RateLimiter()
        self.db = DatabaseManager()
        self.db.create_tables()
        self.setup_scheduler()
    
    def setup_scheduler(self):
        def run_scheduler():
            while True:
                schedule.run_pending()
                time.sleep(60)
        schedule.every().day.at("07:00").do(self.send_weather_notifications)
        schedule.every().day.at("12:00").do(self.send_weather_notifications)
        Thread(target=run_scheduler, daemon=True).start()

    def is_admin(self, username):
        return username and username.lower() in [a.lower() for a in ADMINS]

    def main_menu_keyboard(self):
        return {"keyboard": [[{"text": "📚 Моё расписание"}, {"text": "🏫 Общее расписание"}], [{"text": "🔔 Звонки"}, {"text": "📰 Новости"}], [{"text": "⚙️ Настройки"}, {"text": "🏆 Достижения"}], [{"text": "📈 Статистика"}, {"text": "ℹ️ Помощь"}]], "resize_keyboard": True}
    
    def admin_menu_inline_keyboard(self):
        return {"inline_keyboard": [[{"text": "👥 Список пользователей", "callback_data": "admin_users"}], [{"text": "❌ Удалить пользователя", "callback_data": "admin_delete_user"}], [{"text": "📝 Редактировать расписание", "callback_data": "admin_edit_schedule"}], [{"text": "🏫 Управление классами", "callback_data": "admin_manage_classes"}], [{"text": "🕧 Управление звонками", "callback_data": "admin_bells"}], [{"text": "📤 Загрузить Excel",
        "callback_data": "admin_upload_excel"}], [{"text": "📰 Управление новостями", "callback_data": "admin_manage_news"}], [{"text": "📢 Рассылка сообщений", "callback_data": "admin_broadcast"}], [{"text": "📊 Статистика", "callback_data": "admin_stats"}], [{"text": "⬅️ Назад", "callback_data": "admin_back"}]]}
    
    def cancel_keyboard(self):
        return {"keyboard": [[{"text": "❌ Отменить"}]], "resize_keyboard": True}
    
    def shift_selection_keyboard(self):
        return {"keyboard": [[{"text": "1 смена"}, {"text": "2 смена"}], [{"text": "❌ Отменить"}]], "resize_keyboard": True}

    def day_selection_inline_keyboard(self):
        return {"inline_keyboard": [[{"text": "Понедельник", "callback_data": "day_monday"}], [{"text": "Вторник", "callback_data": "day_tuesday"}], [{"text": "Среда", "callback_data": "day_wednesday"}], [{"text": "Четверг", "callback_data": "day_thursday"}], [{"text": "Пятница", "callback_data": "day_friday"}], [{"text": "Суббота", "callback_data": "day_saturday"}]]}

    def class_selection_keyboard(self):
        classes = [f"{g}{l}" for g in range(5, 10) for l in ['А', 'Б', 'В']] + ["10П", "10Р", "11Р"]
        keyboard = [[{"text": cls}] for cls in classes]
        return {"keyboard": [keyboard[i:i + 3] for i in range(0, len(keyboard), 3)] + [[{"text": "⬅️ Назад"}]], "resize_keyboard": True}

    def safe_message(self, text):
        if not text: return ""
        text = re.sub(r'<script[^>]*>.*?</script>', '', str(text), flags=re.DOTALL | re.IGNORECASE)
        return re.sub(r'<[^>]*>', lambda m: m.group(0) if m.group(0) in ['<b>', '</b>', '<i>', '</i>', '<code>', '</code>'] else '', text)

    def format_date(self, date_obj):
        if not date_obj: return "неизвестно"
        if hasattr(date_obj, 'strftime'): return date_obj.strftime("%d.%m.%Y %H:%M")
        try: return datetime.fromisoformat(str(date_obj).replace('Z', '+00:00')).strftime("%d.%m.%Y %H:%M")
        except: return str(date_obj).split()[0]

    def send_message(self, chat_id, text, reply_markup=None, parse_mode="HTML"):
        url = f"{BASE_URL}/sendMessage"
        data = {"chat_id": chat_id, "text": text[:4000], "parse_mode": parse_mode}
        if reply_markup: data["reply_markup"] = reply_markup
        try:
            res = requests.post(url, json=data, timeout=30).json()
            if len(text) > 4000:
                for i in range(4000, len(text), 4000):
                    requests.post(url, json={"chat_id": chat_id, "text": text[i:i+4000], "parse_mode": parse_mode}, timeout=30)
            return res
        except Exception as e:
            logger.error(f"Ошибка отправки: {e}")
            return None
    
    def answer_callback_query(self, callback_query_id, text=None):
        data = {"callback_query_id": callback_query_id}
        if text: data["text"] = text
        try: requests.post(f"{BASE_URL}/answerCallbackQuery", json=data, timeout=10)
        except Exception as e: logger.error(f"Ошибка callback: {e}")

    def get_updates(self):
        try:
            return requests.get(f"{BASE_URL}/getUpdates", params={"offset": self.last_update_id + 1, "timeout": 30, "limit": 100}, timeout=35).json()
        except requests.exceptions.ReadTimeout: return {"ok": False}
        except Exception as e:
            logger.error(f"Ошибка getUpdates: {e}")
            return {"ok": False}

    def download_file(self, file_path):
        try:
            res = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}", timeout=60)
            return res.content if res.status_code == 200 else None
        except Exception: return None

    def get_file(self, file_id):
        try:
            res = requests.post(f"{BASE_URL}/getFile", json={"file_id": file_id}, timeout=30).json()
            return res["result"] if res.get("ok") else None
        except Exception: return None


    def start_broadcast(self, chat_id, username):
        if not self.is_admin(username): return
        self.admin_states[username] = {"action": "broadcast_waiting_message"}
        self.send_message(chat_id, "📢 Отправьте сообщение для рассылки (поддерживается HTML).", self.cancel_keyboard())

    def execute_broadcast(self, chat_id, username):
        if username not in self.admin_states: return
        msg = self.admin_states[username].get("message", "")
        users = self.db.fetchall("SELECT user_id FROM users")
        if not users or not msg: return self.send_message(chat_id, "❌ Ошибка рассылки")
        
        self.send_message(chat_id, f"🔄 Начинаю рассылку для {len(users)} пользователей...")
        success, failed = 0, 0
        for user in users:
            if self.send_message(user[0], msg): success += 1
            else: failed += 1
            time.sleep(0.1)
        self.send_message(chat_id, f"✅ Успешно: {success}\n❌ Ошибок: {failed}")
        del self.admin_states[username]

    def get_weather(self):
        if not WEATHER_API_KEY: return "Временно недоступна"
        try:
            data = requests.get(f"http://api.weatherapi.com/v1/current.json?key={WEATHER_API_KEY}&q=Otradny,Russia&lang=ru", timeout=10).json()
            if 'error' in data: return data['error']['message']
            cur = data['current']
            return f"🌡️ {cur['temp_c']}°C, {cur['condition']['text']}, 💧 {cur['humidity']}%, 💨 {cur['wind_kph']} км/ч"
        except Exception: return "Временно недоступна"

    def send_weather_notifications(self):
        msg = f"🌤️ Погода:\n{self.get_weather()}"
        users = self.db.fetchall("SELECT user_id FROM notification_settings WHERE weather_notifications = TRUE")
        for u in users:
            self.send_message(u[0], msg)
            time.sleep(0.1)

    def process_update(self, update):
        if "update_id" in update and update["update_id"] in self.processed_updates: return
        self.processed_updates.add(update.get("update_id"))
        
        if "callback_query" in update:
            cb = update["callback_query"]
            chat_id = cb["message"]["chat"]["id"]
            user_id = cb["from"]["id"]
            username = cb["from"].get("username", "")
            data = cb["data"]
            self.answer_callback_query(cb["id"])
            
            if data == "admin_back":
                self.admin_states.pop(username, None)
                self.send_message(chat_id, "Админ-панель", self.admin_menu_inline_keyboard())
            elif data.startswith("day_"):
                day = data.split("_")[1]
                if user_id in self.user_states and self.user_states[user_id].get("action") in ["my_schedule", "general_schedule"]:
                    cls = self.user_states[user_id].get("class") or self.user_states[user_id].get("selected_class")
                    if cls: self.show_schedule(chat_id, cls, day, day)
            
        elif "message" in update and "text" in update["message"]:
            msg = update["message"]
            chat_id = msg["chat"]["id"]
            user_id = msg["from"]["id"]
            username = msg["from"].get("username", "")
            text = msg["text"]

            if text == "❌ Отменить":
                self.admin_states.pop(username, None)
                self.user_states.pop(user_id, None)
                return self.send_message(chat_id, "Отменено", self.main_menu_keyboard())

            if text.startswith("/start"):
                if self.db.fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,)):
                    self.send_message(chat_id, "Вы уже зарегистрированы", self.main_menu_keyboard())
                else:
                    self.user_states[user_id] = {"action": "registration"}
                    self.send_message(chat_id, "Введите данные: Фамилия Имя, Класс (Иванов Иван, 10П)", self.cancel_keyboard())
            elif text.startswith("/admin_panel") and self.is_admin(username):
                self.send_message(chat_id, "Админ-панель", self.admin_menu_inline_keyboard())
            elif user_id in self.user_states and self.user_states[user_id].get("action") == "registration":
                parts = [p.strip() for p in text.split(',')]
                if len(parts) == 2:
                    if self.db.execute("INSERT INTO users (user_id, full_name, class, username) VALUES (?, ?, ?, ?) ON CONFLICT (user_id) DO NOTHING", (user_id, parts[0], parts[1].upper(), username)):
                        self.send_message(chat_id, "Регистрация успешна!", self.main_menu_keyboard())
                        self.user_states.pop(user_id, None)
                else:
                    self.send_message(chat_id, "Неверный формат")

    def show_schedule(self, chat_id, class_name, day_code, day_name):
        sched = self.db.fetchall("SELECT lesson_number, subject, teacher, room FROM schedule WHERE class = ? AND day = ? ORDER BY lesson_number", (class_name, day_code))
        if not sched:
            return self.send_message(chat_id, f"❌ Расписание на {day_name} не найдено")
        res = f"📅 Расписание {class_name}\n\n" + "\n".join([f"{l[0]}. {l[1]} ({l[2] or '-'}) каб.{l[3] or '-'}" for l in sched])
        self.send_message(chat_id, res, self.main_menu_keyboard())

    def run(self):
        try:
            requests.get(f"{BASE_URL}/deleteWebhook", timeout=10)
        except Exception: pass
        while True:
            try:
                updates = self.get_updates()
                for update in updates.get("result", []):
                    self.last_update_id = update["update_id"]
                    self.process_update(update)
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"Main loop error: {e}")
                time.sleep(5)

if name == "main":
    bot = SimpleSchoolBot()
    bot.run()