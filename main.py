import os
import json
import sqlite3
import time
import hashlib
import threading
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters

# ===================== الإعدادات =====================
BOT_TOKEN = '8513010794:AAG4J4n6dZd7MFmEntIP4oTNqc_6y6vWwrs'
MASTER_ADMIN_ID = '8311254462'  # هذا المعرف لا يمكن حذفه أبداً
app = Flask(__name__)
CORS(app)

# ===================== قاعدة البيانات =====================
conn = sqlite3.connect('tomb_bot.db', check_same_thread=False)
c = conn.cursor()

# جدول الطلبات (للموافقات اليدوية)
c.execute('''CREATE TABLE IF NOT EXISTS approvals
             (request_id TEXT PRIMARY KEY, 
              status TEXT, 
              timestamp INTEGER,
              username TEXT,
              device_name TEXT,
              device_info TEXT,
              ip_address TEXT)''')

# جدول الإعدادات
c.execute('''CREATE TABLE IF NOT EXISTS settings
             (key TEXT PRIMARY KEY, 
              value TEXT)''')

# جدول كلمات المرور (الرئيسية)
c.execute('''CREATE TABLE IF NOT EXISTS passwords
             (id INTEGER PRIMARY KEY AUTOINCREMENT,
              password_hash TEXT,
              updated_at INTEGER,
              updated_by TEXT)''')

# جدول سجل الدخول
c.execute('''CREATE TABLE IF NOT EXISTS access_logs
             (id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT,
              device_name TEXT,
              ip_address TEXT,
              status TEXT,
              timestamp INTEGER)''')

# جدول الأجهزة المحظورة
c.execute('''CREATE TABLE IF NOT EXISTS banned_devices
             (device_name TEXT PRIMARY KEY,
              username TEXT,
              banned_at INTEGER,
              banned_until INTEGER,
              ban_type TEXT,
              reason TEXT)''')

# جدول الأجهزة النشطة
c.execute('''CREATE TABLE IF NOT EXISTS active_devices
             (device_name TEXT PRIMARY KEY,
              username TEXT,
              device_info TEXT,
              last_active INTEGER,
              first_active INTEGER,
              total_requests INTEGER DEFAULT 1)''')

# جدول كلمات المرور المؤقتة
c.execute('''CREATE TABLE IF NOT EXISTS temp_passwords
             (id INTEGER PRIMARY KEY AUTOINCREMENT,
              password_hash TEXT,
              device_name TEXT,
              created_at INTEGER,
              expires_at INTEGER,
              used INTEGER DEFAULT 0)''')

# جدول الجلسات النشطة (للكلمات المؤقتة فقط)
c.execute('''CREATE TABLE IF NOT EXISTS active_sessions
             (device_name TEXT PRIMARY KEY,
              username TEXT,
              session_expires INTEGER,
              session_type TEXT,
              created_at INTEGER)''')

# جدول الأجهزة المعتمدة (للدخول الدائم)
c.execute('''CREATE TABLE IF NOT EXISTS approved_devices
             (device_name TEXT PRIMARY KEY,
              username TEXT,
              approved_at INTEGER,
              last_login INTEGER,
              approved_by TEXT)''')

# جدول الإشعارات (بدون عنوان)
c.execute('''CREATE TABLE IF NOT EXISTS notifications
             (id INTEGER PRIMARY KEY AUTOINCREMENT,
              device_name TEXT,
              message TEXT,
              created_at INTEGER,
              is_read INTEGER DEFAULT 0)''')

# جدول المستخدمين المصرح لهم (جديد)
c.execute('''CREATE TABLE IF NOT EXISTS authorized_users
             (user_id TEXT PRIMARY KEY,
              username TEXT,
              added_by TEXT,
              added_at INTEGER,
              can_remove INTEGER DEFAULT 1)''')

# إضافة المستخدم الأساسي (لا يمكن حذفه)
c.execute("INSERT OR IGNORE INTO authorized_users (user_id, username, added_by, added_at, can_remove) VALUES (?, ?, ?, ?, ?)",
          (MASTER_ADMIN_ID, "MASTER", "system", int(time.time()), 0))

conn.commit()

# ===================== دوال المستخدمين المصرح لهم =====================
def is_authorized(chat_id):
    """التحقق مما إذا كان المستخدم مصرح له باستخدام البوت"""
    c.execute("SELECT user_id FROM authorized_users WHERE user_id = ?", (str(chat_id),))
    return c.fetchone() is not None

def add_authorized_user(user_id, username, added_by):
    """إضافة مستخدم جديد مصرح له"""
    try:
        c.execute("INSERT INTO authorized_users (user_id, username, added_by, added_at, can_remove) VALUES (?, ?, ?, ?, 1)",
                  (str(user_id), username, str(added_by), int(time.time())))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def remove_authorized_user(user_id):
    """حذف مستخدم مصرح له (لا يمكن حذف الماستر)"""
    if str(user_id) == MASTER_ADMIN_ID:
        return False
    c.execute("DELETE FROM authorized_users WHERE user_id = ?", (str(user_id),))
    conn.commit()
    return True

def get_authorized_users():
    """الحصول على قائمة المستخدمين المصرح لهم"""
    c.execute("SELECT user_id, username, added_by, added_at, can_remove FROM authorized_users ORDER BY added_at ASC")
    return c.fetchall()

def get_authorized_users_count():
    c.execute("SELECT COUNT(*) FROM authorized_users")
    return c.fetchone()[0]

# ===================== دوال الإعدادات =====================
def get_setting(key, default=""):
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    if row:
        return row[0]
    return default

def set_setting(key, value):
    c.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
    conn.commit()

def get_app_password():
    c.execute("SELECT password_hash FROM passwords ORDER BY updated_at DESC LIMIT 1")
    row = c.fetchone()
    if row:
        return row[0]
    default_hash = hashlib.sha256("123456".encode()).hexdigest()
    c.execute("INSERT INTO passwords (password_hash, updated_at, updated_by) VALUES (?, ?, ?)", 
              (default_hash, int(time.time()), "system"))
    conn.commit()
    return default_hash

def check_password(password):
    current_hash = get_app_password()
    input_hash = hashlib.sha256(password.encode()).hexdigest()
    return input_hash == current_hash

def update_password(new_password, updated_by="bot"):
    new_hash = hashlib.sha256(new_password.encode()).hexdigest()
    c.execute("INSERT INTO passwords (password_hash, updated_at, updated_by) VALUES (?, ?, ?)", 
              (new_hash, int(time.time()), updated_by))
    conn.commit()
    return True

def log_access(username, device_name, ip_address, status):
    now = int(time.time())
    c.execute("""INSERT INTO access_logs 
                 (username, device_name, ip_address, status, timestamp) 
                 VALUES (?, ?, ?, ?, ?)""",
              (username, device_name, ip_address, status, now))
    
    c.execute("SELECT device_name FROM approvals WHERE device_name = ?", (device_name,))
    if c.fetchone():
        c.execute("UPDATE approvals SET username = ? WHERE device_name = ?", (username, device_name))
    else:
        c.execute("INSERT INTO approvals (request_id, status, timestamp, username, device_name, device_info, ip_address) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (f"log_{device_name}_{now}", "logged", now, username, device_name, "System", ip_address))
    
    conn.commit()

def add_active_device(device_name, username, device_info):
    now = int(time.time())
    c.execute("""INSERT OR REPLACE INTO active_devices 
                 (device_name, username, device_info, last_active, first_active, total_requests) 
                 VALUES (?, ?, ?, ?, 
                         COALESCE((SELECT first_active FROM active_devices WHERE device_name = ?), ?),
                         COALESCE((SELECT total_requests FROM active_devices WHERE device_name = ?), 0) + 1)""",
              (device_name, username, device_info, now, device_name, now, device_name))
    conn.commit()

def remove_active_device(device_name):
    c.execute("DELETE FROM active_devices WHERE device_name = ?", (device_name,))
    conn.commit()

def get_active_devices():
    c.execute("SELECT device_name, username, device_info, last_active, first_active, total_requests FROM active_devices ORDER BY last_active DESC")
    return c.fetchall()

def get_all_known_devices():
    devices = {}
    c.execute("SELECT DISTINCT device_name, username FROM approvals")
    for row in c.fetchall():
        device_name, username = row
        if device_name not in devices:
            devices[device_name] = username
    c.execute("SELECT device_name, username FROM active_devices")
    for row in c.fetchall():
        device_name, username = row
        if device_name not in devices:
            devices[device_name] = username
    c.execute("SELECT device_name, username FROM approved_devices")
    for row in c.fetchall():
        device_name, username = row
        if device_name not in devices:
            devices[device_name] = username
    return devices

def ban_device(device_name, username, ban_type="permanent", duration=0, reason="محظور من قبل المطور"):
    now = int(time.time())
    if ban_type == "permanent":
        banned_until = 0
    elif ban_type == "minutes":
        banned_until = now + (duration * 60)
    elif ban_type == "hours":
        banned_until = now + (duration * 3600)
    elif ban_type == "days":
        banned_until = now + (duration * 86400)
    else:
        banned_until = 0
    
    c.execute("""INSERT OR REPLACE INTO banned_devices 
                 (device_name, username, banned_at, banned_until, ban_type, reason) 
                 VALUES (?, ?, ?, ?, ?, ?)""",
              (device_name, username, now, banned_until, ban_type, reason))
    conn.commit()
    remove_active_device(device_name)
    c.execute("DELETE FROM active_sessions WHERE device_name = ?", (device_name,))
    c.execute("DELETE FROM approved_devices WHERE device_name = ?", (device_name,))
    conn.commit()

def unban_device(device_name):
    c.execute("DELETE FROM banned_devices WHERE device_name = ?", (device_name,))
    conn.commit()

def is_device_banned(device_name):
    c.execute("SELECT banned_until, ban_type FROM banned_devices WHERE device_name = ?", (device_name,))
    row = c.fetchone()
    if row:
        banned_until, ban_type = row
        if ban_type != "permanent" and banned_until < int(time.time()):
            unban_device(device_name)
            return False
        return True
    return False

def get_banned_devices():
    c.execute("SELECT device_name, username, banned_at, banned_until, ban_type, reason FROM banned_devices ORDER BY banned_at DESC")
    return c.fetchall()

def get_device_ban_info(device_name):
    c.execute("SELECT banned_until, ban_type, reason FROM banned_devices WHERE device_name = ?", (device_name,))
    row = c.fetchone()
    if row:
        banned_until, ban_type, reason = row
        remaining = 0
        remaining_text = ""
        if ban_type != "permanent" and banned_until > int(time.time()):
            remaining_seconds = banned_until - int(time.time())
            if ban_type == "minutes":
                remaining = remaining_seconds // 60
                remaining_text = f"{remaining} دقيقة"
            elif ban_type == "hours":
                remaining = remaining_seconds // 3600
                remaining_text = f"{remaining} ساعة"
            elif ban_type == "days":
                remaining = remaining_seconds // 86400
                remaining_text = f"{remaining} يوم"
        return {
            "is_banned": True,
            "ban_type": ban_type,
            "reason": reason,
            "remaining": remaining,
            "remaining_text": remaining_text,
            "expires_at": banned_until
        }
    return {"is_banned": False}

def get_access_stats():
    total = c.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
    pending = c.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
    approved = c.execute("SELECT COUNT(*) FROM approvals WHERE status='approved'").fetchone()[0]
    denied = c.execute("SELECT COUNT(*) FROM approvals WHERE status='denied'").fetchone()[0]
    banned = c.execute("SELECT COUNT(*) FROM banned_devices").fetchone()[0]
    active = c.execute("SELECT COUNT(*) FROM active_devices").fetchone()[0]
    approved_devices = c.execute("SELECT COUNT(*) FROM approved_devices").fetchone()[0]
    authorized_users = get_authorized_users_count()
    
    recent = c.execute("""SELECT username, device_name, status, timestamp 
                          FROM approvals ORDER BY timestamp DESC LIMIT 10""").fetchall()
    
    return {
        "total": total,
        "pending": pending,
        "approved": approved,
        "denied": denied,
        "banned": banned,
        "active": active,
        "approved_devices": approved_devices,
        "authorized_users": authorized_users,
        "recent": recent
    }

def is_device_approved(device_name):
    c.execute("SELECT device_name FROM approved_devices WHERE device_name = ?", (device_name,))
    row = c.fetchone()
    return row is not None

def approve_device(device_name, username, approved_by="bot"):
    now = int(time.time())
    c.execute("""INSERT OR REPLACE INTO approved_devices 
                 (device_name, username, approved_at, last_login, approved_by) 
                 VALUES (?, ?, ?, ?, ?)""",
              (device_name, username, now, now, approved_by))
    conn.commit()

def update_device_last_login(device_name):
    """تحديث آخر تسجيل دخول للجهاز"""
    c.execute("UPDATE approved_devices SET last_login = ? WHERE device_name = ?", (int(time.time()), device_name))
    conn.commit()

def cleanup_expired_temp_sessions():
    now = int(time.time())
    c.execute("DELETE FROM active_sessions WHERE session_expires < ? AND session_type = 'temp'", (now,))
    conn.commit()
    c.execute("DELETE FROM temp_passwords WHERE expires_at < ?", (now,))
    conn.commit()

# ===================== إعدادات البوت =====================
bot = telegram.Bot(token=BOT_TOKEN)
pending_requests = {}

CUSTOM_LOGO = get_setting("custom_logo", "𓆩♛✦𓆪 TOMB OF MAKROTEC 𓆩♛✦𓆪")
WELCOME_MESSAGE = get_setting("welcome_message", "🔐 طلب فتح التطبيق")
WELCOME_TEMPLATE = get_setting("welcome_message_template", "مرحباً بك يا & في تطبيق tomb of Makrotik")

def send_main_menu(chat_id):
    logo = get_setting("custom_logo", CUSTOM_LOGO)
    welcome = get_setting("welcome_message", WELCOME_MESSAGE)
    
    keyboard = [
        [
            InlineKeyboardButton("📋 الطلبات المعلقة", callback_data="menu_pending"),
            InlineKeyboardButton("📊 الإحصائيات", callback_data="menu_stats")
        ],
        [
            InlineKeyboardButton("✅ الطلبات المقبولة", callback_data="menu_approved"),
            InlineKeyboardButton("❌ الطلبات المرفوضة", callback_data="menu_denied")
        ],
        [
            InlineKeyboardButton("📜 سجل الطلبات", callback_data="menu_logs"),
            InlineKeyboardButton("⚙️ الإعدادات", callback_data="menu_settings")
        ],
        [
            InlineKeyboardButton("📱 الأجهزة النشطة", callback_data="menu_active_devices"),
            InlineKeyboardButton("🚫 الأجهزة المحظورة", callback_data="menu_banned_devices")
        ],
        [
            InlineKeyboardButton("🔒 حظر جهاز", callback_data="menu_ban_device_list"),
            InlineKeyboardButton("🔓 رفع حظر", callback_data="menu_unban_device_list")
        ],
        [
            InlineKeyboardButton("🔑 كلمة مرور مؤقتة", callback_data="menu_temp_password"),
            InlineKeyboardButton("🚪 تسجيل خروج جهاز", callback_data="menu_force_logout")
        ],
        [
            InlineKeyboardButton("🗑️ مسح الطلبات", callback_data="menu_clear_requests_options"),
            InlineKeyboardButton("👥 إدارة المستخدمين", callback_data="menu_users_management")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        bot.send_message(
            chat_id=chat_id,
            text=f"{logo}\n\n{welcome}\n\n📌 **لوحة التحكم الرئيسية**\n\nاختر أحد الخيارات أدناه:",
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    except:
        bot.send_message(
            chat_id=chat_id,
            text=f"{logo}\n\n{welcome}\n\n📌 لوحة التحكم الرئيسية\n\nاختر أحد الخيارات أدناه:",
            reply_markup=reply_markup
        )

def show_users_management_menu(chat_id, message_id=None):
    """قائمة إدارة المستخدمين"""
    users = get_authorized_users()
    master_id = MASTER_ADMIN_ID
    
    text = "👥 إدارة المستخدمين المصرح لهم\n\n"
    text += f"👑 المالك الأساسي: {master_id} (لا يمكن حذفه)\n\n"
    text += "📋 قائمة المستخدمين المصرح لهم:\n"
    
    if users:
        for user in users:
            user_id, username, added_by, added_at, can_remove = user
            added_time = datetime.fromtimestamp(added_at).strftime('%Y-%m-%d %H:%M')
            if user_id == master_id:
                text += f"👑 {user_id} - {username} - مالك\n"
            else:
                text += f"👤 {user_id} - {username} - أضيف بواسطة: {added_by} - {added_time}\n"
    else:
        text += "لا يوجد مستخدمين غير المالك\n"
    
    keyboard = [
        [InlineKeyboardButton("➕ إضافة مستخدم جديد", callback_data="add_new_user")],
        [InlineKeyboardButton("❌ حذف مستخدم", callback_data="remove_user_list")],
        [InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]
    ]
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

def show_remove_user_list(chat_id, message_id=None):
    """عرض قائمة المستخدمين للحذف"""
    users = get_authorized_users()
    master_id = MASTER_ADMIN_ID
    
    # فلترة المستخدمين لإزالة الماستر من قائمة الحذف
    removable_users = [u for u in users if u[0] != master_id]
    
    if not removable_users:
        text = "📭 لا يوجد مستخدمين قابلين للحذف"
        keyboard = [[InlineKeyboardButton("🔙 العودة", callback_data="menu_users_management")]]
        if message_id:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return
    
    keyboard = []
    for user in removable_users:
        user_id, username, added_by, added_at, can_remove = user
        keyboard.append([InlineKeyboardButton(f"❌ {user_id} - {username}", callback_data=f"remove_user_{user_id}")])
    
    keyboard.append([InlineKeyboardButton("🔙 العودة", callback_data="menu_users_management")])
    text = "❌ اختر المستخدم لحذفه:\n\n⚠️ ملاحظة: لا يمكن حذف المالك الأساسي"
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

def send_approval_request(request_id, app_name="Tomb", username="Unknown", 
                          device_name="Unknown", device_info="", ip_address="Unknown"):
    
    custom_logo = get_setting("custom_logo", CUSTOM_LOGO)
    welcome_msg = get_setting("welcome_message", WELCOME_MESSAGE)
    
    add_active_device(device_name, username, device_info)
    
    message_text = f"""
{custom_logo}

🔐 {welcome_msg}

👤 المستخدم: {username}
📱 الجهاز: {device_name}
ℹ️ المعلومات: {device_info}
🌐 IP: {ip_address}
🕐 الوقت: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

⚠️ هل تسمح بالدخول؟
"""
    
    keyboard = [
        [
            InlineKeyboardButton("✅ موافقة", callback_data=f"approve_{request_id}"),
            InlineKeyboardButton("❌ رفض", callback_data=f"deny_{request_id}")
        ],
        [
            InlineKeyboardButton("📊 معلومات الجهاز", callback_data=f"info_{request_id}"),
            InlineKeyboardButton("🔒 حظر هذا الجهاز", callback_data=f"ban_this_{request_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # إرسال الطلب لجميع المستخدمين المصرح لهم
    users = get_authorized_users()
    for user in users:
        user_id = user[0]
        try:
            bot.send_message(
                chat_id=user_id,
                text=message_text,
                reply_markup=reply_markup
            )
        except Exception as e:
            print(f"Error sending to {user_id}: {e}")
    
    pending_requests[request_id] = {
        "status": "pending", 
        "timestamp": time.time(),
        "username": username,
        "device_name": device_name
    }
    
    c.execute("""INSERT OR REPLACE INTO approvals 
                 (request_id, status, timestamp, username, device_name, device_info, ip_address) 
                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
              (request_id, "pending", int(time.time()), username, device_name, device_info, ip_address))
    conn.commit()

def show_pending_requests_with_buttons(chat_id, message_id=None):
    """عرض الطلبات المعلقة مع أزرار الموافقة والرفض لكل طلب"""
    pending_reqs = c.execute(
        "SELECT request_id, username, device_name, timestamp FROM approvals WHERE status='pending' ORDER BY timestamp DESC"
    ).fetchall()
    
    if pending_reqs:
        keyboard = []
        for req in pending_reqs:
            req_id, username, device_name, timestamp = req
            time_str = datetime.fromtimestamp(timestamp).strftime('%H:%M:%S')
            keyboard.append([
                InlineKeyboardButton(f"✅ {username} - {device_name[:15]}", callback_data=f"approve_{req_id}"),
                InlineKeyboardButton(f"❌ رفض", callback_data=f"deny_{req_id}")
            ])
        keyboard.append([InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")])
        
        text_msg = "⏳ الطلبات المعلقة:\n\nاختر الطلب للموافقة أو الرفض:"
        
        if message_id:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text_msg,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            bot.send_message(
                chat_id=chat_id,
                text=text_msg,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    else:
        keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
        if message_id:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="✅ لا توجد طلبات معلقة",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            bot.send_message(
                chat_id=chat_id,
                text="✅ لا توجد طلبات معلقة",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

def show_device_list_for_ban(chat_id, message_id=None):
    devices = get_all_known_devices()
    
    if not devices:
        text = "📭 لا توجد أجهزة معروفة للحظر"
        keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
        if message_id:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return
    
    keyboard = []
    for device_name, username in list(devices.items())[:20]:
        if not is_device_banned(device_name):
            keyboard.append([InlineKeyboardButton(f"📱 {device_name} (👤 {username})", callback_data=f"select_ban_{device_name}")])
    
    if not keyboard:
        text = "✅ جميع الأجهزة محظورة بالفعل"
        keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
        if message_id:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return
    
    keyboard.append([InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")])
    text = "🔒 اختر الجهاز المراد حظره:\n\n"
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

def show_device_list_for_unban(chat_id, message_id=None):
    banned_devices = get_banned_devices()
    
    if not banned_devices:
        text = "✅ لا توجد أجهزة محظورة"
        keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
        if message_id:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return
    
    keyboard = []
    for device_name, username, banned_at, banned_until, ban_type, reason in banned_devices:
        if ban_type == "permanent":
            ban_info = "🔒 دائم"
        elif ban_type == "minutes":
            remaining = (banned_until - int(time.time())) // 60
            ban_info = f"⏰ {remaining} دقيقة"
        elif ban_type == "hours":
            remaining = (banned_until - int(time.time())) // 3600
            ban_info = f"⏰ {remaining} ساعة"
        elif ban_type == "days":
            remaining = (banned_until - int(time.time())) // 86400
            ban_info = f"⏰ {remaining} يوم"
        else:
            ban_info = "🔒 محظور"
        keyboard.append([InlineKeyboardButton(f"📱 {device_name} (👤 {username}) - {ban_info}", callback_data=f"select_unban_{device_name}")])
    
    keyboard.append([InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")])
    text = "🔓 اختر الجهاز لرفع الحظر عنه:\n\n"
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

def show_device_list_for_logout(chat_id, message_id=None):
    devices = get_active_devices()
    
    if not devices:
        text = "📭 لا توجد أجهزة نشطة"
        keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
        if message_id:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return
    
    keyboard = []
    for device_name, username, device_info, last_active, first_active, total_requests in devices:
        last_active_str = datetime.fromtimestamp(last_active).strftime('%H:%M')
        keyboard.append([InlineKeyboardButton(f"📱 {device_name} (👤 {username}) - {last_active_str}", callback_data=f"force_logout_{device_name}")])
    
    keyboard.append([InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")])
    text = "🚪 اختر الجهاز لتسجيل خروجه:\n\n"
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

def show_ban_type_menu(chat_id, device_name, username, message_id=None):
    keyboard = [
        [InlineKeyboardButton("⏰ حظر بالدقائق", callback_data=f"ban_type_minutes_{device_name}")],
        [InlineKeyboardButton("🕐 حظر بالساعات", callback_data=f"ban_type_hours_{device_name}")],
        [InlineKeyboardButton("📅 حظر بالأيام", callback_data=f"ban_type_days_{device_name}")],
        [InlineKeyboardButton("🔒 حظر دائم", callback_data=f"ban_confirm_permanent_{device_name}")],
        [InlineKeyboardButton("🔙 إلغاء", callback_data="menu_ban_device_list")]
    ]
    
    text = f"🔒 حظر جهاز: {device_name}\n👤 المستخدم: {username}\n\nاختر نوع الحظر:"
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

def show_clear_requests_menu(chat_id, message_id=None):
    keyboard = [
        [InlineKeyboardButton("✅ مسح الطلبات المقبولة", callback_data="clear_approved")],
        [InlineKeyboardButton("❌ مسح الطلبات المرفوضة", callback_data="clear_denied")],
        [InlineKeyboardButton("⏳ مسح الطلبات المعلقة", callback_data="clear_pending")],
        [InlineKeyboardButton("🗑️ مسح الكل", callback_data="clear_all_requests")],
        [InlineKeyboardButton("🔙 العودة", callback_data="back_to_main")]
    ]
    
    text = "🗑️ مسح الطلبات\n\nاختر نوع الطلبات التي تريد مسحها:"
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

def show_temp_password_menu(chat_id, message_id=None):
    """القائمة الرئيسية لكلمة المرور المؤقتة مع خيارين"""
    keyboard = [
        [InlineKeyboardButton("📱 جهاز محدد", callback_data="temp_specific_device")],
        [InlineKeyboardButton("👥 جميع الأجهزة", callback_data="temp_all_devices")],
        [InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]
    ]
    
    text = "🔑 كلمات المرور المؤقتة\n\nاختر نوع كلمة المرور:\n\n• جهاز محدد: كلمة مرور لجهاز واحد فقط\n• جميع الأجهزة: كلمة مرور واحدة صالحة لجميع الأجهزة النشطة\n\n⚠️ جميع كلمات المرور مؤقتة وتنتهي بعد المدة التي تحددها."
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

def show_temp_password_duration_menu(chat_id, device_name, username, message_id=None, is_all_devices=False):
    """قائمة اختيار المدة لكلمة المرور المؤقتة"""
    callback_prefix = "temp_all_duration" if is_all_devices else "temp_duration"
    keyboard = [
        [InlineKeyboardButton("⏰ دقائق", callback_data=f"{callback_prefix}_minutes_{device_name}")],
        [InlineKeyboardButton("🕐 ساعات", callback_data=f"{callback_prefix}_hours_{device_name}")],
        [InlineKeyboardButton("📅 أيام", callback_data=f"{callback_prefix}_days_{device_name}")],
        [InlineKeyboardButton("🔙 إلغاء", callback_data="menu_temp_password")]
    ]
    
    if is_all_devices:
        text = f"🔑 كلمة مرور مؤقتة لجميع الأجهزة\n\nاختر وحدة المدة:"
    else:
        text = f"🔑 كلمة مرور مؤقتة لجهاز: {device_name}\n👤 المستخدم: {username}\n\nاختر وحدة المدة:"
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

def create_temp_password_for_device(device_name, duration, unit, is_all_devices=False):
    """إنشاء كلمة مرور مؤقتة لجهاز أو لجميع الأجهزة"""
    now = int(time.time())
    
    if unit == "minutes":
        expires_at = now + (duration * 60)
        time_text = f"{duration} دقيقة"
    elif unit == "hours":
        expires_at = now + (duration * 3600)
        time_text = f"{duration} ساعة"
    else:
        expires_at = now + (duration * 86400)
        time_text = f"{duration} يوم"
    
    # إنشاء كلمة مرور مؤقتة
    temp_password = hashlib.md5(f"{device_name}{time.time()}{is_all_devices}".encode()).hexdigest()[:8]
    temp_hash = hashlib.sha256(temp_password.encode()).hexdigest()
    
    if is_all_devices:
        # لجميع الأجهزة: نستخدم "all" كاسم الجهاز
        c.execute("""INSERT INTO temp_passwords (password_hash, device_name, created_at, expires_at, used) 
                     VALUES (?, ?, ?, ?, 0)""",
                  (temp_hash, "all", now, expires_at))
        conn.commit()
        
        # إنشاء جلسة مؤقتة لجميع الأجهزة النشطة
        active_devices = get_active_devices()
        for device in active_devices:
            dev_name = device[0]
            c.execute("""INSERT OR REPLACE INTO active_sessions 
                         (device_name, username, session_expires, session_type, created_at) 
                         VALUES (?, ?, ?, ?, ?)""",
                      (dev_name, "TEMP_USER", expires_at, "temp", now))
        conn.commit()
    else:
        # لجهاز محدد
        c.execute("""INSERT INTO temp_passwords (password_hash, device_name, created_at, expires_at, used) 
                     VALUES (?, ?, ?, ?, 0)""",
                  (temp_hash, device_name, now, expires_at))
        conn.commit()
        
        # إنشاء جلسة مؤقتة للجهاز المحدد
        c.execute("""INSERT OR REPLACE INTO active_sessions 
                     (device_name, username, session_expires, session_type, created_at) 
                     VALUES (?, ?, ?, ?, ?)""",
                  (device_name, "TEMP_USER", expires_at, "temp", now))
        conn.commit()
    
    return temp_password, time_text

def show_send_notification_menu(chat_id, message_id=None):
    """قائمة إرسال الإشعارات (بدون عنوان)"""
    keyboard = [
        [InlineKeyboardButton("📱 جهاز محدد", callback_data="notify_single_device")],
        [InlineKeyboardButton("👥 للجميع", callback_data="notify_all_devices")],
        [InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="menu_settings")]
    ]
    
    text = "📢 إرسال إشعارات\n\nيمكنك إرسال إشعار لجهاز محدد أو لجميع الأجهزة النشطة."
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

def show_devices_for_notification(chat_id, message_id=None):
    devices = get_active_devices()
    
    if not devices:
        text = "📭 لا توجد أجهزة نشطة"
        keyboard = [[InlineKeyboardButton("🔙 العودة", callback_data="menu_send_notification")]]
        if message_id:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return
    
    keyboard = []
    for device_name, username, device_info, last_active, first_active, total_requests in devices:
        last_active_str = datetime.fromtimestamp(last_active).strftime('%H:%M')
        keyboard.append([InlineKeyboardButton(f"📱 {device_name} (👤 {username})", callback_data=f"notify_device_{device_name}")])
    
    keyboard.append([InlineKeyboardButton("🔙 العودة", callback_data="menu_send_notification")])
    text = "📢 اختر الجهاز لإرسال إشعار له:\n\n"
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

def show_welcome_template_menu(chat_id, message_id=None):
    current_template = get_setting("welcome_message_template", "مرحباً بك يا & في تطبيق tomb of Makrotik")
    
    text = f"""💬 تغيير رسالة الترحيب

📝 الرسالة الحالية:
{current_template}

💡 ملاحظة: استخدم الرمز & لوضع اسم المستخدم تلقائياً.

أمثلة:
• مرحباً بك يا & في تطبيق TOMB
• حبيبي يا & في tomb of Makrotik

📤 أرسل رسالة الترحيب الجديدة الآن:"""
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text
        )

# متغير سياق مؤقت
context_holder = {'user_data': {}}

def handle_callback(update, context):
    query = update.callback_query
    query.answer()
    
    data = query.data
    chat_id = query.message.chat_id
    message_id = query.message.message_id
    
    # التحقق من الصلاحية
    if not is_authorized(chat_id):
        query.edit_message_text("⚠️ أنت غير مصرح لك باستخدام هذا البوت")
        return
    
    # ========== إدارة المستخدمين ==========
    if data == "menu_users_management":
        show_users_management_menu(chat_id, message_id)
        return
    
    if data == "add_new_user":
        query.edit_message_text(
            "➕ إضافة مستخدم جديد\n\nأرسل معرف المستخدم (ID) الخاص بالشخص الذي تريد إضافته:\n\nمثال: 123456789\n\n⚠️ يمكنك الحصول على ID المستخدم من بوت @userinfobot"
        )
        context.user_data['waiting_for_new_user_id'] = True
        return
    
    if data == "remove_user_list":
        show_remove_user_list(chat_id, message_id)
        return
    
    if data.startswith("remove_user_"):
        user_to_remove = data[12:]
        if remove_authorized_user(user_to_remove):
            query.edit_message_text(f"✅ تم حذف المستخدم {user_to_remove} بنجاح")
        else:
            query.edit_message_text("❌ لا يمكن حذف المالك الأساسي للبوت")
        show_users_management_menu(chat_id)
        return
    
    # ========== زر تسجيل خروج جهاز ==========
    if data == "menu_force_logout":
        show_device_list_for_logout(chat_id, message_id)
        return
    
    if data.startswith("force_logout_"):
        device_name = data[13:]
        c.execute("DELETE FROM active_sessions WHERE device_name = ?", (device_name,))
        c.execute("DELETE FROM approved_devices WHERE device_name = ?", (device_name,))
        conn.commit()
        
        keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
        query.edit_message_text(
            text=f"✅ تم تسجيل خروج الجهاز بنجاح!\n\n📱 {device_name}\n\nسيتم إرجاع المستخدم إلى شاشة تسجيل الدخول.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    # ========== الطلبات المعلقة ==========
    if data == "menu_pending":
        show_pending_requests_with_buttons(chat_id, message_id)
        return
    
    # ========== اختيار جهاز للحظر ==========
    if data.startswith("select_ban_"):
        device_name = data[11:]
        c.execute("SELECT username FROM approvals WHERE device_name = ? ORDER BY timestamp DESC LIMIT 1", (device_name,))
        row = c.fetchone()
        username = row[0] if row else "Unknown"
        show_ban_type_menu(chat_id, device_name, username, message_id)
        return
    
    # ========== اختيار نوع الحظر (مع طلب السبب) ==========
    if data.startswith("ban_type_minutes_"):
        device_name = data[17:]
        context.user_data['ban_device'] = device_name
        context.user_data['ban_type'] = "minutes"
        context.user_data['ban_unit'] = "minutes"
        query.edit_message_text(
            text=f"🔒 حظر جهاز: {device_name}\n\n📝 أرسل سبب الحظر أولاً:\n(سيُطلب منك بعدها إدخال عدد الدقائق)"
        )
        context.user_data['waiting_for_ban_reason'] = True
        return
    
    if data.startswith("ban_type_hours_"):
        device_name = data[15:]
        context.user_data['ban_device'] = device_name
        context.user_data['ban_type'] = "hours"
        context.user_data['ban_unit'] = "hours"
        query.edit_message_text(
            text=f"🔒 حظر جهاز: {device_name}\n\n📝 أرسل سبب الحظر أولاً:\n(سيُطلب منك بعدها إدخال عدد الساعات)"
        )
        context.user_data['waiting_for_ban_reason'] = True
        return
    
    if data.startswith("ban_type_days_"):
        device_name = data[14:]
        context.user_data['ban_device'] = device_name
        context.user_data['ban_type'] = "days"
        context.user_data['ban_unit'] = "days"
        query.edit_message_text(
            text=f"🔒 حظر جهاز: {device_name}\n\n📝 أرسل سبب الحظر أولاً:\n(سيُطلب منك بعدها إدخال عدد الأيام)"
        )
        context.user_data['waiting_for_ban_reason'] = True
        return
    
    # ========== تأكيد الحظر الدائم (مع طلب السبب) ==========
    if data.startswith("ban_confirm_permanent_"):
        device_name = data[22:]
        context.user_data['ban_device'] = device_name
        context.user_data['ban_type'] = "permanent"
        context.user_data['ban_unit'] = "permanent"
        query.edit_message_text(
            text=f"🔒 حظر دائم لجهاز: {device_name}\n\n📝 أرسل سبب الحظر:"
        )
        context.user_data['waiting_for_ban_reason'] = True
        return
    
    # ========== اختيار جهاز لرفع الحظر ==========
    if data.startswith("select_unban_"):
        device_name = data[13:]
        unban_device(device_name)
        
        keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
        query.edit_message_text(
            text=f"✅ تم رفع الحظر عن الجهاز بنجاح!\n\n📱 {device_name}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    # ========== مسح الطلبات ==========
    if data == "menu_clear_requests_options":
        show_clear_requests_menu(chat_id, message_id)
        return
    
    if data == "clear_approved":
        c.execute("DELETE FROM approvals WHERE status='approved'")
        conn.commit()
        query.edit_message_text("✅ تم مسح جميع الطلبات المقبولة")
        send_main_menu(chat_id)
        return
    
    if data == "clear_denied":
        c.execute("DELETE FROM approvals WHERE status='denied'")
        conn.commit()
        query.edit_message_text("✅ تم مسح جميع الطلبات المرفوضة")
        send_main_menu(chat_id)
        return
    
    if data == "clear_pending":
        c.execute("DELETE FROM approvals WHERE status='pending'")
        conn.commit()
        query.edit_message_text("✅ تم مسح جميع الطلبات المعلقة")
        send_main_menu(chat_id)
        return
    
    if data == "clear_all_requests":
        c.execute("DELETE FROM approvals")
        conn.commit()
        query.edit_message_text("✅ تم مسح جميع الطلبات")
        send_main_menu(chat_id)
        return
    
    # ========== كلمة المرور المؤقتة ==========
    if data == "menu_temp_password":
        show_temp_password_menu(chat_id, message_id)
        return
    
    if data == "temp_specific_device":
        devices = get_all_known_devices()
        if not devices:
            query.edit_message_text("📭 لا توجد أجهزة معروفة")
            return
        
        keyboard = []
        for device_name, username in list(devices.items())[:20]:
            if not is_device_banned(device_name):
                keyboard.append([InlineKeyboardButton(f"📱 {device_name} (👤 {username})", callback_data=f"temp_select_{device_name}")])
        keyboard.append([InlineKeyboardButton("🔙 العودة", callback_data="menu_temp_password")])
        
        query.edit_message_text(
            "🔑 اختر الجهاز لإنشاء كلمة مرور مؤقتة له:\n\nبعد الاختيار ستختار مدة الصلاحية (دقائق/ساعات/أيام)",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    if data == "temp_all_devices":
        show_temp_password_duration_menu(chat_id, "all", "جميع الأجهزة", message_id, is_all_devices=True)
        return
    
    if data.startswith("temp_select_"):
        device_name = data[12:]
        c.execute("SELECT username FROM approvals WHERE device_name = ? ORDER BY timestamp DESC LIMIT 1", (device_name,))
        row = c.fetchone()
        username = row[0] if row else "Unknown"
        show_temp_password_duration_menu(chat_id, device_name, username, message_id, is_all_devices=False)
        return
    
    if data.startswith("temp_duration_minutes_"):
        device_name = data[21:]
        context.user_data['temp_device'] = device_name
        context.user_data['temp_unit'] = "minutes"
        context.user_data['temp_all_devices'] = False
        query.edit_message_text(
            text=f"🔑 كلمة مرور مؤقتة لجهاز: {device_name}\n\n⏰ أدخل عدد الدقائق (1-59):"
        )
        context.user_data['waiting_for_temp_duration'] = True
        return
    
    if data.startswith("temp_duration_hours_"):
        device_name = data[19:]
        context.user_data['temp_device'] = device_name
        context.user_data['temp_unit'] = "hours"
        context.user_data['temp_all_devices'] = False
        query.edit_message_text(
            text=f"🔑 كلمة مرور مؤقتة لجهاز: {device_name}\n\n🕐 أدخل عدد الساعات (1-23):"
        )
        context.user_data['waiting_for_temp_duration'] = True
        return
    
    if data.startswith("temp_duration_days_"):
        device_name = data[18:]
        context.user_data['temp_device'] = device_name
        context.user_data['temp_unit'] = "days"
        context.user_data['temp_all_devices'] = False
        query.edit_message_text(
            text=f"🔑 كلمة مرور مؤقتة لجهاز: {device_name}\n\n📅 أدخل عدد الأيام (1-365):"
        )
        context.user_data['waiting_for_temp_duration'] = True
        return
    
    if data.startswith("temp_all_duration_minutes_"):
        device_name = data[24:]
        context.user_data['temp_device'] = "all"
        context.user_data['temp_unit'] = "minutes"
        context.user_data['temp_all_devices'] = True
        query.edit_message_text(
            text=f"🔑 كلمة مرور مؤقتة لجميع الأجهزة\n\n⏰ أدخل عدد الدقائق (1-59):"
        )
        context.user_data['waiting_for_temp_duration'] = True
        return
    
    if data.startswith("temp_all_duration_hours_"):
        device_name = data[22:]
        context.user_data['temp_device'] = "all"
        context.user_data['temp_unit'] = "hours"
        context.user_data['temp_all_devices'] = True
        query.edit_message_text(
            text=f"🔑 كلمة مرور مؤقتة لجميع الأجهزة\n\n🕐 أدخل عدد الساعات (1-23):"
        )
        context.user_data['waiting_for_temp_duration'] = True
        return
    
    if data.startswith("temp_all_duration_days_"):
        device_name = data[21:]
        context.user_data['temp_device'] = "all"
        context.user_data['temp_unit'] = "days"
        context.user_data['temp_all_devices'] = True
        query.edit_message_text(
            text=f"🔑 كلمة مرور مؤقتة لجميع الأجهزة\n\n📅 أدخل عدد الأيام (1-365):"
        )
        context.user_data['waiting_for_temp_duration'] = True
        return
    
    # ========== إرسال الإشعارات (بدون عنوان) ==========
    if data == "menu_send_notification":
        show_send_notification_menu(chat_id, message_id)
        return
    
    if data == "notify_single_device":
        show_devices_for_notification(chat_id, message_id)
        return
    
    if data == "notify_all_devices":
        context.user_data['waiting_for_broadcast_message'] = True
        query.edit_message_text(
            text="📢 إرسال إشعار للجميع\n\n📝 أرسل نص الإشعار الآن:"
        )
        return
    
    if data.startswith("notify_device_"):
        device_name = data[14:]
        context.user_data['notify_device'] = device_name
        context.user_data['waiting_for_notification_message'] = True
        query.edit_message_text(
            text=f"📢 إرسال إشعار للجهاز: {device_name}\n\n📝 أرسل نص الإشعار الآن:"
        )
        return
    
    # ========== تغيير رسالة الترحيب ==========
    if data == "change_welcome_template":
        show_welcome_template_menu(chat_id, message_id)
        return
    
    # ========== القوائم الرئيسية ==========
    if data == "menu_ban_device_list":
        show_device_list_for_ban(chat_id, message_id)
        return
    
    if data == "menu_unban_device_list":
        show_device_list_for_unban(chat_id, message_id)
        return
    
    if data == "menu_stats":
        stats = get_access_stats()
        text_msg = f"""
📊 إحصائيات النظام

📝 إجمالي الطلبات: {stats['total']}
⏳ قيد الانتظار: {stats['pending']}
✅ تمت الموافقة: {stats['approved']}
❌ تم الرفض: {stats['denied']}
🚫 الأجهزة المحظورة: {stats['banned']}
📱 الأجهزة النشطة: {stats['active']}
✅ الأجهزة المعتمدة: {stats['approved_devices']}
👥 المستخدمين المصرح لهم: {stats['authorized_users']}

🔄 آخر تحديث: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
        query.edit_message_text(
            text=text_msg,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    elif data == "menu_approved":
        approved_devices = c.execute(
            "SELECT username, device_name, approved_at FROM approved_devices ORDER BY approved_at DESC LIMIT 20"
        ).fetchall()
        
        if approved_devices:
            text_msg = "✅ الأجهزة المعتمدة:\n\n"
            for device in approved_devices:
                time_str = datetime.fromtimestamp(device[2]).strftime('%Y-%m-%d %H:%M')
                text_msg += f"👤 {device[0]} - 📱 {device[1]} - {time_str}\n"
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text=text_msg,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text="📭 لا توجد أجهزة معتمدة",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return
    
    elif data == "menu_denied":
        denied_reqs = c.execute(
            "SELECT username, device_name, timestamp FROM approvals WHERE status='denied' ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
        
        if denied_reqs:
            text_msg = "❌ الطلبات المرفوضة:\n\n"
            for req in denied_reqs:
                time_str = datetime.fromtimestamp(req[2]).strftime('%Y-%m-%d %H:%M')
                text_msg += f"👤 {req[0]} - {req[1]} - {time_str}\n"
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text=text_msg,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text="📭 لا توجد طلبات مرفوضة",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return
    
    elif data == "menu_logs":
        logs = c.execute(
            "SELECT username, device_name, status, timestamp FROM access_logs ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
        
        if logs:
            log_text = "📋 سجل الدخول الأخير:\n\n"
            for log in logs:
                time_str = datetime.fromtimestamp(log[3]).strftime('%Y-%m-%d %H:%M')
                emoji = "✅" if log[2] == "approved" else "❌" if log[2] == "denied" else "📱" if log[2] == "app_opened" else "⏳"
                log_text += f"{emoji} {log[0]} - {log[1]} - {time_str}\n"
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text=log_text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text="📭 لا يوجد سجل",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return
    
    elif data == "menu_active_devices":
        devices = get_active_devices()
        
        if devices:
            text_msg = "📱 الأجهزة النشطة:\n\n"
            for dev in devices:
                device_name, username, device_info, last_active, first_active, total_requests = dev
                last_active_str = datetime.fromtimestamp(last_active).strftime('%Y-%m-%d %H:%M')
                text_msg += f"📱 {device_name}\n   👤 {username}\n   🕐 آخر ظهور: {last_active_str}\n   📊 عدد الطلبات: {total_requests}\n\n"
            keyboard = [
                [InlineKeyboardButton("🔒 حظر جهاز", callback_data="menu_ban_device_list")],
                [InlineKeyboardButton("🚪 تسجيل خروج", callback_data="menu_force_logout")],
                [InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]
            ]
            query.edit_message_text(
                text=text_msg,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text="📭 لا توجد أجهزة نشطة",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return
    
    elif data == "menu_banned_devices":
        devices = get_banned_devices()
        
        if devices:
            text_msg = "🚫 الأجهزة المحظورة:\n\n"
            for dev in devices:
                device_name, username, banned_at, banned_until, ban_type, reason = dev
                banned_at_str = datetime.fromtimestamp(banned_at).strftime('%Y-%m-%d %H:%M')
                if ban_type == "permanent":
                    expiry = "🔒 دائم"
                elif ban_type == "minutes":
                    remaining = (banned_until - int(time.time())) // 60
                    expiry = f"⏰ متبقي {remaining} دقيقة"
                elif ban_type == "hours":
                    remaining = (banned_until - int(time.time())) // 3600
                    expiry = f"⏰ متبقي {remaining} ساعة"
                elif ban_type == "days":
                    remaining = (banned_until - int(time.time())) // 86400
                    expiry = f"⏰ متبقي {remaining} يوم"
                else:
                    expiry = "🔒 محظور"
                text_msg += f"📱 {device_name}\n   👤 {username}\n   🗓️ حظر في: {banned_at_str}\n   ⏱️ {expiry}\n   📝 السبب: {reason}\n\n"
            keyboard = [
                [InlineKeyboardButton("🔓 رفع حظر", callback_data="menu_unban_device_list")],
                [InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]
            ]
            query.edit_message_text(
                text=text_msg,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text="✅ لا توجد أجهزة محظورة",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return
    
    elif data == "menu_settings":
        logo = get_setting("custom_logo", CUSTOM_LOGO)
        welcome_template = get_setting("welcome_message_template", "مرحباً بك يا & في تطبيق tomb of Makrotik")
        
        text_msg = f"""
⚙️ الإعدادات الحالية

🏷️ الشعار: 
{logo[:50]}...

💬 رسالة الترحيب: 
{welcome_template[:50]}...

🔑 كلمة المرور: {'●' * 8}
"""
        keyboard = [
            [InlineKeyboardButton("📢 إرسال إشعارات", callback_data="menu_send_notification")],
            [InlineKeyboardButton("💬 تغيير رسالة الترحيب", callback_data="change_welcome_template")],
            [InlineKeyboardButton("🔑 تغيير كلمة المرور", callback_data="change_password")],
            [InlineKeyboardButton("👥 إدارة المستخدمين", callback_data="menu_users_management")],
            [InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]
        ]
        query.edit_message_text(
            text=text_msg,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    elif data == "change_password":
        query.edit_message_text(
            "🔑 تغيير كلمة المرور\n\nأرسل كلمة المرور الجديدة (4 أحرف على الأقل):"
        )
        context.user_data['waiting_for_new_password'] = True
        return
    
    elif data == "back_to_main":
        send_main_menu(chat_id)
        try:
            query.delete_message()
        except:
            pass
        return
    
    # ========== معالجة الطلبات ==========
    elif data.startswith("approve_"):
        request_id = data[8:]
        status = "approved"
        
        print(f"✅ Approving request: {request_id}")
        
        c.execute("UPDATE approvals SET status = ? WHERE request_id = ?", (status, request_id))
        conn.commit()
        
        if request_id in pending_requests:
            pending_requests[request_id]["status"] = status
        
        c.execute("SELECT username, device_name, ip_address FROM approvals WHERE request_id = ?", (request_id,))
        row = c.fetchone()
        if row:
            username, device_name, ip_address = row
            approve_device(device_name, username, "bot")
            log_access(username, device_name, ip_address, "approved")
        
        try:
            query.edit_message_text(
                text=f"✅ تمت الموافقة بنجاح\n\nتمت إضافة الجهاز إلى قائمة الأجهزة المعتمدة.\nيمكن للمستخدم الآن الدخول إلى التطبيق في أي وقت.\n\n{get_setting('custom_logo', CUSTOM_LOGO)}"
            )
        except:
            pass
        return
    
    elif data.startswith("deny_"):
        request_id = data[5:]
        status = "denied"
        
        print(f"❌ Denying request: {request_id}")
        
        c.execute("UPDATE approvals SET status = ? WHERE request_id = ?", (status, request_id))
        conn.commit()
        
        if request_id in pending_requests:
            pending_requests[request_id]["status"] = status
        
        c.execute("SELECT username, device_name, ip_address FROM approvals WHERE request_id = ?", (request_id,))
        row = c.fetchone()
        if row:
            log_access(row[0], row[1], row[2], "denied")
        
        try:
            query.edit_message_text(
                text=f"❌ تم رفض الطلب\n\nلم يتم السماح للمستخدم بالدخول.\n\n{get_setting('custom_logo', CUSTOM_LOGO)}"
            )
        except:
            pass
        return
    
    elif data.startswith("ban_this_"):
        request_id = data[9:]
        c.execute("SELECT device_name, username FROM approvals WHERE request_id = ?", (request_id,))
        row = c.fetchone()
        if row:
            device_name, username = row
            show_ban_type_menu(chat_id, device_name, username, message_id)
        return
    
    elif data.startswith("info_"):
        request_id = data[5:]
        c.execute("""SELECT username, device_name, device_info, ip_address, timestamp 
                     FROM approvals WHERE request_id = ?""", (request_id,))
        row = c.fetchone()
        
        if row:
            info_text = f"""
📱 معلومات الجهاز

👤 المستخدم: {row[0]}
📱 اسم الجهاز: {row[1]}
ℹ️ تفاصيل: {row[2]}
🌐 IP: {row[3]}
🕐 الوقت: {datetime.fromtimestamp(row[4]).strftime('%Y-%m-%d %H:%M:%S')}
"""
            keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")]]
            query.edit_message_text(
                text=info_text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            query.edit_message_text("❌ لم يتم العثور على الطلب")
        return

def handle_message(update, context):
    message = update.message
    chat_id = message.chat_id
    text = message.text.strip()
    
    # التحقق من الصلاحية
    if not is_authorized(chat_id):
        bot.send_message(chat_id=chat_id, text="⚠️ أنت غير مصرح لك باستخدام هذا البوت")
        return
    
    # ========== إضافة مستخدم جديد ==========
    if context.user_data.get('waiting_for_new_user_id'):
        try:
            new_user_id = text.strip()
            # التحقق من أن الإدخال رقم
            if not new_user_id.isdigit():
                bot.send_message(chat_id=chat_id, text="❌ معرف المستخدم يجب أن يكون أرقاماً فقط")
                context.user_data.pop('waiting_for_new_user_id', None)
                send_main_menu(chat_id)
                return
            
            # الحصول على اسم المستخدم (اختياري)
            username = f"user_{new_user_id}"
            added_by = str(chat_id)
            
            if add_authorized_user(new_user_id, username, added_by):
                bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ تم إضافة المستخدم بنجاح!\n\n🆔 المعرف: {new_user_id}\n👤 الاسم: {username}\n\nيمكن لهذا المستخدم الآن استخدام البوت."
                )
            else:
                bot.send_message(chat_id=chat_id, text="❌ هذا المستخدم موجود بالفعل في القائمة")
        except Exception as e:
            bot.send_message(chat_id=chat_id, text=f"❌ خطأ: {e}")
        
        context.user_data.pop('waiting_for_new_user_id', None)
        send_main_menu(chat_id)
        return
    
    # ========== معالجة سبب الحظر ==========
    if context.user_data.get('waiting_for_ban_reason'):
        ban_reason = text
        device_name = context.user_data.get('ban_device')
        ban_type = context.user_data.get('ban_type')
        ban_unit = context.user_data.get('ban_unit')
        
        c.execute("SELECT username FROM approvals WHERE device_name = ? ORDER BY timestamp DESC LIMIT 1", (device_name,))
        row = c.fetchone()
        username = row[0] if row else "Unknown"
        
        context.user_data['ban_reason'] = ban_reason
        
        if ban_type == "permanent":
            # حظر دائم مباشرة
            ban_device(device_name, username, "permanent", 0, ban_reason)
            bot.send_message(
                chat_id=chat_id,
                text=f"✅ تم حظر الجهاز بنجاح!\n\n📱 {device_name}\n🔒 حظر دائم\n📝 السبب: {ban_reason}"
            )
            context.user_data.pop('waiting_for_ban_reason', None)
            context.user_data.pop('ban_device', None)
            context.user_data.pop('ban_type', None)
            context.user_data.pop('ban_unit', None)
            context.user_data.pop('ban_reason', None)
            send_main_menu(chat_id)
            return
        else:
            # طلب المدة
            context.user_data['waiting_for_ban_reason'] = False
            context.user_data['waiting_for_ban_duration'] = True
            
            if ban_unit == "minutes":
                bot.send_message(
                    chat_id=chat_id,
                    text=f"🔒 حظر جهاز: {device_name}\n📝 السبب: {ban_reason}\n\n⏰ أدخل عدد الدقائق (1-59):"
                )
            elif ban_unit == "hours":
                bot.send_message(
                    chat_id=chat_id,
                    text=f"🔒 حظر جهاز: {device_name}\n📝 السبب: {ban_reason}\n\n🕐 أدخل عدد الساعات (1-23):"
                )
            elif ban_unit == "days":
                bot.send_message(
                    chat_id=chat_id,
                    text=f"🔒 حظر جهاز: {device_name}\n📝 السبب: {ban_reason}\n\n📅 أدخل عدد الأيام (1-365):"
                )
        return
    
    # ========== معالجة مدة الحظر ==========
    if context.user_data.get('waiting_for_ban_duration'):
        try:
            duration = int(text)
            ban_type = context.user_data.get('ban_type')
            device_name = context.user_data.get('ban_device')
            ban_reason = context.user_data.get('ban_reason', 'محظور من قبل المطور')
            
            if ban_type == "minutes" and (duration < 1 or duration > 59):
                bot.send_message(chat_id=chat_id, text="❌ عدد الدقائق يجب أن يكون بين 1 و 59")
                context.user_data.pop('waiting_for_ban_duration', None)
                context.user_data.pop('ban_device', None)
                context.user_data.pop('ban_type', None)
                context.user_data.pop('ban_reason', None)
                send_main_menu(chat_id)
                return
            elif ban_type == "hours" and (duration < 1 or duration > 23):
                bot.send_message(chat_id=chat_id, text="❌ عدد الساعات يجب أن يكون بين 1 و 23")
                context.user_data.pop('waiting_for_ban_duration', None)
                context.user_data.pop('ban_device', None)
                context.user_data.pop('ban_type', None)
                context.user_data.pop('ban_reason', None)
                send_main_menu(chat_id)
                return
            elif ban_type == "days" and (duration < 1 or duration > 365):
                bot.send_message(chat_id=chat_id, text="❌ عدد الأيام يجب أن يكون بين 1 و 365")
                context.user_data.pop('waiting_for_ban_duration', None)
                context.user_data.pop('ban_device', None)
                context.user_data.pop('ban_type', None)
                context.user_data.pop('ban_reason', None)
                send_main_menu(chat_id)
                return
            
            c.execute("SELECT username FROM approvals WHERE device_name = ? ORDER BY timestamp DESC LIMIT 1", (device_name,))
            row = c.fetchone()
            username = row[0] if row else "Unknown"
            
            if ban_type == "minutes":
                ban_device(device_name, username, "minutes", duration, f"{ban_reason} (مدة: {duration} دقيقة)")
                time_text = f"{duration} دقيقة"
            elif ban_type == "hours":
                ban_device(device_name, username, "hours", duration, f"{ban_reason} (مدة: {duration} ساعة)")
                time_text = f"{duration} ساعة"
            elif ban_type == "days":
                ban_device(device_name, username, "days", duration, f"{ban_reason} (مدة: {duration} يوم)")
                time_text = f"{duration} يوم"
            else:
                ban_device(device_name, username, "permanent", 0, ban_reason)
                time_text = "دائم"
            
            bot.send_message(
                chat_id=chat_id,
                text=f"✅ تم حظر الجهاز بنجاح!\n\n📱 {device_name}\n⏰ مدة الحظر: {time_text}\n📝 السبب: {ban_reason}"
            )
            
        except ValueError:
            bot.send_message(chat_id=chat_id, text="❌ الرجاء إدخال رقم صحيح")
        
        context.user_data.pop('waiting_for_ban_duration', None)
        context.user_data.pop('ban_device', None)
        context.user_data.pop('ban_type', None)
        context.user_data.pop('ban_reason', None)
        send_main_menu(chat_id)
        return
    
    # ========== معالجة مدة كلمة المرور المؤقتة ==========
    if context.user_data.get('waiting_for_temp_duration'):
        try:
            duration = int(text)
            unit = context.user_data.get('temp_unit')
            device_name = context.user_data.get('temp_device')
            is_all_devices = context.user_data.get('temp_all_devices', False)
            
            if unit == "minutes" and (duration < 1 or duration > 59):
                bot.send_message(chat_id=chat_id, text="❌ عدد الدقائق يجب أن يكون بين 1 و 59")
            elif unit == "hours" and (duration < 1 or duration > 23):
                bot.send_message(chat_id=chat_id, text="❌ عدد الساعات يجب أن يكون بين 1 و 23")
            elif unit == "days" and (duration < 1 or duration > 365):
                bot.send_message(chat_id=chat_id, text="❌ عدد الأيام يجب أن يكون بين 1 و 365")
            else:
                temp_password, time_text = create_temp_password_for_device(
                    device_name, duration, unit, is_all_devices
                )
                
                if is_all_devices:
                    bot.send_message(
                        chat_id=chat_id,
                        text=f"✅ تم إنشاء كلمة مرور مؤقتة لجميع الأجهزة!\n\n🔑 كلمة المرور: {temp_password}\n⏰ المدة: {time_text}\n\n⚠️ هذه الكلمة صالحة لجميع الأجهزة النشطة.\nبعد انتهاء المدة سيتم إرجاع جميع المستخدمين لشاشة تسجيل الدخول."
                    )
                else:
                    bot.send_message(
                        chat_id=chat_id,
                        text=f"✅ تم إنشاء كلمة مرور مؤقتة!\n\n📱 الجهاز: {device_name}\n🔑 كلمة المرور: {temp_password}\n⏰ المدة: {time_text}\n\n⚠️ بعد انتهاء المدة سيتم إرجاع المستخدم لشاشة تسجيل الدخول."
                    )
        except ValueError:
            bot.send_message(chat_id=chat_id, text="❌ الرجاء إدخال رقم صحيح")
        
        context.user_data.pop('waiting_for_temp_duration', None)
        context.user_data.pop('temp_device', None)
        context.user_data.pop('temp_unit', None)
        context.user_data.pop('temp_all_devices', None)
        send_main_menu(chat_id)
        return
    
    # ========== معالجة إشعار لجهاز محدد (بدون عنوان) ==========
    if context.user_data.get('waiting_for_notification_message'):
        message_text = text
        device_name = context.user_data.get('notify_device')
        
        c.execute("""INSERT INTO notifications (device_name, message, created_at, is_read)
                     VALUES (?, ?, ?, 0)""",
                  (device_name, message_text, int(time.time())))
        conn.commit()
        
        bot.send_message(
            chat_id=chat_id,
            text=f"✅ تم إرسال الإشعار بنجاح!\n\n📱 الجهاز: {device_name}\n💬 الرسالة: {message_text}"
        )
        
        context.user_data.pop('waiting_for_notification_message', None)
        context.user_data.pop('notify_device', None)
        send_main_menu(chat_id)
        return
    
    # ========== معالجة إشعار للجميع (بدون عنوان) ==========
    if context.user_data.get('waiting_for_broadcast_message'):
        message_text = text
        
        devices = get_active_devices()
        for device in devices:
            device_name = device[0]
            c.execute("""INSERT INTO notifications (device_name, message, created_at, is_read)
                         VALUES (?, ?, ?, 0)""",
                      (device_name, message_text, int(time.time())))
        conn.commit()
        
        bot.send_message(
            chat_id=chat_id,
            text=f"✅ تم إرسال الإشعار للجميع بنجاح!\n\n💬 الرسالة: {message_text}\n📱 عدد الأجهزة: {len(devices)}"
        )
        
        context.user_data.pop('waiting_for_broadcast_message', None)
        send_main_menu(chat_id)
        return
    
    # ========== معالجة تغيير رسالة الترحيب ==========
    if context.user_data.get('waiting_for_welcome_template'):
        new_template = text.strip()
        if '&' in new_template:
            set_setting("welcome_message_template", new_template)
            bot.send_message(
                chat_id=chat_id,
                text=f"✅ تم تغيير رسالة الترحيب بنجاح!\n\nالرسالة الجديدة: {new_template}\n\n(سيتم استبدال & باسم المستخدم)"
            )
        else:
            bot.send_message(
                chat_id=chat_id,
                text="❌ الرسالة يجب أن تحتوي على الرمز &\n\nمثال: مرحباً بك يا & في تطبيق TOMB"
            )
        context.user_data.pop('waiting_for_welcome_template')
        send_main_menu(chat_id)
        return
    
    # ========== معالجة تغيير كلمة المرور ==========
    if context.user_data.get('waiting_for_new_password'):
        new_password = text.strip()
        if len(new_password) >= 4:
            if update_password(new_password, "bot"):
                bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ تم تغيير كلمة مرور التطبيق بنجاح!\n\n🔑 كلمة المرور الجديدة: {new_password}\n\n⚠️ كلمة المرور الأساسية لا تنتهي أبداً"
                )
            else:
                bot.send_message(chat_id=chat_id, text="❌ فشل في تغيير كلمة المرور")
        else:
            bot.send_message(chat_id=chat_id, text="❌ كلمة المرور يجب أن تكون 4 أحرف على الأقل")
        context.user_data.pop('waiting_for_new_password')
        send_main_menu(chat_id)
        return
    
    if text == '/start':
        send_main_menu(chat_id)
    else:
        send_main_menu(chat_id)

def run_bot():
    try:
        updater = Updater(BOT_TOKEN, use_context=True)
        dp = updater.dispatcher
        
        dp.add_handler(CallbackQueryHandler(handle_callback))
        dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
        dp.add_handler(CommandHandler("start", handle_message))
        
        updater.start_polling()
        updater.idle()
    except Exception as e:
        print(f"Bot error: {e}")

bot_thread = threading.Thread(target=run_bot)
bot_thread.daemon = True
bot_thread.start()

# تشغيل مهمة تنظيف الجلسات المنتهية كل ساعة
def cleanup_scheduler():
    while True:
        time.sleep(3600)  # كل ساعة
        cleanup_expired_temp_sessions()
        print(f"[CLEANUP] Cleaned expired temp sessions at {datetime.now()}")

cleanup_thread = threading.Thread(target=cleanup_scheduler)
cleanup_thread.daemon = True
cleanup_thread.start()

# ===================== API للتطبيق =====================
@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "status": "online",
        "service": "Tomb Bot Protection System",
        "version": "8.0",
        "message": "API is working! (Permanent passwords for approved devices, Temporary passwords with expiration)",
        "features": [
            "Ban with reason",
            "Notifications without title",
            "Temporary passwords",
            "Auto-login for approved devices",
            "Multi-user support (authorized users)",
            "Pending requests with approve/deny buttons"
        ],
        "endpoints": [
            "/request_access - POST",
            "/check_status/<request_id> - GET", 
            "/verify_password - POST",
            "/change_password - POST",
            "/update_settings - POST",
            "/get_settings - GET",
            "/get_stats - GET",
            "/check_device_status - POST",
            "/verify_temp_password - POST",
            "/notify_app_opened - POST",
            "/check_session - POST",
            "/force_logout - POST",
            "/refresh_device_status - POST",
            "/update_device_last_login - POST",
            "/get_welcome_template - GET",
            "/get_notifications/<device_name> - GET",
            "/check_device_approved - POST",
            "/health - GET"
        ]
    })

@app.route('/request_access', methods=['POST'])
def request_access():
    try:
        data = request.json
        request_id = data.get('request_id')
        app_name = data.get('app_name', 'Tomb')
        username = data.get('username', 'Unknown')
        device_name = data.get('device_name', 'Unknown')
        device_info = data.get('device_info', 'Unknown')
        
        if not request_id:
            return jsonify({"error": "missing request_id"}), 400
        
        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
        
        print(f"📱 New request from {device_name} - ID: {request_id}")
        
        # التحقق من الحظر
        if is_device_banned(device_name):
            ban_info = get_device_ban_info(device_name)
            return jsonify({
                "status": "banned",
                "message": "جهازك محظور من قبل المطور",
                "ban_type": ban_info['ban_type'],
                "reason": ban_info['reason'],
                "remaining": ban_info['remaining'],
                "remaining_text": ban_info.get('remaining_text', '')
            })
        
        # التحقق مما إذا كان الجهاز معتمداً بالفعل (كلمة أساسية سابقة - لا تنتهي أبداً)
        if is_device_approved(device_name):
            update_device_last_login(device_name)
            add_active_device(device_name, username, device_info)
            log_access(username, device_name, ip_address, "auto_approved")
            print(f"✅ Device {device_name} is already approved, auto-login")
            return jsonify({
                "status": "approved",
                "message": "تم الدخول تلقائياً (جهاز معتمد)",
                "session_type": "normal",
                "expires_at": 0,
                "note": "كلمة المرور الأساسية لا تنتهي أبداً"
            })
        
        # إرسال طلب موافقة للمطورين المصرح لهم
        send_approval_request(
            request_id=request_id,
            app_name=app_name,
            username=username,
            device_name=device_name,
            device_info=device_info,
            ip_address=ip_address
        )
        
        return jsonify({"status": "sent", "request_id": request_id})
    
    except Exception as e:
        print(f"Error in request_access: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/check_status/<request_id>', methods=['GET'])
def check_status(request_id):
    try:
        print(f"🔍 Checking status for: {request_id}")
        
        c.execute("SELECT status FROM approvals WHERE request_id = ?", (request_id,))
        row = c.fetchone()
        
        if row:
            status = row[0]
            print(f"💾 Found in database: {status}")
            
            if request_id in pending_requests:
                pending_requests[request_id]["status"] = status
            
            return jsonify({"status": status})
        
        if request_id in pending_requests:
            status = pending_requests[request_id]["status"]
            print(f"📌 Found in memory: {status}")
            return jsonify({"status": status})
        
        print(f"❌ Request not found: {request_id}")
        return jsonify({"status": "pending"})
    
    except Exception as e:
        print(f"Error in check_status: {e}")
        return jsonify({"status": "pending"}), 500

@app.route('/verify_password', methods=['POST'])
def verify_password():
    try:
        data = request.json
        password = data.get('password', '')
        device_name = data.get('device_name', '')
        
        if check_password(password):
            if device_name and is_device_approved(device_name):
                update_device_last_login(device_name)
            return jsonify({"valid": True, "note": "كلمة المرور الأساسية لا تنتهي أبداً"})
        return jsonify({"valid": False})
    
    except Exception as e:
        return jsonify({"valid": False, "error": str(e)}), 500

@app.route('/verify_temp_password', methods=['POST'])
def verify_temp_password():
    try:
        data = request.json
        temp_password = data.get('temp_password', '')
        device_name = data.get('device_name', '')
        
        temp_hash = hashlib.sha256(temp_password.encode()).hexdigest()
        
        # التحقق من كلمة المرور المؤقتة للجهاز المحدد أو لجميع الأجهزة
        c.execute("""SELECT id, expires_at, device_name FROM temp_passwords 
                     WHERE password_hash = ? AND (device_name = ? OR device_name = 'all') AND used = 0 AND expires_at > ?""",
                  (temp_hash, device_name, int(time.time())))
        row = c.fetchone()
        
        if row:
            # نستخدم الكلمة مرة واحدة
            c.execute("UPDATE temp_passwords SET used = 1 WHERE id = ?", (row[0],))
            conn.commit()
            return jsonify({"valid": True, "expires_at": row[1], "note": "كلمة مرور مؤقتة تنتهي بعد المدة"})
        
        return jsonify({"valid": False})
    
    except Exception as e:
        return jsonify({"valid": False, "error": str(e)}), 500

@app.route('/check_device_approved', methods=['POST'])
def check_device_approved():
    try:
        data = request.json
        device_name = data.get('device_name', '')
        
        if is_device_banned(device_name):
            ban_info = get_device_ban_info(device_name)
            return jsonify({
                "approved": False,
                "banned": True,
                "message": ban_info['reason']
            })
        
        approved = is_device_approved(device_name)
        if approved:
            update_device_last_login(device_name)
        
        return jsonify({
            "approved": approved,
            "banned": False,
            "note": "الأجهزة المعتمدة تستخدم كلمة المرور الأساسية التي لا تنتهي أبداً"
        })
    
    except Exception as e:
        return jsonify({"approved": False, "banned": False, "error": str(e)}), 500

@app.route('/change_password', methods=['POST'])
def change_password():
    try:
        data = request.json
        old_password = data.get('old_password', '')
        new_password = data.get('new_password', '')
        
        if not check_password(old_password):
            return jsonify({"success": False, "error": "كلمة المرور الحالية غير صحيحة"})
        
        if len(new_password) < 4:
            return jsonify({"success": False, "error": "كلمة المرور الجديدة قصيرة جداً"})
        
        if update_password(new_password, "app"):
            return jsonify({"success": True, "note": "كلمة المرور الأساسية الجديدة لا تنتهي أبداً"})
        
        return jsonify({"success": False, "error": "فشل في تحديث كلمة المرور"})
    
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/update_settings', methods=['POST'])
def update_settings():
    try:
        data = request.json
        password = data.get('password', '')
        
        if not check_password(password):
            return jsonify({"success": False, "error": "كلمة المرور غير صحيحة"})
        
        if 'logo' in data:
            set_setting("custom_logo", data['logo'])
        if 'welcome_message' in data:
            set_setting("welcome_message", data['welcome_message'])
        
        return jsonify({"success": True})
    
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/get_settings', methods=['GET'])
def get_settings():
    try:
        return jsonify({
            "logo": get_setting("custom_logo", CUSTOM_LOGO),
            "welcome_message": get_setting("welcome_message", WELCOME_MESSAGE),
            "welcome_template": get_setting("welcome_message_template", "مرحباً بك يا & في تطبيق tomb of Makrotik")
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/get_welcome_template', methods=['GET'])
def get_welcome_template():
    try:
        template = get_setting("welcome_message_template", "مرحباً بك يا & في تطبيق tomb of Makrotik")
        return jsonify({"welcome_template": template})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/get_stats', methods=['GET'])
def get_stats():
    try:
        stats = get_access_stats()
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/check_device_status', methods=['POST'])
def check_device_status():
    try:
        data = request.json
        device_name = data.get('device_name', '')
        
        if is_device_banned(device_name):
            ban_info = get_device_ban_info(device_name)
            return jsonify({
                "banned": True,
                "ban_type": ban_info['ban_type'],
                "reason": ban_info['reason'],
                "remaining": ban_info['remaining'],
                "remaining_text": ban_info.get('remaining_text', '')
            })
        return jsonify({"banned": False})
    
    except Exception as e:
        print(f"Error in check_device_status: {e}")
        return jsonify({"banned": False, "error": str(e)}), 500

@app.route('/notify_app_opened', methods=['POST'])
def notify_app_opened():
    try:
        data = request.json
        device_name = data.get('device_name', 'Unknown')
        device_info = data.get('device_info', 'Unknown')
        ip_address = data.get('ip_address', 'Unknown')
        
        add_active_device(device_name, "SYSTEM", device_info)
        
        if is_device_approved(device_name):
            update_device_last_login(device_name)
        
        message = f"""
📱 جهاز فتح التطبيق 📱

📱 الجهاز: {device_name}
ℹ️ المعلومات: {device_info}
🌐 IP: {ip_address}
🕐 الوقت: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

✅ هذا مجرد إشعار بفتح التطبيق، ليس طلب موافقة
"""
        # إرسال الإشعار لجميع المستخدمين المصرح لهم
        users = get_authorized_users()
        for user in users:
            try:
                bot.send_message(chat_id=user[0], text=message)
            except:
                pass
        
        c.execute("""INSERT INTO access_logs (username, device_name, ip_address, status, timestamp) 
                     VALUES (?, ?, ?, ?, ?)""",
                  ("SYSTEM", device_name, ip_address, "app_opened", int(time.time())))
        conn.commit()
        
        print(f"📱 App opened notification: {device_name} from {ip_address}")
        
        return jsonify({"status": "received"})
    
    except Exception as e:
        print(f"Error in notify_app_opened: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/check_session', methods=['POST'])
def check_session():
    try:
        data = request.json
        device_name = data.get('device_name', '')
        
        if is_device_banned(device_name):
            ban_info = get_device_ban_info(device_name)
            return jsonify({
                "valid": False,
                "reason": "banned",
                "message": ban_info['reason'],
                "remaining": ban_info.get('remaining_text', '')
            })
        
        # التحقق من الأجهزة المعتمدة (كلمة أساسية - لا تنتهي أبداً)
        if is_device_approved(device_name):
            update_device_last_login(device_name)
            return jsonify({
                "valid": True,
                "session_type": "normal",
                "expires_at": 0,
                "remaining_hours": -1,
                "remaining_text": "لا تنتهي أبداً (كلمة المرور الأساسية)"
            })
        
        # التحقق من الجلسات المؤقتة
        c.execute("SELECT session_expires, session_type FROM active_sessions WHERE device_name = ?", (device_name,))
        row = c.fetchone()
        
        if row:
            session_expires, session_type = row
            now = int(time.time())
            if session_expires > now:
                remaining_seconds = session_expires - now
                if session_type == "temp":
                    if remaining_seconds < 3600:
                        remaining_text = f"{remaining_seconds // 60} دقيقة"
                    else:
                        remaining_text = f"{remaining_seconds // 3600} ساعة"
                else:
                    remaining_text = f"{remaining_seconds // 3600} ساعة"
                return jsonify({
                    "valid": True,
                    "session_type": session_type,
                    "expires_at": session_expires,
                    "remaining_hours": remaining_seconds // 3600,
                    "remaining_text": remaining_text
                })
            else:
                c.execute("DELETE FROM active_sessions WHERE device_name = ?", (device_name,))
                conn.commit()
                return jsonify({"valid": False, "reason": "session_expired", "message": "انتهت صلاحية الجلسة المؤقتة"})
        
        return jsonify({"valid": False, "reason": "no_session"})
    
    except Exception as e:
        print(f"Error in check_session: {e}")
        return jsonify({"valid": True}), 500

@app.route('/force_logout', methods=['POST'])
def force_logout():
    try:
        data = request.json
        device_name = data.get('device_name', '')
        
        c.execute("DELETE FROM active_sessions WHERE device_name = ?", (device_name,))
        c.execute("DELETE FROM approved_devices WHERE device_name = ?", (device_name,))
        conn.commit()
        
        return jsonify({"status": "logged_out", "device_name": device_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/refresh_device_status', methods=['POST'])
def refresh_device_status():
    try:
        data = request.json
        device_name = data.get('device_name', '')
        
        if is_device_banned(device_name):
            ban_info = get_device_ban_info(device_name)
            c.execute("DELETE FROM active_sessions WHERE device_name = ?", (device_name,))
            c.execute("DELETE FROM approved_devices WHERE device_name = ?", (device_name,))
            conn.commit()
            return jsonify({
                "status": "banned",
                "banned": True,
                "message": ban_info['reason'],
                "remaining": ban_info.get('remaining_text', '')
            })
        
        if is_device_approved(device_name):
            update_device_last_login(device_name)
            return jsonify({"status": "valid", "type": "permanent", "note": "لا تنتهي أبداً"})
        
        c.execute("SELECT session_expires FROM active_sessions WHERE device_name = ?", (device_name,))
        row = c.fetchone()
        if row:
            if row[0] < int(time.time()):
                c.execute("DELETE FROM active_sessions WHERE device_name = ?", (device_name,))
                conn.commit()
                return jsonify({"status": "expired", "message": "انتهت صلاحية الجلسة"})
            return jsonify({"status": "valid", "type": "temp"})
        
        return jsonify({"status": "no_session"})
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/update_device_last_login', methods=['POST'])
def update_device_last_login_endpoint():
    """تحديث آخر تسجيل دخول للجهاز المعتمد"""
    try:
        data = request.json
        device_name = data.get('device_name', '')
        
        if not device_name:
            return jsonify({"success": False, "error": "device_name is required"}), 400
        
        if is_device_approved(device_name):
            update_device_last_login(device_name)
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Device not approved"}), 404
    
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/get_notifications/<device_name>', methods=['GET'])
def get_notifications(device_name):
    try:
        c.execute("""SELECT id, message, created_at, is_read 
                     FROM notifications WHERE device_name = ? OR device_name = 'all'
                     ORDER BY created_at DESC LIMIT 50""", (device_name,))
        rows = c.fetchall()
        
        notifications = []
        for row in rows:
            notifications.append({
                "id": row[0],
                "message": row[1],
                "created_at": row[2],
                "is_read": row[3]
            })
        
        return jsonify({"notifications": notifications})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/mark_notification_read/<int:notification_id>', methods=['POST'])
def mark_notification_read(notification_id):
    try:
        c.execute("UPDATE notifications SET is_read = 1 WHERE id = ?", (notification_id,))
        conn.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "bot": "running",
        "version": "8.0",
        "timestamp": int(time.time())
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
