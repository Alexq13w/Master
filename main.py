import telebot
from telebot import types
import random
import time
import threading
import datetime
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import psycopg2
from psycopg2 import pool, OperationalError
import socket
import ssl
import os
from flask import Flask, request
from config import TOKEN, ADMIN_ID, PAYMENT_NUMBER, CHANNEL_ID, PAYMENT_ALERTS_CHANNEL, SUBSCRIBE_CHANNEL_ID, DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import timezone, timedelta

ssl._create_default_https_context = ssl._create_unverified_context
socket.setdefaulttimeout(30)

# إعدادات تسجيل الأخطاء
logging.basicConfig(
    filename='bot_errors.log',
    level=logging.ERROR,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
error_logger = logging.getLogger('error_logger')

postgreSQL_pool = None

def init_db_pool():
    global postgreSQL_pool
    try:
        postgreSQL_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            sslmode='disable',
            connect_timeout=5,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5
        )
        error_logger.info("تم إنشاء اتصال قاعدة البيانات بنجاح")
    except OperationalError as e:
        error_logger.error(f"فشل إنشاء connection pool: {e}")
        raise
    except Exception as e:
        error_logger.exception("خطأ غير متوقع في إنشاء اتصال قاعدة البيانات")

def get_db_connection():
    global postgreSQL_pool
    
    if not postgreSQL_pool:
        init_db_pool()
    
    attempts = 0
    max_attempts = 3
    wait_time = 2
    
    while attempts < max_attempts:
        try:
            return postgreSQL_pool.getconn()
        except OperationalError:
            error_logger.error(f"خطأ في الاتصال (المحاولة {attempts+1})")
            time.sleep(wait_time)
            attempts += 1
            wait_time *= 2
        except Exception as e:
            error_logger.exception(f"خطأ غير متوقع في الحصول على اتصال")
            attempts += 1
    
    raise OperationalError("فشل الاتصال بقاعدة البيانات بعد عدة محاولات")

try:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1")
    cursor.close()
    postgreSQL_pool.putconn(conn)
    error_logger.info("تم اختبار اتصال قاعدة البيانات بنجاح")
except Exception as e:
    error_logger.exception("❌ فشل اختبار الاتصال")
    raise

logging.basicConfig(
    filename='bot.log',
    level=logging.WARNING,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(TOKEN)
executor = ThreadPoolExecutor(max_workers=5)

RATE_LIMIT = 10
RATE_LIMIT_PERIOD = 10
user_request_times = defaultdict(list)

def check_rate_limit(user_id):
    now = time.time()
    user_request_times[user_id] = [
        t for t in user_request_times[user_id] 
        if now - t < RATE_LIMIT_PERIOD
    ]
    
    if len(user_request_times[user_id]) >= RATE_LIMIT:
        return False
        
    user_request_times[user_id].append(now)
    return True

bot_commands = [
    telebot.types.BotCommand("start", "بدء استخدام البوت والتحقق من الاشتراك"),
    telebot.types.BotCommand("admin", "لوحة تحكم المشرف (للمشرفين فقط)"),
    telebot.types.BotCommand("stats", "عرض إحصائيات البوت"),
    telebot.types.BotCommand("mytickets", "عرض تذاكري"),
    telebot.types.BotCommand("howto", "طريقة عمل البوت"),
    telebot.types.BotCommand("faq", "الأسئلة الشائعة"),
    telebot.types.BotCommand("pending", "الطلبات المعلقة"),
    telebot.types.BotCommand("share", "مشاركة البوت مع الأصدقاء"),
    telebot.types.BotCommand("winners", "قائمة الفائزين"),
    telebot.types.BotCommand("support", "التواصل مع الدعم")
]
bot.set_my_commands(bot_commands)

conn = get_db_connection()
try:
    with conn.cursor() as cursor:
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            username TEXT,
            ticket_number TEXT,
            purchase_date TEXT,
            ticket_type TEXT,
            is_winner INTEGER DEFAULT 0,
            receipt_number TEXT
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_requests (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            ticket_type TEXT,
            payment_method TEXT,
            request_time TEXT,
            receipt_number TEXT,
            status TEXT DEFAULT 'pending',
            quantity INTEGER DEFAULT 1
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS winners (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            username TEXT,
            ticket_number TEXT,
            purchase_date TEXT,
            ticket_type TEXT,
            win_date TEXT,
            prize_amount INTEGER
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id SERIAL PRIMARY KEY,
            admin_id INTEGER,
            action TEXT,
            target_id INTEGER,
            details TEXT,
            timestamp TEXT
        )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_user_id ON tickets (user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_ticket_type ON tickets (ticket_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_requests_user_id ON pending_requests (user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_requests_status ON pending_requests (status)")
        
        cursor.execute("INSERT INTO settings (key, value) VALUES ('daily_prize', '5000') ON CONFLICT (key) DO NOTHING")
        cursor.execute("INSERT INTO settings (key, value) VALUES ('cumulative_prize', '0') ON CONFLICT (key) DO NOTHING")
        cursor.execute("INSERT INTO settings (key, value) VALUES ('admin_alerts_enabled', '1') ON CONFLICT (key) DO NOTHING")
        conn.commit()
        error_logger.info("تم إنشاء الجداول بنجاح")
except Exception as e:
    error_logger.exception("خطأ في إنشاء الجداول")
finally:
    postgreSQL_pool.putconn(conn)

db_lock = threading.RLock()
subscription_cache = {}
CACHE_EXPIRY = 300  # Reduced cache time to 5 minutes

user_data = {}
scheduler = BackgroundScheduler(timezone="Asia/Damascus")
scheduler.start()

def get_emoji_time(sec):
    minutes = sec // 60
    seconds = sec % 60
    time_str = f"{minutes:02d}:{seconds:02d}"
    
    mapping = {
        '0': '0️⃣',
        '1': '1️⃣',
        '2': '2️⃣',
        '3': '3️⃣',
        '4': '4️⃣',
        '5': '5️⃣',
        '6': '6️⃣',
        '7': '7️⃣',
        '8': '8️⃣',
        '9': '9️⃣',
        ':': '⏱️'
    }
    emoji_str = ''.join(mapping[char] for char in time_str)
    return "⏱️ " + emoji_str

def is_user_subscribed(user_id):
    try:
        chat_member = bot.get_chat_member(SUBSCRIBE_CHANNEL_ID, user_id)
        status = chat_member.status
        if status in ['member', 'administrator', 'creator', 'restricted']:
            return True
        elif status in ['left', 'kicked']:
            return False
        return False
    except telebot.apihelper.ApiTelegramException as e:
        if e.error_code == 400:  # User not found
            return False
        error_logger.error(f"Subscription check error: {e}")
        return False
    except Exception as e:
        error_logger.exception("Unexpected error in subscription check")
        return False

def subscription_markup():
    markup = types.InlineKeyboardMarkup()
    btn = types.InlineKeyboardButton("📢 اشترك في القناة", url=f"https://t.me/{SUBSCRIBE_CHANNEL_ID}")
    markup.add(btn)
    markup.add(types.InlineKeyboardButton("✅ تأكيد الاشتراك", callback_data="check_sub"))
    return markup

def get_local_time():
    return datetime.datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=3))).replace(tzinfo=None)

def clean_old_data():
    try:
        conn = get_db_connection()
        with db_lock:
            with conn.cursor() as cursor:
                old_date = (datetime.datetime.now() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
                cursor.execute("DELETE FROM pending_requests WHERE request_time::date < %s", (old_date,))
                
                old_date_audit = (datetime.datetime.now() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
                cursor.execute("DELETE FROM audit_log WHERE timestamp::date < %s", (old_date_audit,))
                
                old_winners = (datetime.datetime.now() - datetime.timedelta(days=365)).strftime("%Y-%m-%d")
                cursor.execute("DELETE FROM winners WHERE win_date::date < %s", (old_winners,))
                
                conn.commit()
                
                global user_data
                current_time = time.time()
                user_data = {uid: data for uid, data in user_data.items() 
                            if current_time - data.get('timestamp', 0) < 86400}
    except Exception as e:
        error_logger.exception("خطأ في تنظيف البيانات القديمة")
    finally:
        postgreSQL_pool.putconn(conn)

def get_user_pending_requests_count(user_id):
    try:
        conn = get_db_connection()
        with db_lock:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM pending_requests WHERE user_id = %s AND status = 'pending'", (user_id,))
                result = cursor.fetchone()
                return result[0] if result else 0
    except Exception as e:
        error_logger.exception("خطأ في عد الطلبات المعلقة")
        return 0
    finally:
        postgreSQL_pool.putconn(conn)

def get_setting(key):
    try:
        conn = get_db_connection()
        with db_lock:
            with conn.cursor() as cursor:
                cursor.execute("SELECT value FROM settings WHERE key = %s", (key,))
                result = cursor.fetchone()
                return result[0] if result else None
    except Exception as e:
        error_logger.exception(f"خطأ في الحصول على الإعداد: {key}")
        return None
    finally:
        postgreSQL_pool.putconn(conn)

def update_setting(key, value):
    try:
        conn = get_db_connection()
        with db_lock:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO settings (key, value) 
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE 
                    SET value = EXCLUDED.value
                """, (key, str(value)))
                conn.commit()
    except Exception as e:
        error_logger.exception(f"خطأ في تحديث الإعداد: {key}")
    finally:
        postgreSQL_pool.putconn(conn)

def generate_ticket_numbers(ticket_type, quantity):
    try:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        conn = get_db_connection()
        with db_lock:
            with conn.cursor() as cursor:
                if ticket_type == 'يومي':
                    cursor.execute("SELECT ticket_number FROM tickets WHERE DATE(purchase_date) = %s", (today,))
                else:
                    start_of_week = (datetime.datetime.now() - datetime.timedelta(days=datetime.datetime.now().weekday())).strftime("%Y-%m-%d")
                    end_of_week = (datetime.datetime.now() + datetime.timedelta(days=6 - datetime.datetime.now().weekday())).strftime("%Y-%m-%d")
                    cursor.execute("SELECT ticket_number FROM tickets WHERE purchase_date::date BETWEEN %s AND %s", 
                                  (start_of_week, end_of_week))
                
                existing_numbers = {row[0] for row in cursor.fetchall()}
                new_numbers = []
                
                while len(new_numbers) < quantity:
                    num = str(random.randint(10000, 99999))
                    if num not in existing_numbers and num not in new_numbers:
                        new_numbers.append(num)
                
                return new_numbers
    except Exception as e:
        error_logger.exception("خطأ في إنشاء أرقام التذاكر")
        return []
    finally:
        postgreSQL_pool.putconn(conn)

def get_user_tickets(user_id):
    try:
        conn = get_db_connection()
        with db_lock:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id, ticket_number, ticket_type, purchase_date FROM tickets WHERE user_id = %s", (user_id,))
                return cursor.fetchall()
    except Exception as e:
        error_logger.exception("خطأ في الحصول على تذاكر المستخدم")
        return []
    finally:
        postgreSQL_pool.putconn(conn)

def get_stats():
    try:
        conn = get_db_connection()
        with db_lock:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(DISTINCT user_id) FROM tickets")
                users = cursor.fetchone()[0] or 0
                
                current_month = datetime.datetime.now().strftime("%Y-%m")
                cursor.execute("SELECT COUNT(DISTINCT user_id) FROM tickets WHERE to_char(purchase_date::timestamp, 'YYYY-MM') = %s", (current_month,))
                monthly_users = cursor.fetchone()[0] or 0
                
                cursor.execute("SELECT COUNT(*) FROM tickets")
                tickets = cursor.fetchone()[0] or 0
                
                total_amount = tickets * 5000
                prize = int(total_amount * 0.85)
                return users, monthly_users, tickets, total_amount, prize
    except Exception as e:
        error_logger.exception("خطأ في الحصول على الإحصائيات")
        return 0, 0, 0, 0, 0
    finally:
        postgreSQL_pool.putconn(conn)

def get_daily_stats():
    try:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        conn = get_db_connection()
        with db_lock:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM tickets WHERE ticket_type = 'يومي' AND purchase_date::date = %s", (today,))
                daily_tickets = cursor.fetchone()[0] or 0
                
                cursor.execute("SELECT COUNT(DISTINCT user_id) FROM tickets WHERE ticket_type = 'يومي' AND purchase_date::date = %s", (today,))
                daily_users = cursor.fetchone()[0] or 0
                
                daily_prize = daily_tickets * 5000 * 0.85
                return daily_tickets, daily_users, daily_prize
    except Exception as e:
        error_logger.exception("خطأ في الحصول على إحصائيات اليوم")
        return 0, 0, 0
    finally:
        postgreSQL_pool.putconn(conn)

def get_weekly_stats():
    try:
        now = get_local_time()
        start_of_week = now - datetime.timedelta(days=now.weekday())
        end_of_week = start_of_week + datetime.timedelta(days=6)
        
        conn = get_db_connection()
        with db_lock:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT COUNT(*) 
                    FROM tickets 
                    WHERE ticket_type = 'أسبوعي' 
                    AND purchase_date::date BETWEEN %s AND %s
                """, (start_of_week.strftime('%Y-%m-%d'), end_of_week.strftime('%Y-%m-%d')))
                weekly_tickets = cursor.fetchone()[0] or 0
                
                cursor.execute("""
                    SELECT COUNT(DISTINCT user_id) 
                    FROM tickets 
                    WHERE ticket_type = 'أسبوعي' 
                    AND purchase_date::date BETWEEN %s AND %s
                """, (start_of_week.strftime('%Y-%m-%d'), end_of_week.strftime('%Y-%m-%d')))
                weekly_users = cursor.fetchone()[0] or 0
                
                weekly_prize = weekly_tickets * 5000 * 0.85
                return weekly_tickets, weekly_users, weekly_prize
    except Exception as e:
        error_logger.exception("خطأ في الحصول على إحصائيات الأسبوع")
        return 0, 0, 0
    finally:
        postgreSQL_pool.putconn(conn)

def get_winners():
    try:
        conn = get_db_connection()
        with db_lock:
            with conn.cursor() as cursor:
                cursor.execute("SELECT username, ticket_number, ticket_type, win_date, prize_amount FROM winners ORDER BY id DESC LIMIT 10")
                return cursor.fetchall()
    except Exception as e:
        error_logger.exception("خطأ في الحصول على الفائزين")
        return []
    finally:
        postgreSQL_pool.putconn(conn)

def is_admin(user_id):
    return user_id == ADMIN_ID

def main_markup(user_id):
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    pending_count = get_user_pending_requests_count(user_id)
    pending_button = f'🧾 الطلبات المعلّقة ({pending_count})' if pending_count > 0 else '🧾 الطلبات المعلّقة'
    
    buttons = [
        types.KeyboardButton('🎟️ احجز تذكرتك الآن'),
        types.KeyboardButton('📞 تواصل مع الدعم'),
        types.KeyboardButton('📊 الإحصائيات'),
        types.KeyboardButton('🎫 تذاكري'),
        types.KeyboardButton('❓ طريقة العمل'),
        types.KeyboardButton('❓ الأسئلة الشائعة'),
        types.KeyboardButton(pending_button),
        types.KeyboardButton('🎯 شارك مع صديق'),
        types.KeyboardButton('🏆 الفائزين'),
    ]
    markup.add(*buttons)
    return markup

def admin_markup():
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    markup.add(
        types.KeyboardButton('👥 عدد المستخدمين'),
        types.KeyboardButton('📢 إرسال جماعي'),
        types.KeyboardButton('📋 الطلبات المعلقة'),
        types.KeyboardButton('🏠 القائمة الرئيسية')
    )
    return markup

def payment_method_markup():
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    markup.add(
        types.KeyboardButton('سيريتل كاش'),
        types.KeyboardButton('شام كاش'),
        types.KeyboardButton('إلغاء'),
    )
    return markup

def ticket_type_markup():
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    markup.add(
        types.KeyboardButton('يومي'),
        types.KeyboardButton('أسبوعي'),
        types.KeyboardButton('إلغاء'),
    )
    return markup

def quantity_markup():
    markup = types.ReplyKeyboardMarkup(row_width=3, resize_keyboard=True)
    for i in range(1, 11):
        markup.add(types.KeyboardButton(str(i)))
    markup.add(types.KeyboardButton('إلغاء'))
    return markup

# تحسين أداء السحب باستخدام استعلام أكثر كفاءة
def perform_draw(draw_type):
    try:
        now = get_local_time()
        total_seconds = 60
        
        # الحصول على عدد التذاكر أولاً
        conn = get_db_connection()
        try:
            with db_lock:
                with conn.cursor() as cursor:
                    if draw_type == "يومي":
                        today_str = now.strftime("%Y-%m-%d")
                        cursor.execute("SELECT COUNT(*) FROM tickets WHERE ticket_type = %s AND DATE(purchase_date) = %s", (draw_type, today_str))
                        total_tickets = cursor.fetchone()[0]
                    else:
                        start_of_week = (now - datetime.timedelta(days=now.weekday())).strftime("%Y-%m-%d")
                        end_of_week = (now + datetime.timedelta(days=6 - now.weekday())).strftime("%Y-%m-%d")
                        cursor.execute("SELECT COUNT(*) FROM tickets WHERE ticket_type = %s AND purchase_date::date BETWEEN %s AND %s", (draw_type, start_of_week, end_of_week))
                        total_tickets = cursor.fetchone()[0]
        finally:
            postgreSQL_pool.putconn(conn)
        
        if total_tickets == 0:
            bot.send_message(CHANNEL_ID, f"⚠️ لا توجد تذاكر للسحب {draw_type} اليوم")
            return "⚠️ لا توجد تذاكر للسحب"
        
        # اختيار فائز عشوائي باستخدام OFFSET
        offset = random.randint(0, total_tickets - 1)
        conn = get_db_connection()
        try:
            with db_lock:
                with conn.cursor() as cursor:
                    if draw_type == "يومي":
                        cursor.execute("""
                            SELECT id, user_id, username, ticket_number, purchase_date 
                            FROM tickets 
                            WHERE ticket_type = %s AND DATE(purchase_date) = %s
                            OFFSET %s LIMIT 1
                        """, (draw_type, today_str, offset))
                    else:
                        cursor.execute("""
                            SELECT id, user_id, username, ticket_number, purchase_date 
                            FROM tickets 
                            WHERE ticket_type = %s AND purchase_date::date BETWEEN %s AND %s
                            OFFSET %s LIMIT 1
                        """, (draw_type, start_of_week, end_of_week, offset))
                    
                    winner = cursor.fetchone()
        finally:
            postgreSQL_pool.putconn(conn)
        
        if not winner:
            bot.send_message(CHANNEL_ID, f"⚠️ خطأ في اختيار الفائز للسحب {draw_type}")
            return "⚠️ خطأ في اختيار الفائز"
        
        display_type = "السحب اليومي" if draw_type == "يومي" else "السحب الأسبوعي"
        
        countdown_msg = bot.send_message(
            CHANNEL_ID,
            f"🔥✨ العد التنازلي ل{display_type} يبدأ الآن! ✨🔥\n\n"
            "⏳ الوقت المتبقي:\n"
            "⬇️⬇️⬇️⬇️⬇️⬇️⬇️⬇️⬇️⬇️\n"
            f"{get_emoji_time(60)}\n\n"
            "🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢\n\n"
            "🏆 الجائزة تنتظر الفائز المحظوظ!",
            parse_mode='Markdown'
        )
        
        start_time = time.time()
        
        for sec in range(total_seconds, -1, -1):
            elapsed = time.time() - start_time
            remaining_time = max(0, total_seconds - elapsed)
            sec = int(remaining_time)
            
            if sec < 0:
                break
                
            progress = int((sec / total_seconds) * 10)
            progress_bar = "🟢" * (10 - progress) + "⚪" * progress
            
            if sec <= 10:
                fire_effect = "🔥" * (11 - sec)
                text = (
                    f"{fire_effect} العد التنازلي النهائي! {fire_effect}\n\n"
                    f"⏳ الوقت المتبقي:\n"
                    f"⬇️⬇️⬇️⬇️⬇️⬇️⬇️⬇️⬇️⬇️\n"
                    f"{get_emoji_time(sec)}\n\n"
                    f"{progress_bar}\n\n"
                    "🏆 الجائزة تنتظر الفائز المحظوظ!"
                )
            else:
                text = (
                    f"🔥✨ العد التنازلي ل{display_type} ✨🔥\n\n"
                    f"⏳ الوقت المتبقي:\n"
                    f"⬇️⬇️⬇️⬇️⬇️⬇️⬇️⬇️⬇️⬇️\n"
                    f"{get_emoji_time(sec)}\n\n"
                    f"{progress_bar}\n\n"
                    "🏆 الجائزة تنتظر الفائز المحظوظ!"
                )
            
            try:
                bot.edit_message_text(
                    text,
                    chat_id=CHANNEL_ID,
                    message_id=countdown_msg.message_id,
                    parse_mode='Markdown'
                )
            except Exception as e:
                error_logger.error(f"خطأ في تعديل رسالة العد التنازلي: {e}")
            
            time_to_sleep = min(1.0, remaining_time - sec)
            if time_to_sleep > 0:
                time.sleep(time_to_sleep)
        
        ticket_id, user_id, username, ticket_number, purchase_date = winner
        
        prize_amount = int(total_tickets * 5000 * 0.85)
        win_date = now.strftime("%Y-%m-%d %H:%M:%S")
        
        conn = get_db_connection()
        try:
            with db_lock:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO winners (user_id, username, ticket_number, purchase_date, ticket_type, win_date, prize_amount)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (user_id, username, ticket_number, purchase_date, draw_type, win_date, prize_amount))
                    
                    if draw_type == "يومي":
                        cursor.execute("""
                            DELETE FROM tickets 
                            WHERE ticket_type = %s AND DATE(purchase_date) = %s
                        """, (draw_type, today_str))
                    else:
                        cursor.execute("""
                            DELETE FROM tickets 
                            WHERE ticket_type = %s AND purchase_date::date BETWEEN %s AND %s
                        """, (draw_type, start_of_week, end_of_week))
                    
                    conn.commit()
        finally:
            postgreSQL_pool.putconn(conn)
        
        bot_username = bot.get_me().username
        start_link = f"https://t.me/{bot_username}?start=start"
        
        winner_message = (
            f"🎉🎉🎉 مبروك! لقد فزت بجائزة {display_type} 🎉🎉🎉\n\n"
            f"✨ نتائج السحب الرسمية ✨\n"
            f"🏆 نوع السحب: {display_type}\n"
            f"🎫 رقم التذكرة الفائزة: {ticket_number}\n"
            f"💰 قيمة الجائزة: {prize_amount:,} ليرة سورية 💵\n"
            f"📅 تاريخ السحب: {now.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"🎊 تهانينا القلبية على فوزك! هذه لحظة سعيدة ونتمنى لك المزيد من التوفيق والنجاح في حياتك.\n\n"
            f"🔔 سيتم التواصل معك خلال 24 ساعة لترتيب استلام الجائزة.\n\n"
            f"💬 لمزيد من المعلومات، يمكنك التواصل مع الدعم:\n"
            f"👉 /start"
        )
        
        try:
            bot.send_message(
                user_id,
                winner_message,
                parse_mode='Markdown'
            )
        except Exception as e:
            error_logger.error(f"Failed to send message to winner: {e}")
        
        channel_announcement = (
            f"🎉🎉🎉 تم الإعلان عن الفائز بجائزة {display_type} 🎉🎉🎉\n\n"
            f"✨ نتائج السحب الرسمية ✨\n"
            f"🏆 نوع السحب: {display_type}\n"
            f"🎟️ عدد التذاكر المشاركة: {total_tickets}\n"
            f"🥇 الفائز المحظوظ: @{username}\n"
            f"🎫 رقم التذكرة الفائزة: {ticket_number}\n"
            f"💰 قيمة الجائزة: {prize_amount:,} ليرة سورية 💵\n"
            f"📅 تاريخ السحب: {now.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"🎊 مبروك للفائز! نتمنى له التوفيق دائمًا! 🎁\n\n"
            f"💔 لم تكن هذه المرة من نصيبك؟ لا تحزن!\n"
            f"🍀 الفرص ما زالت متاحة والحظ قد يبتسم لك في المرة القادمة!\n\n"
            f"🎯 جرب حظك الآن واشترِ تذكرتك:\n"
            f"[👉 اضغط هنا للبدء]({start_link})"
        )
        bot.send_message(CHANNEL_ID, channel_announcement, parse_mode='Markdown')
        
        return "✅ تم السحب بنجاح"
    except Exception as e:
        error_msg = f"❌ حدث خطأ أثناء السحب: {str(e)}"
        error_logger.exception("خطأ في السحب")
        bot.send_message(CHANNEL_ID, error_msg)
        return error_msg

scheduler.add_job(
    lambda: perform_draw("يومي"),
    'cron',
    hour=12,
    minute=0,
    timezone="Asia/Damascus"
)

scheduler.add_job(
    lambda: perform_draw("أسبوعي"),
    'cron',
    day_of_week='fri',
    hour=12,
    minute=0,
    timezone="Asia/Damascus"
)

scheduler.add_job(
    clean_old_data,
    'cron',
    hour=4,
    minute=0,
    timezone="Asia/Damascus"
)

def send_payment_alert(request_id, user_id, ticket_type, payment_method, quantity, receipt_number):
    try:
        alert_text = (
            "✨🌟✨ طلب دفع جديد! ✨🌟✨\n\n"
            f"🔔 تم استلام طلب جديد!\n"
            f"🆔 رقم الطلب: `{request_id}`\n"
            f"👤 معرف المستخدم: `{user_id}`\n"
            f"🎟️ نوع التذكرة: {ticket_type}\n"
            f"💳 طريقة الدفع: {payment_method}\n"
            f"🔢 الكمية: {quantity}\n"
            f"💸 الإجمالي: {quantity * 5000:,} ليرة سورية\n\n"
            f"📊 الرجاء مراجعة لوحة التحكم للمشرفين"
        )
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📋 فتح لوحة التحكم", callback_data="open_admin_panel"))
        
        # التحقق من نوع الإيصال وإرساله بالطريقة المناسبة
        if isinstance(receipt_number, str) and receipt_number.isdigit():
            # رقم إشعار نصي
            try:
                bot.send_message(
                    PAYMENT_ALERTS_CHANNEL,
                    alert_text + f"\n📋 رقم الإشعار: `{receipt_number}`",
                    parse_mode='Markdown',
                    reply_markup=markup
                )
            except Exception as e:
                error_logger.error(f"فشل إرسال إشعار الدفع: {str(e)}")
        else:
            # صورة إشعار
            try:
                bot.send_photo(
                    PAYMENT_ALERTS_CHANNEL,
                    receipt_number,
                    caption=alert_text,
                    parse_mode='Markdown',
                    reply_markup=markup
                )
            except Exception as e:
                error_logger.error(f"فشل إرسال صورة الإيصال: {str(e)}")
                try:
                    bot.send_message(
                        PAYMENT_ALERTS_CHANNEL,
                        alert_text + f"\n⚠️ تعذر إرسال صورة الإيصال (file_id: {receipt_number})",
                        parse_mode='Markdown',
                        reply_markup=markup
                    )
                except Exception as e:
                    error_logger.error(f"فشل إرسال رسالة المشرف: {e}")
    except Exception as e:
        error_logger.exception(f"خطأ عام في إرسال إشعار الدفع: {e}")

def send_message_safe(user_id, message):
    try:
        bot.copy_message(user_id, message.chat.id, message.message_id)
        return True
    except Exception as e:
        error_logger.error(f"Failed to send message to {user_id}: {e}")
        return False

def ensure_subscription(user_id, chat_id):
    if not is_user_subscribed(user_id):
        friendly_reminder = (
            f"👋 أهلاً بك في بوت ماستر!\n\n"
            "✨ لمتابعة استخدام البوت، يرجى الاشتراك في قناتنا أولاً ✨\n\n"
            "🔔 فوائد الاشتراك في القناة:\n"
            "• ستكون أول من يعرف نتائج السحب اليومي والأسبوعي 🏆\n"
            "• ستتلقى إشعارات حصرية قبل بدء السحب بوقت كافٍ 🔔\n"
            "• ستحصل على آخر التحديثات والعروض الخاصة بالبوت 🎁\n\n"
            "بعد الاشتراك اضغط /تحقق للبدء"
        )
        bot.send_message(
            chat_id,
            friendly_reminder,
            parse_mode='Markdown',
            reply_markup=subscription_markup()
        )
        return False
    return True

def ensure_subscription_callback(user_id, callback_id):
    if not is_user_subscribed(user_id):
        bot.answer_callback_query(callback_id, "🚫 يجب الاشتراك في القناة أولاً. اشترك ثم اضغط تأكيد الاشتراك", show_alert=True)
        return False
    return True

@bot.message_handler(commands=['start'])
def start(message):
    try:
        if not check_rate_limit(message.from_user.id):
            bot.reply_to(message, "⚠️ لقد تجاوزت الحد المسموح من الطلبات. يرجى الانتظار قليلاً.")
            return

        user = message.from_user
        
        if not ensure_subscription(user.id, message.chat.id):
            return
            
        cumulative_prize = get_setting('cumulative_prize')
        prize_text = "🎁 الجائزة اليوم: 85% من قيمة التذاكر المباعة"
            
        welcome_text = (
            f"🎉 أهلاً بك في بوت ماستر - يانصيب سوريا الذكي، {user.first_name}!\n"
            f"اختبر حظك كل يوم واربح جوائز مميزة 💰\n\n"
            f"{prize_text}\n\n"
            f"💰 سعر التذكرة: 5000 ل.س\n"
            f"📊 شفافية تامة ونتائج واضحة للجميع\n\n"
            f"📌 يمكنك معرفة الفائزين ونتائج السحب عبر زر '🏆 الفائزين'\n\n"
            f"👇 اختر من القائمة:"
        )

        bot.send_message(message.chat.id, welcome_text, reply_markup=main_markup(user.id))
    except Exception as e:
        error_logger.exception("خطأ في أمر البداية")
        bot.reply_to(message, "حدث خطأ غير متوقع. يرجى المحاولة لاحقًا.")

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not is_admin(message.from_user.id):
            return
        
        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🎯 السحب اليومي", callback_data="daily_draw"),
            types.InlineKeyboardButton("🏆 السحب الأسبوعي", callback_data="weekly_draw"),
            types.InlineKeyboardButton("👥 عدد المستخدمين", callback_data="users_count"),
            types.InlineKeyboardButton("📢 إرسال جماعي", callback_data="broadcast"),
            types.InlineKeyboardButton("📋 الطلبات المعلقة", callback_data="pending_requests_admin")
        )
        bot.send_message(message.chat.id, "🎛️ لوحة تحكم المشرف", reply_markup=markup)
    except Exception as e:
        error_logger.exception("خطأ في لوحة المشرف")

@bot.callback_query_handler(func=lambda call: call.data in ["daily_draw", "weekly_draw", "users_count", "broadcast", "pending_requests_admin"])
def handle_admin_actions(call):
    try:
        if not is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "⛔ هذا الأمر للمشرف فقط!")
            return
            
        if not ensure_subscription_callback(call.from_user.id, call.id):
            return
            
        if call.data in ["daily_draw", "weekly_draw"]:
            bot.answer_callback_query(call.id, "جاري معالجة السحب...")
            draw_type = "يومي" if call.data == "daily_draw" else "أسبوعي"
            result = perform_draw(draw_type)
        elif call.data == "users_count":
            users, monthly_users, tickets, total, prize = get_stats()
            response = f"👤 عدد المستخدمين: {users}\n🎟️ عدد التذاكر المباعة: {tickets}"
            bot.answer_callback_query(call.id, response, show_alert=True)
        elif call.data == "broadcast":
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add(types.KeyboardButton('الغاء'))
            bot.send_message(
                call.message.chat.id,
                "📤 أرسل الرسالة التي تريد إذاعتها (أو اضغط 'الغاء' للرجوع):",
                reply_markup=markup
            )
            bot.register_next_step_handler(call.message, process_broadcast_message)
        elif call.data == "pending_requests_admin":
            show_pending_requests(call.message.chat.id)
    except Exception as e:
        error_logger.exception("خطأ في معالجة إجراءات المشرف")
        bot.answer_callback_query(call.id, "حدث خطأ أثناء المعالجة")

@bot.callback_query_handler(func=lambda call: call.data == "open_admin_panel")
def handle_admin_panel(call):
    try:
        if not is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "⛔ هذا الأمر للمشرف فقط!")
            return
            
        if not ensure_subscription_callback(call.from_user.id, call.id):
            return
            
        try:
            admin_markup = types.InlineKeyboardMarkup(row_width=2)
            admin_markup.add(
                types.InlineKeyboardButton("🎯 السحب اليومي", callback_data="daily_draw"),
                types.InlineKeyboardButton("🏆 السحب الأسبوعي", callback_data="weekly_draw"),
                types.InlineKeyboardButton("👥 عدد المستخدمين", callback_data="users_count"),
                types.InlineKeyboardButton("📢 إرسال جماعي", callback_data="broadcast"),
                types.InlineKeyboardButton("📋 الطلبات المعلقة", callback_data="pending_requests_admin")
            )
            bot.send_message(call.from_user.id, "🎛️ لوحة تحكم المشرف", reply_markup=admin_markup)
        except Exception as e:
            bot.answer_callback_query(call.id, f"❌ فشل في فتح لوحة التحكم: {str(e)}")
    except Exception as e:
        error_logger.exception("خطأ في فتح لوحة المشرف")

def show_pending_requests(chat_id, page=0, filters=None):
    try:
        per_page = 10
        offset = page * per_page
        
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                query = "SELECT * FROM pending_requests WHERE 1=1"
                params = []
                
                if filters and filters.get('ticket_type'):
                    query += " AND ticket_type = %s"
                    params.append(filters['ticket_type'])
                    
                if filters and filters.get('status'):
                    query += " AND status = %s"
                    params.append(filters['status'])
                    
                if filters and filters.get('date'):
                    query += " AND request_time::date = %s"
                    params.append(filters['date'])
                
                count_query = f"SELECT COUNT(*) FROM ({query}) AS subquery"
                cursor.execute(count_query, params)
                total_count = cursor.fetchone()[0]
                
                query += " ORDER BY id ASC LIMIT %s OFFSET %s"
                params.extend([per_page, offset])
                cursor.execute(query, params)
                requests = cursor.fetchall()
        finally:
            postgreSQL_pool.putconn(conn)
        
        if not requests:
            bot.send_message(chat_id, "🎉 لا توجد طلبات معلّقة!")
            return
            
        total_pages = (total_count + per_page - 1) // per_page
        
        response = f"📋 الطلبات المعلقة (الصفحة {page+1}/{total_pages})\n\n"
        for i, req in enumerate(requests, 1):
            response += f"🔹 الطلب #{req[0]} (عدد التذاكر: {req[7]})\n"
        
        markup = types.InlineKeyboardMarkup(row_width=3)
        
        pagination_btns = []
        if page > 0:
            pagination_btns.append(types.InlineKeyboardButton("⬅️ السابق", callback_data=f"pending_page_{page-1}"))
        if page < total_pages - 1:
            pagination_btns.append(types.InlineKeyboardButton("التالي ➡️", callback_data=f"pending_page_{page+1}"))
        
        if pagination_btns:
            markup.row(*pagination_btns)
        
        for req in requests:
            markup.row(
                types.InlineKeyboardButton(f"👁️ تفاصيل {req[0]}", callback_data=f"admin_details_{req[0]}")
            )
        
        filter_btn = types.InlineKeyboardButton("🔍 تصفية", callback_data="filter_requests")
        markup.add(filter_btn)
        
        alerts_enabled = get_setting('admin_alerts_enabled') == '1'
        alerts_text = "🔕 إيقاف التنبيهات" if alerts_enabled else "🔔 تفعيل التنبيهات"
        alerts_callback = "disable_alerts" if alerts_enabled else "enable_alerts"
        markup.add(types.InlineKeyboardButton(alerts_text, callback_data=alerts_callback))
        
        bot.send_message(chat_id, response, reply_markup=markup)
    except Exception as e:
        error_logger.exception("خطأ في عرض الطلبات المعلقة")

@bot.callback_query_handler(func=lambda call: call.data in ['enable_alerts', 'disable_alerts'])
def toggle_alerts(call):
    try:
        if not ensure_subscription_callback(call.from_user.id, call.id):
            return
            
        new_value = '1' if call.data == 'enable_alerts' else '0'
        update_setting('admin_alerts_enabled', new_value)
        
        alerts_text = "تم تفعيل التنبيهات 🔔" if new_value == '1' else "تم تعطيل التنبيهات 🔕"
        bot.answer_callback_query(call.id, alerts_text)
        
        show_pending_requests(call.message.chat.id)
    except Exception as e:
        error_logger.exception("خطأ في تبديل التنبيهات")

@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_details_'))
def show_request_details(call):
    try:
        if not ensure_subscription_callback(call.from_user.id, call.id):
            return
            
        request_id = int(call.data.split('_')[2])
        
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM pending_requests WHERE id = %s", (request_id,))
                req = cursor.fetchone()
        finally:
            postgreSQL_pool.putconn(conn)
        
        if not req:
            bot.answer_callback_query(call.id, "❌ الطلب غير موجود")
            return
        
        details = (
            f"📋 تفاصيل الطلب #{req[0]}\n\n"
            f"👤 المستخدم: {req[1]}\n"
            f"🎫 النوع: {req[2]}\n"
            f"💳 الدفع: {req[3]}\n"
            f"🔢 الكمية: {req[7]}\n"
            f"💸 الإجمالي: {req[7] * 5000:,} ليرة سورية\n"
            f"🕒 التاريخ: {req[4]}\n"
            f"📌 الحالة: {req[6]}"
        )
        
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ قبول", callback_data=f"admin_approve_{req[0]}"),
            types.InlineKeyboardButton("❌ رفض", callback_data=f"admin_reject_{req[0]}"),
            types.InlineKeyboardButton("🔙 رجوع", callback_data="pending_back")
        )
        
        try:
            if req[5]:
                bot.send_photo(
                    call.message.chat.id,
                    req[5],
                    caption=details,
                    reply_markup=markup
                )
            else:
                bot.send_message(
                    call.message.chat.id,
                    details,
                    reply_markup=markup
                )
        except:
            bot.send_message(
                call.message.chat.id,
                details + f"\n✏️ رقم الإشعار: {req[5]}",
                reply_markup=markup
            )
        
        bot.answer_callback_query(call.id, "تم عرض تفاصيل الطلب")
    except Exception as e:
        error_logger.exception("خطأ في عرض تفاصيل الطلب")

@bot.callback_query_handler(func=lambda call: call.data == 'pending_back')
def back_to_pending(call):
    try:
        if not ensure_subscription_callback(call.from_user.id, call.id):
            return
            
        show_pending_requests(call.message.chat.id)
    except Exception as e:
        error_logger.exception("خطأ في العودة للطلبات")

@bot.callback_query_handler(func=lambda call: call.data.startswith('pending_page_'))
def handle_pending_page(call):
    try:
        if not ensure_subscription_callback(call.from_user.id, call.id):
            return
            
        page = int(call.data.split('_')[2])
        show_pending_requests(call.message.chat.id, page)
    except Exception as e:
        error_logger.exception("خطأ في تغيير صفحة الطلبات")

@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_reject_'))
def admin_reject_request(call):
    try:
        if not ensure_subscription_callback(call.from_user.id, call.id):
            return
            
        request_id = int(call.data.split('_')[2])
        
        try:
            conn = get_db_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT * FROM pending_requests WHERE id = %s", (request_id,))
                    req = cursor.fetchone()
                    
                    if not req:
                        bot.answer_callback_query(call.id, "❌ الطلب غير موجود")
                        return
                        
                    user_id = req[1]
                    
                    cursor.execute("DELETE FROM pending_requests WHERE id = %s", (request_id,))
                    
                    cursor.execute(
                        "INSERT INTO audit_log (admin_id, action, target_id, details, timestamp) VALUES (%s, %s, %s, %s, %s)",
                        (call.from_user.id, "reject", user_id, f"طلب #{request_id}", get_local_time().strftime("%Y-%m-%d %H:%M:%S"))
                    )
                    
                    conn.commit()
            finally:
                postgreSQL_pool.putconn(conn)
            
            try:
                bot.send_message(
                    user_id,
                    f"❌ عذرًا، تم رفض طلب شراء التذكرة الخاص بك.\n\n"
                    f"🔍 لم يتم قبول إشعار الدفع الذي أرسلته.\n\n"
                    f"إذا كنت تعتقد أن هناك خطأ، يرجى التواصل مع الدعم الفني:\n"
                    f"👉 <a href='tg://user?id={ADMIN_ID}'>اضغط هنا للتواصل مع الدعم</a>\n\n"
                    f"شكرًا لتفهمك! 🤝",
                    parse_mode='HTML',
                    reply_markup=main_markup(user_id)
                )
            except Exception as e:
                error_logger.error(f"Error sending rejection message: {e}")
            
            bot.send_message(
                call.message.chat.id,
                f"❌ تم رفض الطلب #{request_id} بنجاح!",
                reply_markup=admin_markup()
            )
            bot.answer_callback_query(call.id, f"❌ تم رفض الطلب #{request_id}")
        except Exception as e:
            bot.send_message(
                call.message.chat.id,
                f"❌ حدث خطأ أثناء معالجة الطلب",
                reply_markup=admin_markup()
            )
            bot.answer_callback_query(call.id, f"❌ حدث خطأ أثناء المعالجة")
        
        show_pending_requests(call.message.chat.id)
    except Exception as e:
        error_logger.exception("خطأ في رفض الطلب")

@bot.callback_query_handler(func=lambda call: call.data == 'filter_requests')
def filter_requests(call):
    try:
        if not ensure_subscription_callback(call.from_user.id, call.id):
            return
            
        markup = types.InlineKeyboardMarkup(row_width=2)
        
        markup.add(
            types.InlineKeyboardButton("🎫 يومي", callback_data="filter_type_يومي"),
            types.InlineKeyboardButton("📅 أسبوعي", callback_data="filter_type_أسبوعي")
        )
        
        markup.add(
            types.InlineKeyboardButton("🟢 معالجة", callback_data="filter_status_processing"),
            types.InlineKeyboardButton("🔴 معلق", callback_data="filter_status_pending")
        )
        
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        markup.add(types.InlineKeyboardButton("📅 اليوم", callback_data=f"filter_date_{today}"))
        
        markup.add(types.InlineKeyboardButton("🔎 بحث برقم الطلب", callback_data="search_by_id"))
        
        markup.add(types.InlineKeyboardButton("🔄 إعادة تعيين", callback_data="filter_reset"))
        
        bot.send_message(call.message.chat.id, "🔍 اختر معايير التصفية:", reply_markup=markup)
    except Exception as e:
        error_logger.exception("خطأ في تصفية الطلبات")

@bot.callback_query_handler(func=lambda call: call.data == 'search_by_id')
def search_by_id(call):
    try:
        if not ensure_subscription_callback(call.from_user.id, call.id):
            return
            
        msg = bot.send_message(
            call.message.chat.id,
            "🔢 الرجاء إرسال رقم الطلب للبحث:",
            reply_markup=types.ReplyKeyboardRemove()
        )
        bot.register_next_step_handler(msg, process_search_by_id)
    except Exception as e:
        error_logger.exception("خطأ في البحث برقم الطلب")

def process_search_by_id(message):
    try:
        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        if not message.text.isdigit():
            bot.send_message(
                message.chat.id,
                "⚠️ رقم الطلب غير صالح! يجب أن يكون رقماً فقط.",
                reply_markup=admin_markup()
            )
            return
        
        request_id = int(message.text)
        
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM pending_requests WHERE id = %s", (request_id,))
                req = cursor.fetchone()
        finally:
            postgreSQL_pool.putconn(conn)
        
        if not req:
            bot.send_message(
                message.chat.id,
                f"⚠️ لا يوجد طلب بالرقم {request_id}",
                reply_markup=admin_markup()
            )
            return
        
        details = (
            f"📋 تفاصيل الطلب #{req[0]}\n\n"
            f"👤 المستخدم: {req[1]}\n"
            f"🎫 النوع: {req[2]}\n"
            f"💳 الدفع: {req[3]}\n"
            f"🔢 الكمية: {req[7]}\n"
            f"💸 الإجمالي: {req[7] * 5000:,} ليرة سورية\n"
            f"🕒 التاريخ: {req[4]}\n"
            f"📌 الحالة: {req[6]}"
        )
        
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ قبول", callback_data=f"admin_approve_{req[0]}"),
            types.InlineKeyboardButton("❌ رفض", callback_data=f"admin_reject_{req[0]}"),
            types.InlineKeyboardButton("🔙 رجوع", callback_data="pending_back")
        )
        
        try:
            if req[5]:
                bot.send_photo(
                    message.chat.id,
                    req[5],
                    caption=details,
                    reply_markup=markup
                )
            else:
                bot.send_message(
                    message.chat.id,
                    details,
                    reply_markup=markup
                )
        except:
            bot.send_message(
                message.chat.id,
                details + f"\n✏️ رقم الإشعار: {req[5]}",
                reply_markup=markup
            )
    except Exception as e:
        error_logger.exception("خطأ في معالجة البحث برقم الطلب")

@bot.callback_query_handler(func=lambda call: call.data.startswith('filter_'))
def apply_filter(call):
    try:
        if not ensure_subscription_callback(call.from_user.id, call.id):
            return
            
        filter_type = call.data.split('_')[1]
        filter_value = call.data.split('_')[2] if len(call.data.split('_')) > 2 else None
        
        filters = {}
        if filter_type == "type":
            filters['ticket_type'] = filter_value
        elif filter_type == "status":
            filters['status'] = filter_value
        elif filter_type == "date":
            filters['date'] = filter_value
        elif filter_type == "reset":
            filters = None
        
        show_pending_requests(call.message.chat.id, 0, filters)
    except Exception as e:
        error_logger.exception("خطأ في تطبيق التصفية")

def process_broadcast_message(message):
    try:
        if not is_admin(message.from_user.id):
            return
            
        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        if message.text == 'الغاء':
            bot.send_message(message.chat.id, "تم إلغاء الإذاعة.")
            return
        
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT DISTINCT user_id FROM tickets")
                users = cursor.fetchall()
        finally:
            postgreSQL_pool.putconn(conn)
        
        count = 0
        errors = 0
        
        futures = []
        for (uid,) in users:
            futures.append(executor.submit(send_message_safe, uid, message))
        
        for future in futures:
            if future.result():
                count += 1
            else:
                errors += 1
        
        bot.send_message(message.chat.id, f"✅ تم إرسال الرسالة إلى {count} مستخدم. ❌ فشل: {errors}", reply_markup=admin_markup())
    except Exception as e:
        error_logger.exception("خطأ في معالجة البث")

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def check_sub(call):
    try:
        user = call.from_user
        
        if is_user_subscribed(user.id):
            cumulative_prize = get_setting('cumulative_prize')
            prize_text = "🎁 الجائزة اليوم: 85% من قيمة التذاكر المباعة"
            
            welcome_text = (
                f"🎉 أهلاً بك في بوت ماستر - يانصيب سوريا الذكي، {user.first_name}!\n"
                f"اختبر حظك كل يوم واربح جوائز مميزة 💰\n\n"
                f"{prize_text}\n\n"
                f"💰 سعر التذكرة: 5000 ل.س\n"
                f"📊 شفافية تامة ونتائج واضحة للجميع\n\n"
                f"📌 يمكنك معرفة الفائزين ونتائج السحب عبر زر '🏆 الفائزين'\n\n"
                f"👇 اختر من القائمة:"
            )

            bot.send_message(call.message.chat.id, welcome_text, reply_markup=main_markup(user.id))
        else:
            bot.answer_callback_query(call.id, "لم تقم بالاشتراك بعد! اشترك ثم اضغط تأكيد الاشتراك", show_alert=True)
    except Exception as e:
        error_logger.exception("خطأ في التحقق من الاشتراك")

@bot.message_handler(commands=['تحقق'])
def check_sub_command(message):
    try:
        user = message.from_user
        
        if is_user_subscribed(user.id):
            cumulative_prize = get_setting('cumulative_prize')
            prize_text = "🎁 الجائزة اليوم: 85% من قيمة التذاكر المباعة"
            
            welcome_text = (
                f"🎉 أهلاً بك في بوت ماستر - يانصيب سوريا الذكي، {user.first_name}!\n"
                f"اختبر حظك كل يوم واربح جوائز مميزة 💰\n\n"
                f"{prize_text}\n\n"
                f"💰 سعر التذكرة: 5000 ل.س\n"
                f"📊 شفانية تامة ونتائج واضحة للجميع\n\n"
                f"📌 يمكنك معرفة الفائزين ونتائج السحب عبر زر '🏆 الفائزين'\n\n"
                f"👇 اختر من القائمة:"
            )

            bot.send_message(message.chat.id, welcome_text, reply_markup=main_markup(user.id))
        else:
            friendly_reminder = (
                f"👋 أهلاً {user.first_name}!\n\n"
                "✨ لمتابعة استخدام البوت، يرجى الاشتراك في قناتنا أولاً ✨\n\n"
                "🔔 فوائد الاشتراك في القناة:\n"
                "• ستكون أول من يعرف نتائج السحب اليومي والأسبوعي 🏆\n"
                "• ستتلقى إشعارات حصرية قبل بدء السحب بوقت كافٍ 🔔\n"
                "• ستحصل على آخر التحديثات والعروض الخاصة بالبوت 🎁\n\n"
                "بعد الاشتراك اضغط /تحقق للبدء"
            )
            bot.send_message(
                message.chat.id,
                friendly_reminder,
                parse_mode='Markdown',
                reply_markup=subscription_markup()
            )
    except Exception as e:
        error_logger.exception("خطأ في أمر التحقق")

@bot.message_handler(commands=['stats', 'mytickets', 'howto', 'faq', 'pending', 'share', 'winners', 'support'])
def handle_commands(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        command = message.text.split('@')[0].split('/')[-1]
        
        if command == 'stats':
            handle_stats(message)
        elif command == 'mytickets':
            my_tickets(message)
        elif command == 'howto':
            how_it_works(message)
        elif command == 'faq':
            faq(message)
        elif command == 'pending':
            handle_user_pending_requests(message)
        elif command == 'share':
            share_bot(message)
        elif command == 'winners':
            show_winners(message)
        elif command == 'support':
            support(message)
    except Exception as e:
        error_logger.exception("خطأ في معالجة الأوامر")

@bot.message_handler(func=lambda message: message.text == '❓ طريقة العمل')
def how_it_works(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        explanation = (
            "📚 طريقة عمل بوت اليانصيب:\n\n"
            "1️⃣ شراء تذكرة:\n"
            "   - اختر نوع التذكرة (يومي/أسبوعي)\n"
            "   - اختر عدد التذاكر (1-10)\n"
            "   - اختر طريقة الدفع (سيريتل كاش/شام كاش)\n"
            "   - أرسل إشعار الدفع للتحقق\n\n"
            "2️⃣ متابعة التذاكر:\n"
            "   - اضغط على 'تذاكري 🎟️' لمشاهدة تذاكرك\n\n"
            "3️⃣ السحب والجوائز:\n"
            "   - 🕒 السحب اليومي: كل يوم الساعة 12 ظهراً\n"
            "   - 🕒 السحب الأسبوعي: كل جمعة الساعة 12 ظهراً\n"
            "   - 🏆 الجائزة: 85% من إجمالي قيمة التذاكر\n\n"
            "📌 مثال على حساب الجائزة:\n"
            "   - إذا تم بيع 100 تذكرة يومية:\n"
            "   - 💰 إجمالي المبلغ: 100 × 5000 = 500,000 ليرة\n"
            "   - 🎁 قيمة الجائزة: 500,000 × 85% = 425,000 ليرة\n\n"
            "4️⃣ الشفانية:\n"
            "   - يمكنك مشاهدة الإحصائيات عبر زر 'الإحصائيات 📊'\n"
            "   - يمكنك مشاهدة الفائزين السابقين عبر زر 'الفائزين 🏆'\n\n"
            "✅ البوت آمن وشفاف تماماً، ونتمنى لك حظاً طيباً! 🍀"
        )
        bot.send_message(message.chat.id, explanation)
    except Exception as e:
        error_logger.exception("خطأ في طريقة العمل")

@bot.message_handler(func=lambda message: message.text == '❓ الأسئلة الشائعة')
def faq(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        faq_text = (
            "❓ الأسئلة الشائعة:\n\n"
            "1. هل عملية الدفع آمنة؟\n"
            "نعم، جميع عمليات الدفع تتم عبر أنظمة الدفع المعتمدة في سوريا.\n\n"
            "2. كيف أعرف أنني فزت؟\n"
            "سيتم إرسال إشعار فوري لك عند الفوز مع تفاصيل الجائزة.\n\n"
            "3. متى يتم السحب؟\n"
            "- السحب اليومي: 12 ظهراً\n"
            "- السحب الأسبوعي: الجمعة 12 ظهراً\n\n"
            "4. كيف يتم احتساب الجوائز؟\n"
            "الجائزة = 85% من إجمالي قيمة التذاكر المباعة\n\n"
            "للمزيد من الأسئلة، تواصل مع الدعم الفني."
        )
        bot.send_message(message.chat.id, faq_text)
    except Exception as e:
        error_logger.exception("خطأ في الأسئلة الشائعة")

@bot.message_handler(func=lambda message: message.text == '🎫 تذاكري')
def my_tickets(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        user_id = message.from_user.id
        tickets = get_user_tickets(user_id)
        
        if not tickets:
            bot.send_message(user_id, "⚠️ لم تقم بشراء أي تذاكر بعد!")
            return
        
        markup = types.InlineKeyboardMarkup(row_width=2)
        for ticket in tickets:
            markup.add(types.InlineKeyboardButton(
                f"🎫 {ticket[1]}",
                callback_data=f"ticket_detail_{ticket[0]}"
            ))
        
        bot.send_message(
            user_id,
            "🎫 تذاكرك المشتراة. اختر تذكرة لعرض تفاصيلها:",
            reply_markup=markup
        )
    except Exception as e:
        error_logger.exception("خطأ في تذاكري")

@bot.callback_query_handler(func=lambda call: call.data.startswith('ticket_detail_'))
def show_ticket_details(call):
    try:
        if not ensure_subscription_callback(call.from_user.id, call.id):
            return
            
        ticket_id = int(call.data.split('_')[2])
        
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT ticket_number, ticket_type, purchase_date, is_winner 
                    FROM tickets 
                    WHERE id = %s
                """, (ticket_id,))
                ticket = cursor.fetchone()
        finally:
            postgreSQL_pool.putconn(conn)
        
        if not ticket:
            bot.answer_callback_query(call.id, "❌ التذكرة غير موجودة")
            return
        
        try:
            dt = datetime.datetime.strptime(ticket[2], "%Y-%m-%d %H:%M:%S")
            formatted_date = dt.strftime("%Y-%m-%d %H:%M:%S")
        except:
            formatted_date = ticket[2]
        
        winner_status = "🟢 فائزة!" if ticket[3] else "🔴 لم تفز بعد"
        
        now = get_local_time()
        
        if ticket[1] == 'يومي':
            next_draw = now.replace(hour=12, minute=0, second=0, microsecond=0)
            if now >= next_draw:
                next_draw += datetime.timedelta(days=1)
            time_left = next_draw - now
            hours, remainder = divmod(time_left.seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            time_left_str = f"{hours} ساعة {minutes} دقيقة"
        else:
            next_draw = now.replace(hour=12, minute=0, second=0, microsecond=0)
            if now.weekday() < 4:
                days_until_friday = 4 - now.weekday()
            elif now.weekday() == 4:
                if now.time() < next_draw.time():
                    days_until_friday = 0
                else:
                    days_until_friday = 7
            else:
                days_until_friday = 4 + 7 - now.weekday()
            
            next_draw = next_draw + datetime.timedelta(days=days_until_friday)
            time_left = next_draw - now
            days = time_left.days
            hours, remainder = divmod(time_left.seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            time_left_str = f"{days} يوم {hours} ساعة {minutes} دقيقة"
        
        details = (
            f"🎫 تفاصيل التذكرة:\n\n"
            f"🔢 الرقم: {ticket[0]}\n"
            f"📅 تاريخ الشراء: {formatted_date}\n"
            f"📦 النوع: {ticket[1]}\n"
            f"🏆 الحالة: {winner_status}\n"
            f"⏳ الوقت المتبقي للسحب: {time_left_str}"
        )
        
        bot.send_message(call.message.chat.id, details)
        bot.answer_callback_query(call.id)
    except Exception as e:
        error_logger.exception("خطأ في تفاصيل التذكرة")

@bot.message_handler(func=lambda message: message.text.startswith('🧾 الطلبات المعلّقة'))
def handle_user_pending_requests(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        user_id = message.from_user.id
        
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT id, ticket_type, quantity, request_time, status 
                    FROM pending_requests 
                    WHERE user_id = %s
                    ORDER BY id ASC
                """, (user_id,))
                requests = cursor.fetchall()
        finally:
            postgreSQL_pool.putconn(conn)
        
        if not requests:
            bot.send_message(user_id, "🎉 لا توجد لديك طلبات معلّقة!")
            return
            
        response = "📋 طلباتك المعلّقة:\n\n"
        for req in requests:
            response += (
                f"🔹 الطلب #{req[0]}\n"
                f"   🎫 النوع: {req[1]}\n"
                f"   🔢 الكمية: {req[2]}\n"
                f"   🕒 التاريخ: {req[3]}\n"
                f"   📌 الحالة: {req[4]}\n\n"
            )
            
        bot.send_message(user_id, response)
    except Exception as e:
        error_logger.exception("خطأ في طلبات المستخدم المعلقة")

@bot.message_handler(func=lambda message: message.text == '🎯 شارك مع صديق')
def share_bot(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        user = message.from_user
        bot_username = bot.get_me().username
        ref_link = f"https://t.me/{bot_username}?start=ref_{user.id}"
        
        share_text = (
            "🔥 جرب حظك في يانصيب ماستر! \n\n"
            "🎫 اشتر تذكرة بـ 5000 ليرة واربح جائزة يومية وأسبوعية!\n"
            "💰 الجوائز تصل إلى ملايين الليرات!\n\n"
            f"👇 انضم الآن عبر الرابط:\n{ref_link}"
        )
        
        bot.send_message(
            message.chat.id,
            share_text,
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("مشاركة الرابط", url=f"tg://msg?text={share_text}")
            )
        )
    except Exception as e:
        error_logger.exception("خطأ في مشاركة البوت")

@bot.message_handler(func=lambda message: message.text == '🏆 الفائزين')
def show_winners(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        winners = get_winners()
        if not winners:
            bot.reply_to(message, "لا يوجد فائزين حتى الآن.")
            return
        
        response = "🏆 الفائزون السابقون:\n\n"
        for i, (username, ticket_number, ticket_type, win_date, prize_amount) in enumerate(winners, 1):
            try:
                dt = datetime.datetime.strptime(win_date, "%Y-%m-%d %H:%M:%S.%f")
                formatted_date = dt.strftime("%Y-%m-%d")
            except:
                formatted_date = win_date
            
            response += (
                f"{i}. 👤 {username}\n"
                f"   🎫 رقم التذكرة: {ticket_number}\n"
                f"   📅 تاريخ الفوز: {formatted_date}\n"
                f"   📦 النوع: {ticket_type}\n"
                f"   💰 الجائزة: {prize_amount:,} ليرة\n\n"
            )
        
        bot.reply_to(message, response)
    except Exception as e:
        error_logger.exception("خطأ في عرض الفائزين")

@bot.message_handler(func=lambda message: message.text == '📞 تواصل مع الدعم')
def support(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        support_text = (
        "📞 مرحبًا بك في دعم بوت ماستر!\n\n"
        "👨‍💻 فريق الدعم جاهز لمساعدتك على مدار الساعة.\n"
        "لأي استفسار أو مشكلة، يرجى التواصل مع:\n\n"
        f"👉 <a href='tg://user?id={ADMIN_ID}'>اضغط هنا للتواصل مع الدعم الفني</a>\n\n"
        "⏰ وقت الاستجابة: 24 ساعة\n"
        "✅ سنكون سعداء بخدمتك!"
        )
        bot.send_message(
            message.chat.id, 
            support_text,
            parse_mode='HTML'
        )
    except Exception as e:
        error_logger.exception("خطأ في الدعم")

@bot.message_handler(func=lambda message: message.text == '📊 الإحصائيات')
def handle_stats(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        daily_tickets, daily_users, daily_prize = get_daily_stats()
        weekly_tickets, weekly_users, weekly_prize = get_weekly_stats()
        users, monthly_users, tickets, total, prize = get_stats()
        
        response = (
            f"📊 إحصائيات البوت\n\n"
            f"📈 إحصائيات اليوم\n"
            f"🎫 التذاكر اليومية: {daily_tickets}\n"
            f"💰 جائزة اليوم: {daily_prize:,} ل.س\n\n"
            f"📅 إحصائيات الأسبوع\n"
            f"🎫 التذاكر الأسبوعية: {weekly_tickets}\n"
            f"💰 جائزة الأسبوع: {weekly_prize:,} ل.س"
        )
        bot.reply_to(message, response)
    except Exception as e:
        error_logger.exception("خطأ في الإحصائيات")

def buy_ticket_step(message):
    try:
        user_id = message.from_user.id
        
        if not ensure_subscription(user_id, message.chat.id):
            return
            
        bot.send_message(
            user_id,
            "📝 اختر نوع التذكرة:",
            reply_markup=ticket_type_markup()
        )
        bot.register_next_step_handler(message, process_ticket_type)
    except Exception as e:
        error_logger.exception("خطأ في بدء شراء التذكرة")

def process_ticket_type(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        user_id = message.from_user.id
        if message.text == 'إلغاء':
            if user_id in user_data:
                del user_data[user_id]
            bot.send_message(user_id, "تم إلغاء العملية.", reply_markup=main_markup(user_id))
            return
        
        ticket_type = message.text
        if ticket_type not in ['يومي', 'أسبوعي']:
            bot.send_message(user_id, "⚠️ نوع تذكرة غير صالح! يرجى الاختيار من القائمة.", reply_markup=ticket_type_markup())
            bot.register_next_step_handler(message, process_ticket_type)
            return
        
        now = get_local_time()
        if now.weekday() == 4 and ticket_type == 'يومي':
            bot.send_message(
                user_id,
                "⚠️ عذراً، لا يمكن شراء تذاكر يومية يوم الجمعة.\n"
                "يرجى اختيار التذكرة الأسبوعية.",
                reply_markup=ticket_type_markup()
            )
            bot.register_next_step_handler(message, process_ticket_type)
            return
        
        user_data[user_id] = {'ticket_type': ticket_type, 'timestamp': time.time()}
        bot.send_message(
            user_id,
            "🔢 أدخل عدد التذاكر التي تريد شراءها (1-10):",
            reply_markup=quantity_markup()
        )
        bot.register_next_step_handler(message, process_ticket_quantity)
    except Exception as e:
        error_logger.exception("خطأ في معالجة نوع التذكرة")

def process_ticket_quantity(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        user_id = message.from_user.id
        if message.text == 'إلغاء':
            if user_id in user_data:
                del user_data[user_id]
            bot.send_message(user_id, "تم إلغاء العملية.", reply_markup=main_markup(user_id))
            return
        
        try:
            quantity = int(message.text)
            if quantity < 1 or quantity > 10:
                raise ValueError
        except ValueError:
            bot.send_message(
                user_id,
                "⚠️ عدد غير صالح! يرجى إدخال رقم بين 1 و 10.",
                reply_markup=quantity_markup()
            )
            bot.register_next_step_handler(message, process_ticket_quantity)
            return
        
        user_data[user_id]['quantity'] = quantity
        bot.send_message(
            user_id,
            "💳 اختر طريقة الدفع:",
            reply_markup=payment_method_markup()
        )
        bot.register_next_step_handler(message, process_payment_method)
    except Exception as e:
        error_logger.exception("خطأ في معالجة كمية التذاكر")

def process_payment_method(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        user_id = message.from_user.id
        if message.text == 'إلغاء':
            if user_id in user_data:
                del user_data[user_id]
            bot.send_message(user_id, "تم إلغاء العملية.", reply_markup=main_markup(user_id))
            return
        
        payment_method = message.text
        if payment_method not in ['سيريتل كاش', 'شام كاش']:
            bot.send_message(user_id, "⚠️ طريقة دفع غير صالحة! يرجى الاختيار من القائمة.", reply_markup=payment_method_markup())
            bot.register_next_step_handler(message, process_payment_method)
            return
        
        user_data[user_id]['payment_method'] = payment_method
        user_data[user_id]['timestamp'] = time.time()
        
        remove_markup = types.ReplyKeyboardRemove()
        total_amount = user_data[user_id]['quantity'] * 5000
        bot.send_message(
            user_id, 
            f"💸 الإجمالي: {total_amount:,} ليرة سورية",
            reply_markup=remove_markup
        )
        
        if payment_method == 'سيريتل كاش':
            payment_info = PAYMENT_NUMBER
            copy_markup = types.InlineKeyboardMarkup()
            copy_markup.add(types.InlineKeyboardButton("📋 نسخ الرقم", callback_data=f"copy_{payment_info}"))
            copy_markup.add(types.InlineKeyboardButton("❌ إلغاء العملية", callback_data=f"cancel_payment_{user_id}"))
            
            instructions = (
                f"📋 لشراء التذاكر يرجى اتباع الخطوات التالية:\n\n"
                f"1️⃣ قم بتحويل {total_amount:,} ليرة إلى الرقم:\n"
                f"<code>{payment_info}</code>\n\n"
                f"2️⃣ أرسل لقطة شاشة للإيصال هنا أو رقم الإشعار\n\n"
                f"📌 ملاحظة: إذا لم يتم الرد على طلبك خلال 6 ساعات، يرجى التواصل مع الدعم الفني."
            )
            
            bot.send_message(
                user_id,
                instructions,
                reply_markup=copy_markup,
                parse_mode='HTML'
            )
        else:
            payment_info = "9937130045912810"
            copy_markup = types.InlineKeyboardMarkup()
            copy_markup.add(types.InlineKeyboardButton("📋 نسخ الحساب", callback_data=f"copy_{payment_info}"))
            copy_markup.add(types.InlineKeyboardButton("❌ إلغاء العملية", callback_data=f"cancel_payment_{user_id}"))
            
            instructions = (
                f"📋 لشراء التذاكر يرجى اتباع الخطوات التالية:\n\n"
                f"1️⃣ قم بتحويل {total_amount:,} ليرة إلى الحساب:\n"
                f"<code>{payment_info}</code>\n\n"
                f"2️⃣ أرسل لقطة شاشة للإيصال هنا\n\n"
                f"📌 ملاحظة: إذا لم يتم الرد على طلبك خلال 6 ساعات، يرجى التواصل مع الدعم الفني."
            )
            
            bot.send_message(
                user_id,
                instructions,
                reply_markup=copy_markup,
                parse_mode='HTML'
            )
        
        bot.register_next_step_handler(message, process_payment_receipt)
    except Exception as e:
        error_logger.exception("خطأ في معالجة طريقة الدفع")

@bot.callback_query_handler(func=lambda call: call.data.startswith('copy_'))
def copy_number(call):
    try:
        if not ensure_subscription_callback(call.from_user.id, call.id):
            return
            
        number = call.data.split('_', 1)[1]
        bot.answer_callback_query(call.id, f"تم نسخ الرقم: {number}", show_alert=True)
        bot.send_message(call.message.chat.id, f"الرقم: `{number}`", parse_mode='Markdown')
    except Exception as e:
        error_logger.exception("خطأ في نسخ الرقم")

@bot.callback_query_handler(func=lambda call: call.data.startswith('cancel_payment_'))
def handle_cancel_payment(call):
    try:
        if not ensure_subscription_callback(call.from_user.id, call.id):
            return
            
        user_id = int(call.data.split('_')[-1])
        if user_id in user_data:
            del user_data[user_id]
        bot.send_message(call.message.chat.id, "تم إلغاء عملية الدفع.", reply_markup=main_markup(user_id))
        bot.answer_callback_query(call.id)
    except Exception as e:
        error_logger.exception("خطأ في إلغاء الدفع")

def process_payment_receipt(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        user_id = message.from_user.id
        
        # التحقق من وجود بيانات المستخدم
        if user_id not in user_data:
            bot.send_message(user_id, "انتهت جلسة العمل. يرجى البدء من جديد.")
            return
            
        # إلغاء العملية إذا كان النص أمراً
        if message.text and message.text.startswith('/'):
            del user_data[user_id]
            bot.send_message(user_id, "تم إلغاء العملية.")
            return
        
        if message.text == 'إلغاء':
            if user_id in user_data:
                del user_data[user_id]
            bot.send_message(user_id, "تم إلغاء العملية.", reply_markup=main_markup(user_id))
            return
        
        payment_method = user_data[user_id]['payment_method']
        receipt_content = None
        
        # إصلاح التحقق من إيصالات شام كاش
        if payment_method == 'شام كاش':
            if not message.photo:
                bot.send_message(user_id, "⚠️ لشام كاش، يرجى إرسال صورة الإشعار!")
                bot.register_next_step_handler(message, process_payment_receipt)
                return
            receipt_content = message.photo[-1].file_id
        else:  # سيريتل كاش
            if message.photo:
                receipt_content = message.photo[-1].file_id
            elif message.text and message.text.isdigit() and len(message.text) >= 12:
                receipt_content = message.text
            else:
                bot.send_message(user_id, "⚠️ يرجى إرسال رقم الإشعار (12 رقم) أو صورة الإيصال")
                bot.register_next_step_handler(message, process_payment_receipt)
                return
        
        user_data[user_id]['receipt_number'] = receipt_content
        
        request_time = get_local_time().strftime("%Y-%m-%d %H:%M:%S")
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO pending_requests (user_id, ticket_type, payment_method, request_time, receipt_number, quantity) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (user_id, user_data[user_id]['ticket_type'], payment_method, request_time, user_data[user_id]['receipt_number'], user_data[user_id]['quantity'])
                )
                request_id = cursor.fetchone()[0]
                conn.commit()
        finally:
            postgreSQL_pool.putconn(conn)
        
        # إرسال إشعار الدفع مع رقم الإيصال
        send_payment_alert(request_id, user_id, user_data[user_id]['ticket_type'], payment_method, user_data[user_id]['quantity'], user_data[user_id]['receipt_number'])
        
        bot.send_message(
            user_id,
            "📬 تم إرسال إشعار الدفع للمراجعة. سيتم إعلامك بالنتيجة قريبًا.\n"
            "🚨 مراجعة الطلبات تتم خلال 15 دقيقة – 2 ساعة كحد أقصى",
            reply_markup=main_markup(user_id)
        )
        
        if user_id in user_data:
            del user_data[user_id]
    except Exception as e:
        error_logger.exception(f"Payment processing error: {e}")
        bot.send_message(user_id, "حدث خطأ أثناء معالجة طلبك. يرجى المحاولة مرة أخرى.")

@bot.message_handler(func=lambda message: message.text == '🎟️ احجز تذكرتك الآن')
def handle_buy_ticket(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        user_id = message.from_user.id
        buy_ticket_step(message)
    except Exception as e:
        error_logger.exception("خطأ في معالجة شراء التذكرة")

@bot.message_handler(func=lambda message: message.text == '👥 عدد المستخدمين' and is_admin(message.from_user.id))
def admin_users_count(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        users, monthly_users, tickets, total, prize = get_stats()
        response = f"👤 عدد المستخدمين: {users}\n🎟️ عدد التذاكر المباعة: {tickets}"
        bot.reply_to(message, response)
    except Exception as e:
        error_logger.exception("خطأ في عد المستخدمين للمشرف")

@bot.message_handler(func=lambda message: message.text == '📢 إرسال جماعي' and is_admin(message.from_user.id))
def ask_broadcast_message(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(types.KeyboardButton('الغاء'))
        
        sent = bot.reply_to(
            message, 
            "📤 أرسل الرسالة التي تريد إذاعتها (أو اضغط 'الغاء' للرجوع):",
            reply_markup=markup
        )
        bot.register_next_step_handler(sent, process_broadcast_message)
    except Exception as e:
        error_logger.exception("خطأ في طلب رسالة البث")

@bot.message_handler(func=lambda message: message.text == '📋 الطلبات المعلقة' and is_admin(message.from_user.id))
def admin_pending_requests(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        show_pending_requests(message.chat.id)
    except Exception as e:
        error_logger.exception("خطأ في طلبات المشرف المعلقة")

@bot.message_handler(func=lambda message: message.text == '🏠 القائمة الرئيسية' and is_admin(message.from_user.id))
def back_to_main_admin(message):
    try:
        if not check_rate_limit(message.from_user.id):
            return

        if not ensure_subscription(message.from_user.id, message.chat.id):
            return
            
        bot.send_message(message.chat.id, "🏠 العودة للقائمة الرئيسية", reply_markup=main_markup(message.from_user.id))
    except Exception as e:
        error_logger.exception("خطأ في العودة للقائمة الرئيسية")

@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_approve_'))
def admin_approve_request(call):
    try:
        if not ensure_subscription_callback(call.from_user.id, call.id):
            return
            
        request_id = int(call.data.split('_')[2])
        
        try:
            conn = get_db_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT * FROM pending_requests WHERE id = %s", (request_id,))
                    req = cursor.fetchone()
                    if not req:
                        bot.answer_callback_query(call.id, "❌ الطلب غير موجود")
                        return

                    user_id = req[1]
                    ticket_type = req[2]
                    receipt_number = req[5]
                    quantity = req[7]
                    
                    try:
                        user_info = bot.get_chat(user_id)
                        username = user_info.username or user_info.first_name or ""
                    except Exception:
                        username = ""
                    
                    purchase_date = get_local_time().strftime("%Y-%m-%d %H:%M:%S")
                    
                    ticket_numbers = generate_ticket_numbers(ticket_type, quantity)
                    
                    tickets_data = [(user_id, username, num, purchase_date, ticket_type, 0, receipt_number) 
                                   for num in ticket_numbers]
                    
                    cursor.executemany(
                        "INSERT INTO tickets (user_id, username, ticket_number, purchase_date, ticket_type, is_winner, receipt_number) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        tickets_data
                    )
                    
                    cursor.execute("DELETE FROM pending_requests WHERE id = %s", (request_id,))
                    
                    cursor.execute(
                        "INSERT INTO audit_log (admin_id, action, target_id, details, timestamp) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (call.from_user.id, "approve", user_id, f"طلب #{request_id}", purchase_date)
                    )
                    
                    conn.commit()
            finally:
                postgreSQL_pool.putconn(conn)

            try:
                tickets_info = "\n".join([f"🎫 التذكرة #{i+1}: {num}" for i, num in enumerate(ticket_numbers)])
                
                bot.send_message(
                    user_id,
                    f"🎉 تمت الموافقة على طلبك بنجاح!\n\n"
                    f"✅ تم حجز {quantity} تذكرة للسحب {ticket_type} القادم:\n"
                    f"{tickets_info}\n\n"
                    f"📅 تاريخ الشراء: {purchase_date}",
                    reply_markup=main_markup(user_id)
                )
            except Exception as e:
                error_logger.error(f"خطأ أثناء إرسال رسالة الموافقة: {e}")
                
            bot.answer_callback_query(call.id, f"✅ تم قبول الطلب #{request_id}")
            bot.send_message(
                call.message.chat.id,
                f"✅ تم قبول الطلب #{request_id} بنجاح!\n"
                f"🎫 عدد التذاكر: {quantity}",
                reply_markup=admin_markup()
            )

        except Exception as e:
            error_msg = f"❌ حدث خطأ أثناء معالجة الطلب: {str(e)}"
            error_logger.exception("خطأ في قبول الطلب")
            bot.answer_callback_query(call.id, error_msg)
            bot.send_message(
                call.message.chat.id,
                error_msg,
                reply_markup=admin_markup()
            )
        
        try:
            show_pending_requests(call.message.chat.id)
        except Exception as e:
            error_logger.error(f"خطأ في تحديث قائمة الطلبات: {e}")
    except Exception as e:
        error_logger.exception("خطأ في معالجة قبول الطلب")

app = Flask(__name__)

@app.route('/' + TOKEN, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ''
    return 'Bad request', 400

@app.route('/')
def index():
    return 'Bot is running!', 200

if __name__ == '__main__':
    PORT = int(os.environ.get('PORT', 10000))
    bot.remove_webhook()
    bot.set_webhook(url='https://master-gfh3.onrender.com/' + TOKEN)
    app.run(host='0.0.0.0', port=PORT)
