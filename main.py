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
ADMIN_CHAT_ID = '8311254462'
app = Flask(__name__)
CORS(app)

# ===================== قاعدة البيانات =====================
conn = sqlite3.connect('tomb_bot.db', check_same_thread=False)
c = conn.cursor()

# جدول الطلبات
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

# جدول كلمات المرور
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

# جدول الجلسات النشطة
c.execute('''CREATE TABLE IF NOT EXISTS active_sessions
             (device_name TEXT PRIMARY KEY,
              username TEXT,
              session_expires INTEGER,
              session_type TEXT,
              created_at INTEGER)''')

# جدول الإشعارات
c.execute('''CREATE TABLE IF NOT EXISTS notifications
             (id INTEGER PRIMARY KEY AUTOINCREMENT,
              device_name TEXT,
              title TEXT,
              message TEXT,
              created_at INTEGER,
              is_read INTEGER DEFAULT 0)''')

conn.commit()

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
    c.execute("""INSERT INTO access_logs 
                 (username, device_name, ip_address, status, timestamp) 
                 VALUES (?, ?, ?, ?, ?)""",
              (username, device_name, ip_address, status, int(time.time())))
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
    c.execute("SELECT DISTINCT device_name, username FROM approvals ORDER BY timestamp DESC")
    for row in c.fetchall():
        device_name, username = row
        if device_name not in devices:
            devices[device_name] = username
    c.execute("SELECT device_name, username FROM active_devices")
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
    
    recent = c.execute("""SELECT username, device_name, status, timestamp 
                          FROM approvals ORDER BY timestamp DESC LIMIT 10""").fetchall()
    
    return {
        "total": total,
        "pending": pending,
        "approved": approved,
        "denied": denied,
        "banned": banned,
        "active": active,
        "recent": recent
    }

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
            InlineKeyboardButton("🔄 تحديث حالة الأجهزة", callback_data="menu_refresh_devices")
        ],
        [
            InlineKeyboardButton("🚪 تسجيل خروج جهاز", callback_data="menu_force_logout"),
            InlineKeyboardButton("🗑️ مسح الطلبات", callback_data="menu_clear_requests_options")
        ],
        [
            InlineKeyboardButton("📢 إرسال إشعار", callback_data="menu_send_notification"),
            InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="back_to_main")
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

def send_approval_request(request_id, app_name="Tomb", username="Unknown", 
                          device_name="Unknown", device_info="", ip_address="Unknown"):
    
    custom_logo = get_setting("custom_logo", CUSTOM_LOGO)
    welcome_msg = get_setting("welcome_message", WELCOME_MESSAGE)
    
    add_active_device(device_name, username, device_info)
    
    message_text = f"""
{custom_logo}

🔐 *{welcome_msg}*

👤 *المستخدم:* `{username}`
📱 *الجهاز:* `{device_name}`
ℹ️ *المعلومات:* `{device_info}`
🌐 *IP:* `{ip_address}`
🕐 *الوقت:* `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`

⚠️ *هل تسمح بالدخول؟*
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
    
    try:
        bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=message_text,
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    except Exception as e:
        print(f"Error sending message: {e}")
        bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=message_text.replace('*', '').replace('`', ''),
            reply_markup=reply_markup
        )
    
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
    text = "🔒 **اختر الجهاز المراد حظره:**\n\n"
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=telegram.ParseMode.MARKDOWN,
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
    text = "🔓 **اختر الجهاز لرفع الحظر عنه:**\n\n"
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=telegram.ParseMode.MARKDOWN,
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
    text = "🚪 **اختر الجهاز لتسجيل خروجه:**\n\n"
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=telegram.ParseMode.MARKDOWN,
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
    
    text = f"🔒 **حظر جهاز:** `{device_name}`\n👤 **المستخدم:** {username}\n\nاختر نوع الحظر:"
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=telegram.ParseMode.MARKDOWN,
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
    
    text = "🗑️ **مسح الطلبات**\n\nاختر نوع الطلبات التي تريد مسحها:"
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

def show_temp_password_duration_menu(chat_id, device_name, username, message_id=None):
    keyboard = [
        [InlineKeyboardButton("⏰ دقائق", callback_data=f"temp_duration_minutes_{device_name}")],
        [InlineKeyboardButton("🕐 ساعات", callback_data=f"temp_duration_hours_{device_name}")],
        [InlineKeyboardButton("📅 أيام", callback_data=f"temp_duration_days_{device_name}")],
        [InlineKeyboardButton("🔙 إلغاء", callback_data="menu_temp_password")]
    ]
    
    text = f"🔑 **كلمة مرور مؤقتة لجهاز:** `{device_name}`\n👤 **المستخدم:** {username}\n\nاختر وحدة المدة:"
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

def show_send_notification_menu(chat_id, message_id=None):
    keyboard = [
        [InlineKeyboardButton("📱 إشعار لجهاز محدد", callback_data="notify_single_device")],
        [InlineKeyboardButton("👥 إشعار للجميع", callback_data="notify_all_devices")],
        [InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="menu_settings")]
    ]
    
    text = "📢 **إرسال إشعارات**\n\nيمكنك إرسال إشعار لجهاز محدد أو لجميع الأجهزة النشطة.\n\nسيظهر الإشعار في لوحة الإشعارات وفي شاشة التطبيق."
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=telegram.ParseMode.MARKDOWN,
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
    text = "📢 **اختر الجهاز لإرسال إشعار له:**\n\n"
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

def show_welcome_template_menu(chat_id, message_id=None):
    current_template = get_setting("welcome_message_template", "مرحباً بك يا & في تطبيق tomb of Makrotik")
    
    text = f"""💬 **تغيير رسالة الترحيب**

📝 **الرسالة الحالية:**
`{current_template}`

💡 **ملاحظة:** استخدم الرمز `&` لوضع اسم المستخدم تلقائياً.

**أمثلة:**
• `مرحباً بك يا & في تطبيق TOMB`
• `حبيبي يا & في tomb of Makrotik`

📤 **أرسل رسالة الترحيب الجديدة الآن:**"""
    
    if message_id:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=telegram.ParseMode.MARKDOWN
        )
    else:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=telegram.ParseMode.MARKDOWN
        )
    context.user_data['waiting_for_welcome_template'] = True

def handle_callback(update, context):
    query = update.callback_query
    query.answer()
    
    data = query.data
    chat_id = query.message.chat_id
    message_id = query.message.message_id
    
    # ========== زر تحديث حالة الأجهزة ==========
    if data == "menu_refresh_devices":
        keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
        query.edit_message_text(
            text="✅ **تم تحديث حالة الأجهزة بنجاح!**\n\nسيتم إعادة التحقق من جميع الأجهزة النشطة.",
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    # ========== زر تسجيل خروج جهاز ==========
    if data == "menu_force_logout":
        show_device_list_for_logout(chat_id, message_id)
        return
    
    if data.startswith("force_logout_"):
        device_name = data[13:]
        c.execute("DELETE FROM active_sessions WHERE device_name = ?", (device_name,))
        conn.commit()
        
        keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
        query.edit_message_text(
            text=f"✅ **تم تسجيل خروج الجهاز بنجاح!**\n\n📱 {device_name}\n\nسيتم إرجاع المستخدم إلى شاشة تسجيل الدخول.",
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    # ========== اختيار جهاز للحظر ==========
    if data.startswith("select_ban_"):
        device_name = data[11:]
        c.execute("SELECT username FROM approvals WHERE device_name = ? ORDER BY timestamp DESC LIMIT 1", (device_name,))
        row = c.fetchone()
        username = row[0] if row else "Unknown"
        show_ban_type_menu(chat_id, device_name, username, message_id)
        return
    
    # ========== اختيار نوع الحظر ==========
    if data.startswith("ban_type_minutes_"):
        device_name = data[17:]
        context.user_data['ban_device'] = device_name
        context.user_data['ban_type'] = "minutes"
        query.edit_message_text(
            text=f"🔒 **حظر جهاز:** `{device_name}`\n\n⏰ أدخل عدد الدقائق (1-59):",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_ban_duration'] = True
        return
    
    if data.startswith("ban_type_hours_"):
        device_name = data[15:]
        context.user_data['ban_device'] = device_name
        context.user_data['ban_type'] = "hours"
        query.edit_message_text(
            text=f"🔒 **حظر جهاز:** `{device_name}`\n\n🕐 أدخل عدد الساعات (1-23):",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_ban_duration'] = True
        return
    
    if data.startswith("ban_type_days_"):
        device_name = data[14:]
        context.user_data['ban_device'] = device_name
        context.user_data['ban_type'] = "days"
        query.edit_message_text(
            text=f"🔒 **حظر جهاز:** `{device_name}`\n\n📅 أدخل عدد الأيام (1-365):",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_ban_duration'] = True
        return
    
    # ========== تأكيد الحظر الدائم ==========
    if data.startswith("ban_confirm_permanent_"):
        device_name = data[22:]
        c.execute("SELECT username FROM approvals WHERE device_name = ? ORDER BY timestamp DESC LIMIT 1", (device_name,))
        row = c.fetchone()
        username = row[0] if row else "Unknown"
        
        ban_device(device_name, username, "permanent", 0, "حظر دائم من قبل المطور")
        
        keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
        query.edit_message_text(
            text=f"✅ **تم حظر الجهاز بنجاح!**\n\n📱 {device_name}\n🔒 حظر دائم",
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    # ========== اختيار جهاز لرفع الحظر ==========
    if data.startswith("select_unban_"):
        device_name = data[13:]
        unban_device(device_name)
        
        keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
        query.edit_message_text(
            text=f"✅ **تم رفع الحظر عن الجهاز بنجاح!**\n\n📱 {device_name}",
            parse_mode=telegram.ParseMode.MARKDOWN,
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
        keyboard = [
            [InlineKeyboardButton("🔑 إنشاء كلمة مرور مؤقتة", callback_data="create_temp_password")],
            [InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]
        ]
        query.edit_message_text(
            "🔑 **كلمات المرور المؤقتة**\n\nيمكنك إنشاء كلمة مرور مؤقتة لجهاز معين.\n\nسيتم إنشاء كلمة مرور صالحة للمدة التي تختارها.\n⚠️ **ملاحظة:** بعد انتهاء الصلاحية سيتم إرجاع المستخدم لشاشة تسجيل الدخول.",
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    if data == "create_temp_password":
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
            "🔑 **اختر الجهاز لإنشاء كلمة مرور مؤقتة له:**\n\nبعد الاختيار ستختار مدة الصلاحية (دقائق/ساعات/أيام)",
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    if data.startswith("temp_select_"):
        device_name = data[12:]
        c.execute("SELECT username FROM approvals WHERE device_name = ? ORDER BY timestamp DESC LIMIT 1", (device_name,))
        row = c.fetchone()
        username = row[0] if row else "Unknown"
        show_temp_password_duration_menu(chat_id, device_name, username, message_id)
        return
    
    if data.startswith("temp_duration_minutes_"):
        device_name = data[21:]
        context.user_data['temp_device'] = device_name
        context.user_data['temp_unit'] = "minutes"
        query.edit_message_text(
            text=f"🔑 **كلمة مرور مؤقتة لجهاز:** `{device_name}`\n\n⏰ أدخل عدد الدقائق (1-59):",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_temp_duration'] = True
        return
    
    if data.startswith("temp_duration_hours_"):
        device_name = data[19:]
        context.user_data['temp_device'] = device_name
        context.user_data['temp_unit'] = "hours"
        query.edit_message_text(
            text=f"🔑 **كلمة مرور مؤقتة لجهاز:** `{device_name}`\n\n🕐 أدخل عدد الساعات (1-23):",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_temp_duration'] = True
        return
    
    if data.startswith("temp_duration_days_"):
        device_name = data[18:]
        context.user_data['temp_device'] = device_name
        context.user_data['temp_unit'] = "days"
        query.edit_message_text(
            text=f"🔑 **كلمة مرور مؤقتة لجهاز:** `{device_name}`\n\n📅 أدخل عدد الأيام (1-365):",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_temp_duration'] = True
        return
    
    # ========== إرسال الإشعارات ==========
    if data == "menu_send_notification":
        show_send_notification_menu(chat_id, message_id)
        return
    
    if data == "notify_single_device":
        show_devices_for_notification(chat_id, message_id)
        return
    
    if data == "notify_all_devices":
        context.user_data['waiting_for_broadcast_title'] = True
        query.edit_message_text(
            text="📢 **إرسال إشعار للجميع**\n\nأرسل عنوان الإشعار أولاً:",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        return
    
    if data.startswith("notify_device_"):
        device_name = data[14:]
        context.user_data['notify_device'] = device_name
        context.user_data['waiting_for_notification_title'] = True
        query.edit_message_text(
            text=f"📢 **إرسال إشعار للجهاز:** `{device_name}`\n\nأرسل عنوان الإشعار أولاً:",
            parse_mode=telegram.ParseMode.MARKDOWN
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
    
    if data == "menu_pending":
        pending_reqs = c.execute(
            "SELECT request_id, username, device_name, timestamp FROM approvals WHERE status='pending' ORDER BY timestamp DESC"
        ).fetchall()
        
        if pending_reqs:
            text_msg = "⏳ *الطلبات المعلقة:*\n\n"
            for req in pending_reqs:
                time_str = datetime.fromtimestamp(req[3]).strftime('%H:%M:%S')
                text_msg += f"🆔 `{req[0][:8]}` - {req[1]} - {req[2]} - {time_str}\n"
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text=text_msg,
                parse_mode=telegram.ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text="✅ لا توجد طلبات معلقة",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return
    
    elif data == "menu_stats":
        stats = get_access_stats()
        text_msg = f"""
📊 *إحصائيات النظام*

📝 *إجمالي الطلبات:* {stats['total']}
⏳ *قيد الانتظار:* {stats['pending']}
✅ *تمت الموافقة:* {stats['approved']}
❌ *تم الرفض:* {stats['denied']}
🚫 *الأجهزة المحظورة:* {stats['banned']}
📱 *الأجهزة النشطة:* {stats['active']}

🔄 *آخر تحديث:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
        query.edit_message_text(
            text=text_msg,
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    elif data == "menu_approved":
        approved_reqs = c.execute(
            "SELECT username, device_name, timestamp FROM approvals WHERE status='approved' ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
        
        if approved_reqs:
            text_msg = "✅ *الطلبات المقبولة:*\n\n"
            for req in approved_reqs:
                time_str = datetime.fromtimestamp(req[2]).strftime('%Y-%m-%d %H:%M')
                text_msg += f"👤 {req[0]} - {req[1]} - {time_str}\n"
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text=text_msg,
                parse_mode=telegram.ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text="📭 لا توجد طلبات مقبولة",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return
    
    elif data == "menu_denied":
        denied_reqs = c.execute(
            "SELECT username, device_name, timestamp FROM approvals WHERE status='denied' ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
        
        if denied_reqs:
            text_msg = "❌ *الطلبات المرفوضة:*\n\n"
            for req in denied_reqs:
                time_str = datetime.fromtimestamp(req[2]).strftime('%Y-%m-%d %H:%M')
                text_msg += f"👤 {req[0]} - {req[1]} - {time_str}\n"
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text=text_msg,
                parse_mode=telegram.ParseMode.MARKDOWN,
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
            log_text = "📋 *سجل الدخول الأخير:*\n\n"
            for log in logs:
                time_str = datetime.fromtimestamp(log[3]).strftime('%Y-%m-%d %H:%M')
                emoji = "✅" if log[2] == "approved" else "❌" if log[2] == "denied" else "📱" if log[2] == "app_opened" else "⏳"
                log_text += f"{emoji} {log[0]} - {log[1]} - {time_str}\n"
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text=log_text,
                parse_mode=telegram.ParseMode.MARKDOWN,
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
            text_msg = "📱 *الأجهزة النشطة:*\n\n"
            for dev in devices:
                device_name, username, device_info, last_active, first_active, total_requests = dev
                last_active_str = datetime.fromtimestamp(last_active).strftime('%Y-%m-%d %H:%M')
                text_msg += f"📱 **{device_name}**\n   👤 {username}\n   🕐 آخر ظهور: {last_active_str}\n   📊 عدد الطلبات: {total_requests}\n\n"
            keyboard = [
                [InlineKeyboardButton("🔒 حظر جهاز", callback_data="menu_ban_device_list")],
                [InlineKeyboardButton("🚪 تسجيل خروج", callback_data="menu_force_logout")],
                [InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]
            ]
            query.edit_message_text(
                text=text_msg,
                parse_mode=telegram.ParseMode.MARKDOWN,
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
            text_msg = "🚫 *الأجهزة المحظورة:*\n\n"
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
                text_msg += f"📱 **{device_name}**\n   👤 {username}\n   🗓️ حظر في: {banned_at_str}\n   ⏱️ {expiry}\n   📝 السبب: {reason}\n\n"
            keyboard = [
                [InlineKeyboardButton("🔓 رفع حظر", callback_data="menu_unban_device_list")],
                [InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]
            ]
            query.edit_message_text(
                text=text_msg,
                parse_mode=telegram.ParseMode.MARKDOWN,
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
⚙️ *الإعدادات الحالية*

🏷️ *الشعار:* 
{logo[:50]}...

💬 *رسالة الترحيب:* 
{welcome_template[:50]}...

🔑 *كلمة المرور:* {'●' * 8}
"""
        keyboard = [
            [InlineKeyboardButton("📢 إرسال إشعارات", callback_data="menu_send_notification")],
            [InlineKeyboardButton("💬 تغيير رسالة الترحيب", callback_data="change_welcome_template")],
            [InlineKeyboardButton("🔑 تغيير كلمة المرور", callback_data="change_password")],
            [InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]
        ]
        query.edit_message_text(
            text=text_msg,
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    elif data == "change_password":
        query.edit_message_text(
            "🔑 **تغيير كلمة المرور**\n\nأرسل كلمة المرور الجديدة (4 أحرف على الأقل):",
            parse_mode=telegram.ParseMode.MARKDOWN
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
            session_expires = int(time.time()) + 86400
            c.execute("""INSERT OR REPLACE INTO active_sessions 
                         (device_name, username, session_expires, session_type, created_at) 
                         VALUES (?, ?, ?, ?, ?)""",
                      (device_name, username, session_expires, "normal", int(time.time())))
            conn.commit()
            log_access(username, device_name, ip_address, "approved")
        
        try:
            query.edit_message_text(
                text=f"✅ **تمت الموافقة بنجاح**\n\nيمكن للمستخدم الآن الدخول إلى التطبيق.\n\n{get_setting('custom_logo', CUSTOM_LOGO)}",
                parse_mode=telegram.ParseMode.MARKDOWN
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
                text=f"❌ **تم رفض الطلب**\n\nلم يتم السماح للمستخدم بالدخول.\n\n{get_setting('custom_logo', CUSTOM_LOGO)}",
                parse_mode=telegram.ParseMode.MARKDOWN
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
📱 *معلومات الجهاز*

👤 *المستخدم:* {row[0]}
📱 *اسم الجهاز:* {row[1]}
ℹ️ *تفاصيل:* {row[2]}
🌐 *IP:* {row[3]}
🕐 *الوقت:* {datetime.fromtimestamp(row[4]).strftime('%Y-%m-%d %H:%M:%S')}
"""
            keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")]]
            query.edit_message_text(
                text=info_text,
                parse_mode=telegram.ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            query.edit_message_text("❌ لم يتم العثور على الطلب")
        return

def handle_message(update, context):
    message = update.message
    chat_id = message.chat_id
    text = message.text.strip()
    
    if str(chat_id) != str(ADMIN_CHAT_ID):
        bot.send_message(chat_id=chat_id, text="⚠️ أنت غير مصرح لك باستخدام هذا البوت")
        return
    
    # معالجة إدخال مدة الحظر
    if context.user_data.get('waiting_for_ban_duration'):
        try:
            duration = int(text)
            ban_type = context.user_data.get('ban_type')
            device_name = context.user_data.get('ban_device')
            
            if ban_type == "minutes" and (duration < 1 or duration > 59):
                bot.send_message(chat_id=chat_id, text="❌ عدد الدقائق يجب أن يكون بين 1 و 59")
                context.user_data.pop('waiting_for_ban_duration', None)
                context.user_data.pop('ban_device', None)
                context.user_data.pop('ban_type', None)
                send_main_menu(chat_id)
                return
            elif ban_type == "hours" and (duration < 1 or duration > 23):
                bot.send_message(chat_id=chat_id, text="❌ عدد الساعات يجب أن يكون بين 1 و 23")
                context.user_data.pop('waiting_for_ban_duration', None)
                context.user_data.pop('ban_device', None)
                context.user_data.pop('ban_type', None)
                send_main_menu(chat_id)
                return
            elif ban_type == "days" and (duration < 1 or duration > 365):
                bot.send_message(chat_id=chat_id, text="❌ عدد الأيام يجب أن يكون بين 1 و 365")
                context.user_data.pop('waiting_for_ban_duration', None)
                context.user_data.pop('ban_device', None)
                context.user_data.pop('ban_type', None)
                send_main_menu(chat_id)
                return
            
            c.execute("SELECT username FROM approvals WHERE device_name = ? ORDER BY timestamp DESC LIMIT 1", (device_name,))
            row = c.fetchone()
            username = row[0] if row else "Unknown"
            
            if ban_type == "minutes":
                ban_device(device_name, username, "minutes", duration, f"حظر لمدة {duration} دقيقة")
                time_text = f"{duration} دقيقة"
            elif ban_type == "hours":
                ban_device(device_name, username, "hours", duration, f"حظر لمدة {duration} ساعة")
                time_text = f"{duration} ساعة"
            elif ban_type == "days":
                ban_device(device_name, username, "days", duration, f"حظر لمدة {duration} يوم")
                time_text = f"{duration} يوم"
            else:
                ban_device(device_name, username, "permanent", 0, "حظر دائم")
                time_text = "دائم"
            
            bot.send_message(
                chat_id=chat_id,
                text=f"✅ **تم حظر الجهاز بنجاح!**\n\n📱 {device_name}\n⏰ مدة الحظر: {time_text}",
                parse_mode=telegram.ParseMode.MARKDOWN
            )
            
        except ValueError:
            bot.send_message(chat_id=chat_id, text="❌ الرجاء إدخال رقم صحيح")
        
        context.user_data.pop('waiting_for_ban_duration', None)
        context.user_data.pop('ban_device', None)
        context.user_data.pop('ban_type', None)
        send_main_menu(chat_id)
        return
    
    # معالجة مدة كلمة المرور المؤقتة
    if context.user_data.get('waiting_for_temp_duration'):
        try:
            duration = int(text)
            unit = context.user_data.get('temp_unit')
            device_name = context.user_data.get('temp_device')
            
            if unit == "minutes" and (duration < 1 or duration > 59):
                bot.send_message(chat_id=chat_id, text="❌ عدد الدقائق يجب أن يكون بين 1 و 59")
            elif unit == "hours" and (duration < 1 or duration > 23):
                bot.send_message(chat_id=chat_id, text="❌ عدد الساعات يجب أن يكون بين 1 و 23")
            elif unit == "days" and (duration < 1 or duration > 365):
                bot.send_message(chat_id=chat_id, text="❌ عدد الأيام يجب أن يكون بين 1 و 365")
            else:
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
                
                temp_password = hashlib.md5(f"{device_name}{time.time()}".encode()).hexdigest()[:8]
                temp_hash = hashlib.sha256(temp_password.encode()).hexdigest()
                
                c.execute("""INSERT INTO temp_passwords (password_hash, device_name, created_at, expires_at, used) 
                             VALUES (?, ?, ?, ?, 0)""",
                          (temp_hash, device_name, now, expires_at))
                conn.commit()
                
                c.execute("""INSERT OR REPLACE INTO active_sessions 
                             (device_name, username, session_expires, session_type, created_at) 
                             VALUES (?, ?, ?, ?, ?)""",
                          (device_name, "TEMP_USER", expires_at, "temp", now))
                conn.commit()
                
                bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ **تم إنشاء كلمة مرور مؤقتة!**\n\n📱 الجهاز: `{device_name}`\n🔑 كلمة المرور: `{temp_password}`\n⏰ المدة: {time_text}\n\n⚠️ بعد انتهاء المدة سيتم إرجاع المستخدم لشاشة تسجيل الدخول",
                    parse_mode=telegram.ParseMode.MARKDOWN
                )
        except ValueError:
            bot.send_message(chat_id=chat_id, text="❌ الرجاء إدخال رقم صحيح")
        
        context.user_data.pop('waiting_for_temp_duration', None)
        context.user_data.pop('temp_device', None)
        context.user_data.pop('temp_unit', None)
        send_main_menu(chat_id)
        return
    
    # معالجة تغيير رسالة الترحيب
    if context.user_data.get('waiting_for_welcome_template'):
        new_template = text.strip()
        if '&' in new_template:
            set_setting("welcome_message_template", new_template)
            bot.send_message(
                chat_id=chat_id,
                text=f"✅ **تم تغيير رسالة الترحيب بنجاح!**\n\nالرسالة الجديدة: `{new_template}`\n\n(سيتم استبدال & باسم المستخدم)",
                parse_mode=telegram.ParseMode.MARKDOWN
            )
        else:
            bot.send_message(
                chat_id=chat_id,
                text="❌ **الرسالة يجب أن تحتوي على الرمز `&`**\n\nمثال: `مرحباً بك يا & في تطبيق TOMB`",
                parse_mode=telegram.ParseMode.MARKDOWN
            )
        context.user_data.pop('waiting_for_welcome_template')
        send_main_menu(chat_id)
        return
    
    # معالجة إرسال إشعار لجهاز محدد
    if context.user_data.get('waiting_for_notification_title'):
        context.user_data['notification_title'] = text
        context.user_data['waiting_for_notification_title'] = False
        context.user_data['waiting_for_notification_message'] = True
        bot.send_message(
            chat_id=chat_id,
            text="📝 **أرسل نص الإشعار الآن:**",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        return
    
    if context.user_data.get('waiting_for_notification_message'):
        title = context.user_data.get('notification_title', 'تطبيق TOMB')
        message = text
        device_name = context.user_data.get('notify_device')
        
        c.execute("""INSERT INTO notifications (device_name, title, message, created_at, is_read)
                     VALUES (?, ?, ?, ?, 0)""",
                  (device_name, title, message, int(time.time())))
        conn.commit()
        
        bot.send_message(
            chat_id=chat_id,
            text=f"✅ **تم إرسال الإشعار بنجاح!**\n\n📱 الجهاز: `{device_name}`\n📌 العنوان: {title}\n💬 الرسالة: {message}",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        
        context.user_data.pop('waiting_for_notification_title', None)
        context.user_data.pop('waiting_for_notification_message', None)
        context.user_data.pop('notification_title', None)
        context.user_data.pop('notify_device', None)
        send_main_menu(chat_id)
        return
    
    # معالجة إرسال إشعار للجميع
    if context.user_data.get('waiting_for_broadcast_title'):
        context.user_data['broadcast_title'] = text
        context.user_data['waiting_for_broadcast_title'] = False
        context.user_data['waiting_for_broadcast_message'] = True
        bot.send_message(
            chat_id=chat_id,
            text="📝 **أرسل نص الإشعار للجميع الآن:**",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        return
    
    if context.user_data.get('waiting_for_broadcast_message'):
        title = context.user_data.get('broadcast_title', 'تطبيق TOMB')
        message = text
        
        devices = get_active_devices()
        for device in devices:
            device_name = device[0]
            c.execute("""INSERT INTO notifications (device_name, title, message, created_at, is_read)
                         VALUES (?, ?, ?, ?, 0)""",
                      (device_name, title, message, int(time.time())))
        conn.commit()
        
        bot.send_message(
            chat_id=chat_id,
            text=f"✅ **تم إرسال الإشعار للجميع بنجاح!**\n\n📌 العنوان: {title}\n💬 الرسالة: {message}\n📱 عدد الأجهزة: {len(devices)}",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        
        context.user_data.pop('waiting_for_broadcast_title', None)
        context.user_data.pop('waiting_for_broadcast_message', None)
        context.user_data.pop('broadcast_title', None)
        send_main_menu(chat_id)
        return
    
    # معالجة تغيير كلمة المرور
    if context.user_data.get('waiting_for_new_password'):
        new_password = text.strip()
        if len(new_password) >= 4:
            if update_password(new_password, "bot"):
                bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ تم تغيير كلمة مرور التطبيق بنجاح!\n\n🔑 كلمة المرور الجديدة: `{new_password}`",
                    parse_mode=telegram.ParseMode.MARKDOWN
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

# ===================== API للتطبيق =====================
@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "status": "online",
        "service": "Tomb Bot Protection System",
        "version": "5.0",
        "message": "API is working! (Advanced ban system with temp passwords)",
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
            "/get_welcome_template - GET",
            "/get_notifications/<device_name> - GET",
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
        
        # التحقق من كلمة المرور المؤقتة
        temp_password = data.get('temp_password')
        if temp_password:
            temp_hash = hashlib.sha256(temp_password.encode()).hexdigest()
            c.execute("""SELECT id, device_name, expires_at FROM temp_passwords 
                         WHERE password_hash = ? AND used = 0 AND expires_at > ?""",
                      (temp_hash, int(time.time())))
            row = c.fetchone()
            if row:
                temp_id, temp_device, expires_at = row
                c.execute("UPDATE temp_passwords SET used = 1 WHERE id = ?", (temp_id,))
                conn.commit()
                # إنشاء جلسة مؤقتة
                c.execute("""INSERT OR REPLACE INTO active_sessions 
                             (device_name, username, session_expires, session_type, created_at) 
                             VALUES (?, ?, ?, ?, ?)""",
                          (device_name, username, expires_at, "temp", int(time.time())))
                conn.commit()
                return jsonify({
                    "status": "approved", 
                    "message": "تم الدخول عبر كلمة مرور مؤقتة",
                    "expires_at": expires_at,
                    "session_type": "temp"
                })
            else:
                # التحقق من كلمة المرور العادية
                if check_password(temp_password):
                    # إنشاء جلسة عادية
                    session_expires = int(time.time()) + 86400
                    c.execute("""INSERT OR REPLACE INTO active_sessions 
                                 (device_name, username, session_expires, session_type, created_at) 
                                 VALUES (?, ?, ?, ?, ?)""",
                              (device_name, username, session_expires, "normal", int(time.time())))
                    conn.commit()
                    return jsonify({
                        "status": "approved",
                        "message": "تم الدخول عبر كلمة المرور الرئيسية",
                        "expires_at": session_expires,
                        "session_type": "normal"
                    })
                else:
                    return jsonify({"status": "temp_expired", "message": "كلمة المرور غير صحيحة"})
        
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
        
        if check_password(password):
            return jsonify({"valid": True})
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
        c.execute("""SELECT id, expires_at FROM temp_passwords 
                     WHERE password_hash = ? AND device_name = ? AND used = 0 AND expires_at > ?""",
                  (temp_hash, device_name, int(time.time())))
        row = c.fetchone()
        
        if row:
            c.execute("UPDATE temp_passwords SET used = 1 WHERE id = ?", (row[0],))
            conn.commit()
            return jsonify({"valid": True, "expires_at": row[1]})
        
        return jsonify({"valid": False})
    
    except Exception as e:
        return jsonify({"valid": False, "error": str(e)}), 500

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
            return jsonify({"success": True})
        
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
        
        message = f"""
📱 *جهاز فتح التطبيق* 📱

📱 *الجهاز:* `{device_name}`
ℹ️ *المعلومات:* `{device_info}`
🌐 *IP:* `{ip_address}`
🕐 *الوقت:* `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`

✅ هذا مجرد إشعار بفتح التطبيق، ليس طلب موافقة
"""
        bot.send_message(chat_id=ADMIN_CHAT_ID, text=message, parse_mode=telegram.ParseMode.MARKDOWN)
        
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
        
        c.execute("SELECT session_expires, session_type FROM active_sessions WHERE device_name = ?", (device_name,))
        row = c.fetchone()
        
        if row:
            session_expires, session_type = row
            if session_expires > int(time.time()):
                remaining_seconds = session_expires - int(time.time())
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
                return jsonify({"valid": False, "reason": "session_expired"})
        
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
            conn.commit()
            return jsonify({
                "status": "banned",
                "banned": True,
                "message": ban_info['reason'],
                "remaining": ban_info.get('remaining_text', '')
            })
        
        c.execute("SELECT session_expires FROM active_sessions WHERE device_name = ?", (device_name,))
        row = c.fetchone()
        if row:
            if row[0] < int(time.time()):
                c.execute("DELETE FROM active_sessions WHERE device_name = ?", (device_name,))
                conn.commit()
                return jsonify({"status": "expired", "message": "انتهت صلاحية الجلسة"})
            return jsonify({"status": "valid"})
        
        return jsonify({"status": "no_session"})
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/get_notifications/<device_name>', methods=['GET'])
def get_notifications(device_name):
    try:
        c.execute("""SELECT id, title, message, created_at, is_read 
                     FROM notifications WHERE device_name = ? OR device_name = 'all'
                     ORDER BY created_at DESC LIMIT 50""", (device_name,))
        rows = c.fetchall()
        
        notifications = []
        for row in rows:
            notifications.append({
                "id": row[0],
                "title": row[1],
                "message": row[2],
                "created_at": row[3],
                "is_read": row[4]
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
        "version": "5.0",
        "timestamp": int(time.time())
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
