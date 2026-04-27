import logging
import sqlite3
import requests
import time
import re
import os
import pandas as pd
from datetime import datetime, timedelta
from html import escape
from collections import defaultdict
import io
import psycopg2
from urllib.parse import urlparse
import sys
import json
import pytz
from threading import Thread
import schedule

if not os.environ.get('BOT_TOKEN'):
    logging.error("❌ BOT_TOKEN не установлен!")
    logging.info("Добавьте BOT_TOKEN в настройках Railway")
    sys.exit(1)

if not os.environ.get('DATABASE_URL'):
    if os.environ.get('RAILWAY_ENVIRONMENT'):
        logging.error("❌ DATABASE_URL не установлен в Railway!")
        logging.info("Добавьте PostgreSQL в Railway Dashboard")
        sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN:
    logging.error("BOT_TOKEN environment variable is not set!")
    exit(1)

ADMINS = [admin.strip() for admin in os.environ.get('ADMINS', 'r1kuza,nadya_yakovleva01,Priikalist').split(',') if admin.strip()]
WEATHER_API_KEY = os.environ.get('WEATHER_API_KEY')
SAMARA_TIMEZONE = pytz.timezone('Europe/Samara')

MAX_MESSAGE_LENGTH = 4000
MAX_USERS_PER_CLASS = 30
MAX_REQUESTS_PER_MINUTE = 20

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self):
        self.conn = None
        self.db_type = None
        self.connect()
    
    def connect(self):
        database_url = os.environ.get('DATABASE_URL')
        
        if database_url:
            try:
                url = urlparse(database_url)
                self.conn = psycopg2.connect(
                    database=url.path[1:],
                    user=url.username,
                    password=url.password,
                    host=url.hostname,
                    port=url.port,
                    sslmode='require'
                )
                self.db_type = 'postgresql'
                logger.info("✅ Подключение к PostgreSQL установлено")
            except Exception as e:
                logger.error(f"❌ Ошибка подключения к PostgreSQL: {e}")
                self.fallback_to_sqlite()
        else:
            self.fallback_to_sqlite()
    
    def fallback_to_sqlite(self):
        try:
            db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "school_bot.db")
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
            self.db_type = 'sqlite'
            logger.info("✅ Используется SQLite база данных")
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к SQLite: {e}")
            raise
    
    def execute(self, query, params=None):
        if self.db_type == 'postgresql':
            query = query.replace('?', '%s')
        
        cursor = self.conn.cursor()
        try:
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            self.conn.commit()
            return cursor
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Ошибка выполнения запроса: {e}")
            raise e
    
    def fetchone(self, query, params=None):
        cursor = self.execute(query, params)
        return cursor.fetchone()
    
    def fetchall(self, query, params=None):
        cursor = self.execute(query, params)
        return cursor.fetchall()
    
    def close(self):
        if self.conn:
            self.conn.close()

    def create_tables(self):
        try:    
            self.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    class TEXT NOT NULL,
                    username TEXT,
                    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            self.execute("""
                CREATE TABLE IF NOT EXISTS bell_schedule (
                    lesson_number INTEGER PRIMARY KEY,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL
                )
            """)
            
            self.execute("""
                CREATE TABLE IF NOT EXISTS notification_settings (
                    user_id BIGINT PRIMARY KEY,
                    weather_notifications BOOLEAN DEFAULT FALSE,
                    news_notifications BOOLEAN DEFAULT TRUE,
                    achievement_notifications BOOLEAN DEFAULT TRUE
                )
            """)
            
            self.execute("""
                CREATE TABLE IF NOT EXISTS school_news (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    author TEXT NOT NULL,
                    target_audience TEXT DEFAULT 'all',
                    publish_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_published BOOLEAN DEFAULT TRUE
                )
            """)
            
            self.execute("""
                CREATE TABLE IF NOT EXISTS achievements (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    icon TEXT NOT NULL,
                    condition_type TEXT NOT NULL,
                    condition_value INTEGER
                )
            """)
            
            self.execute("""
                CREATE TABLE IF NOT EXISTS user_achievements (
                    user_id BIGINT,
                    achievement_id INTEGER,
                    achieved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, achievement_id)
                )
            """)
            
            self.execute("""
                CREATE TABLE IF NOT EXISTS user_activity (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    action_type TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    details TEXT
                )
            """)
            
            self.execute("""
                CREATE TABLE IF NOT EXISTS broadcast_messages (
                    id SERIAL PRIMARY KEY,
                    admin_username TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    target_audience TEXT DEFAULT 'all',
                    sent_count INTEGER DEFAULT 0,
                    failed_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'pending'
                )
            """)

            self.execute("""
                CREATE TABLE IF NOT EXISTS class_rosters (
                    id SERIAL PRIMARY KEY,
                    class TEXT NOT NULL,
                    full_name TEXT NOT NULL,
                    UNIQUE(class, full_name)
                )
            """)
            
            logger.info("✅ Таблица class_rosters создана")
            
            result = self.fetchone("SELECT COUNT(*) FROM bell_schedule")
            if result and result[0] == 0:
                bell_schedule = [
                    (1, '8:00', '8:40'),
                    (2, '8:50', '9:30'),
                    (3, '9:40', '10:20'),
                    (4, '10:30', '11:10'),
                    (5, '11:25', '12:05'),
                    (6, '12:10', '12:50'),
                    (7, '13:00', '13:40')
                ]
                for bell in bell_schedule:
                    self.execute(
                        "INSERT INTO bell_schedule (lesson_number, start_time, end_time) VALUES (?, ?, ?)",
                        bell
                    )
                logger.info("✅ Начальные данные для звонков созданы")
            
            self._create_default_achievements()
            self._cleanup_duplicate_achievements()
            
        except Exception as e:
            logger.error(f"Ошибка создания таблиц: {e}")
            raise

    def _create_default_achievements(self):
        default_achievements = [
            ("🎓 Первые шаги", "Зарегистрировался в системе", "🎓", "registration", 1),
            ("📚 Любознательный", "Посмотрел расписание 10 раз", "📚", "schedule_views", 10),
            ("⭐ Активный ученик", "Использовал бота 50 раз", "⭐", "total_actions", 50),
            ("📰 Информированный", "Прочитал 5 новостей", "📰", "news_read", 5),
            ("🌦️ Метеоролог", "Включил уведомления о погоде", "🌦️", "weather_enabled", 1)
        ]
        
        for name, description, icon, condition_type, condition_value in default_achievements:
            # Проверяем, существует ли уже достижение с таким condition_type
            existing = self.fetchone(
                "SELECT 1 FROM achievements WHERE condition_type = ?",
                (condition_type,)
            )
            
            # Добавляем только если не существует
            if not existing:
                self.execute(
                    "INSERT INTO achievements (name, description, icon, condition_type, condition_value) VALUES (?, ?, ?, ?, ?)",
                    (name, description, icon, condition_type, condition_value)
                )
    
    def _cleanup_duplicate_achievements(self):
        """Удаляет дублирующиеся достижения из базы данных"""
        try:
            # Для PostgreSQL
            if self.db_type == 'postgresql':
                self.execute("""
                    DELETE FROM achievements a1
                    USING achievements a2
                    WHERE a1.condition_type = a2.condition_type 
                      AND a1.id > a2.id
                """)
            # Для SQLite
            else:
                self.execute("""
                    DELETE FROM achievements
                    WHERE id NOT IN (
                        SELECT MIN(id)
                        FROM achievements
                        GROUP BY condition_type
                    )
                """)
            logger.info("✅ Дублирующиеся достижения очищены")
        except Exception as e:
            logger.error(f"❌ Ошибка очистки дубликатов: {e}")

class RateLimiter:
    def __init__(self, max_requests=MAX_REQUESTS_PER_MINUTE, window=60):
        self.requests = defaultdict(list)
        self.max_requests = max_requests
        self.window = window
    
    def is_limited(self, user_id):
        now = time.time()
        user_requests = self.requests[user_id]
        user_requests = [req for req in user_requests if now - req < self.window]
        
        if len(user_requests) >= self.max_requests:
            return True
        
        user_requests.append(now)
        self.requests[user_id] = user_requests[-self.max_requests:]
        return False

class SimpleSchoolBot:
    def __init__(self):
        self.last_update_id = 0
        self.admin_states = {}
        self.user_states = {}
        self.processed_updates = set()
        self.rate_limiter = RateLimiter()
        self.db = DatabaseManager()
        
        self.init_db()
        self.setup_scheduler()
    
    def init_db(self):
        self.create_tables()
    
    def create_tables(self):
        self.db.create_tables()
    
    def setup_scheduler(self):
        def run_scheduler():
            while True:
                schedule.run_pending()
                time.sleep(60)
        
        schedule.every().day.at("07:00").do(self.send_weather_notifications)
        schedule.every().day.at("12:00").do(self.send_weather_notifications)
        
        scheduler_thread = Thread(target=run_scheduler, daemon=True)
        scheduler_thread.start()
    
    def start_broadcast(self, chat_id, username):
        if not self.is_admin(username):
            self.send_message(chat_id, "❌ У вас нет прав для рассылки сообщений")
            return
            
        self.admin_states[username] = {"action": "broadcast_waiting_message"}
        self.send_message(
            chat_id,
            "📢 <b>Система рассылки сообщений</b>\n\n"
            "Отправьте сообщение для рассылки всем пользователям.\n\n"
            "Вы можете использовать HTML-разметку:\n"
            "• <code>&lt;b&gt;жирный текст&lt;/b&gt;</code>\n"
            "• <code>&lt;i&gt;курсив&lt;/i&gt;</code>\n"
            "• <code>&lt;code&gt;код&lt;/code&gt;</code>\n\n"
            "Для отмены отправьте /cancel",
            self.cancel_keyboard()
        )
    
    def handle_broadcast_message(self, chat_id, username, text):
        if username not in self.admin_states:
            return
            
        state = self.admin_states[username]
        
        if state.get("action") == "broadcast_waiting_message":
            state["action"] = "broadcast_confirmation"
            state["message"] = text
            
            users_count = self.db.fetchone("SELECT COUNT(*) FROM users")[0]
            
            self.send_message(
                chat_id,
                f"📢 <b>Подтверждение рассылки</b>\n\n"
                f"Сообщение для рассылки:\n\n{text}\n\n"
                f"Получателей: {users_count} пользователей\n\n"
                f"Отправить рассылку?",
                {
                    "inline_keyboard": [
                        [{"text": "✅ Да, отправить", "callback_data": "broadcast_confirm"}],
                        [{"text": "❌ Отменить", "callback_data": "broadcast_cancel"}]
                    ]
                }
            )
    
    def execute_broadcast(self, chat_id, username):
        if username not in self.admin_states:
            return
            
        state = self.admin_states[username]
        message_text = state.get("message", "")
        
        if not message_text:
            self.send_message(chat_id, "❌ Ошибка: сообщение не найдено")
            return
            
        try:
            # Проверяем подключение к базе данных
            users = self.db.fetchall("SELECT user_id FROM users")
            if not users:
                self.send_message(chat_id, "❌ Нет пользователей для рассылки")
                return
            
            total_users = len(users)
            success_count = 0
            failed_count = 0
            
            self.send_message(chat_id, f"🔄 Начинаю рассылку сообщений... Всего пользователей: {total_users}")
            
            # Отправляем сообщение себе (админу) для теста
            test_result = self.send_message(chat_id, f"📢 <b>Тест рассылки</b>\n\n{message_text}")
            if not test_result or not test_result.get('ok'):
                self.send_message(chat_id, "❌ Ошибка: не удалось отправить тестовое сообщение. Проверьте форматирование HTML.")
                return
            
            for i, user in enumerate(users):
                user_id = user[0]
                
                try:
                    # Проверяем, что user_id является числом
                    if isinstance(user_id, (int, float)):
                        result = self.send_message(int(user_id), message_text)
                        if result and result.get('ok'):
                            success_count += 1
                        else:
                            failed_count += 1
                            logger.error(f"Ошибка отправки пользователю {user_id}: {result}")
                    else:
                        failed_count += 1
                        logger.error(f"Некорректный user_id: {user_id}")
                    
                    if i % 10 == 0 and i > 0:
                        progress = (i + 1) * 100 // total_users
                        self.send_message(chat_id, f"📊 Прогресс рассылки: {progress}% ({i+1}/{total_users})")
                    
                    time.sleep(0.2)  # Увеличиваем задержку для избежания лимитов Telegram
                    
                except Exception as e:
                    logger.error(f"Ошибка отправки пользователю {user_id}: {e}")
                    failed_count += 1
                    time.sleep(0.5)
            
            # Сохраняем статистику
            self.db.execute(
                "INSERT INTO broadcast_messages (admin_username, message_text, sent_count, failed_count, status) VALUES (?, ?, ?, ?, ?)",
                (username, message_text, success_count, failed_count, 'completed')
            )
            
            report = (
                f"📢 <b>Рассылка завершена</b>\n\n"
                f"✅ Успешно отправлено: {success_count}\n"
                f"❌ Не удалось отправить: {failed_count}\n"
                f"👥 Всего пользователей: {total_users}\n"
                f"📊 Успешных: {success_count * 100 // total_users if total_users > 0 else 0}%"
            )
            
            self.send_message(chat_id, report)
            
        except Exception as e:
            logger.error(f"Ошибка выполнения рассылки: {e}")
            self.send_message(chat_id, f"❌ Ошибка при выполнении рассылки: {str(e)}")
        
        finally:
            if username in self.admin_states:
                del self.admin_states[username]
    
    def get_broadcast_history(self, chat_id):
        broadcasts = self.db.fetchall(
            "SELECT admin_username, message_text, sent_count, failed_count, created_at FROM broadcast_messages ORDER BY created_at DESC LIMIT 10"
        )
        
        if not broadcasts:
            self.send_message(chat_id, "📋 История рассылок пуста")
            return
            
        history_text = "📋 <b>История рассылок</b>\n\n"
        
        for broadcast in broadcasts:
            admin, message, sent, failed, created_at = broadcast
            date_str = self.format_date(created_at)
            preview = message[:50] + "..." if len(message) > 50 else message
            
            history_text += (
                f"👤 <b>Админ:</b> {admin}\n"
                f"📅 <b>Дата:</b> {date_str}\n"
                f"📨 <b>Статус:</b> {sent} ✓ / {failed} ✗\n"
                f"💬 <b>Сообщение:</b> {preview}\n"
                f"{'─' * 30}\n"
            )
        
        self.send_message(chat_id, history_text)
    
    def get_notification_settings(self, user_id):
        result = self.db.fetchone(
            "SELECT weather_notifications, news_notifications, achievement_notifications FROM notification_settings WHERE user_id = ?",
            (user_id,)
        )
        if result:
            return {
                'weather_notifications': result[0],
                'news_notifications': result[1],
                'achievement_notifications': result[2]
            }
        else:
            self.db.execute(
                "INSERT INTO notification_settings (user_id) VALUES (?)",
                (user_id,)
            )
            return {
                'weather_notifications': False,
                'news_notifications': True,
                'achievement_notifications': True
            }
    
    def update_notification_settings(self, user_id, settings):
        self.db.execute(
            """INSERT INTO notification_settings 
            (user_id, weather_notifications, news_notifications, achievement_notifications) 
            VALUES (?, ?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
            weather_notifications = EXCLUDED.weather_notifications,
            news_notifications = EXCLUDED.news_notifications,
            achievement_notifications = EXCLUDED.achievement_notifications""",
            (user_id, settings.get('weather_notifications', False),
             settings.get('news_notifications', True), settings.get('achievement_notifications', True))
        )
    
    def add_news(self, title, content, author, target_audience="all"):
        self.db.execute(
            "INSERT INTO school_news (title, content, author, target_audience) VALUES (?, ?, ?, ?)",
            (title, content, author, target_audience)
        )
        self.notify_about_news(title, content)
        return True
    
    def get_news(self, limit=10, for_class=None):
        if for_class:
            return self.db.fetchall(
                """SELECT id, title, content, author, publish_date, target_audience
                FROM school_news 
                WHERE (target_audience = ? OR target_audience = 'all') AND is_published = TRUE
                ORDER BY publish_date DESC LIMIT ?""",
                (for_class, limit)
            )
        else:
            return self.db.fetchall(
                """SELECT id, title, content, author, publish_date, target_audience
                FROM school_news 
                WHERE is_published = TRUE
                ORDER BY publish_date DESC LIMIT ?""",
                (limit,)
            )
    
    def get_news_by_id(self, news_id):
        return self.db.fetchone(
            "SELECT id, title, content, author, target_audience, publish_date FROM school_news WHERE id = ?",
            (news_id,)
        )
    
    def update_news(self, news_id, title=None, content=None, author=None, target_audience=None):
        try:
            if title is not None:
                self.db.execute("UPDATE school_news SET title = ? WHERE id = ?", (title, news_id))
            if content is not None:
                self.db.execute("UPDATE school_news SET content = ? WHERE id = ?", (content, news_id))
            if author is not None:
                self.db.execute("UPDATE school_news SET author = ? WHERE id = ?", (author, news_id))
            if target_audience is not None:
                self.db.execute("UPDATE school_news SET target_audience = ? WHERE id = ?", (target_audience, news_id))
            return True
        except Exception as e:
            logger.error(f"Ошибка обновления новости: {e}")
            return False
    
    def delete_news(self, news_id):
        try:
            self.db.execute("DELETE FROM school_news WHERE id = ?", (news_id,))
            return True
        except Exception as e:
            logger.error(f"Ошибка удаления новости: {e}")
            return False
    
    def get_all_news(self, limit=50):
        return self.db.fetchall(
            """SELECT id, title, content, author, target_audience, publish_date 
            FROM school_news 
            ORDER BY publish_date DESC LIMIT ?""",
            (limit,)
        )
    
    def notify_about_news(self, title, content):
        users = self.db.fetchall(
            "SELECT user_id FROM notification_settings WHERE news_notifications = TRUE"
        )
        
        # Создаем краткое уведомление
        message = f"📰 <b>Новая школьная новость</b>\n\n<b>{self.safe_message(title)}</b>\n\n"
        
        # Обрезаем для уведомления
        if len(content) > 200:
            message += f"{self.safe_message(content[:200])}..."
        else:
            message += self.safe_message(content)
        
        for user in users:
            try:
                self.send_message(user[0], message)
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления пользователю {user[0]}: {e}")
    
    def check_achievements(self, user_id, action_type, value=1):
        """Проверка достижений с защитой от повторной выдачи"""
        logger.info(f"🔍 Проверка достижений для пользователя {user_id}, действие: {action_type}")
        
        # Получаем достижения для этого типа действия
        achievements = self.db.fetchall(
            "SELECT id, name, description, icon, condition_type, condition_value FROM achievements WHERE condition_type = ?",
            (action_type,)
        )
        
        for achievement in achievements:
            achievement_id, name, description, icon, condition_type, condition_value = achievement
            
            # Проверяем, есть ли уже это достижение у пользователя
            existing = self.db.fetchone(
                "SELECT 1 FROM user_achievements WHERE user_id = ? AND achievement_id = ?",
                (user_id, achievement_id)
            )
            
            # Если уже есть - пропускаем
            if existing:
                logger.info(f"✓ Достижение '{name}' уже получено пользователем {user_id}")
                continue
            
            # Проверяем прогресс пользователя
            user_progress = self.get_user_achievement_progress(user_id, condition_type)
            logger.info(f"📊 Прогресс пользователя {user_id} для '{name}': {user_progress}/{condition_value}")
            
            # Если достигнут необходимый прогресс
            if user_progress >= condition_value:
                logger.info(f"🎉 Пользователь {user_id} выполнил условие для достижения '{name}'")
                # Выдаем достижение
                self.grant_achievement(user_id, achievement_id, name, description, icon)
    
    def get_user_achievement_progress(self, user_id, condition_type):
        if condition_type == "registration":
            # Проверяем, зарегистрирован ли пользователь
            user = self.get_user(user_id)
            return 1 if user else 0
        elif condition_type == "schedule_views":
            result = self.db.fetchone(
                "SELECT COUNT(*) FROM user_activity WHERE user_id = ? AND action_type = 'schedule_view'",
                (user_id,)
            )
            return result[0] if result else 0
        elif condition_type == "total_actions":
            result = self.db.fetchone(
                "SELECT COUNT(*) FROM user_activity WHERE user_id = ?",
                (user_id,)
            )
            return result[0] if result else 0
        elif condition_type == "news_read":
            result = self.db.fetchone(
                "SELECT COUNT(*) FROM user_activity WHERE user_id = ? AND action_type = 'news_read'",
                (user_id,)
            )
            return result[0] if result else 0
        elif condition_type == "weather_enabled":
            settings = self.get_notification_settings(user_id)
            return 1 if settings.get('weather_notifications') else 0
        
        return 0
    
    def grant_achievement(self, user_id, achievement_id, name, description, icon):
        """Выдача достижения с проверкой дубликатов"""
        logger.info(f"🎁 Попытка выдать достижение '{name}' пользователю {user_id}")
        
        # Проверяем, есть ли уже это достижение у пользователя
        existing = self.db.fetchone(
            "SELECT 1 FROM user_achievements WHERE user_id = ? AND achievement_id = ?",
            (user_id, achievement_id)
        )
        
        if existing:
            logger.info(f"⚠️ Достижение '{name}' уже есть у пользователя {user_id}, пропускаем")
            return
        
        try:
            # Добавляем достижение с обработкой конфликта
            if self.db.db_type == 'postgresql':
                query = """
                    INSERT INTO user_achievements (user_id, achievement_id) 
                    VALUES (%s, %s) 
                    ON CONFLICT (user_id, achievement_id) DO NOTHING
                    RETURNING 1
                """
            else:
                query = """
                    INSERT OR IGNORE INTO user_achievements (user_id, achievement_id) 
                    VALUES (?, ?)
                """
            
            result = self.db.execute(query, (user_id, achievement_id))
            
            # Проверяем, была ли вставлена новая запись
            if self.db.db_type == 'postgresql':
                inserted = result.fetchone() is not None
            else:
                inserted = result.rowcount > 0
            
            if inserted:
                # Логируем действие
                logger.info(f"✅ Достижение '{name}' успешно выдано пользователю {user_id}")
                
                # Проверяем настройки уведомлений
                settings = self.get_notification_settings(user_id)
                if settings.get('achievement_notifications'):
                    message = f"{icon} <b>Новое достижение!</b>\n\n<b>{name}</b>\n{description}"
                    self.send_message(user_id, message)
                    logger.info(f"📨 Уведомление о достижении отправлено пользователю {user_id}")
                else:
                    logger.info(f"🔕 Уведомления о достижениях отключены для пользователя {user_id}")
            else:
                logger.info(f"⏩ Достижение '{name}' уже было выдано пользователю {user_id} ранее")
                
        except Exception as e:
            logger.error(f"❌ Ошибка при выдаче достижения: {e}")
    
    def get_user_achievements(self, user_id):
        return self.db.fetchall("""
            SELECT a.name, a.description, a.icon, ua.achieved_at 
            FROM user_achievements ua 
            JOIN achievements a ON ua.achievement_id = a.id 
            WHERE ua.user_id = ? 
            ORDER BY ua.achieved_at DESC
        """, (user_id,))
    
    def get_weather(self):
        if not WEATHER_API_KEY:
            return "🌤️ Погода в Отрадном: сервис погоды не настроен"
        
        try:
            # Изменено на город Отрадный
            url = f"http://api.weatherapi.com/v1/current.json?key={WEATHER_API_KEY}&q=Otradny,Russia&lang=ru"
            response = requests.get(url, timeout=10)
            data = response.json()
            
            if 'error' in data:
                return f"🌤️ Погода в Отрадном: {data['error']['message']}"
            
            current = data['current']
            temp = current['temp_c']
            condition = current['condition']['text']
            humidity = current['humidity']
            wind = current['wind_kph']
            
            return (f"🌤️ <b>Погода в Отрадном</b>\n\n"
                   f"🌡️ Температура: {temp}°C\n"
                   f"☁️ Состояние: {condition}\n"
                   f"💧 Влажность: {humidity}%\n"
                   f"💨 Ветер: {wind} км/ч")
        
        except Exception as e:
            logger.error(f"Ошибка получения погоды: {e}")
            return "🌤️ Погода в Отрадном: временно недоступна"
    
    def send_weather_notifications(self):
        try:
            users = self.db.fetchall(
                "SELECT user_id FROM notification_settings WHERE weather_notifications = TRUE"
            )
            
            if not users:
                logger.info("Нет пользователей с включенными уведомлениями о погоде")
                return
            
            weather_message = self.get_weather()
            logger.info(f"Отправка уведомлений о погоде {len(users)} пользователям")
            
            success_count = 0
            failed_count = 0
            
            for user in users:
                try:
                    user_id = user[0]
                    self.send_message(user_id, weather_message)
                    success_count += 1
                    time.sleep(0.1)  # Небольшая задержка между отправками
                except Exception as e:
                    logger.error(f"Ошибка отправки погоды пользователю {user_id}: {e}")
                    failed_count += 1
            
            logger.info(f"Уведомления о погоде отправлены: успешно {success_count}, неудачно {failed_count}")
            
        except Exception as e:
            logger.error(f"Ошибка при отправке уведомлений о погоде: {e}")
    
    def log_user_activity(self, user_id, action_type, details=None):
        try:
            self.db.execute(
                "INSERT INTO user_activity (user_id, action_type, details) VALUES (?, ?, ?)",
                (user_id, action_type, details)
            )
        except Exception as e:
            logger.error(f"Ошибка логирования активности: {e}")
    
    def get_user_statistics(self, user_id):
        total_actions = self.db.fetchone(
            "SELECT COUNT(*) FROM user_activity WHERE user_id = ?",
            (user_id,)
        )
        total_actions = total_actions[0] if total_actions else 0
        
        schedule_views = self.db.fetchone(
            "SELECT COUNT(*) FROM user_activity WHERE user_id = ? AND action_type = 'schedule_view'",
            (user_id,)
        )
        schedule_views = schedule_views[0] if schedule_views else 0
        
        news_read = self.db.fetchone(
            "SELECT COUNT(*) FROM user_activity WHERE user_id = ? AND action_type = 'news_read'",
            (user_id,)
        )
        news_read = news_read[0] if news_read else 0
        
        last_active = self.db.fetchone(
            "SELECT timestamp FROM user_activity WHERE user_id = ? ORDER BY timestamp DESC LIMIT 1",
            (user_id,)
        )
        
        return {
            'total_actions': total_actions,
            'schedule_views': schedule_views,
            'news_read': news_read,
            'last_active': last_active[0] if last_active else None
        }
    
    def format_date(self, date_obj):
        if not date_obj:
            return "неизвестно"
        
        if hasattr(date_obj, 'strftime'):
            return date_obj.strftime("%d.%m.%Y %H:%M")
        elif isinstance(date_obj, str):
            try:
                dt = datetime.fromisoformat(date_obj.replace('Z', '+00:00'))
                return dt.strftime("%d.%m.%Y %H:%M")
            except:
                return date_obj.split()[0]
        else:
            return str(date_obj)
    
    def safe_message(self, text):
        if not text:
            return ""
        text = str(text)
        # Убираем только опасные теги, но сохраняем безопасные HTML
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]*>', lambda m: m.group(0) if m.group(0) in ['<b>', '</b>', '<i>', '</i>', '<code>', '</code>'] else '', text)
        return text
    
    def truncate_message(self, text, max_length=MAX_MESSAGE_LENGTH):
        if len(text) <= max_length:
            return text
        return text[:max_length-3] + "..."
    
    def send_message(self, chat_id, text, reply_markup=None, parse_mode="HTML"):
        try:
            # Разбиваем длинные сообщения на части
            if len(text) > 4096:
                logger.info(f"Сообщение слишком длинное ({len(text)} символов), разбиваем на части")
                
                # Отправляем первую часть
                first_part = text[:4000]
                result = self._send_message_part(chat_id, first_part, reply_markup, parse_mode)
                
                # Отправляем остальные части без клавиатуры
                remaining_text = text[4000:]
                chunk_size = 4000
                for i in range(0, len(remaining_text), chunk_size):
                    chunk = remaining_text[i:i + chunk_size]
                    self._send_message_part(chat_id, chunk, None, parse_mode)
                
                return result
            else:
                return self._send_message_part(chat_id, text, reply_markup, parse_mode)
                
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения: {e}")
            return None

    def _send_message_part(self, chat_id, text, reply_markup=None, parse_mode="HTML"):
        """Вспомогательный метод для отправки части сообщения"""
        url = f"{BASE_URL}/sendMessage"
        data = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode
        }
        if reply_markup:
            data["reply_markup"] = reply_markup
        
        try:
            response = requests.post(url, json=data, timeout=30)
            result = response.json()
            
            if not result.get('ok'):
                logger.error(f"Ошибка отправки части сообщения: {result}")
            
            return result
        except Exception as e:
            logger.error(f"Ошибка отправки части сообщения: {e}")
            return None

    def send_document(self, chat_id, document, filename=None):
        url = f"{BASE_URL}/sendDocument"
        data = {"chat_id": chat_id}
        files = {"document": (filename, document)}
        
        try:
            response = requests.post(url, data=data, files=files, timeout=60)
            return response.json()
        except Exception as e:
            logger.error(f"Ошибка отправки документа: {e}")
            return None
    
    def get_file(self, file_id):
        url = f"{BASE_URL}/getFile"
        data = {"file_id": file_id}
        
        try:
            response = requests.post(url, json=data, timeout=30)
            result = response.json()
            if result.get("ok"):
                return result["result"]
            return None
        except Exception as e:
            logger.error(f"Ошибка получения файла: {e}")
            return None
    
    def download_file(self, file_path):
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        
        try:
            response = requests.get(url, timeout=60)
            if response.status_code == 200:
                return response.content
            return None
        except Exception as e:
            logger.error(f"Ошибка загрузки файла: {e}")
            return None
    
    def log_security_event(self, event_type, user_id, details):
        logger.warning(f"SECURITY: {event_type} - User: {user_id} - {details}")
    
    def get_updates(self):
        url = f"{BASE_URL}/getUpdates"
        params = {
            "offset": self.last_update_id + 1,
            "timeout": 30,
            "limit": 100
        }
        
        try:
            response = requests.get(url, params=params, timeout=35)
            result = response.json()
            
            if not result.get("ok") and "Conflict" in str(result.get("description", "")):
                logger.warning("Обнаружен конфликт getUpdates")
                return {"ok": False, "conflict": True}
                
            return result
        except requests.exceptions.ReadTimeout:
            logger.warning("⚠️ Таймаут получения обновлений, продолжаем работу...")
            return {"ok": False}
        except Exception as e:
            logger.error(f"Ошибка получения обновлений: {e}")
            return {"ok": False}
    
    def get_user(self, user_id):
        if not self.is_valid_user_id(user_id):
            return None
            
        try:
            return self.db.fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))
        except Exception as e:
            logger.error(f"Ошибка получения пользователя: {e}")
            return None

    def find_user_by_username(self, username):
        try:
            return self.db.fetchone("SELECT * FROM users WHERE username = ?", (username,))
        except Exception as e:
            logger.error(f"Ошибка поиска пользователя по username: {e}")
            return None
    
    def is_valid_user_id(self, user_id):
        return isinstance(user_id, int) and user_id > 0
    
    def create_user(self, user_id, full_name, class_name, username=None):
        if not self.is_valid_user_id(user_id):
            return False
            
        try:
            result = self.db.fetchone("SELECT COUNT(*) FROM users WHERE class = ?", (class_name,))
            count = result[0] if result else 0
            
            if count >= MAX_USERS_PER_CLASS:
                self.log_security_event("class_limit_exceeded", user_id, f"Class: {class_name}")
                return False
            
            self.db.execute(
                "INSERT INTO users (user_id, full_name, class, username) VALUES (?, ?, ?, ?) ON CONFLICT (user_id) DO UPDATE SET full_name = EXCLUDED.full_name, class = EXCLUDED.class, username = EXCLUDED.username",
                (user_id, full_name, class_name, username)
            )
            
            # Проверяем достижение при регистрации
            self.check_achievements(user_id, "registration")
            
            return True
        except Exception as e:
            logger.error(f"Ошибка создания пользователя: {e}")
            return False
    
    def delete_user(self, user_id):
        if not self.is_valid_user_id(user_id):
            return False
            
        try:
            self.db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            return True
        except Exception as e:
            logger.error(f"Ошибка удаления пользователя: {e}")
            return False

    def delete_user_by_username(self, username):
        try:
            self.db.execute("DELETE FROM users WHERE username = ?", (username,))
            return True
        except Exception as e:
            logger.error(f"Ошибка удаления пользователя по username: {e}")
            return False
    
    def get_all_users(self):
        try:
            return self.db.fetchall("SELECT user_id, full_name, class, username, registered_at FROM users ORDER BY registered_at DESC")
        except Exception as e:
            logger.error(f"Ошибка получения пользователей: {e}")
            return []
    
    def get_schedule(self, class_name, day):
        try:
            return self.db.fetchall(
                "SELECT lesson_number, subject, teacher, room FROM schedule WHERE class = ? AND day = ? ORDER BY lesson_number",
                (class_name, day)
            )
        except Exception as e:
            logger.error(f"Ошибка получения расписания: {e}")
            return []
    
    def save_schedule(self, class_name, day, lessons):
        try:
            self.db.execute("DELETE FROM schedule WHERE class = ? AND day = ?", (class_name, day))
            
            for lesson_num, subject, teacher, room in lessons:
                subject = subject[:100] if subject else ""
                teacher = teacher[:50] if teacher else ""
                room = room[:20] if room else ""
                
                self.db.execute(
                    "INSERT INTO schedule (class, day, lesson_number, subject, teacher, room) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (class, day, lesson_number) DO UPDATE SET subject = EXCLUDED.subject, teacher = EXCLUDED.teacher, room = EXCLUDED.room",
                    (class_name, day, lesson_num, subject, teacher, room)
                )
            
            return True
        except Exception as e:
            logger.error(f"Ошибка сохранения расписания: {e}")
            return False
    
    def get_bell_schedule(self):
        try:
            return self.db.fetchall("SELECT lesson_number, start_time, end_time FROM bell_schedule ORDER BY lesson_number")
        except Exception as e:
            logger.error(f"Ошибка получения расписания звонков: {e}")
            return []
    
    def is_admin(self, username):
        return username and username.lower() in [admin.lower() for admin in ADMINS]
    
    def main_menu_keyboard(self):
        return {
            "keyboard": [
                [{"text": "📚 Моё расписание"}, {"text": "🏫 Общее расписание"}],
                [{"text": "🔔 Звонки"}, {"text": "📰 Новости"}],
                [{"text": "⚙️ Настройки"}, {"text": "🏆 Достижения"}],
                [{"text": "📈 Статистика"}, {"text": "ℹ️ Помощь"}]
            ],
            "resize_keyboard": True
        }
    
    def admin_menu_inline_keyboard(self):
        return {
            "inline_keyboard": [
                [{"text": "👥 Список пользователей", "callback_data": "admin_users"}],
                [{"text": "❌ Удалить пользователя", "callback_data": "admin_delete_user"}],
                [{"text": "📝 Редактировать расписание", "callback_data": "admin_edit_schedule"}],
                [{"text": "🏫 Управление классами", "callback_data": "admin_manage_classes"}],
                [{"text": "🕧 Управление звонками", "callback_data": "admin_bells"}],
                [{"text": "📤 Загрузить Excel", "callback_data": "admin_upload_excel"}],
                [{"text": "📰 Управление новостями", "callback_data": "admin_manage_news"}],
                [{"text": "📢 Рассылка сообщений", "callback_data": "admin_broadcast"}],
                [{"text": "📊 Статистика", "callback_data": "admin_stats"}],
                [{"text": "⬅️ Назад", "callback_data": "admin_back"}]
            ]
        }
    
    def news_management_inline_keyboard(self):
        return {
            "inline_keyboard": [
                [{"text": "➕ Добавить новость", "callback_data": "admin_add_news"}],
                [{"text": "📝 Редактировать новость", "callback_data": "admin_edit_news"}],
                [{"text": "🗑️ Удалить новость", "callback_data": "admin_delete_news"}],
                [{"text": "📋 Список новостей", "callback_data": "admin_list_news"}],
                [{"text": "⬅️ Назад в админку", "callback_data": "admin_back"}]
            ]
        }

    def roster_management_inline_keyboard(self):
            return {
                "inline_keyboard": [
                    [{"text": "➕ Добавить ученика", "callback_data": "roster_add"}],
                    [{"text": "➖ Удалить ученика", "callback_data": "roster_remove"}],
                    [{"text": "👥 Просмотр списка", "callback_data": "roster_view"}],
                    [{"text": "📤 Импорт из Excel", "callback_data": "roster_import"}],
                    [{"text": "⬅️ Назад в админку", "callback_data": "admin_back"}]
                ]
            }

    def show_roster_management(self, chat_id, username):
            """Показать меню управления списками учеников"""
            if not self.is_admin(username):
                return
            
            # Статистика
            total_students = self.db.fetchone("SELECT COUNT(*) FROM class_rosters")
            total_students = total_students[0] if total_students else 0
            
            classes_count = self.db.fetchone("SELECT COUNT(DISTINCT class) FROM class_rosters")
            classes_count = classes_count[0] if classes_count else 0
            
            text = (f"📋 <b>Управление списками учеников</b>\n\n"
                f"📊 Статистика:\n"
                f"• Всего учеников: {total_students}\n"
                f"• Классов в системе: {classes_count}\n\n"
                f"<b>Формат данных:</b>\n"
                f"• ФИО: <i>Иванов Иван</i>\n"
                f"• Класс: <i>10П</i>\n\n"
                f"Выберите действие:")
            
            self.send_message(chat_id, text, self.roster_management_inline_keyboard())
        
    def start_add_student(self, chat_id, username):
            """Начать добавление ученика в список"""
            if not self.is_admin(username):
                return
            
            self.admin_states[username] = {"action": "roster_add_waiting_data"}
            self.send_message(
                chat_id,
                "➕ <b>Добавление ученика в список</b>\n\n"
                "Введите данные в формате:\n"
                "<b>Класс, Фамилия Имя</b>\n\n"
                "Например: <i>10П, Иванов Иван</i>\n\n"
                "Для отмены нажмите '❌ Отменить'",
                self.cancel_keyboard()
            )

    def start_remove_student(self, chat_id, username):
            """Начать удаление ученика из списка"""
            if not self.is_admin(username):
                return
            
            self.admin_states[username] = {"action": "roster_remove_waiting_data"}
            self.send_message(
                chat_id,
                "➖ <b>Удаление ученика из списка</b>\n\n"
                "Введите данные в формате:\n"
                "<b>Класс, Фамилия Имя</b>\n\n"
                "Например: <i>10П, Иванов Иван</i>\n\n"
                "Для отмены нажмите '❌ Отменить'",
                self.cancel_keyboard()
            )

    def start_view_roster(self, chat_id, username):
            """Просмотр списка учеников класса"""
            if not self.is_admin(username):
                return
            
            self.admin_states[username] = {"action": "roster_view_waiting_class"}
            self.send_message(
                chat_id,
                "👥 <b>Просмотр списка учеников</b>\n\n"
                "Введите название класса:\n\n"
                "Например: <i>10П</i>\n\n"
                "Для отмены нажмите '❌ Отменить'",
                self.cancel_keyboard()
            )

    def start_roster_import(self, chat_id, username):
            """Импорт списка учеников из Excel"""
            if not self.is_admin(username):
                return
            
            self.admin_states[username] = {"action": "roster_waiting_excel"}
            self.send_message(
                chat_id,
                "📤 <b>Импорт списка учеников из Excel</b>\n\n"
                "Отправьте Excel файл с двумя колонками:\n"
                "1. Класс (например: 10П)\n"
                "2. ФИО (например: Иванов Иван)\n\n"
                "Файл должен быть в формате .xlsx или .xls",
                self.cancel_keyboard()
            )
    
    def notifications_settings_keyboard(self):
        return {
            "inline_keyboard": [
                [{"text": "🌤️ Уведомления о погоде", "callback_data": "toggle_weather"}],
                [{"text": "📰 Новости школы", "callback_data": "toggle_news"}],
                [{"text": "🏆 Достижения", "callback_data": "toggle_achievements"}],
                [{"text": "⬅️ Назад", "callback_data": "settings_back"}]
            ]
        }
    
    def achievements_keyboard(self):
        return {
            "inline_keyboard": [
                [{"text": "🏆 Мои достижения", "callback_data": "my_achievements"}],
                [{"text": "📊 Прогресс", "callback_data": "achievement_progress"}],
                [{"text": "⬅️ Назад", "callback_data": "achievements_back"}]
            ]
        }
    
    def news_keyboard(self):
        return {
            "inline_keyboard": [
                [{"text": "📰 Последние новости", "callback_data": "recent_news"}],
                [{"text": "📊 Статистика новостей", "callback_data": "news_stats"}],
                [{"text": "🔍 Поиск новостей", "callback_data": "news_search"}],
                [{"text": "⬅️ Назад", "callback_data": "news_back"}]
            ]
        }
    def statistics_keyboard(self):
        return {
            "inline_keyboard": [
                [{"text": "📈 Моя статистика", "callback_data": "my_statistics"}],
                [{"text": "🏆 Достижения", "callback_data": "my_achievements"}],
                [{"text": "⬅️ Назад", "callback_data": "stats_back"}]
            ]
        }

    def classes_management_inline_keyboard(self):
        return {
            "inline_keyboard": [
                [{"text": "➕ Добавить класс", "callback_data": "admin_add_class"}],
                [{"text": "➖ Удалить класс", "callback_data": "admin_delete_class"}],
                [{"text": "⬅️ Назад в админку", "callback_data": "admin_back"}]
            ]
        }
    
    def bells_management_inline_keyboard(self):
        return {
            "inline_keyboard": [
                [{"text": "✏️ Изменить звонок", "callback_data": "admin_edit_bell"}],
                [{"text": "👀 Посмотреть все звонки", "callback_data": "admin_view_bells"}],
                [{"text": "⬅️ Назад в админку", "callback_data": "admin_back"}]
            ]
        }
    
    def day_selection_inline_keyboard(self):
        return {
            "inline_keyboard": [
                [{"text": "Понедельник", "callback_data": "day_monday"}],
                [{"text": "Вторник", "callback_data": "day_tuesday"}],
                [{"text": "Среда", "callback_data": "day_wednesday"}],
                [{"text": "Четверг", "callback_data": "day_thursday"}],
                [{"text": "Пятница", "callback_data": "day_friday"}],
                [{"text": "Суббота", "callback_data": "day_saturday"}]
            ]
        }
    
    def class_selection_keyboard(self):
        classes = []
        
        for grade in range(5, 10):
            for letter in ['А', 'Б', 'В']:
                classes.append(f"{grade}{letter}")
        
        classes.extend(["10П", "10Р", "11Р"])
        
        keyboard = []
        row = []
        for i, cls in enumerate(classes):
            row.append({"text": cls})
            if (i + 1) % 3 == 0:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        
        keyboard.append([{"text": "⬅️ Назад"}])
        
        return {"keyboard": keyboard, "resize_keyboard": True}
    
    def shift_selection_keyboard(self):
        return {
            "keyboard": [
                [{"text": "1 смена"}, {"text": "2 смена"}],
                [{"text": "❌ Отменить"}]
            ],
            "resize_keyboard": True
        }
    
    def cancel_keyboard(self):
        return {
            "keyboard": [[{"text": "❌ Отменить"}]],
            "resize_keyboard": True
        }
    
    def is_valid_class(self, class_str):
        class_str = class_str.strip().upper()
        
        if re.match(r'^[5-9][А-В]$', class_str):
            return True
        
        if class_str in ['10П', '10Р', '11Р']:
            return True
        
        return False
    
    def is_valid_fullname(self, name):
        name = name.strip()
        if len(name) > 100:
            return False
            
        parts = name.split()
        if len(parts) < 2:
            return False
        
        for part in parts:
            if not part.isalpha() or len(part) < 2 or len(part) > 20:
                return False
        
        return True
    
    def is_valid_time(self, time_str):
        return bool(re.match(r'^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$', time_str))
    
    def get_existing_classes(self):
        try:
            result = self.db.fetchall("SELECT DISTINCT class FROM users ORDER BY class")
            return [row[0] for row in result]
        except Exception as e:
            logger.error(f"Ошибка получения классов: {e}")
            return []
    
    def add_class(self, class_name):
        return self.is_valid_class(class_name)
    
    def delete_class(self, class_name):
        try:
            self.db.execute("DELETE FROM users WHERE class = ?", (class_name,))
            return True
        except Exception as e:
            logger.error(f"Ошибка удаления класса: {e}")
            return False
    
    def update_bell_schedule(self, lesson_number, start_time, end_time):
        try:
            self.db.execute(
                "UPDATE bell_schedule SET start_time = ?, end_time = ? WHERE lesson_number = ?",
                (start_time, end_time, lesson_number)
            )
            return True
        except Exception as e:
            logger.error(f"Ошибка обновления расписания звонков: {e}")
            return False

    def parse_excel_schedule(self, file_content, shift):
        try:
            import pandas as pd
            
            lessons_data = []
            
            logger.info(f"=== НАЧАЛО ПАРСИНГА ДЛЯ СМЕНЫ {shift} ===")
            logger.info("Используется метод парсинга: method3 (структурный)")
            
            try:
                excel_file = pd.ExcelFile(io.BytesIO(file_content))
                sheet_names = excel_file.sheet_names
                logger.info(f"Доступные листы в файе: {sheet_names}")
                
                selected_sheet = self._select_sheet(sheet_names, shift)
                if not selected_sheet:
                    logger.error("Не удалось найти подходящий лист!")
                    return None
                
                logger.info(f"Выбран лист: '{selected_sheet}'")
                
                df = pd.read_excel(io.BytesIO(file_content), sheet_name=selected_sheet, header=None)
                logger.info(f"Размер таблицы: {df.shape} (строк: {df.shape[0]}, колонок: {df.shape[1]})")
                
                self._log_file_structure(df, selected_sheet)
                
                success = self._parse_method3(df, shift, lessons_data, selected_sheet)
                
                if not success:
                    logger.error("Метод парсинга не дал результатов")
                    return None
                
            except Exception as e:
                logger.error(f"Ошибка чтения Excel файла для смены {shift}: {e}")
                import traceback
                logger.error(f"Трассировка: {traceback.format_exc()}")
                return None
            
            logger.info(f"=== ЗАВЕРШЕНИЕ ПАРСИНГА ДЛЯ СМЕНЫ {shift} ===")
            logger.info(f"Найдено уроков: {len(lessons_data)}")
            
            if lessons_data:
                class_stats = {}
                for lesson in lessons_data:
                    class_name = lesson['class']
                    class_stats[class_name] = class_stats.get(class_name, 0) + 1
                
                logger.info(f"Статистика по классам: {class_stats}")
            
            return lessons_data if lessons_data else None
            
        except Exception as e:
            logger.error(f"Общая ошибка парсинга Excel для смены {shift}: {e}")
            import traceback
            logger.error(f"Трассировка: {traceback.format_exc()}")
            return None

    def _parse_method3(self, df, shift, lessons_data, sheet_name):
        try:
            logger.info("=== МЕТОД 3: СТРУКТУРНЫЙ ПАРСИНГ ===")
            
            class_row_idx = self._find_class_header_row(df)
            if class_row_idx is None:
                logger.error("Не удалось найти строку с заголовками классов")
                return False
            
            logger.info(f"Найдена строка с классами: строка {class_row_idx}")
            
            class_columns = self._extract_class_columns(df, class_row_idx)
            if not class_columns:
                logger.error("Не удалось определить классы и их колонки")
                return False
            
            logger.info(f"Найдены классы и колонки: {class_columns}")
            
            day_rows = self._find_day_rows(df)
            if not day_rows:
                logger.error("Не удалось найти дни недели")
                return False
            
            logger.info(f"Найдены дни недели: {day_rows}")
            
            for day_name, day_row_idx in day_rows:
                logger.info(f"Обрабатываем день: {day_name} (строка {day_row_idx})")
                
                next_day_idx = None
                for next_day, next_idx in day_rows:
                    if next_idx > day_row_idx:
                        next_day_idx = next_idx
                        break
                
                end_row = next_day_idx if next_day_idx else len(df)
                
                day_lessons = self._parse_day_schedule(df, day_row_idx, end_row, class_columns, shift, day_name)
                lessons_data.extend(day_lessons)
                logger.info(f"Для дня {day_name} найдено {len(day_lessons)} уроков")
            
            logger.info(f"Метод 3: успешно распаршено {len(lessons_data)} уроков")
            return len(lessons_data) > 0
            
        except Exception as e:
            logger.error(f"Ошибка в методе 3: {e}")
            import traceback
            logger.error(f"Трассировка: {traceback.format_exc()}")
            return False

    def _find_class_header_row(self, df):
        for i in range(min(15, len(df))):
            row = df.iloc[i]
            class_count = 0
            for cell in row:
                if pd.notna(cell) and self._is_class_header(str(cell)):
                    class_count += 1
            if class_count >= 2:
                return i
        return None

    def _extract_class_columns(self, df, class_row_idx):
        class_columns = {}
        class_row = df.iloc[class_row_idx]
        
        for j, cell in enumerate(class_row):
            if pd.notna(cell):
                cell_str = str(cell).strip()
                class_name = self._extract_class_name(cell_str)
                if class_name:
                    class_columns[class_name] = j
                    logger.debug(f"Найден класс {class_name} в колонке {j}")
        
        return class_columns

    def _find_day_rows(self, df):
        day_rows = []
        day_patterns = {
            'понедельник': 'monday',
            'вторник': 'tuesday',
            'среда': 'wednesday',
            'четверг': 'thursday',
            'пятница': 'friday',
            'суббота': 'saturday'
        }
        
        for i in range(len(df)):
            for j in range(min(3, len(df.columns))):
                if pd.notna(df.iloc[i, j]) and isinstance(df.iloc[i, j], str):
                    cell_value = str(df.iloc[i, j]).lower().strip()
                    for ru_day, en_day in day_patterns.items():
                        if ru_day in cell_value:
                            day_rows.append((en_day, i))
                            logger.debug(f"Найден день '{en_day}' в строке {i}, колонке {j}")
                            break
                    else:
                        continue
                    break
        
        day_rows.sort(key=lambda x: x[1])
        return day_rows

    def _parse_day_schedule(self, df, start_row, end_row, class_columns, shift, day_name):
        lessons = []
        
        lesson_numbers = {}
        for row_idx in range(start_row, min(end_row, len(df))):
            row = df.iloc[row_idx]
            
            if len(row) > 1 and pd.notna(row[1]):
                lesson_str = str(row[1]).strip()
                numbers = re.findall(r'\d+', lesson_str)
                if numbers:
                    lesson_num = int(numbers[0])
                    if 1 <= lesson_num <= 10:
                        lesson_numbers[row_idx] = lesson_num
                        logger.debug(f"Найден номер урока {lesson_num} в строке {row_idx}")
        
        current_lesson_num = 1
        
        for row_idx in range(start_row, min(end_row, len(df))):
            row = df.iloc[row_idx]
            
            if all(pd.isna(cell) for cell in row):
                continue
            
            lesson_num = lesson_numbers.get(row_idx)
            if lesson_num is not None:
                current_lesson_num = lesson_num
            else:
                lesson_num = current_lesson_num
            
            lesson_found_in_row = False
            
            for class_name, col_idx in class_columns.items():
                subject_col = col_idx
                if subject_col < len(row) and pd.notna(row[subject_col]):
                    subject = str(row[subject_col]).strip()
                    
                    if not subject or subject in ['-', '—', ''] or self._is_day_of_week(subject):
                        continue
                    
                    room = ""
                    room_col = col_idx + 1
                    if room_col < len(row) and pd.notna(row[room_col]):
                        room_cell = str(row[room_col]).strip()
                        if room_cell and not self._is_day_of_week(room_cell):
                            room = room_cell
                    
                    teacher = ""
                    if '(' in subject and ')' in subject:
                        teacher_match = re.search(r'\((.*?)\)', subject)
                        if teacher_match:
                            teacher = teacher_match.group(1)
                            subject = re.sub(r'\(.*?\)', '', subject).strip()
                    
                    if ' - ' in subject:
                        room_parts = subject.split(' - ', 1)
                        subject = room_parts[0].strip()
                        room = room_parts[1].strip()
                    
                    if subject:
                        lessons.append({
                            'class': class_name,
                            'day': day_name,
                            'lesson_number': lesson_num,
                            'subject': subject,
                            'teacher': teacher,
                            'room': room,
                            'shift': shift
                        })
                        
                        lesson_found_in_row = True
                        logger.debug(f"Добавлен урок: {class_name}, {day_name}, {lesson_num}, {subject}, {teacher}, {room}")
            
            if lesson_found_in_row and row_idx not in lesson_numbers:
                current_lesson_num += 1
        
        return lessons

    def _is_class_header(self, text):
        text = text.lower().strip()
        patterns = [
            r'^\d[абв]$',
            r'^10[пр]$',
            r'^11[р]$',
            r'^\d[абв]\s*$',
            r'^\d[абв].*класс',
            r'^класс.*\d[абв]'
        ]
        return any(re.match(pattern, text) for pattern in patterns)

    def _extract_class_name(self, text):
        text = text.lower().strip()
        
        text = re.sub(r'(класс|смена|урок|расписание|№)', '', text).strip()
        
        patterns = [
            (r'(\d[абв])', 1),
            (r'(10[пр])', 1),
            (r'(11[р])', 1)
        ]
        
        for pattern, group in patterns:
            match = re.search(pattern, text)
            if match:
                class_name = match.group(group).upper()
                return class_name
        
        return None

    def _is_day_of_week(self, text):
        text = text.lower().strip()
        days = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота']
        return any(day in text for day in days)

    def _select_sheet(self, sheet_names, shift):
        possible_sheet_names = [
            f"{shift} СМЕНА",
            f"{shift} смена", 
            f"Смена {shift}",
            f"СМЕНА {shift}",
            f"1 СМЕНА",
            "1 СМЕНА"
        ]
        
        for sheet_name in possible_sheet_names:
            if sheet_name in sheet_names:
                return sheet_name
        
        for sheet_name in sheet_names:
            if any(name.lower() in sheet_name.lower() for name in possible_sheet_names):
                return sheet_name
        
        if sheet_names:
            logger.warning(f"Лист для смены {shift} не найден, используем первый лист: {sheet_names[0]}")
            return sheet_names[0]
        
        return None

    def _log_file_structure(self, df, sheet_name):
        logger.info(f"=== СТРУКТУРА ФАЙЛА '{sheet_name}' ===")
        
        logger.info("Первые 15 строк файла:")
        for i in range(min(15, len(df))):
            row_preview = []
            for j in range(min(20, len(df.columns))):
                cell_value = df.iloc[i, j]
                if pd.isna(cell_value):
                    row_preview.append("")
                else:
                    row_preview.append(str(cell_value).strip())
            logger.info(f"Строка {i:2d}: {row_preview}")
        
        non_empty_cells = 0
        for i in range(min(20, len(df))):
            for j in range(min(20, len(df.columns))):
                if pd.notna(df.iloc[i, j]) and str(df.iloc[i, j]).strip():
                    non_empty_cells += 1
        
        logger.info(f"Непустых ячеек в первых 20x20: {non_empty_cells}")

    def import_schedule_from_excel(self, file_content, shift):
        try:
            lessons_data = self.parse_excel_schedule(file_content, shift)
            if not lessons_data:
                return False, f"Не удалось распарсить Excel файл для {shift} смены"
            
            imported_count = 0
            error_count = 0
            
            imported_classes = set(lesson['class'] for lesson in lessons_data)
            
            for class_name in imported_classes:
                self.db.execute("DELETE FROM schedule WHERE class = ?", (class_name,))
                logger.info(f"Удалены старые уроки для класса {class_name}")
            
            for lesson in lessons_data:
                try:
                    lesson_number = int(lesson['lesson_number'])
                    class_name = lesson['class']
                    day = lesson['day']
                    
                    self.db.execute(
                        "INSERT INTO schedule (class, day, lesson_number, subject, teacher, room) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (class, day, lesson_number) DO UPDATE SET subject = EXCLUDED.subject, teacher = EXCLUDED.teacher, room = EXCLUDED.room",
                        (class_name, day, lesson_number, lesson['subject'], lesson['teacher'], lesson['room'])
                    )
                    imported_count += 1
                except Exception as e:
                    logger.error(f"Ошибка импорта урока {lesson}: {e}")
                    error_count += 1
            
            message = f"✅ Успешно импортировано {imported_count} уроков для {shift} смены"
            if error_count > 0:
                message += f", ошибок: {error_count}"
                
            return True, message
        except Exception as e:
            logger.error(f"Ошибка импорта из Excel для смены {shift}: {e}")
            return False, f"Ошибка импорта для {shift} смены: {str(e)}"

    def handle_start(self, chat_id, user):
        user_data = self.get_user(user["id"])
        
        if user_data:
            text = (
                f"Привет, {self.safe_message(user.get('first_name', 'друг'))}!\n"
                f"Ты уже зарегистрирован в системе.\n"
                f"Твой класс: {self.safe_message(user_data[2])}"
            )
            self.send_message(chat_id, text, self.main_menu_keyboard())
        else:
            self.handle_registration_start(chat_id, user["id"])
    
    def handle_registration_start(self, chat_id, user_id):
        self.user_states[user_id] = {"action": "registration"}
        self.send_message(
            chat_id,
            "👋 <b>Добро пожаловать в школьный бот!</b>\n\n"
            "Для регистрации введи свои данные в формате:\n"
            "<b>Фамилия Имя, Класс</b>\n\n"
            "Например: <i>Иванов Иван, 10П</i>",
            self.cancel_keyboard()
        )
    
    def handle_help(self, chat_id, username):
        text = (
            "📚 <b>Школьный бот - помощь</b>\n\n"
            "Я помогу тебе узнать расписание уроков и многое другое.\n\n"
            "<b>Основные команды:</b>\n"
            "• /start - начать работу\n"
            "• /help - показать эту справку\n\n"
            "<b>Новые возможности:</b>\n"
            "• <b>📰 Новости</b> - школьные новости и объявления\n"
            "• <b>⚙️ Настройки</b> - уведомления и предпочтения\n"
            "• <b>🏆 Достижения</b> - система наград за активность\n"
            "• <b>📈 Статистика</b> - ваша активность и прогресс\n\n"
            "<b>Классические функции:</b>\n"
            "• <b>Моё расписание</b> - расписание для твоего класса\n"
            "• <b>Общее расписание</b> - расписание для любого класса\n"
            "• <b>Звонки</b> - расписание звонков\n\n"
            "Для регистрации просто введи свои данные в формате: Фамилия Имя, Класс\n\n"
            "🛠 <b>Техническая помощь</b>\n"
            "Если вы обнаружили ошибку или у вас есть предложения, "
            "напишите разработчику: @r1kuza"
        )
        
        if self.is_admin(username):
            text += "\n\n🔐 <b>Секретная команда для админа:</b>\n/admin_panel"
        
        self.send_message(chat_id, text)
    
    def handle_admin_panel(self, chat_id, username):
        if not self.is_admin(username):
            self.log_security_event("unauthorized_admin_access", chat_id, f"Username: {username}")
            self.send_message(chat_id, "❌ У вас нет доступа к админ-панели")
            return
        
        text = "👨‍💼 <b>Панель администратора</b>\n\nВыберите действие:"
        self.send_message(chat_id, text, self.admin_menu_inline_keyboard())
        
    def handle_callback_query(self, update):
        callback_query = update.get("callback_query")
        if not callback_query:
            return
            
        chat_id = callback_query["message"]["chat"]["id"]
        user = callback_query["from"]
        user_id = user["id"]
        username = user.get("username", "")
        data = callback_query["data"]
        
        # Добавьте это логирование
        logger.info(f"📲 Callback получен: '{data}' от пользователя {username} (ID: {user_id})")
        
        # Обработка кнопок управления новостями
        if data == "admin_manage_news":
            self.show_news_management(chat_id, username)
        elif data == "admin_add_news":
            self.start_add_news(chat_id, username)
        elif data == "admin_edit_news":
            self.start_edit_news(chat_id, username)
        elif data == "admin_delete_news":
            self.start_delete_news(chat_id, username)
        elif data == "admin_list_news":
            self.show_all_news(chat_id, username)
        elif data.startswith("news_action_"):
            self.handle_news_action(chat_id, username, data)
        # Добавляем обработку кнопок редактирования полей новостей
        elif data.startswith("news_edit_field_"):
            self.handle_news_edit_field(chat_id, username, data)
        
        elif data.startswith("news_full_"):
            news_id = int(data.replace("news_full_", ""))
            self.show_full_news(chat_id, user_id, news_id)

        elif data == "broadcast_confirm":
            self.execute_broadcast(chat_id, username)
        elif data == "broadcast_cancel":
            if username in self.admin_states:
                del self.admin_states[username]
            self.send_message(chat_id, "❌ Рассылка отменена", self.admin_menu_inline_keyboard())
        
        elif data.startswith("toggle_"):
            self.handle_toggle_setting(chat_id, user_id, data)
        elif data == "my_achievements":
            self.show_user_achievements(chat_id, user_id)
        elif data == "achievement_progress":
            logger.info(f"Вызов show_achievement_progress для пользователя {user_id}")
            self.show_achievement_progress(chat_id, user_id)
        
        # === ВАЖНО: ДОБАВЬТЕ ЭТИ СТРОКИ ДЛЯ НОВОСТЕЙ ===
        elif data == "recent_news":
            self.show_recent_news(chat_id, user_id)
        elif data == "news_stats":
            self.show_news_statistics(chat_id, user_id)
        elif data == "news_search":  # ← ДОБАВЬТЕ ЭТУ СТРОКУ!
            self.handle_news_search(chat_id, user_id)
        # ==============================================
        
        elif data == "my_statistics":
            self.show_detailed_statistics(chat_id, user_id)
        elif data in ["settings_back", "achievements_back", "news_back", "stats_back"]:
            self.send_message(chat_id, "Главное меню", self.main_menu_keyboard())
        
        elif data.startswith("day_"):
            day_code = data[4:]
            day_map = {
                'monday': 'понедельник',
                'tuesday': 'вторник', 
                'wednesday': 'среда',
                'thursday': 'четверг',
                'friday': 'пятница',
                'saturday': 'суббота'
            }
            day_text = day_map.get(day_code, day_code)
            
            if username in self.admin_states and self.admin_states[username].get("action") == "edit_schedule_day":
                self.handle_schedule_day_selection(chat_id, username, day_text)
            else:
                self.handle_day_selection(chat_id, user_id, day_text)
            
        elif data.startswith("admin_"):
            self.handle_admin_callback(chat_id, username, data)
         
        else:
            logger.warning(f"⚠️ Неизвестный callback_data: {data}")
        
        self.answer_callback_query(callback_query["id"])

    def handle_news_search(self, chat_id, user_id):
        """Обработка поиска новостей"""
        logger.info(f"🟢 Вызван handle_news_search для пользователя {user_id}")
        
        self.user_states[user_id] = {"action": "news_search"}
        self.send_message(
            chat_id,
            "🔍 <b>Поиск новостей</b>\n\n"
            "Введите ключевое слово для поиска в заголовке или содержании новостей.\n"
            "Например: 'расписание', 'олимпиада', 'праздник'\n\n"
            "Для отмены нажмите '❌ Отменить'",
            self.cancel_keyboard()
        )

    def search_news(self, query, limit=10):
        """Поиск новостей по запросу"""
        try:
            # Поиск по заголовку и содержанию
            search_query = f"%{query}%"
            return self.db.fetchall(
                """SELECT id, title, content, author, publish_date, target_audience 
                FROM school_news 
                WHERE (title LIKE ? OR content LIKE ?) AND is_published = TRUE
                ORDER BY publish_date DESC LIMIT ?""",
                (search_query, search_query, limit)
            )
        except Exception as e:
            logger.error(f"Ошибка поиска новостей: {e}")
            return []

    def handle_news_edit_field(self, chat_id, username, data):
        """Обработка нажатия на кнопку редактирования поля новости"""
        if not self.is_admin(username):
            return
        
        parts = data.split("_")
        # Формат: news_edit_field_тип_поля_id_новости
        field_type = parts[3]  # title, content, author, audience
        news_id = int(parts[4])
        
        # Сохраняем состояние для админа
        self.admin_states[username] = {
            "action": "edit_news_field",
            "field": field_type,
            "news_id": news_id
        }
        
        # Получаем текущую новость для отображения
        news = self.get_news_by_id(news_id)
        if not news:
            self.send_message(chat_id, "❌ Новость не найдена", self.news_management_inline_keyboard())
            return
        
        _, title, content, author, target_audience, _ = news
        
        field_names = {
            "title": "заголовок",
            "content": "содержание",
            "author": "автор",
            "audience": "аудиторию"
        }
        
        field_values = {
            "title": title,
            "content": content,
            "author": author,
            "audience": target_audience
        }
        
        field_name = field_names.get(field_type, "поле")
        current_value = field_values.get(field_type, "")
        
        # Отправляем сообщение с запросом на ввод нового значения
        self.send_message(
            chat_id,
            f"📝 <b>Редактирование {field_name}</b>\n\n"
            f"Текущее значение:\n"
            f"<code>{self.safe_message(current_value[:200])}</code>\n\n"
            f"Введите новое значение для {field_name}:",
            self.cancel_keyboard()
        )     

    def handle_roster_add(self, chat_id, username, text):
            """Обработка добавления ученика"""
            parts = text.split(',')
            if len(parts) != 2:
                self.send_message(chat_id, "❌ Неверный формат. Введите: Класс, Фамилия Имя")
                return
            
            class_name = parts[0].strip().upper()
            full_name = parts[1].strip()
            
            if not self.is_valid_class(class_name):
                self.send_message(chat_id, "❌ Неверный формат класса")
                return
            
            if not self.is_valid_fullname(full_name):
                self.send_message(chat_id, "❌ Неверный формат ФИО")
                return
            
            if self.db.add_student_to_roster(class_name, full_name):
                self.send_message(
                    chat_id,
                    f"✅ Ученик добавлен в список:\n\n"
                    f"Класс: {class_name}\n"
                    f"ФИО: {full_name}",
                    self.roster_management_inline_keyboard()
                )
            else:
                self.send_message(chat_id, "❌ Ошибка добавления ученика")
            
            del self.admin_states[username]

    def handle_roster_remove(self, chat_id, username, text):
            """Обработка удаления ученика"""
            parts = text.split(',')
            if len(parts) != 2:
                self.send_message(chat_id, "❌ Неверный формат. Введите: Класс, Фамилия Имя")
                return
            
            class_name = parts[0].strip().upper()
            full_name = parts[1].strip()
            
            if self.db.remove_student_from_roster(class_name, full_name):
                self.send_message(
                    chat_id,
                    f"✅ Ученик удален из списка:\n\n"
                    f"Класс: {class_name}\n"
                    f"ФИО: {full_name}",
                    self.roster_management_inline_keyboard()
                )
            else:
                self.send_message(chat_id, "❌ Ученик не найден или ошибка удаления")
            
            del self.admin_states[username]

    def handle_roster_view(self, chat_id, username, text):
            """Обработка просмотра списка учеников"""
            class_name = text.strip().upper()
            
            if not self.is_valid_class(class_name):
                self.send_message(chat_id, "❌ Неверный формат класса")
                del self.admin_states[username]
                return
            
            students = self.db.get_students_by_class(class_name)
            
            if not students:
                self.send_message(
                    chat_id,
                    f"📋 <b>Список учеников {class_name} класса</b>\n\n"
                    f"❌ В списке нет учеников",
                    self.roster_management_inline_keyboard()
                )
            else:
                text = f"📋 <b>Список учеников {class_name} класса</b>\n\n"
                text += f"Всего учеников: {len(students)}\n\n"
                
                for i, student in enumerate(students, 1):
                    text += f"{i}. {student}\n"
                
                self.send_message(chat_id, text, self.roster_management_inline_keyboard())
            
            del self.admin_states[username]  
    
    def handle_admin_callback(self, chat_id, username, data):
        if not self.is_admin(username):
            self.log_security_event("unauthorized_admin_access", chat_id, f"Username: {username}")
            self.send_message(chat_id, "❌ У вас нет доступа к админ-панели")
            return
        
        if data == "admin_users":
            self.show_users_list(chat_id)
        elif data == "admin_delete_user":
            self.start_delete_user(chat_id, username)
        elif data == "admin_edit_schedule":
            self.start_edit_schedule(chat_id, username)
        elif data == "admin_manage_classes":
            self.show_classes_management(chat_id, username)
        elif data == "admin_bells":
            self.show_bells_management(chat_id, username)
        elif data == "admin_upload_excel":
            self.send_message(
                chat_id,
                "📤 <b>Загрузка расписания из Excel</b>\n\n"
                "Выберите смену для загрузки:",
                self.shift_selection_keyboard()
            )
            self.admin_states[username] = {"action": "select_shift"}
        elif data == "admin_stats":
            self.show_statistics(chat_id)
        elif data == "admin_broadcast":
            self.start_broadcast(chat_id, username)
        elif data == "admin_back":
            if username in self.admin_states:
                del self.admin_states[username]
            self.send_message(chat_id, "Главное меню", self.main_menu_keyboard())
        elif data == "admin_add_class":
            self.start_add_class(chat_id, username)
        elif data == "admin_delete_class":
            self.start_delete_class(chat_id, username)
        elif data == "admin_edit_bell":
            self.start_edit_bell(chat_id, username)
        elif data == "admin_view_bells":
            self.show_all_bells(chat_id)
        elif data == "admin_manage_rosters":
            self.show_roster_management(chat_id, username)
        elif data == "roster_add":
            self.start_add_student(chat_id, username)
        elif data == "roster_remove":
            self.start_remove_student(chat_id, username)
        elif data == "roster_view":
            self.start_view_roster(chat_id, username)
        elif data == "roster_import":
            self.start_roster_import(chat_id, username)

    def process_news_search(self, chat_id, user_id, query):
        """Обработка поискового запроса"""
        if not query or len(query.strip()) < 2:
            self.send_message(
                chat_id,
                "❌ Слишком короткий запрос. Введите минимум 2 символа.",
                self.news_keyboard()
            )
            del self.user_states[user_id]
            return
        
        # Выполняем поиск
        news_results = self.search_news(query)
        
        if not news_results:
            self.send_message(
                chat_id,
                f"🔍 <b>Результаты поиска по запросу: '{query}'</b>\n\n"
                "❌ Новости не найдены. Попробуйте другие ключевые слова.",
                self.news_keyboard()
            )
            del self.user_states[user_id]
            return
        
        # Создаем сообщение с результатами
        text = f"🔍 <b>Результаты поиска по запросу: '{query}'</b>\n\n"
        text += f"📊 Найдено новостей: {len(news_results)}\n\n"
        
        # Создаем клавиатуру с кнопками для каждой новости
        keyboard = {"inline_keyboard": []}
        
        for news_item in news_results:
            news_id, title, content, author, publish_date, target_audience = news_item
            date_str = self.format_date(publish_date)
            
            # Добавляем информацию о новости в текст
            text += f"📰 <b>{self.safe_message(title)}</b>\n"
            text += f"📅 {date_str} | 👤 {author}\n"
            text += f"🎯 Аудитория: {target_audience}\n"
            
            # Создаем превью с найденным запросом
            query_lower = query.lower()
            content_lower = content.lower()
            
            if query_lower in content_lower:
                pos = content_lower.find(query_lower)
                start = max(0, pos - 30)
                end = min(len(content), pos + len(query) + 30)
                preview = content[start:end]
                if start > 0:
                    preview = "..." + preview
                if end < len(content):
                    preview = preview + "..."
                text += f"📝 {self.safe_message(preview)}\n"
            
            text += "─" * 30 + "\n\n"
            
            # Добавляем кнопку для чтения полной новости
            button_text = f"📖 {title[:20]}..." if len(title) > 20 else f"📖 {title}"
            keyboard["inline_keyboard"].append(
                [{"text": button_text, "callback_data": f"news_full_{news_id}"}]
            )
        
        # Добавляем навигационные кнопки
        keyboard["inline_keyboard"].append([
            {"text": "🔍 Новый поиск", "callback_data": "news_search"},
            {"text": "📰 Все новости", "callback_data": "recent_news"}
        ])
        keyboard["inline_keyboard"].append([
            {"text": "⬅️ Назад", "callback_data": "news_back"}
        ])
        
        self.send_message(chat_id, text, keyboard)
        del self.user_states[user_id]
    
    def show_news_management(self, chat_id, username):
        if not self.is_admin(username):
            self.send_message(chat_id, "❌ У вас нет прав для управления новостями")
            return
        
        text = "📰 <b>Управление новостями</b>\n\nВыберите действие:"
        self.send_message(chat_id, text, self.news_management_inline_keyboard())
    
    def start_add_news(self, chat_id, username):
        if not self.is_admin(username):
            return
        
        self.admin_states[username] = {"action": "add_news_title"}
        self.send_message(
            chat_id,
            "📝 <b>Добавление новой новости</b>\n\n"
            "Введите заголовок новости:",
            self.cancel_keyboard()
        )
    
    def start_edit_news(self, chat_id, username):
        if not self.is_admin(username):
            return
        
        news_list = self.get_all_news(limit=20)
        if not news_list:
            self.send_message(chat_id, "❌ Нет новостей для редактирования", self.news_management_inline_keyboard())
            return
        
        keyboard = []
        for news in news_list:
            news_id, title, _, author, _, publish_date = news
            date_str = self.format_date(publish_date)
            button_text = f"{news_id}. {title[:30]}... ({author}, {date_str})"
            keyboard.append([{"text": button_text, "callback_data": f"news_action_edit_{news_id}"}])
        
        keyboard.append([{"text": "⬅️ Назад", "callback_data": "admin_manage_news"}])
        
        self.send_message(
            chat_id,
            "📝 <b>Редактирование новости</b>\n\n"
            "Выберите новость для редактирования:",
            {"inline_keyboard": keyboard}
        )
    
    def start_delete_news(self, chat_id, username):
        if not self.is_admin(username):
            return
        
        news_list = self.get_all_news(limit=20)
        if not news_list:
            self.send_message(chat_id, "❌ Нет новостей для удаления", self.news_management_inline_keyboard())
            return
        
        keyboard = []
        for news in news_list:
            news_id, title, _, author, _, publish_date = news
            date_str = self.format_date(publish_date)
            button_text = f"{news_id}. {title[:30]}... ({author}, {date_str})"
            keyboard.append([{"text": button_text, "callback_data": f"news_action_delete_{news_id}"}])
        
        keyboard.append([{"text": "⬅️ Назад", "callback_data": "admin_manage_news"}])
        
        self.send_message(
            chat_id,
            "🗑️ <b>Удаление новости</b>\n\n"
            "Выберите новость для удаления:",
            {"inline_keyboard": keyboard}
        )
    
    def show_all_news(self, chat_id, username):
        if not self.is_admin(username):
            return
        
        news_list = self.get_all_news(limit=20)
        if not news_list:
            self.send_message(chat_id, "❌ Нет новостей", self.news_management_inline_keyboard())
            return
        
        text = "📋 <b>Все новости</b>\n\n"
        for news in news_list:
            news_id, title, content, author, target_audience, publish_date = news
            date_str = self.format_date(publish_date)
            text += f"<b>ID {news_id}: {title}</b>\n"
            text += f"Автор: {author}\n"
            text += f"Аудитория: {target_audience}\n"
            text += f"Дата: {date_str}\n"
            text += f"Содержание: {content[:100]}...\n"
            text += "─" * 30 + "\n"
        
        self.send_message(chat_id, text, self.news_management_inline_keyboard())
    
    def handle_news_action(self, chat_id, username, data):
        if not self.is_admin(username):
            return
        
        parts = data.split("_")
        action = parts[2]
        news_id = int(parts[3])
        
        if action == "edit":
            self.admin_states[username] = {
                "action": "edit_news_select_field",
                "news_id": news_id
            }
            
            news = self.get_news_by_id(news_id)
            if not news:
                self.send_message(chat_id, "❌ Новость не найдена", self.news_management_inline_keyboard())
                return
            
            _, title, content, author, target_audience, _ = news
            
            keyboard = {
                "inline_keyboard": [
                    [{"text": "📝 Заголовок", "callback_data": f"news_edit_field_title_{news_id}"}],
                    [{"text": "📄 Содержание", "callback_data": f"news_edit_field_content_{news_id}"}],
                    [{"text": "👤 Автор", "callback_data": f"news_edit_field_author_{news_id}"}],
                    [{"text": "🎯 Аудитория", "callback_data": f"news_edit_field_audience_{news_id}"}],
                    [{"text": "⬅️ Назад", "callback_data": "admin_manage_news"}]
                ]
            }
            
            text = f"📝 <b>Редактирование новости ID {news_id}</b>\n\n"
            text += f"<b>Текущие данные:</b>\n"
            text += f"• Заголовок: {title}\n"
            text += f"• Автор: {author}\n"
            text += f"• Аудитория: {target_audience}\n"
            text += f"• Содержание: {content[:100]}...\n\n"
            text += "Выберите поле для редактирования:"
            
            self.send_message(chat_id, text, keyboard)
        
        elif action == "delete":
            if self.delete_news(news_id):
                self.send_message(chat_id, f"✅ Новость ID {news_id} удалена", self.news_management_inline_keyboard())
            else:
                self.send_message(chat_id, f"❌ Ошибка при удалении новости", self.news_management_inline_keyboard())
    
    def handle_text_message(self, chat_id, user_id, username, text):
        if text == "❌ Отменить":
            if username in self.admin_states:
                del self.admin_states[username]
            if user_id in self.user_states:
                del self.user_states[user_id]
            self.send_message(chat_id, "Действие отменено", self.main_menu_keyboard())
            return
        
        # Проверяем состояние поиска новостей
        if user_id in self.user_states and self.user_states[user_id].get("action") == "news_search":
            self.process_news_search(chat_id, user_id, text)
            return
            
        if username in self.admin_states:
            state = self.admin_states[username]
            
            if state.get("action") == "broadcast_waiting_message":
                self.handle_broadcast_message(chat_id, username, text)
                return
            
            elif state.get("action") == "add_news_title":
                state["action"] = "add_news_content"
                state["title"] = text
                self.send_message(
                    chat_id,
                    f"Заголовок сохранен: {text}\n\n"
                    "Теперь введите содержание новости:",
                    self.cancel_keyboard()
                )
                return
            
            elif state.get("action") == "add_news_content":
                state["action"] = "add_news_audience"
                state["content"] = text
                self.send_message(
                    chat_id,
                    "Содержание сохранено.\n\n"
                    "Теперь введите аудиторию новости:\n"
                    "(например: 'all', '10П', '11Р' или '5-9 классы'):",
                    self.cancel_keyboard()
                )
                return
            
            elif state.get("action") == "add_news_audience":
                title = state.get("title")
                content = state.get("content")
                target_audience = text.strip()
                author = username
                
                if self.add_news(title, content, author, target_audience):
                    self.send_message(
                        chat_id,
                        f"✅ Новость успешно добавлена!\n\n"
                        f"<b>Заголовок:</b> {title}\n"
                        f"<b>Аудитория:</b> {target_audience}\n"
                        f"<b>Автор:</b> {author}",
                        self.news_management_inline_keyboard()
                    )
                else:
                    self.send_message(chat_id, "❌ Ошибка при добавлении новости", self.news_management_inline_keyboard())
                
                del self.admin_states[username]
                return
            
            elif state.get("action") == "edit_news_field":
                news_id = state.get("news_id")
                field = state.get("field")
                
                if field == "title":
                    self.update_news(news_id, title=text)
                elif field == "content":
                    self.update_news(news_id, content=text)
                elif field == "author":
                    self.update_news(news_id, author=text)
                elif field == "audience":
                    self.update_news(news_id, target_audience=text)
                
                self.send_message(
                    chat_id,
                    f"✅ Поле '{field}' новости ID {news_id} обновлено",
                    self.news_management_inline_keyboard()
                )
                
                del self.admin_states[username]
                return
            
            elif state.get("action") == "edit_news_field":
                news_id = state.get("news_id")
                field = state.get("field")
                
                if field == "title":
                    self.update_news(news_id, title=text)
                elif field == "content":
                    self.update_news(news_id, content=text)
                elif field == "author":
                    self.update_news(news_id, author=text)
                elif field == "audience":
                    self.update_news(news_id, target_audience=text)
                
                self.send_message(
                    chat_id,
                    f"✅ Поле '{field}' новости ID {news_id} обновлено",
                    self.news_management_inline_keyboard()
                )
                
                del self.admin_states[username]
                return
            elif state.get("action") == "roster_add_waiting_data":
                        self.handle_roster_add(chat_id, username, text)
                        return
            elif state.get("action") == "roster_remove_waiting_data":
                        self.handle_roster_remove(chat_id, username, text)
                        return
            elif state.get("action") == "roster_view_waiting_class":
                        self.handle_roster_view(chat_id, username, text)
                        return
                   
        if user_id in self.user_states:
            state = self.user_states[user_id]
            if state.get("action") == "registration":
                self.handle_registration_input(chat_id, user_id, username, text)
                return
        
        user_data = self.get_user(user_id)
        if user_data:
            self.send_message(chat_id, "🤖 Я школьный бот! Используй меню для навигации или напиши /help для справки.", self.main_menu_keyboard())
        else:
            self.handle_registration_start(chat_id, user_id)
    
    def handle_registration_input(self, chat_id, user_id, username, text):
        parts = text.split(',')
        if len(parts) != 2:
            self.send_message(chat_id, "❌ Неверный формат. Введите: Фамилия Имя, Класс")
            return
        
        full_name = parts[0].strip()
        class_name = parts[1].strip().upper()
    
        
        if not self.is_valid_fullname(full_name):
            self.send_message(chat_id, "❌ Неверный формат ФИО")
            return
        
        if not self.is_valid_class(class_name):
            self.send_message(chat_id, "❌ Неверный формат класса")
            return
        
 # ✅ ПРОВЕРКА: Есть ли ученик в официальном списке
        if not self.db.check_student_in_roster(class_name, full_name):
            self.send_message(
                chat_id,
                f"❌ <b>Проверка не пройдена</b>\n\n"
                f"ФИО: {full_name}\n"
                f"Класс: {class_name}\n\n"
                f"Ваших данных нет в официальном списке учеников.\n\n"
                f"<b>Возможные причины:</b>\n"
                f"1. Ошибка в написании ФИО\n"
                f"2. Вы указали неверный класс\n"
                f"3. Ваши данные еще не добавлены в систему\n\n"
                f"Обратитесь к классному руководителю или администратору."
            )
            return
        
        if self.create_user(user_id, full_name, class_name, username):
            self.send_message(
                chat_id, 
                f"✅ Регистрация прошла успешно!\nФИО: {self.safe_message(full_name)}\nКласс: {class_name}", 
                self.main_menu_keyboard()
            )
            self.db.execute(
                "INSERT INTO user_activity (user_id, action_type, details) VALUES (?, ?, ?)",
                (user_id, "registration", f"class: {class_name}")
            )
        else:
            self.send_message(chat_id, "❌ Ошибка регистрации", self.main_menu_keyboard())
        
        if user_id in self.user_states:
            del self.user_states[user_id]

    def show_classes_management(self, chat_id, username):
        text = "🏫 <b>Управление классами</b>\n\nВыберите действие:"
        self.send_message(chat_id, text, self.classes_management_inline_keyboard())
    
    def show_bells_management(self, chat_id, username):
        text = "🕧 <b>Управление расписанием звонков</b>\n\nВыберите действие:"
        self.send_message(chat_id, text, self.bells_management_inline_keyboard())
    
    def start_add_class(self, chat_id, username):
        self.admin_states[username] = {"action": "add_class_input"}
        self.send_message(
            chat_id,
            "Введите название класса для добавления:\n\n"
            "Формат: 5А, 10П, 11Р и т.д.\n"
            "Доступные классы: 5-9 классы (А, Б, В), 10-11 классы (П, Р)",
            self.cancel_keyboard()
        )
    
    def start_delete_class(self, chat_id, username):
        self.admin_states[username] = {"action": "delete_class_input"}
        
        classes = self.get_existing_classes()
        classes_text = "Существующие классы:\n" + "\n".join(classes) if classes else "❌ Нет зарегистрированных классов"
        
        self.send_message(
            chat_id,
            f"{classes_text}\n\nВведите название класса для удаления:",
            self.cancel_keyboard()
        )
    
    def start_edit_bell(self, chat_id, username):
        self.admin_states[username] = {"action": "edit_bell_number"}
        self.send_message(
            chat_id,
            "Введите номер урока для изменения (1-7):",
            self.cancel_keyboard()
        )
    
    def show_all_bells(self, chat_id):
        bells = self.get_bell_schedule()
        bells_text = "🔔 <b>Текущее расписание звонков</b>\n\n"
        for bell in bells:
            bells_text += f"{bell[0]}. {bell[1]} - {bell[2]}\n"
        self.send_message(chat_id, bells_text)
    
    def handle_class_input(self, chat_id, username, text):
        if username not in self.admin_states:
            return
        
        action = self.admin_states[username].get("action")
        class_name = text.strip().upper()
        
        if not self.is_valid_class(class_name):
            self.send_message(chat_id, "❌ Неверный формат класса", self.admin_menu_inline_keyboard())
            del self.admin_states[username]
            return
        
        if action == "add_class_input":
            if self.add_class(class_name):
                self.send_message(chat_id, f"✅ Класс {class_name} доступен для регистрации", self.admin_menu_inline_keyboard())
            else:
                self.send_message(chat_id, f"❌ Неверный формат класса", self.admin_menu_inline_keyboard())
        elif action == "delete_class_input":
            if self.delete_class(class_name):
                self.send_message(chat_id, f"✅ Класс {class_name} и все связанные пользователи удалены", self.admin_menu_inline_keyboard())
            else:
                self.send_message(chat_id, f"❌ Класс {class_name} не найден или в нем нет пользователей", self.admin_menu_inline_keyboard())
        
        del self.admin_states[username]
    
    def handle_bell_input(self, chat_id, username, text):
        if username not in self.admin_states:
            return
        
        state = self.admin_states[username]
        
        if state.get("action") == "edit_bell_number":
            try:
                lesson_number = int(text)
                if 1 <= lesson_number <= 7:
                    state["action"] = "edit_bell_start"
                    state["lesson_number"] = lesson_number
                    self.send_message(chat_id, f"Урок {lesson_number}. Введите время начала (формат ЧЧ:ММ):", self.cancel_keyboard())
                else:
                    self.send_message(chat_id, "❌ Номер урока должен быть от 1 до 7", self.bells_management_inline_keyboard())
                    del self.admin_states[username]
            except ValueError:
                self.send_message(chat_id, "❌ Введите число от 1 до 7", self.bells_management_inline_keyboard())
                del self.admin_states[username]
        
        elif state.get("action") == "edit_bell_start":
            if self.is_valid_time(text):
                state["action"] = "edit_bell_end"
                state["start_time"] = text
                self.send_message(chat_id, f"Введите время окончания (формат ЧЧ:ММ):", self.cancel_keyboard())
            else:
                self.send_message(chat_id, "❌ Неверный формат времени. Используйте ЧЧ:ММ", self.bells_management_inline_keyboard())
                del self.admin_states[username]
        
        elif state.get("action") == "edit_bell_end":
            if self.is_valid_time(text):
                lesson_number = state["lesson_number"]
                start_time = state["start_time"]
                end_time = text
                
                if self.update_bell_schedule(lesson_number, start_time, end_time):
                    self.send_message(chat_id, f"✅ Звонок для урока {lesson_number} обновлен: {start_time} - {end_time}", self.bells_management_inline_keyboard())
                else:
                    self.send_message(chat_id, f"❌ Ошибка обновления звонка", self.bells_management_inline_keyboard())
                
                del self.admin_states[username]
            else:
                self.send_message(chat_id, "❌ Неверный формат времени. Используйте ЧЧ:ММ", self.bells_management_inline_keyboard())
                del self.admin_states[username]
    
    def handle_main_menu(self, chat_id, user_id, text, username):
        user_data = self.get_user(user_id)
        
        # Обработка кнопки "Общее расписание" для всех пользователей
        if text == "🏫 Общее расписание":
            if not user_data:
                self.send_message(
                    chat_id,
                    "❌ Вы не зарегистрированы. Пожалуйста, введите свои данные в формате: Фамилия Имя, Класс"
                )
                return
            
            self.user_states[user_id] = {"action": "general_schedule"}
            self.send_message(
                chat_id,
                "Выберите класс:",
                self.class_selection_keyboard()
            )
            return
        
        # Обработка кнопки "📚 Моё расписание"
        if text == "📚 Моё расписание":
            if not user_data:
                self.send_message(
                    chat_id,
                    "❌ Вы не зарегистрированы. Пожалуйста, введите свои данные в формате: Фамилия Имя, Класс"
                )
                return
            
            class_name = user_data[2]
            self.user_states[user_id] = {"action": "my_schedule", "class": class_name}
            self.send_message(
                chat_id,
                f"Выберите день недели для расписания {self.safe_message(class_name)} класса:",
                self.day_selection_inline_keyboard()
            )
            self.log_user_activity(user_id, "schedule_view", f"Class: {class_name}")
            # Проверяем достижения при просмотре расписания
            self.check_achievements(user_id, "schedule_views")
            return
        
        # Остальные кнопки главного меню...
        elif text == "🔔 Звонки":
            bells = self.get_bell_schedule()
            bells_text = "🔔 <b>Расписание звонков</b>\n\n"
            for bell in bells:
                bells_text += f"{bell[0]}. {bell[1]} - {bell[2]}\n"
                if bell[0] == 4:
                    bells_text += "    ⏰ Перемена 15 минут\n"
                elif bell[0] == 5:
                    bells_text += "    ⏰ Перемена 5 минут\n"
                elif bell[0] < 7:
                    bells_text += "    ⏰ Перемена 10 минут\n"
            
            bells_text += "\n📝 Уроки по 40 минут"
            self.send_message(chat_id, bells_text)
        
        elif text == "📰 Новости":
            self.handle_news_menu(chat_id, user_id)
        
        elif text == "⚙️ Настройки":
            self.handle_notifications_settings(chat_id, user_id)
        
        elif text == "🏆 Достижения":
            self.handle_achievements_menu(chat_id, user_id)
        
        elif text == "📈 Статистика":
            self.handle_statistics_menu(chat_id, user_id)
        
        elif text == "ℹ️ Помощь":
            self.handle_help(chat_id, username)
        
        elif text == "⬅️ Назад":
            if user_id in self.user_states:
                del self.user_states[user_id]
            self.send_message(chat_id, "Главное меню", self.main_menu_keyboard())
        
        elif self.is_valid_class(text):
            self.handle_class_selection(chat_id, user_id, text)
    
    def handle_notifications_settings(self, chat_id, user_id):
        settings = self.get_notification_settings(user_id)
        
        weather_status = "✅ ВКЛ" if settings['weather_notifications'] else "❌ ВЫКЛ"
        news_status = "✅ ВКЛ" if settings['news_notifications'] else "❌ ВЫКЛ"
        achievements_status = "✅ ВКЛ" if settings['achievement_notifications'] else "❌ ВЫКЛ"
        
        text = (f"⚙️ <b>Настройки уведомлений</b>\n\n"
               f"🌤️ Погода: {weather_status}\n"
               f"📰 Новости: {news_status}\n"
               f"🏆 Достижения: {achievements_status}\n\n"
               f"Нажмите на кнопку для переключения:")
        
        self.send_message(chat_id, text, self.notifications_settings_keyboard())
    
    def handle_achievements_menu(self, chat_id, user_id):
        achievements = self.get_user_achievements(user_id)
        text = "🏆 <b>Система достижений</b>\n\n"
        
        if achievements:
            text += f"🎯 Получено достижений: {len(achievements)}\n\n"
            for i, (name, desc, icon, date) in enumerate(achievements[:3], 1):
                text += f"{icon} <b>{name}</b>\n{desc}\n\n"
        else:
            text += "У вас пока нет достижений. Продолжайте использовать бота для их получения!"
        
        self.send_message(chat_id, text, self.achievements_keyboard())
    
    def handle_news_menu(self, chat_id, user_id):
        news_count = self.db.fetchone("SELECT COUNT(*) FROM school_news WHERE is_published = TRUE")
        news_count = news_count[0] if news_count else 0
        user_news_read = self.get_user_statistics(user_id)['news_read']
        
        text = (f"📰 <b>Школьные новости</b>\n\n"
               f"📊 Всего новостей: {news_count}\n"
               f"📖 Прочитано вами: {user_news_read}\n\n"
               f"Будьте в курсе всех школьных событий!")
        
        self.send_message(chat_id, text, self.news_keyboard())
    
    def handle_statistics_menu(self, chat_id, user_id):
        stats = self.get_user_statistics(user_id)
        achievements = len(self.get_user_achievements(user_id))
        
        last_active = self.format_date(stats['last_active']) if stats['last_active'] else "неизвестно"
        
        text = (f"📈 <b>Ваша статистика</b>\n\n"
               f"📊 Всего действий: {stats['total_actions']}\n"
               f"📚 Просмотров расписания: {stats['schedule_views']}\n"
               f"📰 Прочитано новостей: {stats['news_read']}\n"
               f"🏆 Получено достижений: {achievements}\n"
               f"🕐 Последняя активность: {last_active}")
        
        self.send_message(chat_id, text, self.statistics_keyboard())
    
    def handle_toggle_setting(self, chat_id, user_id, data):
        settings = self.get_notification_settings(user_id)
        setting_map = {
            "toggle_weather": "weather_notifications", 
            "toggle_news": "news_notifications",
            "toggle_achievements": "achievement_notifications"
        }
        
        setting_key = setting_map[data]
        
        # Сохраняем предыдущее значение
        previous_value = settings[setting_key]
        # Меняем значение
        settings[setting_key] = not settings[setting_key]
        
        # Обновляем настройки в базе
        self.update_notification_settings(user_id, settings)
        
        # Логируем изменение
        logger.info(f"⚙️ Пользователь {user_id} изменил настройку {setting_key}: {previous_value} -> {settings[setting_key]}")
        
        # Проверяем достижения только для weather_enabled и только если ВКЛЮЧИЛИ
        if setting_key == "weather_notifications" and settings[setting_key] and not previous_value:
            logger.info(f"🔍 Проверка достижения 'weather_enabled' для пользователя {user_id}")
            self.check_achievements(user_id, "weather_enabled")
        
        # Обновляем интерфейс настроек
        self.handle_notifications_settings(chat_id, user_id)

    def show_user_achievements(self, chat_id, user_id):
        achievements = self.get_user_achievements(user_id)
        
        if not achievements:
            self.send_message(chat_id, "🎯 У вас пока нет достижений. Продолжайте использовать бота!", self.achievements_keyboard())
            return
        
        text = "🏆 <b>Ваши достижения</b>\n\n"
        for name, description, icon, achieved_at in achievements:
            date_str = self.format_date(achieved_at)
            text += f"{icon} <b>{name}</b>\n{description}\n📅 {date_str}\n\n"
        
        self.send_message(chat_id, text, self.achievements_keyboard())

    def show_achievement_progress(self, chat_id, user_id):
        logger.info(f"Показ прогресса достижений для пользователя {user_id}")
        # Получаем все достижения
        achievements = self.db.fetchall(
            "SELECT name, condition_type, condition_value FROM achievements"
        )
        
        logger.info(f"Найдено достижений в базе: {len(achievements)}")
        
        if not achievements:
            self.send_message(chat_id, "📊 В системе пока нет достижений для отслеживания прогресса.", self.achievements_keyboard())
            return
        
        text = "📊 <b>Ваш прогресс по достижениям</b>\n\n"
        
        for name, condition_type, condition_value in achievements:
            progress = self.get_user_achievement_progress(user_id, condition_type)
            percentage = min(100, int((progress / condition_value) * 100)) if condition_value > 0 else 100
            progress_bar = "🟩" * (percentage // 20) + "⬜" * (5 - percentage // 20)
            text += f"<b>{name}</b>\nПрогресс: {progress}/{condition_value}\n{progress_bar} {percentage}%\n\n"
        
        self.send_message(chat_id, text, self.achievements_keyboard())

    def show_recent_news(self, chat_id, user_id):
        """Показать последние новости с inline кнопками"""
        news = self.get_news(limit=5)
        
        if not news:
            self.send_message(chat_id, "📰 Пока нет новостей.", self.news_keyboard())
            return
        
        text = "📰 <b>Последние новости</b>\n\n"
        
        # Создаем клавиатуру с кнопками
        keyboard = {"inline_keyboard": []}
        
        for news_item in news:
            news_id, title, content, author, publish_date, target_audience = news_item
            date_str = self.format_date(publish_date)
            
            # Показываем краткую информацию
            text += f"📰 <b>{self.safe_message(title)}</b>\n"
            text += f"👤 {author} | 📅 {date_str}\n"
            
            # Краткое превью
            preview = content[:80] + "..." if len(content) > 80 else content
            text += f"📝 {preview}\n"
            text += "─" * 30 + "\n\n"
            
            # Кнопка для чтения полной новости
            button_text = f"📖 Читать: {title[:15]}..." if len(title) > 15 else f"📖 {title}"
            keyboard["inline_keyboard"].append(
                [{"text": button_text, "callback_data": f"news_full_{news_id}"}]
            )
            
            self.log_user_activity(user_id, "news_read", f"News: {title}")
        
        # Добавляем навигационные кнопки
        keyboard["inline_keyboard"].append([
            {"text": "🔍 Поиск новостей", "callback_data": "news_search"},
            {"text": "📊 Статистика", "callback_data": "news_stats"}
        ])
        keyboard["inline_keyboard"].append([
            {"text": "⬅️ Назад", "callback_data": "news_back"}
        ])
        
        # Проверяем достижения
        self.check_achievements(user_id, "news_read")
        
        self.send_message(chat_id, text, keyboard)

    def show_full_news(self, chat_id, user_id, news_id):
        """Показать полную новость по ID"""
        news = self.get_news_by_id(news_id)
        
        if not news:
            self.send_message(chat_id, "❌ Новость не найдена.", self.news_keyboard())
            return
        
        _, title, content, author, target_audience, publish_date = news
        date_str = self.format_date(publish_date)
        
        text = f"📰 <b>{self.safe_message(title)}</b>\n\n"
        text += f"{self.safe_message(content)}\n\n"
        text += f"👤 {self.safe_message(author)}\n"
        text += f"📅 {date_str}\n"
        text += f"🎯 Аудитория: {target_audience}\n"
        
        self.log_user_activity(user_id, "news_read_full", f"News ID: {news_id}")
        self.check_achievements(user_id, "news_read")
        
        # Кнопки навигации
        keyboard = {
            "inline_keyboard": [
                [{"text": "📰 К списку новостей", "callback_data": "recent_news"}],
                [{"text": "🔍 Поиск новостей", "callback_data": "news_search"}],
                [{"text": "⬅️ Главное меню", "callback_data": "news_back"}]
            ]
        }
        
        self.send_message(chat_id, text, keyboard)

    def show_news_statistics(self, chat_id, user_id):
        total_news = self.db.fetchone("SELECT COUNT(*) FROM school_news WHERE is_published = TRUE")
        total_news = total_news[0] if total_news else 0
        
        user_stats = self.get_user_statistics(user_id)
        user_news_read = user_stats['news_read']
        
        percentage = (user_news_read / total_news * 100) if total_news > 0 else 0
        
        text = (f"📊 <b>Статистика новостей</b>\n\n"
               f"📰 Всего новостей: {total_news}\n"
               f"📖 Прочитано вами: {user_news_read}\n"
               f"📈 Процент прочитанного: {percentage:.1f}%\n\n")
        
        if percentage >= 80:
            text += "🎉 Вы отлично информированы!"
        elif percentage >= 50:
            text += "👍 Вы в курсе основных событий!"
        else:
            text += "💡 Читайте больше новостей, чтобы быть в курсе!"
        
        self.send_message(chat_id, text, self.news_keyboard())

    def show_detailed_statistics(self, chat_id, user_id):
        stats = self.get_user_statistics(user_id)
        achievements = self.get_user_achievements(user_id)
        user_data = self.get_user(user_id)
        
        text = (f"📈 <b>Подробная статистика</b>\n\n"
               f"👤 <b>Профиль</b>\n"
               f"• Имя: {self.safe_message(user_data[1]) if user_data else 'Неизвестно'}\n"
               f"• Класс: {self.safe_message(user_data[2]) if user_data else 'Неизвестно'}\n\n"
               
               f"📊 <b>Активность</b>\n"
               f"• Всего действий: {stats['total_actions']}\n"
               f"• Просмотров расписания: {stats['schedule_views']}\n"
               f"• Прочитано новостей: {stats['news_read']}\n"
               f"• Получено достижений: {len(achievements)}\n"
               f"• Последняя активность: {self.format_date(stats['last_active']) if stats['last_active'] else 'неизвестно'}\n\n")
        
        if achievements:
            text += "🏆 <b>Последние достижения</b>\n"
            for name, _, icon, date in achievements[:3]:
                text += f"{icon} {name} - {self.format_date(date)}\n"
        
        self.send_message(chat_id, text, self.statistics_keyboard())
    
    def start_delete_user(self, chat_id, username):
        self.admin_states[username] = {"action": "delete_user"}
        self.send_message(
            chat_id,
            "Введите ID пользователя или username для удаления:\n\n"
            "ID можно узнать через команду '👥 Список пользователей'\n"
            "Username должен начинаться с @",
            self.cancel_keyboard()
        )

    def delete_user_by_identifier(self, chat_id, admin_username, identifier):
        try:
            if identifier.isdigit():
                user_id = int(identifier)
                if self.delete_user(user_id):
                    self.log_security_event("user_deleted", admin_username, f"Deleted user: {user_id}")
                    self.send_message(chat_id, f"✅ Пользователь с ID {user_id} удален", self.admin_menu_inline_keyboard())
                else:
                    self.send_message(chat_id, f"❌ Пользователь с ID {identifier} не найден", self.admin_menu_inline_keyboard())
            elif identifier.startswith('@'):
                username = identifier[1:]
                if self.delete_user_by_username(username):
                    self.log_security_event("user_deleted", admin_username, f"Deleted user by username: {username}")
                    self.send_message(chat_id, f"✅ Пользователь с username @{username} удален", self.admin_menu_inline_keyboard())
                else:
                    self.send_message(chat_id, f"❌ Пользователь с username @{username} не найден", self.admin_menu_inline_keyboard())
            else:
                self.send_message(chat_id, "❌ Неверный формат. Введите ID (число) или username (начинается с @)", self.admin_menu_inline_keyboard())
        
        except ValueError:
            self.send_message(chat_id, "❌ Неверный формат ID", self.admin_menu_inline_keyboard())
        
        if admin_username in self.admin_states:
            del self.admin_states[admin_username]
    def answer_callback_query(self, callback_query_id, text=None):
        url = f"{BASE_URL}/answerCallbackQuery"
        data = {"callback_query_id": callback_query_id}
        if text:
            data["text"] = text
        
        try:
            logger.info(f"📤 Отправляем answerCallbackQuery для ID: {callback_query_id}")
            response = requests.post(url, json=data, timeout=10)
            result = response.json()
            logger.info(f"📥 Ответ answerCallbackQuery: {result}")
            return result
        except Exception as e:
            logger.error(f"❌ Ошибка ответа на callback: {e}")
            return None
    
    def handle_day_selection(self, chat_id, user_id, day_text):
        if user_id not in self.user_states:
            logger.error(f"User state not found for user {user_id}")
            self.send_message(chat_id, "❌ Ошибка: действие не найдено", self.main_menu_keyboard())
            return
        
        state = self.user_states[user_id]
        day_map = {
            'понедельник': 'monday',
            'вторник': 'tuesday',
            'среда': 'wednesday',
            'четверг': 'thursday',
            'пятница': 'friday',
            'суббота': 'saturday'
        }
        
        day_code = day_map.get(day_text.lower())
        if not day_code:
            self.send_message(chat_id, "❌ Неверный день недели", self.main_menu_keyboard())
            return
        
        if state.get("action") == "my_schedule":
            class_name = state.get("class")
            if not class_name:
                self.send_message(chat_id, "❌ Ошибка: класс не найден", self.main_menu_keyboard())
                return
            
            self.show_schedule(chat_id, class_name, day_code, day_text)
        
        elif state.get("action") == "general_schedule":
            class_name = state.get("selected_class")
            if not class_name:
                self.send_message(chat_id, "❌ Ошибка: класс не выбран", self.main_menu_keyboard())
                return
            
            self.show_schedule(chat_id, class_name, day_code, day_text)
    
    def handle_class_selection(self, chat_id, user_id, class_name):
        if user_id not in self.user_states:
            self.send_message(chat_id, "❌ Ошибка: действие не найдено", self.main_menu_keyboard())
            return
        
        state = self.user_states[user_id]
        
        if state.get("action") == "general_schedule":
            self.user_states[user_id] = {
                "action": "general_schedule",
                "selected_class": class_name
            }
            self.send_message(
                chat_id,
                f"Выбран класс: {class_name}\nТеперь выберите день недели:",
                self.day_selection_inline_keyboard()
            )
    
    def show_schedule(self, chat_id, class_name, day_code, day_name):
        schedule = self.get_schedule(class_name, day_code)
    
        if schedule:
            schedule_text = f"📅 <b>Расписание {self.safe_message(class_name)} класса</b>\n{day_name}\n\n"
            for lesson in schedule:
                lesson_num, subject, teacher, room = lesson
                schedule_text += f"{lesson_num}. <b>{self.safe_message(subject)}</b>"
                if teacher:
                    schedule_text += f" ({self.safe_message(teacher)})"
                if room:
                    schedule_text += f" - {self.safe_message(room)}"
                schedule_text += "\n"
            
            bells = self.get_bell_schedule()
            if bells:
                schedule_text += "\n🔔 <b>Расписание звонков:</b>\n"
                for bell in bells[:len(schedule)]:  
                    bell_num, start_time, end_time = bell
                    if bell_num <= len(schedule):
                        schedule_text += f"{bell_num}. {start_time} - {end_time}\n"
        else:
            schedule_text = f"❌ Расписание для {self.safe_message(class_name)} класса на {day_name.lower()} не найдено"
        
        self.send_message(chat_id, schedule_text, self.main_menu_keyboard())
    
    def handle_schedule_input(self, chat_id, username, text):
        if username not in self.admin_states:
            return
        
        class_name = self.admin_states[username].get("class")
        day_code = self.admin_states[username].get("day")
        
        if not class_name or not day_code:
            self.send_message(chat_id, "❌ Ошибка: данные не найдены", self.admin_menu_inline_keyboard())
            return
        
        if text == '-':
            self.save_schedule(class_name, day_code, [])
            self.send_message(chat_id, "✅ Расписание очищено!", self.admin_menu_inline_keyboard())
        else:
            lessons = []
            lines = text.split('\n')
            
            for line in lines:
                line = line.strip()
                if not line or not line[0].isdigit():
                    continue
                    
                parts = line.split('.', 1)
                if len(parts) < 2:
                    continue
                    
                try:
                    lesson_num = int(parts[0].strip())
                    lesson_info = parts[1].strip()
                    
                    # Разделяем на части по дефисам
                    parts_by_dash = [part.strip() for part in lesson_info.split('-')]
                    
                    # Первая часть - это предмет и возможный учитель в скобках
                    subject_part = parts_by_dash[0].strip()
                    subject = subject_part
                    teacher = ""
                    room = ""
                    
                    # Проверяем, есть ли учитель в скобках в первой части
                    if '(' in subject_part and ')' in subject_part:
                        # Извлекаем учителя из скобок
                        start = subject_part.find('(')
                        end = subject_part.find(')')
                        teacher = subject_part[start+1:end].strip()
                        # Удаляем скобки с учителем из предмета
                        subject = subject_part[:start].strip()
                    
                    # Если есть вторая часть после дефиса - это кабинет
                    if len(parts_by_dash) >= 2:
                        room = parts_by_dash[1].strip()
                        # Если есть третья часть - это тоже кабинет (дублирование)
                        if len(parts_by_dash) >= 3:
                            # Берем последнюю часть как кабинет
                            room = parts_by_dash[-1].strip()
                            # Логируем дублирование для отладки
                            logger.info(f"Обнаружено дублирование кабинета в строке: {line}")
                    
                    # Специальная обработка для случаев вроде "алгебра (6)"
                    # Если teacher похож на номер кабинета, а room пустой
                    if teacher and not room and teacher.isdigit():
                        room = teacher
                        teacher = ""
                        logger.info(f"Перенос учителя как кабинета: teacher={teacher} -> room={room}")
                    
                    # Очищаем предмет от лишних символов
                    subject = subject.replace('()', '').strip()
                    
                    # Убираем дублирование в кабинете (если что-то вроде "37 - 37")
                    if ' - ' in room:
                        room_parts = room.split(' - ')
                        # Берем первую часть как кабинет
                        room = room_parts[0].strip()
                    
                    if subject:
                        lessons.append((lesson_num, subject, teacher, room))
                        logger.info(f"Парсинг урока: номер={lesson_num}, предмет={subject}, учитель={teacher}, кабинет={room}")
                except ValueError as e:
                    logger.error(f"Ошибка парсинга строки '{line}': {e}")
                    continue
            
            if lessons:
                self.save_schedule(class_name, day_code, lessons)
                # Формируем отчет о сохранении
                schedule_text = f"✅ Расписание для {self.safe_message(class_name)} класса обновлено!\n\n"
                schedule_text += f"<b>Сохранено уроков:</b> {len(lessons)}\n\n"
                schedule_text += "<b>Обновленное расписание:</b>\n"
                for lesson_num, subject, teacher, room in sorted(lessons, key=lambda x: x[0]):
                    schedule_text += f"{lesson_num}. <b>{subject}</b>"
                    if teacher:
                        schedule_text += f" ({teacher})"
                    if room:
                        schedule_text += f" - {room}"
                    schedule_text += "\n"
                
                self.send_message(chat_id, schedule_text, self.admin_menu_inline_keyboard())
            else:
                self.send_message(chat_id, "❌ Не удалось распарсить ни одного урока", self.admin_menu_inline_keyboard())
        
        if username in self.admin_states:
            del self.admin_states[username]
    
    def handle_admin_menu(self, chat_id, username, text):
        if not self.is_admin(username):
            self.log_security_event("unauthorized_admin_action", chat_id, f"Action: {text}")
            self.send_message(chat_id, "❌ У вас нет доступа к этой функции")
            return
        
        if text == "👥 Список пользователей":
            self.show_users_list(chat_id)
        elif text == "❌ Удалить пользователя":
            self.start_delete_user(chat_id, username)
        elif text == "📝 Редактировать расписание":
            self.start_edit_schedule(chat_id, username)
        elif text == "🏫 Управление классами":
            self.show_classes_management(chat_id, username)
        elif text == "🕧 Управление звонками":
            self.show_bells_management(chat_id, username)
        elif text == "📤 Загрузить Excel":
            self.send_message(
                chat_id,
                "📤 <b>Загрузка расписания из Excel</b>\n\n"
                "Выберите смену для загрузки:",
                self.shift_selection_keyboard()
            )
            self.admin_states[username] = {"action": "select_shift"}
        elif text == "📊 Статистика":
            self.show_statistics(chat_id)
        elif text == "⬅️ Назад":
            self.send_message(chat_id, "Главное меню", self.main_menu_keyboard())
        elif text in ["1 смена", "2 смена"]:
            self.handle_shift_selection(chat_id, username, text)
    
    def handle_shift_selection(self, chat_id, username, shift_text):
        if username not in self.admin_states:
            return
        
        shift = "1" if shift_text == "1 смена" else "2"
        self.admin_states[username] = {"action": "waiting_excel", "shift": shift}
        
        self.send_message(
            chat_id,
            f"📤 <b>Загрузка расписания для {shift_text}</b>\n\n"
            f"Отправьте Excel файл с расписанием для {shift_text}.\n"
            f"После загрузки файла расписание для {shift_text} будет автоматически обновлено.",
            self.cancel_keyboard()
        )
    
    def show_users_list(self, chat_id):
        users = self.get_all_users()
        
        if not users:
            self.send_message(chat_id, "❌ Нет зарегистрированных пользователей")
            return
        
        users_text = "👥 <b>Список пользователей</b>\n\n"
        for user in users:
            reg_date_str = self.format_date(user[4])
            username_display = f" (@{user[3]})" if user[3] else ""
                
            users_text += f"👤 {self.safe_message(user[1])}{username_display}\n"
            users_text += f"   Класс: {self.safe_message(user[2])} | ID: {user[0]}\n"
            users_text += f"   📅 Зарегистрирован: {reg_date_str}\n\n"
        
        self.send_message(chat_id, users_text, self.admin_menu_inline_keyboard())
    
    def start_edit_schedule(self, chat_id, username):
        self.admin_states[username] = {"action": "edit_schedule_class"}
        self.send_message(
            chat_id,
            "Выберите класс для редактирования расписания:",
            self.class_selection_keyboard()
        )
    
    def handle_schedule_class_selection(self, chat_id, username, class_name):
        if username not in self.admin_states:
            return
        
        self.admin_states[username] = {
            "action": "edit_schedule_day",
            "class": class_name
        }
        
        self.send_message(
            chat_id,
            f"Выбран класс: {self.safe_message(class_name)}\nТеперь выберите день недели:",
            self.day_selection_inline_keyboard()
        )
    
    def handle_schedule_day_selection(self, chat_id, username, day_name):
        logger.info(f"Handling schedule day selection for {username}, day: {day_name}")
        
        if username not in self.admin_states:
            logger.error(f"Admin state not found for {username}")
            self.send_message(chat_id, "❌ Ошибка: действие не найдено", self.admin_menu_inline_keyboard())
            return
        
        class_name = self.admin_states[username].get("class")
        if not class_name:
            logger.error(f"Class not found in admin state for {username}")
            self.send_message(chat_id, "❌ Ошибка: класс не выбран", self.admin_menu_inline_keyboard())
            return
        
        day_map = {
            "понедельник": "monday",
            "вторник": "tuesday",
            "среда": "wednesday",
            "четверг": "thursday",
            "пятница": "friday",
            "суббота": "saturday"
        }
        
        day_code = day_map.get(day_name.lower(), day_name.lower())
        
        current_schedule = self.get_schedule(class_name, day_code)
        
        schedule_text = ""
        if current_schedule:
            schedule_text = "<b>Текущее расписание:</b>\n"
            for lesson in current_schedule:
                schedule_text += f"{lesson[0]}. {self.safe_message(lesson[1])}"
                if lesson[2]:
                    schedule_text += f" ({self.safe_message(lesson[2])})"
                if lesson[3]:
                    schedule_text += f" - {self.safe_message(lesson[3])}"
                schedule_text += "\n"
            schedule_text += "\n"
        
        self.admin_states[username] = {
            "action": "edit_schedule_input",
            "class": class_name,
            "day": day_code
        }
        
        self.send_message(
            chat_id,
            f"Редактирование расписания:\n"
            f"Класс: {self.safe_message(class_name)}\n"
            f"День: {day_name}\n\n"
            f"{schedule_text}"
            f"Введите новое расписание в формате:\n\n"
            f"<code>1. Математика\n2. Физика (Иванов) - 201\n3. Химия - 301</code>\n\n"
            f"Или отправьте '-' для очистки расписания.",
            self.cancel_keyboard()
        )
    
    def show_statistics(self, chat_id):
        users = self.get_all_users()
        total_users = len(users)
        
        classes = {}
        for user in users:
            class_name = user[2]
            if class_name in classes:
                classes[class_name] += 1
            else:
                classes[class_name] = 1
        
        stats_text = "📊 <b>Статистика бота</b>\n\n"
        stats_text += f"👥 Всего пользователей: {total_users}\n\n"
        
        if classes:
            stats_text += "<b>Распределение по классам:</b>\n"
            for class_name, count in sorted(classes.items()):
                stats_text += f"• {self.safe_message(class_name)}: {count} чел.\n"
        
        self.send_message(chat_id, stats_text, self.admin_menu_inline_keyboard())
    
    def process_update(self, update):
            update_id = update.get("update_id")
            
            if update_id in self.processed_updates:
                logger.info(f"Пропускаем уже обработанное обновление: {update_id}")
                return
            
            self.processed_updates.add(update_id)
            
            if len(self.processed_updates) > 1000:
                self.processed_updates = set(list(self.processed_updates)[-500:])
            
            try:
                if "callback_query" in update:
                    self.handle_callback_query(update)
                    return
                
                if "message" in update:
                    message = update["message"]
                    chat_id = message["chat"]["id"]
                    user = message.get("from", {})
                    user_id = user.get("id")
                    username = user.get("username", "")
                    
                    if user_id and self.rate_limiter.is_limited(user_id):
                        self.log_security_event("rate_limit_exceeded", user_id, f"Username: {username}")
                        self.send_message(chat_id, "⚠️ Слишком много запросов. Пожалуйста, подождите.")
                        return
                    
                    # ======= СУЩЕСТВУЮЩИЙ КОД ДЛЯ РАСПИСАНИЯ =======
                    if "document" in message and username in self.admin_states and self.admin_states[username].get("action") == "waiting_excel":
                        document = message["document"]
                        file_id = document["file_id"]
                        file_name = document.get("file_name", "")
                        shift = self.admin_states[username].get("shift", "1")
                        
                        if not file_name.lower().endswith(('.xlsx', '.xls')):
                            self.send_message(chat_id, "❌ Пожалуйста, отправьте файл в формате Excel (.xlsx или .xls)")
                            return
                        
                        self.send_message(chat_id, f"📥 Начинаю загрузку файла для {shift} смены...")
                        
                        file_info = self.get_file(file_id)
                        if not file_info:
                            self.send_message(chat_id, "❌ Ошибка получения информации о файле")
                            return
                        
                        file_content = self.download_file(file_info["file_path"])
                        if not file_content:
                            self.send_message(chat_id, "❌ Ошибка загрузки файла")
                            return
                        
                        self.send_message(chat_id, f"🔍 Обрабатываю расписание для {shift} смены...")
                        
                        success, message_text = self.import_schedule_from_excel(file_content, shift)
                        
                        if success:
                            self.send_message(chat_id, f"✅ {message_text}", self.admin_menu_inline_keyboard())
                        else:
                            self.send_message(chat_id, f"❌ {message_text}", self.admin_menu_inline_keyboard())
                        
                        del self.admin_states[username]
                        return
                    
                    # ======= НОВЫЙ КОД ДЛЯ СПИСКОВ УЧЕНИКОВ =======
                    if "document" in message and username in self.admin_states and self.admin_states[username].get("action") == "roster_waiting_excel":
                        document = message["document"]
                        file_id = document["file_id"]
                        file_name = document.get("file_name", "")
                        
                        if not file_name.lower().endswith(('.xlsx', '.xls')):
                            self.send_message(chat_id, "❌ Пожалуйста, отправьте файл в формате Excel (.xlsx или .xls)")
                            return
                        
                        self.send_message(chat_id, "📥 Загружаю файл со списком учеников...")
                        
                        file_info = self.get_file(file_id)
                        if not file_info:
                            self.send_message(chat_id, "❌ Ошибка получения информации о файле")
                            return
                        
                        file_content = self.download_file(file_info["file_path"])
                        if not file_content:
                            self.send_message(chat_id, "❌ Ошибка загрузки файла")
                            return
                        
                        self.send_message(chat_id, "🔍 Обрабатываю список учеников...")
                        
                        success, message_text = self.db.import_roster_from_excel(file_content)
                        
                        if success:
                            self.send_message(chat_id, f"✅ {message_text}", self.roster_management_inline_keyboard())
                        else:
                            self.send_message(chat_id, f"❌ {message_text}", self.roster_management_inline_keyboard())
                        
                        del self.admin_states[username]
                        return
                    
                    # Дальше идет обработка текстовых сообщений
                    if "text" in message:
                        text = message["text"]
                        
                        if text == "❌ Отменить":
                            if username in self.admin_states:
                                del self.admin_states[username]
                            if user_id in self.user_states:
                                del self.user_states[user_id]
                            self.send_message(chat_id, "Действие отменено", self.main_menu_keyboard())
                            return
                        
                        if username in self.admin_states:
                            state = self.admin_states[username]
                            
                            if state.get("action") in ["add_class_input", "delete_class_input"]:
                                self.handle_class_input(chat_id, username, text)
                                return
                            
                            if state.get("action") in ["edit_bell_number", "edit_bell_start", "edit_bell_end"]:
                                self.handle_bell_input(chat_id, username, text)
                                return
                            
                            if state.get("action") == "delete_user":
                                self.delete_user_by_identifier(chat_id, username, text)
                                return
                            elif state.get("action") == "edit_schedule_input":
                                self.handle_schedule_input(chat_id, username, text)
                                return
                            elif state.get("action") == "edit_schedule_class":
                                self.handle_schedule_class_selection(chat_id, username, text)
                                return
                            elif state.get("action") == "edit_schedule_day":
                                self.handle_schedule_day_selection(chat_id, username, text)
                                return
                            elif state.get("action") == "select_shift":
                                self.handle_shift_selection(chat_id, username, text)
                                return
                            elif state.get("action") == "broadcast_waiting_message":
                                self.handle_broadcast_message(chat_id, username, text)
                                return
                            elif state.get("action") in ["add_news_title", "add_news_content", "add_news_audience", "edit_news_field"]:
                                self.handle_text_message(chat_id, user_id, username, text)
                                return
                            elif state.get("action") == "roster_add_waiting_data":
                                self.handle_roster_add(chat_id, username, text)
                                return
                            elif state.get("action") == "roster_remove_waiting_data":
                                self.handle_roster_remove(chat_id, username, text)
                                return
                            elif state.get("action") == "roster_view_waiting_class":
                                self.handle_roster_view(chat_id, username, text)
                                return
                        
                        if user_id in self.user_states:
                            state = self.user_states[user_id]
                            if state.get("action") == "registration":
                                self.handle_registration_input(chat_id, user_id, username, text)
                                return
                            elif state.get("action") == "news_search":
                                self.process_news_search(chat_id, user_id, text)
                                return
                        
                        if text.startswith("/start"):
                            self.handle_start(chat_id, user)
                        elif text.startswith("/help"):
                            self.handle_help(chat_id, username)
                        elif text.startswith("/admin_panel"):
                            self.handle_admin_panel(chat_id, username)
                        elif text.startswith("/rosters"):
                            if self.is_admin(username):
                                self.show_roster_management(chat_id, username)
                            else:
                                self.send_message(chat_id, "❌ У вас нет доступа к этой функции")
                        elif text.startswith("/import_rosters"):
                            if self.is_admin(username):
                                self.start_roster_import(chat_id, username)
                            else:
                                self.send_message(chat_id, "❌ У вас нет доступа к этой функции")
                        
                        # Проверяем обычные команды меню для всех пользователей
                        elif text in ["📚 Моё расписание", "🏫 Общее расписание", "🔔 Звонки", "📰 Новости", 
                                    "⚙️ Настройки", "🏆 Достижения", "📈 Статистика", "ℹ️ Помощь", "⬅️ Назад"]:
                            self.handle_main_menu(chat_id, user_id, text, username)
                        elif self.is_valid_class(text):
                            self.handle_main_menu(chat_id, user_id, text, username)
                        # Проверяем команды админа
                        elif text in ["👥 Список пользователей", "❌ Удалить пользователя", "📝 Редактировать расписание", 
                                    "🏫 Управление классами", "🕧 Управление звонками", "📤 Загрузить Excel", "📊 Статистика"]:
                            # Это команды админа - проверяем права
                            if self.is_admin(username):
                                self.handle_admin_menu(chat_id, username, text)
                            else:
                                self.send_message(chat_id, "❌ У вас нет доступа к этой функции", self.main_menu_keyboard())
                        elif text in ["1 смена", "2 смена"]:
                            # Команды смены - только для админов
                            if self.is_admin(username):
                                self.handle_shift_selection(chat_id, username, text)
                            else:
                                self.send_message(chat_id, "❌ У вас нет доступа к этой функции", self.main_menu_keyboard())
                        else:
                            # Остальные сообщения
                            if not self.get_user(user_id):
                                self.handle_registration_start(chat_id, user_id)
                            else:
                                self.handle_text_message(chat_id, user_id, username, text)
            
            except Exception as e:
                logger.error(f"Ошибка в process_update: {e}")
                import traceback
                logger.error(traceback.format_exc())

    def run(self):
            """Основной цикл работы бота"""
            logger.info("Бот запущен!")
            
            try:
                delete_url = f"{BASE_URL}/deleteWebhook"
                response = requests.get(delete_url, timeout=10)
                if response.json().get("ok"):
                    logger.info("Вебхук очищен, используется long polling")
                else:
                    logger.warning("Не удалось очистить вебхук")
            except Exception as e:
                logger.error(f"Ошибка при очистке вебхука: {e}")
            
            conflict_count = 0
            max_conflicts = 5
            
            while True:
                try:
                    updates = self.get_updates()
                    
                    if updates.get("conflict"):
                        conflict_count += 1
                        logger.warning(f"Обнаружен конфликт getUpdates ({conflict_count}/{max_conflicts})")
                        
                        if conflict_count >= max_conflicts:
                            logger.error("Достигнуто максимальное количество конфликтов. Завершаем работу.")
                            break
                        
                        time.sleep(10)
                        continue
                    else:
                        conflict_count = 0
                    
                    if updates.get("ok") and "result" in updates:
                        for update in updates["result"]:
                            self.last_update_id = update["update_id"]
                            self.process_update(update)
                    else:
                        if "description" in updates:
                            error_desc = updates.get('description', '')
                            if "Conflict" not in error_desc:
                                logger.error(f"Ошибка Telegram API: {error_desc}")
                    
                    time.sleep(0.5)
                    
                except Exception as e:
                    logger.error(f"Ошибка в основном цикле: {e}")
                    time.sleep(5)

if __name__ == "__main__":
    bot = SimpleSchoolBot()
    bot.run()