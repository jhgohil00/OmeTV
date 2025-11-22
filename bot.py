import logging
import psycopg2
from psycopg2 import pool
import datetime
import asyncio
import os
import threading
import random
import time
from flask import Flask
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, 
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, 
    Update, error
)
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, 
    CallbackQueryHandler, MessageHandler, filters
)
from telegram.request import HTTPXRequest

# ==============================================================================
# 🔐 SECURITY & CONFIGURATION
# ==============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
admin_env = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in admin_env.split(",") if x.strip().isdigit()]

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ==============================================================================
# 🧠 GLOBAL STATE (RAM)
# ==============================================================================
ACTIVE_CHATS = {}      # {user_id: partner_id}
GAME_STATES = {}       # {user_id: {'game': 'Truth or Dare', 'turn': user_id, 'partner': partner_id}}
GAME_COOLDOWNS = {}    # {user_id: timestamp}
DB_POOL = None

# ==============================================================================
# 📚 GAME CONTENT LIBRARY
# ==============================================================================
GAME_DATA = {
    "tod_truth": [
        "What is your biggest fear?", "What is the last lie you told?", "Who is your secret crush?",
        "What is your most embarrassing moment?", "Have you ever cheated on a test?",
        "What is the worst gift you ever received?", "What is your biggest regret?",
        "When was the last time you cried?", "What is a secret you've never told anyone?",
        "If you could switch lives with one person, who would it be?"
    ],
    "tod_dare": [
        "Send a voice note singing 'Happy Birthday'.", "Send the 3rd photo in your gallery.",
        "Type a message with your nose.", "Send a sticker that describes you.",
        "Do 10 pushups and send a video note.",
        "Talk in emojis for the next 3 turns.", "Describe your crush without naming them.",
        "Send a screenshot of your home screen."
    ],
    "wyr": [
        ("Be invisible", "Be able to fly"), ("Always be cold", "Always be hot"),
        ("Have unlimited money", "Have unlimited time"), ("Know how you die", "Know when you die"),
        ("Explore Space", "Explore the Ocean"), ("Talk to animals", "Speak all languages")
    ]
}

# ==============================================================================
# ❤️ THE HEARTBEAT
# ==============================================================================
app_flask = Flask(__name__)

@app_flask.route('/')
def health_check():
    return "Bot is Alive!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app_flask.run(host="0.0.0.0", port=port)

# ==============================================================================
# 🛠️ DATABASE ENGINE
# ==============================================================================
def init_db_pool():
    global DB_POOL
    if not DATABASE_URL: return
    try:
        DB_POOL = psycopg2.pool.SimpleConnectionPool(1, 20, dsn=DATABASE_URL)
        print("✅ CONNECTION POOL STARTED.")
    except Exception as e:
        print(f"❌ Pool Error: {e}")

def get_conn():
    if DB_POOL: return DB_POOL.getconn()
    return None

def release_conn(conn):
    if DB_POOL and conn: DB_POOL.putconn(conn)

def init_db():
    init_db_pool()
    conn = get_conn()
    if not conn: return
    cur = conn.cursor()
    
    tables = [
        """CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
            language TEXT DEFAULT 'English', gender TEXT DEFAULT 'Hidden',
            age_range TEXT DEFAULT 'Hidden', region TEXT DEFAULT 'Hidden',
            interests TEXT DEFAULT '', mood TEXT DEFAULT 'Neutral',
            karma_score INTEGER DEFAULT 100, status TEXT DEFAULT 'idle',
            partner_id BIGINT DEFAULT 0, report_count INTEGER DEFAULT 0,
            banned_until TIMESTAMP, joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );""",
        """CREATE TABLE IF NOT EXISTS chat_logs (
            id SERIAL PRIMARY KEY, sender_id BIGINT, receiver_id BIGINT,
            message TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );""",
        """CREATE TABLE IF NOT EXISTS reports (
            id SERIAL PRIMARY KEY, reporter_id BIGINT, reported_id BIGINT,
            reason TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );""",
        """CREATE TABLE IF NOT EXISTS user_interactions (
            id SERIAL PRIMARY KEY, rater_id BIGINT, target_id BIGINT,
            score INTEGER, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );""",
        """CREATE TABLE IF NOT EXISTS feedback (
            id SERIAL PRIMARY KEY, user_id BIGINT, message TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );"""
    ]
    
    for t in tables: cur.execute(t)
    
    # Auto-Migration
    try:
        cols = ["username TEXT", "first_name TEXT", "report_count INTEGER DEFAULT 0", 
                "banned_until TIMESTAMP", "gender TEXT DEFAULT 'Hidden'", 
                "age_range TEXT DEFAULT 'Hidden'", "region TEXT DEFAULT 'Hidden'"]
        for c in cols: cur.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {c};")
    except: pass

    conn.commit()
    cur.close()
    release_conn(conn)
    print("✅ DATABASE SCHEMA READY.")

# ==============================================================================
# ⌨️ KEYBOARD LAYOUTS
# ==============================================================================
def get_keyboard_lobby():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🚀 Start Matching")],
        [KeyboardButton("🎯 Change Interests"), KeyboardButton("⚙️ Settings")],
        [KeyboardButton("🪪 My ID"), KeyboardButton("🆘 Help")]
    ], resize_keyboard=True)

def get_keyboard_searching():
    return ReplyKeyboardMarkup([
        [KeyboardButton("❌ Stop Searching")]
    ], resize_keyboard=True)

def get_keyboard_chat():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🎮 Games")],
        [KeyboardButton("⏭️ Next"), KeyboardButton("🛑 Stop")]
    ], resize_keyboard=True)

def get_keyboard_game():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🛑 Stop Game"), KeyboardButton("🛑 Stop Chat")]
    ], resize_keyboard=True)

# ==============================================================================
# 🧩 CRITICAL HELPER FUNCTIONS (Must be defined before use)
# ==============================================================================
async def show_main_menu(update):
    """Universal function to show the Lobby UI"""
    msg_text = "👋 **Lobby**"
    kb = get_keyboard_lobby()
    try:
        if hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.message.reply_text(msg_text, reply_markup=kb, parse_mode='Markdown')
        elif hasattr(update, 'message') and update.message:
            await update.message.reply_text(msg_text, reply_markup=kb, parse_mode='Markdown')
    except: pass

async def update_user_profile(user_id, column, value):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(f"UPDATE users SET {column} = %s WHERE user_id = %s", (value, user_id))
    conn.commit(); cur.close(); release_conn(conn)

async def send_onboarding_step(update, step):
    kb = []
    msg = ""
    
    if step == 1:
        msg = "1️⃣ **What's your gender?**"
        kb = [[InlineKeyboardButton("👨 Male", callback_data="set_gen_Male"), InlineKeyboardButton("👩 Female", callback_data="set_gen_Female")], 
              [InlineKeyboardButton("🌈 Other", callback_data="set_gen_Other"), InlineKeyboardButton("⏭️ Skip", callback_data="set_gen_Hidden")]]
    elif step == 2:
        msg = "2️⃣ **Age Group?**"
        kb = [[InlineKeyboardButton("🐣 ~18", callback_data="set_age_~18"), InlineKeyboardButton("🧢 20-25", callback_data="set_age_20-25")], 
              [InlineKeyboardButton("💼 25-30", callback_data="set_age_25-30"), InlineKeyboardButton("☕ 30+", callback_data="set_age_30+")],
              [InlineKeyboardButton("⏭️ Skip", callback_data="set_age_Hidden")]]
    elif step == 3:
        msg = "3️⃣ **Primary Language?**"
        kb = [[InlineKeyboardButton("🇺🇸 English", callback_data="set_lang_English"), InlineKeyboardButton("🇮🇳 Hindi", callback_data="set_lang_Hindi")],
              [InlineKeyboardButton("🇮🇩 Indo", callback_data="set_lang_Indo"), InlineKeyboardButton("🇪🇸 Spanish", callback_data="set_lang_Spanish")],
              [InlineKeyboardButton("🇫🇷 French", callback_data="set_lang_French"), InlineKeyboardButton("🇯🇵 Japanese", callback_data="set_lang_Japanese")],
              [InlineKeyboardButton("🌍 Other", callback_data="set_lang_Other"), InlineKeyboardButton("⏭️ Skip", callback_data="set_lang_English")]]
    elif step == 4:
        msg = "4️⃣ **Region?**"
        kb = [[InlineKeyboardButton("🌏 Asia", callback_data="set_reg_Asia"), InlineKeyboardButton("🌍 Europe", callback_data="set_reg_Europe")],
              [InlineKeyboardButton("🌎 America", callback_data="set_reg_America"), InlineKeyboardButton("🌍 Africa", callback_data="set_reg_Africa")],
              [InlineKeyboardButton("⏭️ Skip", callback_data="set_reg_Hidden")]]
    elif step == 5:
        msg = "5️⃣ **Current Mood?**"
        kb = [[InlineKeyboardButton("😃 Happy", callback_data="set_mood_Happy"), InlineKeyboardButton("😔 Sad", callback_data="set_mood_Sad")],
              [InlineKeyboardButton("😴 Bored", callback_data="set_mood_Bored"), InlineKeyboardButton("🤔 Don't Know", callback_data="set_mood_Confused")],
              [InlineKeyboardButton("🥀 Lonely", callback_data="set_mood_Lonely"), InlineKeyboardButton("⏭️ Skip", callback_data="set_mood_Neutral")]]
    elif step == 6:
        msg = "6️⃣ **Final Step! Interests**\n\nType keywords (e.g., *Cricket, Movies*) or click Skip."
        kb = [[InlineKeyboardButton("⏭️ Skip & Finish", callback_data="onboarding_done")]]

    try:
        if hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        else:
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    except: pass

async def show_profile(update, context):
    user_id = update.effective_user.id
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT language, interests, karma_score, gender, age_range, region, mood FROM users WHERE user_id = %s", (user_id,))
    data = cur.fetchone(); cur.close(); release_conn(conn)
    text = (
        f"👤 **IDENTITY CARD**\n━━━━━━━━━━━━━━━━\n"
        f"🗣️ **Lang:** {data[0]}\n🏷️ **Tags:** {data[1]}\n"
        f"🚻 **Gender:** {data[3]}\n🎂 **Age:** {data[4]}\n"
        f"🌍 **Region:** {data[5]}\n🎭 **Mood:** {data[6]}\n"
        f"🛡️ **Trust Score:** {data[2]}%\n━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def send_reroll_option(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT status FROM users WHERE user_id = %s", (user_id,))
    status = cur.fetchone()
    if status and status[0] == 'searching':
        kb = [[InlineKeyboardButton("🎲 Switch to Random Match", callback_data="force_random")]]
        try: await context.bot.send_message(user_id, "🐢 **Quiet on these frequencies...**\nWe couldn't find a perfect match yet.", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        except: pass
    cur.close(); release_conn(conn)

# ==============================================================================
# 📱 MAIN HANDLERS
# ==============================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_conn(); cur = conn.cursor()
    
    # Ban Check
    cur.execute("SELECT banned_until, gender FROM users WHERE user_id = %s", (user.id,))
    data = cur.fetchone()
    if data and data[0] and data[0] > datetime.datetime.now():
        await update.message.reply_text(f"🚫 Banned until {data[0]}."); cur.close(); release_conn(conn); return

    # Register User
    cur.execute("""INSERT INTO users (user_id, username, first_name) VALUES (%s, %s, %s) 
                   ON CONFLICT (user_id) DO UPDATE SET username = %s, first_name = %s""", 
                   (user.id, user.username, user.first_name, user.username, user.first_name))
    conn.commit(); cur.close(); release_conn(conn)
    
    welcome = "👋 **Welcome to OmeTV Chatbot!**\n\nConnect with strangers worldwide. 🌍\nNo names. No login.\n\n*Let's vibe check.* 👇"
    
    if not data or data[1] == 'Hidden':
        await update.message.reply_text(welcome, reply_markup=ReplyKeyboardRemove(), parse_mode='Markdown')
        await send_onboarding_step(update, 1)
    else:
        # Ghost Button Fix
        msg = await update.message.reply_text("🔄 Loading...", reply_markup=ReplyKeyboardRemove())
        try: await context.bot.delete_message(chat_id=user.id, message_id=msg.message_id)
        except: pass
        await show_main_menu(update)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = "🆘 **HELP**\n\n🚀 Start: Match\n🛑 Stop: End\n🎮 Games: Play\n📨 Feedback: `/feedback msg`"
    await update.message.reply_text(txt, parse_mode='Markdown')

# ==============================================================================
# 🎮 CONTROLLER
# ==============================================================================
async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    text = update.message.text
    user_id = update.effective_user.id

    # Manual Game Input
    if context.user_data.get("state") == "GAME_MANUAL":
        pid = ACTIVE_CHATS.get(user_id)
        if pid:
            await context.bot.send_message(pid, f"❓ **Question:** {text}", parse_mode='Markdown')
            await update.message.reply_text("✅ Sent.")
            # Swap Turn
            GAME_STATES[user_id]["turn"] = pid; GAME_STATES[pid]["turn"] = pid
            if GAME_STATES[user_id]["game"] == "Truth or Dare": await send_tod_turn(context, pid)
        context.user_data["state"] = None; return

    # Onboarding Input
    if context.user_data.get("state") == "ONBOARDING_INTEREST":
        await update_user_profile(user_id, "interests", text)
        context.user_data["state"] = None
        await update.message.reply_text("✅ **Ready!**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown'); return

    # Buttons
    if text == "🚀 Start Matching": await start_search(update, context); return
    if text in ["🛑 Stop", "🛑 Stop Chat"]: await stop_chat(update, context); return
    if text == "⏭️ Next": await stop_chat(update, context, is_next=True); return
    if text == "❌ Stop Searching": await stop_search_process(update, context); return
    if text == "🎯 Change Interests": context.user_data["state"] = "ONBOARDING_INTEREST"; await update.message.reply_text("👇 Type interests:", reply_markup=ReplyKeyboardRemove()); return
    
    if text == "⚙️ Settings":
        kb = [[InlineKeyboardButton("Lang", callback_data="set_lang_English"), InlineKeyboardButton("Mood", callback_data="set_mood_Neutral")], [InlineKeyboardButton("Close", callback_data="close_settings")]]
        await update.message.reply_text("⚙️ Settings:", reply_markup=InlineKeyboardMarkup(kb)); return
    if text == "🪪 My ID": await show_profile(update, context); return
    if text == "🆘 Help": await help_command(update, context); return
    
    if text == "🎮 Games":
        kb = [[InlineKeyboardButton("😈 Truth or Dare", callback_data="game_offer_Truth or Dare")],
              [InlineKeyboardButton("🎲 Would You Rather", callback_data="game_offer_Would You Rather")],
              [InlineKeyboardButton("✂️ Rock Paper Scissors", callback_data="game_offer_Rock Paper Scissors")]]
        await update.message.reply_text("🎮 **Game Center**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown'); return
    
    if text == "🛑 Stop Game":
        pid = ACTIVE_CHATS.get(user_id)
        if user_id in GAME_STATES: del GAME_STATES[user_id]
        if pid and pid in GAME_STATES: del GAME_STATES[pid]
        await update.message.reply_text("🛑 Game Stopped.", reply_markup=get_keyboard_chat())
        if pid: await context.bot.send_message(pid, "🛑 Partner stopped the game.", reply_markup=get_keyboard_chat())
        return

    # Commands
    if text.startswith("/"):
        if text == "/stop": await stop_chat(update, context); return
        if text == "/admin": await admin_panel(update, context); return
        if text.startswith("/ban"): await admin_ban_command(update, context); return
        if text.startswith("/warn"): await admin_warn_command(update, context); return
        if text.startswith("/broadcast"): await admin_broadcast_execute(update, context); return
        if text.startswith("/feedback"): await handle_feedback_command(update, context); return

    await relay_message(update, context)

# ==============================================================================
# 🔌 CONNECTION LOGIC
# ==============================================================================
async def start_search(update, context):
    user_id = update.effective_user.id
    if user_id in ACTIVE_CHATS: await update.message.reply_text("⛔ **Already in chat!**", parse_mode='Markdown'); return
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET status = 'searching' WHERE user_id = %s", (user_id,))
    conn.commit()
    cur.execute("SELECT interests FROM users WHERE user_id = %s", (user_id,))
    tags = cur.fetchone()[0] or "Any"
    cur.close(); release_conn(conn)
    await update.message.reply_text(f"📡 **Scanning...**\nLooking for: `{tags}`...", parse_mode='Markdown', reply_markup=get_keyboard_searching())
    if context.job_queue: context.job_queue.run_once(send_reroll_option, 15, data=user_id)
    await perform_match(update, context, user_id)

async def stop_search_process(update, context):
    user_id = update.effective_user.id
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET status = 'idle' WHERE user_id = %s", (user_id,))
    conn.commit(); cur.close(); release_conn(conn)
    await update.message.reply_text("🛑 Search Stopped.", reply_markup=get_keyboard_lobby())

async def perform_match(update, context, user_id):
    partner_id, common, p_mood, p_lang = find_match(user_id)
    if partner_id:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("UPDATE users SET status='chatting', partner_id=%s WHERE user_id=%s", (partner_id, user_id))
        cur.execute("UPDATE users SET status='chatting', partner_id=%s WHERE user_id=%s", (user_id, partner_id))
        conn.commit(); cur.close(); release_conn(conn)
        
        ACTIVE_CHATS[user_id] = partner_id
        ACTIVE_CHATS[partner_id] = user_id
        
        msg = (f"⚡ **YOU ARE CONNECTED!**\n\n"
               f"🎭 **Mood:** {p_mood}\n🔗 **Interest:** {', '.join(common) if common else 'Random'}\n"
               f"🗣️ **Language:** {p_lang}\n\n⚠️ *Tip: Say Hi!*")
        
        kb = get_keyboard_chat()
        await context.bot.send_message(user_id, msg, reply_markup=kb, parse_mode='Markdown')
        try: await context.bot.send_message(partner_id, msg, reply_markup=kb, parse_mode='Markdown')
        except: pass

async def stop_chat(update, context, is_next=False):
    user_id = update.effective_user.id
    partner_id = ACTIVE_CHATS.pop(user_id, 0)
    if partner_id and partner_id in ACTIVE_CHATS: del ACTIVE_CHATS[partner_id]
    
    if user_id in GAME_STATES: del GAME_STATES[user_id]
    if partner_id in GAME_STATES: del GAME_STATES[partner_id]

    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET status='idle', partner_id=0 WHERE user_id IN (%s, %s)", (user_id, partner_id))
    conn.commit(); cur.close(); release_conn(conn)
    
    k = [[InlineKeyboardButton("👍", callback_data=f"rate_like_{partner_id}"), InlineKeyboardButton("👎", callback_data=f"rate_dislike_{partner_id}")],
         [InlineKeyboardButton("⚠️ Report", callback_data=f"rate_report_{partner_id}")],
         [InlineKeyboardButton("🚀 New Match", callback_data="action_search"), InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]]
    
    if is_next:
        await update.message.reply_text("⏭️ **Skipping...**", reply_markup=ReplyKeyboardRemove(), parse_mode='Markdown')
        await update.message.reply_text("📊 Feedback?", reply_markup=InlineKeyboardMarkup(k))
        await start_search(update, context)
    else:
        await update.message.reply_text("🔌 **Disconnected.**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown')
        await update.message.reply_text("📊 Feedback?", reply_markup=InlineKeyboardMarkup(k))

    if partner_id:
        try: await context.bot.send_message(partner_id, "🔌 **Partner Disconnected.**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown')
        except: pass

async def relay_message(update, context):
    user_id = update.effective_user.id
    partner_id = ACTIVE_CHATS.get(user_id)
    
    if not partner_id: # Fallback
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT partner_id FROM users WHERE user_id = %s AND status='chatting'", (user_id,))
        row = cur.fetchone(); cur.close(); release_conn(conn)
        if row and row[0]: partner_id = row[0]; ACTIVE_CHATS[user_id] = partner_id

    if partner_id:
        if update.message.text:
            conn = get_conn(); cur = conn.cursor()
            cur.execute("INSERT INTO chat_logs (sender_id, receiver_id, message) VALUES (%s, %s, %s)", (user_id, partner_id, update.message.text))
            conn.commit(); cur.close(); release_conn(conn)
        try: await update.message.copy(chat_id=partner_id)
        except: await stop_chat(update, context)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); data = q.data; uid = q.from_user.id

    if data == "force_random": await perform_match(update, context, uid); return
    if data == "close_settings": await q.delete_message(); return
    
    # GAME LOGIC
    if data.startswith("game_offer_"): await offer_game(update, context, uid, data.split("_", 2)[2]); return
    if data.startswith("game_accept_"): pid = ACTIVE_CHATS.get(uid); await start_game_session(update, context, data.split("_", 2)[2], pid, uid) if pid else None; return
    if data == "game_reject": pid = ACTIVE_CHATS.get(uid); await context.bot.send_message(pid, "❌ Declined.") if pid else None; await q.edit_message_text("❌ Declined."); return
    if data.startswith("tod_pick_"): await send_tod_options(update, context, data.split("_")[2]); return
    if data.startswith("tod_send_"): 
        gd = GAME_STATES.get(uid)
        if gd:
            q_text = gd["options"][int(data.split("_")[2])]
            pid = gd["partner"]
            await context.bot.send_message(pid, f"🎲 **QUESTION:**\n{q_text}", parse_mode='Markdown')
            await q.edit_message_text(f"✅ Sent: {q_text}")
            GAME_STATES[uid]["turn"] = pid; GAME_STATES[pid]["turn"] = pid
            await send_tod_turn(context, pid)
        return
    if data == "tod_manual": context.user_data["state"] = "GAME_MANUAL"; await q.edit_message_text("✍️ **Type your question now:**"); return

    # Onboarding
    if data.startswith("set_gen_"): await update_user_profile(uid, "gender", data.split("_")[2]); await send_onboarding_step(update, 2); return
    if data.startswith("set_age_"): await update_user_profile(uid, "age_range", data.split("_")[2]); await send_onboarding_step(update, 3); return
    if data.startswith("set_lang_"): await update_user_profile(uid, "language", data.split("_")[2]); await send_onboarding_step(update, 4); return
    if data.startswith("set_reg_"): await update_user_profile(uid, "region", data.split("_")[2]); await send_onboarding_step(update, 5); return
    if data.startswith("set_mood_"): await update_user_profile(uid, "mood", data.split("_")[2]); context.user_data["state"] = "ONBOARDING_INTEREST"; await send_onboarding_step(update, 6); return
    if data == "onboarding_done": context.user_data["state"] = None; await show_main_menu(update); return
    if data == "restart_onboarding": await send_onboarding_step(update, 1); return

    # Admin Logic
    if data == "admin_broadcast_info" and uid in ADMIN_IDS:
        try: await q.edit_message_text("📢 **Broadcast:**\nType `/broadcast Msg`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_home")]]), parse_mode='Markdown'); return
        except error.BadRequest: pass

    if data == "admin_users" and uid in ADMIN_IDS:
        conn = get_conn(); cur = conn.cursor(); cur.execute("SELECT user_id, first_name FROM users ORDER BY joined_at DESC LIMIT 10"); users = cur.fetchall(); cur.close(); release_conn(conn)
        msg = "📜 **Recent:**\n" + "\n".join([f"• {u[1]} (`{u[0]}`)" for u in users])
        try: await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_home")]]), parse_mode='Markdown'); return
        except error.BadRequest: pass

    if data == "admin_reports" and uid in ADMIN_IDS:
        conn = get_conn(); cur = conn.cursor(); cur.execute("SELECT user_id, report_count FROM users WHERE report_count > 0 ORDER BY report_count DESC LIMIT 5"); users = cur.fetchall(); cur.close(); release_conn(conn)
        if not users: 
            try: await q.edit_message_text("✅ No reports.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_home")]])); return
            except error.BadRequest: pass
        kb = []; 
        for u in users: kb.append([InlineKeyboardButton(f"🔨 {u[0]}", callback_data=f"ban_user_{u[0]}"), InlineKeyboardButton(f"✅ {u[0]}", callback_data=f"clear_user_{u[0]}")])
        kb.append([InlineKeyboardButton("🔙", callback_data="admin_home")])
        try: await q.edit_message_text("⚠️ **Flagged:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown'); return
        except error.BadRequest: pass

    if data == "admin_banlist" and uid in ADMIN_IDS:
        conn = get_conn(); cur = conn.cursor(); cur.execute("SELECT user_id, banned_until FROM users WHERE banned_until > NOW() LIMIT 5"); users = cur.fetchall(); cur.close(); release_conn(conn)
        if not users:
            try: await q.edit_message_text("✅ No bans.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_home")]])); return
            except error.BadRequest: pass
        kb = []; 
        for u in users: kb.append([InlineKeyboardButton(f"✅ Unban {u[0]}", callback_data=f"unban_user_{u[0]}")])
        kb.append([InlineKeyboardButton("🔙", callback_data="admin_home")])
        try: await q.edit_message_text("🚫 **Bans:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown'); return
        except error.BadRequest: pass

    if data == "admin_feedbacks" and uid in ADMIN_IDS:
        conn = get_conn(); cur = conn.cursor(); cur.execute("SELECT message FROM feedback ORDER BY timestamp DESC LIMIT 5"); rows = cur.fetchall(); cur.close(); release_conn(conn)
        txt = "\n".join([r[0] for r in rows]) or "None"
        try: await q.edit_message_text(f"📨 **Feed:**\n{txt}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_home")]]), parse_mode='Markdown'); return
        except error.BadRequest: pass

    if data == "admin_home" and uid in ADMIN_IDS: await admin_panel(update, context); return

    if data.startswith("ban_user_") and uid in ADMIN_IDS: await admin_ban_command(update, context); return
    if data.startswith("clear_user_") and uid in ADMIN_IDS:
        tid = int(data.split("_")[2]); conn = get_conn(); cur = conn.cursor(); cur.execute("UPDATE users SET report_count = 0 WHERE user_id = %s", (tid,)); conn.commit(); cur.close(); release_conn(conn)
        try: await q.edit_message_text(f"✅ Cleared {tid}.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_reports")]]), parse_mode='Markdown'); return
        except error.BadRequest: pass

    if data.startswith("unban_user_") and uid in ADMIN_IDS:
        tid = int(data.split("_")[2]); conn = get_conn(); cur = conn.cursor(); cur.execute("UPDATE users SET banned_until = NULL WHERE user_id = %s", (tid,)); conn.commit(); cur.close(); release_conn(conn)
        try: await q.edit_message_text(f"✅ Unbanned {tid}.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_banlist")]]), parse_mode='Markdown'); return
        except error.BadRequest: pass

    # Rate Logic
    if data.startswith("rate_"):
        act, target = data.split("_")[1], int(data.split("_")[2])
        if act == "report":
            await handle_report(update, context, uid, target)
            try: await q.edit_message_text("⚠️ Reported.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Block", callback_data=f"rate_dislike_{target}")]]))
            except error.BadRequest: pass
        else:
            sc = 1 if act == "like" else -1
            conn = get_conn(); cur = conn.cursor(); cur.execute("INSERT INTO user_interactions (rater_id, target_id, score) VALUES (%s, %s, %s)", (uid, target, sc)); conn.commit(); cur.close(); release_conn(conn)
            try: await q.edit_message_text("✅ Sent.")
            except error.BadRequest: pass
            
    if data == "action_search": await start_search(update, context); return
    if data == "main_menu": await show_main_menu(update); return
    if data == "stop_search": await stop_search_process(update, context); return

if __name__ == '__main__':
    if not BOT_TOKEN: print("ERROR: Config missing")
    else:
        init_db()
        flask_thread = threading.Thread(target=run_flask); flask_thread.daemon = True; flask_thread.start()
        req = HTTPXRequest(connect_timeout=60, read_timeout=60)
        app = ApplicationBuilder().token(BOT_TOKEN).job_queue(None).request(req).build()
        
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("admin", admin_panel))
        app.add_handler(CommandHandler("ban", admin_ban_command))
        app.add_handler(CommandHandler("warn", admin_warn_command))
        app.add_handler(CommandHandler("broadcast", admin_broadcast_execute))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("feedback", handle_feedback_command))
        
        app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_input))
        app.add_handler(CallbackQueryHandler(button_handler))
        app.add_handler(MessageHandler(filters.ALL, relay_message))
        
        print("🤖 PHASE 15 FIXED BOT LIVE")
        app.run_polling()
