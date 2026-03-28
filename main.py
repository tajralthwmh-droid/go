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
BOT_TOKEN = '8618535073:AAEK5fucW34Ir2oQg1LLHKDqiNv9K0Qvfjs'
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
    """التحقق من كلمة المرور"""
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

# ===================== دوال إدارة الأجهزة =====================
def add_active_device(device_name, username, device_info):
    """إضافة جهاز إلى قائمة الأجهزة النشطة"""
    now = int(time.time())
    c.execute("""INSERT OR REPLACE INTO active_devices 
                 (device_name, username, device_info, last_active, first_active, total_requests) 
                 VALUES (?, ?, ?, ?, 
                         COALESCE((SELECT first_active FROM active_devices WHERE device_name = ?), ?),
                         COALESCE((SELECT total_requests FROM active_devices WHERE device_name = ?), 0) + 1)""",
              (device_name, username, device_info, now, device_name, now, device_name))
    conn.commit()

def remove_active_device(device_name):
    """إزالة جهاز من قائمة الأجهزة النشطة"""
    c.execute("DELETE FROM active_devices WHERE device_name = ?", (device_name,))
    conn.commit()

def get_active_devices():
    """الحصول على قائمة الأجهزة النشطة"""
    c.execute("SELECT device_name, username, device_info, last_active, first_active, total_requests FROM active_devices ORDER BY last_active DESC")
    return c.fetchall()

def get_all_known_devices():
    """الحصول على جميع الأجهزة المعروفة"""
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

# ===================== دوال الحظر =====================
def ban_device(device_name, username, ban_type="permanent", days=0, reason="محظور من قبل المطور"):
    """حظر جهاز"""
    now = int(time.time())
    banned_until = now + (days * 86400) if ban_type == "temporary" else 0
    c.execute("""INSERT OR REPLACE INTO banned_devices 
                 (device_name, username, banned_at, banned_until, ban_type, reason) 
                 VALUES (?, ?, ?, ?, ?, ?)""",
              (device_name, username, now, banned_until, ban_type, reason))
    conn.commit()
    remove_active_device(device_name)

def unban_device(device_name):
    """رفع الحظر عن جهاز"""
    c.execute("DELETE FROM banned_devices WHERE device_name = ?", (device_name,))
    conn.commit()

def is_device_banned(device_name):
    """التحقق من حظر الجهاز"""
    c.execute("SELECT banned_until, ban_type FROM banned_devices WHERE device_name = ?", (device_name,))
    row = c.fetchone()
    if row:
        banned_until, ban_type = row
        if ban_type == "temporary" and banned_until < int(time.time()):
            unban_device(device_name)
            return False
        return True
    return False

def get_banned_devices():
    """الحصول على قائمة الأجهزة المحظورة"""
    c.execute("SELECT device_name, username, banned_at, banned_until, ban_type, reason FROM banned_devices ORDER BY banned_at DESC")
    return c.fetchall()

def get_device_ban_info(device_name):
    """الحصول على معلومات حظر جهاز"""
    c.execute("SELECT banned_until, ban_type, reason FROM banned_devices WHERE device_name = ?", (device_name,))
    row = c.fetchone()
    if row:
        banned_until, ban_type, reason = row
        remaining = 0
        if ban_type == "temporary" and banned_until > int(time.time()):
            remaining = (banned_until - int(time.time())) // 86400
        return {
            "is_banned": True,
            "ban_type": ban_type,
            "reason": reason,
            "remaining_days": remaining,
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

def send_main_menu(chat_id):
    """إرسال القائمة الرئيسية بالأزرار"""
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
            InlineKeyboardButton("🗑️ مسح الطلبات", callback_data="menu_clear_requests")
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
    """عرض قائمة الأجهزة للحظر"""
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
    """عرض قائمة الأجهزة لرفع الحظر"""
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
        ban_info = "🔒 دائم" if ban_type == "permanent" else "⏰ مؤقت"
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

def handle_callback(update, context):
    query = update.callback_query
    query.answer()
    
    data = query.data
    chat_id = query.message.chat_id
    message_id = query.message.message_id
    
    # ========== اختيار جهاز للحظر ==========
    if data.startswith("select_ban_"):
        device_name = data[11:]
        c.execute("SELECT username FROM approvals WHERE device_name = ? ORDER BY timestamp DESC LIMIT 1", (device_name,))
        row = c.fetchone()
        username = row[0] if row else "Unknown"
        
        keyboard = [
            [InlineKeyboardButton("🔒 حظر دائم", callback_data=f"ban_confirm_permanent_{device_name}")],
            [InlineKeyboardButton("⏰ حظر مؤقت (يوم)", callback_data=f"ban_confirm_temporary_1_{device_name}")],
            [InlineKeyboardButton("⏰ حظر مؤقت (3 أيام)", callback_data=f"ban_confirm_temporary_3_{device_name}")],
            [InlineKeyboardButton("⏰ حظر مؤقت (7 أيام)", callback_data=f"ban_confirm_temporary_7_{device_name}")],
            [InlineKeyboardButton("🔙 إلغاء", callback_data="menu_ban_device_list")]
        ]
        
        query.edit_message_text(
            text=f"🔒 **حظر جهاز:** `{device_name}`\n👤 **المستخدم:** {username}\n\nاختر نوع الحظر:",
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    # ========== تأكيد الحظر ==========
    if data.startswith("ban_confirm_"):
        parts = data.split("_")
        if len(parts) >= 4:
            ban_type = parts[2]
            if ban_type == "permanent":
                days = 0
                device_name = "_".join(parts[3:])
            else:
                days = int(parts[3])
                device_name = "_".join(parts[4:])
            
            c.execute("SELECT username FROM approvals WHERE device_name = ? ORDER BY timestamp DESC LIMIT 1", (device_name,))
            row = c.fetchone()
            username = row[0] if row else "Unknown"
            
            ban_device(device_name, username, "temporary" if ban_type != "permanent" else "permanent", days, "محظور من قبل المطور")
            
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text=f"✅ **تم حظر الجهاز بنجاح!**\n\n📱 {device_name}\n{'🔒 حظر دائم' if ban_type == 'permanent' else f'⏰ مدة الحظر: {days} يوم'}",
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
                emoji = "✅" if log[2] == "approved" else "❌"
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
                if ban_type == "temporary" and banned_until > 0:
                    remaining = (banned_until - int(time.time())) // 86400
                    expiry = f"⏰ متبقي {remaining} يوم" if remaining > 0 else "⏰ ينتهي اليوم"
                else:
                    expiry = "🔒 دائم"
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
    
    elif data == "menu_clear_requests":
        c.execute("DELETE FROM approvals WHERE status != 'pending'")
        conn.commit()
        keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
        query.edit_message_text(
            text="🗑️ تم مسح جميع الطلبات المنتهية بنجاح",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    elif data == "menu_settings":
        logo = get_setting("custom_logo", CUSTOM_LOGO)
        welcome = get_setting("welcome_message", WELCOME_MESSAGE)
        
        text_msg = f"""
⚙️ *الإعدادات الحالية*

🏷️ *الشعار:* 
{logo[:50]}...

📝 *رسالة الترحيب:* 
{welcome[:50]}...

🔑 *كلمة المرور:* {'●' * 8}
"""
        keyboard = [
            [InlineKeyboardButton("📝 تغيير الشعار", callback_data="change_logo")],
            [InlineKeyboardButton("💬 تغيير رسالة الترحيب", callback_data="change_welcome")],
            [InlineKeyboardButton("🔑 تغيير كلمة المرور", callback_data="change_password")],
            [InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]
        ]
        query.edit_message_text(
            text=text_msg,
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    elif data == "change_logo":
        query.edit_message_text(
            "📝 **تغيير الشعار**\n\nأرسل الشعار الجديد:",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_logo'] = True
        return
    
    elif data == "change_welcome":
        query.edit_message_text(
            "💬 **تغيير رسالة الترحيب**\n\nأرسل الرسالة الجديدة:",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_welcome'] = True
        return
    
    elif data == "change_password":
        query.edit_message_text(
            "🔑 **تغيير كلمة المرور**\n\nأرسل كلمة المرور الجديدة (4 أحرف على الأقل):",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_new_password'] = True
        return
    
    elif data == "menu_temp_password":
        keyboard = [
            [InlineKeyboardButton("🔑 إنشاء كلمة مرور مؤقتة", callback_data="create_temp_password")],
            [InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]
        ]
        query.edit_message_text(
            "🔑 **كلمات المرور المؤقتة**\n\nيمكنك إنشاء كلمة مرور مؤقتة لجهاز معين.\n\nسيتم إنشاء كلمة مرور صالحة لمدة 24 ساعة.",
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    elif data == "create_temp_password":
        devices = get_all_known_devices()
        if not devices:
            query.edit_message_text("📭 لا توجد أجهزة معروفة")
            return
        
        keyboard = []
        for device_name, username in list(devices.items())[:20]:
            keyboard.append([InlineKeyboardButton(f"📱 {device_name} (👤 {username})", callback_data=f"temp_pass_{device_name}")])
        keyboard.append([InlineKeyboardButton("🔙 العودة", callback_data="menu_temp_password")])
        
        query.edit_message_text(
            "🔑 **اختر الجهاز لإنشاء كلمة مرور مؤقتة له:**",
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    elif data.startswith("temp_pass_"):
        device_name = data[9:]
        
        temp_password = hashlib.md5(f"{device_name}{time.time()}".encode()).hexdigest()[:8]
        temp_hash = hashlib.sha256(temp_password.encode()).hexdigest()
        now = int(time.time())
        expires_at = now + 86400
        
        c.execute("""INSERT INTO temp_passwords (password_hash, device_name, created_at, expires_at) 
                     VALUES (?, ?, ?, ?)""",
                  (temp_hash, device_name, now, expires_at))
        conn.commit()
        
        keyboard = [[InlineKeyboardButton("🔙 العودة", callback_data="menu_temp_password")]]
        query.edit_message_text(
            f"✅ **تم إنشاء كلمة مرور مؤقتة للجهاز:** `{device_name}`\n\n🔑 **كلمة المرور:** `{temp_password}`\n⏰ **صالحة لمدة:** 24 ساعة\n\n📝 يمكنك مشاركة هذه الكلمة مع المستخدم",
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
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
        
        if request_id in pending_requests:
            pending_requests[request_id]["status"] = status
        
        c.execute("UPDATE approvals SET status = ? WHERE request_id = ?", (status, request_id))
        conn.commit()
        
        c.execute("SELECT username, device_name, ip_address FROM approvals WHERE request_id = ?", (request_id,))
        row = c.fetchone()
        if row:
            log_access(row[0], row[1], row[2], "approved")
        
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
        
        if request_id in pending_requests:
            pending_requests[request_id]["status"] = status
        
        c.execute("UPDATE approvals SET status = ? WHERE request_id = ?", (status, request_id))
        conn.commit()
        
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
            
            keyboard = [
                [InlineKeyboardButton("🔒 حظر دائم", callback_data=f"ban_confirm_permanent_{device_name}")],
                [InlineKeyboardButton("⏰ حظر مؤقت (يوم)", callback_data=f"ban_confirm_temporary_1_{device_name}")],
                [InlineKeyboardButton("⏰ حظر مؤقت (3 أيام)", callback_data=f"ban_confirm_temporary_3_{device_name}")],
                [InlineKeyboardButton("⏰ حظر مؤقت (7 أيام)", callback_data=f"ban_confirm_temporary_7_{device_name}")],
                [InlineKeyboardButton("🔙 إلغاء", callback_data="back_to_main")]
            ]
            query.edit_message_text(
                text=f"🔒 **حظر جهاز:** `{device_name}`\n👤 **المستخدم:** {username}\n\nاختر نوع الحظر:",
                parse_mode=telegram.ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
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
    text = message.text
    
    if str(chat_id) != str(ADMIN_CHAT_ID):
        bot.send_message(chat_id=chat_id, text="⚠️ أنت غير مصرح لك باستخدام هذا البوت")
        return
    
    if context.user_data.get('waiting_for_logo'):
        new_logo = text.strip()
        set_setting("custom_logo", new_logo)
        context.user_data.pop('waiting_for_logo')
        bot.send_message(chat_id=chat_id, text=f"✅ تم تغيير الشعار بنجاح!")
        send_main_menu(chat_id)
        return
    
    if context.user_data.get('waiting_for_welcome'):
        new_welcome = text.strip()
        set_setting("welcome_message", new_welcome)
        context.user_data.pop('waiting_for_welcome')
        bot.send_message(chat_id=chat_id, text=f"✅ تم تغيير رسالة الترحيب بنجاح!")
        send_main_menu(chat_id)
        return
    
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

# ===================== API للتطبيق (المسارات الأساسية) =====================
@app.route('/', methods=['GET'])
def home():
    """الصفحة الرئيسية"""
    return jsonify({
        "status": "online",
        "service": "Tomb Bot Protection System",
        "version": "4.5",
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
        
        if is_device_banned(device_name):
            ban_info = get_device_ban_info(device_name)
            return jsonify({
                "status": "banned",
                "message": "جهازك محظور من قبل المطور",
                "ban_type": ban_info['ban_type'],
                "reason": ban_info['reason'],
                "remaining_days": ban_info['remaining_days']
            })
        
        temp_password = data.get('temp_password')
        if temp_password:
            temp_hash = hashlib.sha256(temp_password.encode()).hexdigest()
            c.execute("""SELECT device_name, expires_at FROM temp_passwords 
                         WHERE password_hash = ? AND used = 0 AND expires_at > ?""",
                      (temp_hash, int(time.time())))
            row = c.fetchone()
            if row:
                c.execute("UPDATE temp_passwords SET used = 1 WHERE password_hash = ?", (temp_hash,))
                conn.commit()
                return jsonify({"status": "approved", "message": "تم الدخول عبر كلمة مرور مؤقتة"})
        
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
        return jsonify({"error": str(e)}), 500

@app.route('/check_status/<request_id>', methods=['GET'])
def check_status(request_id):
    """التحقق من حالة الطلب"""
    try:
        if request_id in pending_requests:
            status = pending_requests[request_id]["status"]
            if status != "pending":
                del pending_requests[request_id]
            return jsonify({"status": status})
        
        c.execute("SELECT status FROM approvals WHERE request_id = ?", (request_id,))
        row = c.fetchone()
        if row:
            return jsonify({"status": row[0]})
        
        return jsonify({"status": "pending"})
    
    except Exception as e:
        return jsonify({"status": "pending"}), 500

@app.route('/verify_password', methods=['POST'])
def verify_password():
    """التحقق من كلمة المرور"""
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
    """التحقق من كلمة المرور المؤقتة"""
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
            return jsonify({"valid": True})
        
        return jsonify({"valid": False})
    
    except Exception as e:
        return jsonify({"valid": False, "error": str(e)}), 500

@app.route('/change_password', methods=['POST'])
def change_password():
    """تغيير كلمة المرور"""
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
    """تحديث إعدادات البوت من التطبيق"""
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
            "welcome_message": get_setting("welcome_message", WELCOME_MESSAGE)
        })
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
    """التحقق من حالة الجهاز"""
    try:
        data = request.json
        device_name = data.get('device_name', '')
        
        if is_device_banned(device_name):
            ban_info = get_device_ban_info(device_name)
            return jsonify({
                "banned": True,
                "ban_type": ban_info['ban_type'],
                "reason": ban_info['reason'],
                "remaining_days": ban_info['remaining_days']
            })
        return jsonify({"banned": False})
    
    except Exception as e:
        return jsonify({"banned": False, "error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "bot": "running",
        "version": "4.5",
        "timestamp": int(time.time())
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
