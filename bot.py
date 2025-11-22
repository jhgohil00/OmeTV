import logging
import psycopg2
from psycopg2 import pool
import datetime
import asyncio
import os
import threading
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

# Parse Admin IDs safely
ADMIN_IDS = []
if admin_env:
    try:
        ADMIN_IDS = [int(x) for x in admin_env.split(",") if x.strip().isdigit()]
    except:
        print("⚠️ Warning: ADMIN_IDS could not be parsed.")

# Logging Setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)

# ==============================================================================
# 🧠 GLOBAL STATE (RAM CACHE)
# ==============================================================================
# Stores active connections: {user_id: partner_id}
# This allows 0-latency message relaying.
ACTIVE_CHATS = {} 

# Database Connection Pool
DB_POOL = None

# ==============================================================================
# ❤️ THE HEARTBEAT (Prevent Render from sleeping)
# ==============================================================================
app_flask = Flask(__name__)

@app_flask.route('/')
def health_check():
    return "Bot is Alive!", 200

def run_flask():
    # Render assigns a random port in the environment variable 'PORT'
    port = int(os.environ.get("PORT", 8080))
    app_flask.run(host="0.0.0.0", port=port)

# ==============================================================================
# 🛠️ DATABASE ENGINE (Connection Pooling)
# ==============================================================================
def init_db_pool():
    global DB_POOL
    if not DATABASE_URL:
        print("❌ ERROR: DATABASE_URL is missing!")
        return
    try:
        DB_POOL = psycopg2.pool.SimpleConnectionPool(1, 20, dsn=DATABASE_URL)
        print("✅ CONNECTION POOL STARTED.")
    except Exception as e:
        print(f"❌ Pool Error: {e}")

def get_conn():
    if DB_POOL:
        return DB_POOL.getconn()
    return None

def release_conn(conn):
    if DB_POOL and conn:
        DB_POOL.putconn(conn)

def init_db():
    init_db_pool()
    conn = get_conn()
    if not conn: return
    cur = conn.cursor()
    
    # 1. Users Table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            language TEXT DEFAULT 'English',
            gender TEXT DEFAULT 'Hidden',
            age_range TEXT DEFAULT 'Hidden',
            region TEXT DEFAULT 'Hidden',
            interests TEXT DEFAULT '',
            mood TEXT DEFAULT 'Neutral',
            karma_score INTEGER DEFAULT 100,
            status TEXT DEFAULT 'idle',
            partner_id BIGINT DEFAULT 0,
            report_count INTEGER DEFAULT 0,
            banned_until TIMESTAMP,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # 2. Chat Logs
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_logs (
            id SERIAL PRIMARY KEY,
            sender_id BIGINT,
            receiver_id BIGINT,
            message TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # 3. Reports
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id SERIAL PRIMARY KEY,
            reporter_id BIGINT,
            reported_id BIGINT,
            reason TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # 4. Interactions (Likes/Dislikes)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_interactions (
            id SERIAL PRIMARY KEY,
            rater_id BIGINT,
            target_id BIGINT,
            score INTEGER,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # 5. Feedback
    cur.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            message TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    # Auto-Migration for older DB versions
    try:
        cols = [
            "username TEXT", "first_name TEXT", 
            "report_count INTEGER DEFAULT 0", "banned_until TIMESTAMP", 
            "gender TEXT DEFAULT 'Hidden'", "age_range TEXT DEFAULT 'Hidden'", 
            "region TEXT DEFAULT 'Hidden'"
        ]
        for c in cols:
            cur.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {c};")
    except: pass

    conn.commit()
    cur.close()
    release_conn(conn)
    print("✅ DATABASE SCHEMA READY.")


# ==============================================================================
# ⌨️ KEYBOARD LAYOUTS
# ==============================================================================
def get_keyboard_lobby():
    """Keyboard A: The Waiting Room"""
    return ReplyKeyboardMarkup([
        [KeyboardButton("🚀 Start Matching")],
        [KeyboardButton("🎯 Change Interests"), KeyboardButton("⚙️ Settings")],
        [KeyboardButton("🪪 My ID"), KeyboardButton("🆘 Help")]
    ], resize_keyboard=True)

def get_keyboard_searching():
    """Keyboard D: Searching State"""
    return ReplyKeyboardMarkup([
        [KeyboardButton("❌ Stop Searching")]
    ], resize_keyboard=True)

def get_keyboard_chat():
    """Keyboard B: The Chat"""
    return ReplyKeyboardMarkup([
        [KeyboardButton("🎮 Games")],
        [KeyboardButton("⏭️ Next"), KeyboardButton("🛑 Stop")]
    ], resize_keyboard=True)

# ==============================================================================
# 🧩 HELPER FUNCTIONS (Defined BEFORE use to prevent NameError)
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
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE users SET {column} = %s WHERE user_id = %s", (value, user_id))
    conn.commit()
    cur.close()
    release_conn(conn)

async def show_profile(update, context):
    user_id = update.effective_user.id
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT language, interests, karma_score, gender, age_range, region, mood FROM users WHERE user_id = %s", (user_id,))
    data = cur.fetchone()
    cur.close()
    release_conn(conn)
    
    if data:
        text = (
            f"👤 **IDENTITY CARD**\n━━━━━━━━━━━━━━━━\n"
            f"🗣️ **Lang:** {data[0]}\n"
            f"🏷️ **Tags:** {data[1]}\n"
            f"🚻 **Gender:** {data[3]}\n"
            f"🎂 **Age:** {data[4]}\n"
            f"🌍 **Region:** {data[5]}\n"
            f"🎭 **Mood:** {data[6]}\n"
            f"🛡️ **Trust Score:** {data[2]}%\n━━━━━━━━━━━━━━━━"
        )
        await update.message.reply_text(text, parse_mode='Markdown')

async def send_reroll_option(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT status FROM users WHERE user_id = %s", (user_id,))
    status = cur.fetchone()
    
    if status and status[0] == 'searching':
        kb = [[InlineKeyboardButton("🎲 Switch to Random Match", callback_data="force_random")]]
        try: 
            await context.bot.send_message(
                user_id, 
                "🐢 **Quiet on these frequencies...**\nWe couldn't find a perfect match yet.", 
                reply_markup=InlineKeyboardMarkup(kb), 
                parse_mode='Markdown'
            )
        except: pass
    
    cur.close()
    release_conn(conn)

# ==============================================================================
# 📝 ONBOARDING (Fully Expanded)
# ==============================================================================
async def send_onboarding_step(update, step):
    kb = []
    msg = ""
    
    if step == 1:
        msg = "1️⃣ **What's your gender?**"
        kb = [
            [InlineKeyboardButton("👨 Male", callback_data="set_gen_Male"), InlineKeyboardButton("👩 Female", callback_data="set_gen_Female")], 
            [InlineKeyboardButton("🌈 Other", callback_data="set_gen_Other"), InlineKeyboardButton("⏭️ Skip", callback_data="set_gen_Hidden")]
        ]
    elif step == 2:
        msg = "2️⃣ **Age Group?**"
        kb = [
            [InlineKeyboardButton("🐣 ~18", callback_data="set_age_~18"), InlineKeyboardButton("🧢 20-25", callback_data="set_age_20-25")], 
            [InlineKeyboardButton("💼 25-30", callback_data="set_age_25-30"), InlineKeyboardButton("☕ 30+", callback_data="set_age_30+")],
            [InlineKeyboardButton("⏭️ Skip", callback_data="set_age_Hidden")]
        ]
    elif step == 3:
        msg = "3️⃣ **Primary Language?**"
        kb = [
            [InlineKeyboardButton("🇺🇸 English", callback_data="set_lang_English"), InlineKeyboardButton("🇮🇳 Hindi", callback_data="set_lang_Hindi")],
            [InlineKeyboardButton("🇮🇩 Indo", callback_data="set_lang_Indo"), InlineKeyboardButton("🇪🇸 Spanish", callback_data="set_lang_Spanish")],
            [InlineKeyboardButton("🇫🇷 French", callback_data="set_lang_French"), InlineKeyboardButton("🇯🇵 Japanese", callback_data="set_lang_Japanese")],
            [InlineKeyboardButton("🌍 Other", callback_data="set_lang_Other"), InlineKeyboardButton("⏭️ Skip", callback_data="set_lang_English")]
        ]
    elif step == 4:
        msg = "4️⃣ **Region?**"
        kb = [
            [InlineKeyboardButton("🌏 Asia", callback_data="set_reg_Asia"), InlineKeyboardButton("🌍 Europe", callback_data="set_reg_Europe")],
            [InlineKeyboardButton("🌎 America", callback_data="set_reg_America"), InlineKeyboardButton("🌍 Africa", callback_data="set_reg_Africa")],
            [InlineKeyboardButton("⏭️ Skip", callback_data="set_reg_Hidden")]
        ]
    elif step == 5:
        msg = "5️⃣ **Current Mood?**"
        kb = [
            [InlineKeyboardButton("😃 Happy", callback_data="set_mood_Happy"), InlineKeyboardButton("😔 Sad", callback_data="set_mood_Sad")],
            [InlineKeyboardButton("😴 Bored", callback_data="set_mood_Bored"), InlineKeyboardButton("🤔 Don't Know", callback_data="set_mood_Confused")],
            [InlineKeyboardButton("🥀 Lonely", callback_data="set_mood_Lonely"), InlineKeyboardButton("⏭️ Skip", callback_data="set_mood_Neutral")]
        ]
    elif step == 6:
        msg = "6️⃣ **Final Step! Interests**\n\nType keywords (e.g., *Cricket, Movies*) or click Skip."
        kb = [[InlineKeyboardButton("⏭️ Skip & Finish", callback_data="onboarding_done")]]

    try:
        if hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        else:
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    except: pass


# ==============================================================================
# 🧠 MATCHMAKING ENGINE (70-Point System)
# ==============================================================================
def find_match(user_id):
    conn = get_conn()
    cur = conn.cursor()
    
    # Get User Profile
    cur.execute("SELECT language, interests, age_range, mood FROM users WHERE user_id = %s", (user_id,))
    me = cur.fetchone()
    if not me: 
        cur.close()
        release_conn(conn)
        return None, [], "Neutral", "English"
        
    my_lang, my_interests, my_age, my_mood = me
    my_tags = [t.strip().lower() for t in my_interests.split(',')] if my_interests else []

    # Get Disliked List
    cur.execute("SELECT target_id FROM user_interactions WHERE rater_id = %s AND score = -1", (user_id,))
    disliked_ids = {row[0] for row in cur.fetchall()}

    # Find Candidates
    cur.execute("""
        SELECT user_id, language, interests, age_range, mood 
        FROM users 
        WHERE status = 'searching' 
        AND user_id != %s
        AND (banned_until IS NULL OR banned_until < NOW())
    """, (user_id,))
    candidates = cur.fetchall()
    
    best_match = None
    best_score = -999999
    common_interests = []
    p_mood = "Neutral"
    p_lang = "English"

    for cand in candidates:
        cand_id, cand_lang, cand_interests, cand_age, cand_mood = cand
        cand_tags = [t.strip().lower() for t in cand_interests.split(',')] if cand_interests else []
        
        score = 0
        if cand_id in disliked_ids: score -= 1000
        
        # Logic: Interest (40) > Language (20) > Age (10)
        matched_tags = list(set(my_tags) & set(cand_tags))
        if matched_tags: score += 40
        if cand_lang == my_lang: score += 20
        if cand_age == my_age and cand_age != 'Hidden': score += 10
            
        if score > best_score:
            best_score = score
            best_match = cand_id
            common_interests = matched_tags
            p_mood = cand_mood
            p_lang = cand_lang

    cur.close()
    release_conn(conn)
    return best_match, common_interests, p_mood, p_lang

# ==============================================================================
# 👮 ADMIN SYSTEM
# ==============================================================================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    conn = get_conn()
    cur = conn.cursor()
    
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE status != 'idle'")
    online = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE report_count > 0")
    flagged = cur.fetchone()[0]
    
    # Gender Stats
    cur.execute("SELECT gender, COUNT(*) FROM users GROUP BY gender")
    g_rows = cur.fetchall()
    g_stats = " | ".join([f"{r[0]}:{r[1]}" for r in g_rows]) if g_rows else "No Data"

    def get_stat(col):
        cur.execute(f"SELECT {col}, COUNT(*) FROM users GROUP BY {col} ORDER BY COUNT(*) DESC LIMIT 3")
        return " | ".join([f"{r[0]}:{r[1]}" for r in cur.fetchall()])

    msg = (f"👮 **CONTROL ROOM**\n👥 Total: `{total}` | 🟢 Online: `{online}`\n⚠️ Flagged: `{flagged}`\n\n"
           f"🚻 **Gender:** {g_stats}\n"
           f"🌍 **Geo:** {get_stat('region')}\n🗣️ **Lang:** {get_stat('language')}")
    
    kb = [
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast_info"), InlineKeyboardButton("📜 Recent Users", callback_data="admin_users")],
        [InlineKeyboardButton("⚠️ Reports", callback_data="admin_reports"), InlineKeyboardButton("📨 Feedbacks", callback_data="admin_feedbacks")],
        [InlineKeyboardButton("🚫 Banned List", callback_data="admin_banlist")]
    ]
    
    try:
        if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        else: await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    except error.BadRequest: pass
    cur.close()
    release_conn(conn)

async def admin_ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        target = int(context.args[0])
        hours = int(context.args[1])
        conn = get_conn(); cur = conn.cursor()
        ban_until = datetime.datetime.now() + datetime.timedelta(hours=hours)
        cur.execute("UPDATE users SET banned_until = %s WHERE user_id = %s", (ban_until, target))
        conn.commit(); cur.close(); release_conn(conn)
        await update.message.reply_text(f"🔨 Banned {target} for {hours}h.")
        
        # Clear RAM cache
        if target in ACTIVE_CHATS: del ACTIVE_CHATS[target]
        
        try: await context.bot.send_message(target, f"🚫 You are banned for {hours} hours.")
        except: pass
    except: await update.message.reply_text("Usage: /ban ID HOURS")

async def admin_warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        target = int(context.args[0])
        reason = " ".join(context.args[1:])
        await context.bot.send_message(target, f"⚠️ **OFFICIAL WARNING**\n\n{reason}", parse_mode='Markdown')
        await update.message.reply_text(f"✅ Warned {target}.")
    except: await update.message.reply_text("Usage: /warn ID REASON")

async def admin_broadcast_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    msg = " ".join(context.args)
    if not msg: return await update.message.reply_text("Usage: /broadcast MSG")
    
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    users = cur.fetchall(); cur.close(); release_conn(conn)
    
    await update.message.reply_text(f"📢 Sending to {len(users)} users...")
    for u in users:
        try: await context.bot.send_message(u[0], f"📢 **ANNOUNCEMENT:**\n\n{msg}", parse_mode='Markdown')
        except: pass
    await update.message.reply_text("✅ Broadcast done.")

async def handle_feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    feedback_text = update.message.text.replace("/feedback", "").strip()
    if not feedback_text:
        await update.message.reply_text("❌ Type message: `/feedback Hello`", parse_mode='Markdown'); return

    conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO feedback (user_id, message) VALUES (%s, %s)", (user_id, feedback_text))
    conn.commit(); cur.close(); release_conn(conn)
    
    await update.message.reply_text("✅ **Feedback Sent!**", parse_mode='Markdown')

async def handle_report(update, context, reporter, reported):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET report_count = report_count + 1 WHERE user_id = %s RETURNING report_count", (reported,))
    cnt = cur.fetchone()[0]
    cur.execute("INSERT INTO reports (reporter_id, reported_id, reason) VALUES (%s, %s, 'Report')", (reporter, reported))
    conn.commit()
    if cnt >= 3:
        cur.execute("SELECT message FROM chat_logs WHERE sender_id = %s ORDER BY timestamp DESC LIMIT 5", (reported,))
        logs = [l[0] for l in cur.fetchall()]
        msg = f"🚨 **REPORT ALERT (3+)**\nUser: `{reported}`\nReports: {cnt}\n\nLogs: {logs}"
        kb = [[InlineKeyboardButton(f"🔨 BAN {reported}", callback_data=f"ban_user_{reported}")]]
        for a in ADMIN_IDS:
            try: await context.bot.send_message(a, msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
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
        await update.message.reply_text(f"🚫 Banned until {data[0]}.")
        cur.close(); release_conn(conn); return

    # Register
    cur.execute("""
        INSERT INTO users (user_id, username, first_name) VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET username = %s, first_name = %s
    """, (user.id, user.username, user.first_name, user.username, user.first_name))
    conn.commit(); cur.close(); release_conn(conn)

    welcome_msg = (
        "👋 **Welcome to OmeTV Chatbot!**\n\n"
        "Connect with strangers worldwide. 🌍\n"
        "No names. No login. Just chat.\n\n"
        "*First, let's do a quick vibe check to find your best match.* 👇"
    )

    if not data or data[1] == 'Hidden':
        await update.message.reply_text(welcome_msg, reply_markup=ReplyKeyboardRemove(), parse_mode='Markdown')
        await send_onboarding_step(update, 1)
    else:
        msg = await update.message.reply_text("🔄 Loading...", reply_markup=ReplyKeyboardRemove())
        try: await context.bot.delete_message(chat_id=user.id, message_id=msg.message_id)
        except: pass
        await show_main_menu(update)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "🆘 **OmeTV Safety Guide**\n\n"
        "🚀 **Start:** Connect with a stranger.\n"
        "⏭️ **Next:** Skip current chat instantly.\n"
        "🛑 **Stop:** End chat & rate user.\n"
        "📨 **Feedback:** Type `/feedback Your Message` to contact admin.\n\n"
        "**Rules:**\n"
        "1. No 18+ content (Instant Ban).\n"
        "2. No selling/ads.\n"
        "3. Be respectful.\n\n"
        "*3 Reports = Auto-Flag for Admin Review.*"
    )
    await update.message.reply_text(txt, parse_mode='Markdown')

# ==============================================================================
# 🎮 CONTROLLER
# ==============================================================================
async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    text = update.message.text
    user_id = update.effective_user.id

    if context.user_data.get("state") == "ONBOARDING_INTEREST":
        await update_user_profile(user_id, "interests", text)
        context.user_data["state"] = None
        await update.message.reply_text("✅ **Vibe Check Complete!**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown')
        return

    if text == "🚀 Start Matching": await start_search(update, context); return
    if text in ["🛑 Stop", "🛑 Stop Chat"]: await stop_chat(update, context); return
    if text == "⏭️ Next": await stop_chat(update, context, is_next=True); return
    if text == "❌ Stop Searching": await stop_search_process(update, context); return
    
    if text == "🎯 Change Interests":
        context.user_data["state"] = "ONBOARDING_INTEREST"
        await update.message.reply_text("👇 **Type new interests:**", reply_markup=ReplyKeyboardRemove(), parse_mode='Markdown'); return
    
    if text == "⚙️ Settings":
        kb = [[InlineKeyboardButton("🚻 Gender", callback_data="set_gen_Hidden"), InlineKeyboardButton("🎂 Age", callback_data="set_age_Hidden")],
              [InlineKeyboardButton("🗣️ Lang", callback_data="set_lang_English"), InlineKeyboardButton("🎭 Mood", callback_data="set_mood_Neutral")],
              [InlineKeyboardButton("🔙 Close", callback_data="close_settings")]]
        await update.message.reply_text("⚙️ **Settings:**\nClick to update specific fields.", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown'); return

    if text == "🪪 My ID": await show_profile(update, context); return
    if text == "🆘 Help": await help_command(update, context); return
    
    if text == "🎮 Games": 
        await update.message.reply_text("🎮 **Games coming soon!**", reply_markup=get_keyboard_game(), parse_mode='Markdown'); return
    
    if text == "🛑 Stop Game": 
        await update.message.reply_text("🎮 **Game Ended.**", reply_markup=get_keyboard_chat(), parse_mode='Markdown'); return

    # Commands
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
    
    # Fast Check RAM First
    if user_id in ACTIVE_CHATS:
        await update.message.reply_text("⛔ **Already in chat!**", parse_mode='Markdown'); return

    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET status = 'searching' WHERE user_id = %s", (user_id,))
    conn.commit()
    cur.execute("SELECT interests FROM users WHERE user_id = %s", (user_id,))
    tags = cur.fetchone()[0] or "Any"
    cur.close(); release_conn(conn)
    
    await update.message.reply_text(f"📡 **Scanning Frequencies...**\nLooking for: `{tags}`...", parse_mode='Markdown', reply_markup=get_keyboard_searching())
    
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
        
        # RAM CACHE
        ACTIVE_CHATS[user_id] = partner_id
        ACTIVE_CHATS[partner_id] = user_id
        
        msg = (f"⚡ **YOU ARE CONNECTED!**\n\n"
               f"🎭 **Mood:** {p_mood}\n🔗 **Common Interest:** {', '.join(common) if common else 'Random'}\n"
               f"🗣️ **Language:** {p_lang}\n\n⚠️ *Tip: Say Hi!*")
        
        kb = get_keyboard_chat()
        await context.bot.send_message(user_id, msg, reply_markup=kb, parse_mode='Markdown')
        try: await context.bot.send_message(partner_id, msg, reply_markup=kb, parse_mode='Markdown')
        except: pass

async def stop_chat(update, context, is_next=False):
    user_id = update.effective_user.id
    partner_id = ACTIVE_CHATS.pop(user_id, 0)
    if partner_id and partner_id in ACTIVE_CHATS: del ACTIVE_CHATS[partner_id]
    
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET status='idle', partner_id=0 WHERE user_id IN (%s, %s)", (user_id, partner_id))
    conn.commit(); cur.close(); release_conn(conn)
    
    k_me = [
        [InlineKeyboardButton("👍 Cool", callback_data=f"rate_like_{partner_id}"), InlineKeyboardButton("👎 Lame", callback_data=f"rate_dislike_{partner_id}")],
        [InlineKeyboardButton("⚠️ Report User", callback_data=f"rate_report_{partner_id}")],
        [InlineKeyboardButton("🚀 Find New Match", callback_data="action_search"), InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
    ]
    k_partner = [
        [InlineKeyboardButton("👍 Cool", callback_data=f"rate_like_{user_id}"), InlineKeyboardButton("👎 Lame", callback_data=f"rate_dislike_{user_id}")],
        [InlineKeyboardButton("⚠️ Report User", callback_data=f"rate_report_{user_id}")],
        [InlineKeyboardButton("🚀 Find New Match", callback_data="action_search"), InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
    ]

    if is_next:
        await update.message.reply_text("⏭️ **Skipping...**", reply_markup=ReplyKeyboardRemove(), parse_mode='Markdown')
        await update.message.reply_text("📊 Feedback on last partner?", reply_markup=InlineKeyboardMarkup(k_me))
        await start_search(update, context)
    else:
        await update.message.reply_text("🔌 **Connection Severed.**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown')
        await update.message.reply_text("📊 **Experience Report:**", reply_markup=InlineKeyboardMarkup(k_me), parse_mode='Markdown')

    if partner_id:
        try: 
            await context.bot.send_message(partner_id, "🔌 **Partner Disconnected.**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown')
            await context.bot.send_message(partner_id, "📊 **Experience Report:**", reply_markup=InlineKeyboardMarkup(k_partner), parse_mode='Markdown')
        except: pass

async def relay_message(update, context):
    user_id = update.effective_user.id
    
    # Fast Path: RAM
    partner_id = ACTIVE_CHATS.get(user_id)
    
    # Slow Path: DB (Recovery)
    if not partner_id:
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
    
    # Onboarding Logic
    if data.startswith("set_gen_"): await update_user_profile(uid, "gender", data.split("_")[2]); await send_onboarding_step(update, 2); return
    if data.startswith("set_age_"): await update_user_profile(uid, "age_range", data.split("_")[2]); await send_onboarding_step(update, 3); return
    if data.startswith("set_lang_"): await update_user_profile(uid, "language", data.split("_")[2]); await send_onboarding_step(update, 4); return
    if data.startswith("set_reg_"): await update_user_profile(uid, "region", data.split("_")[2]); await send_onboarding_step(update, 5); return
    if data.startswith("set_mood_"): await update_user_profile(uid, "mood", data.split("_")[2]); context.user_data["state"] = "ONBOARDING_INTEREST"; await send_onboarding_step(update, 6); return
    
    if data == "onboarding_done": context.user_data["state"] = None; await show_main_menu(update); return
    if data == "restart_onboarding": await send_onboarding_step(update, 1); return

    # Admin Logic
    if data == "admin_broadcast_info" and uid in ADMIN_IDS:
        try: await q.edit_message_text("📢 **Broadcast:**\nType `/broadcast Msg`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_home")]]), parse_mode='Markdown'); return
        except error.BadRequest: pass

    if data == "admin_users" and uid in ADMIN_IDS:
        conn = get_conn(); cur = conn.cursor(); cur.execute("SELECT user_id, first_name FROM users ORDER BY joined_at DESC LIMIT 10"); users = cur.fetchall(); cur.close(); release_conn(conn)
        msg = "📜 **Recent:**\n" + "\n".join([f"• {u[1]} (`{u[0]}`)" for u in users])
        try: await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_home")]]), parse_mode='Markdown'); return
        except error.BadRequest: pass

    if data == "admin_reports" and uid in ADMIN_IDS:
        conn = get_conn(); cur = conn.cursor(); cur.execute("SELECT user_id, report_count FROM users WHERE report_count > 0 ORDER BY report_count DESC LIMIT 5"); users = cur.fetchall(); cur.close(); release_conn(conn)
        if not users: 
            try: await q.edit_message_text("✅ No reports.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_home")]])); return
            except error.BadRequest: pass
        kb = []
        for u in users: kb.append([InlineKeyboardButton(f"🔨 Ban {u[0]}", callback_data=f"ban_user_{u[0]}"), InlineKeyboardButton(f"✅ Clear {u[0]}", callback_data=f"clear_user_{u[0]}")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_home")])
        try: await q.edit_message_text("⚠️ **Flagged:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown'); return
        except error.BadRequest: pass

    if data == "admin_banlist" and uid in ADMIN_IDS:
        conn = get_conn(); cur = conn.cursor(); cur.execute("SELECT user_id, banned_until FROM users WHERE banned_until > NOW() LIMIT 5"); users = cur.fetchall(); cur.close(); release_conn(conn)
        if not users:
            try: await q.edit_message_text("✅ No bans.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_home")]])); return
            except error.BadRequest: pass
        kb = []
        for u in users: kb.append([InlineKeyboardButton(f"✅ Unban {u[0]}", callback_data=f"unban_user_{u[0]}")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_home")])
        try: await q.edit_message_text("🚫 **Bans:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown'); return
        except error.BadRequest: pass

    if data == "admin_feedbacks" and uid in ADMIN_IDS:
        conn = get_conn(); cur = conn.cursor(); cur.execute("SELECT message FROM feedback ORDER BY timestamp DESC LIMIT 5"); rows = cur.fetchall(); cur.close(); release_conn(conn)
        txt = "📨 **Recent Feedback:**\n\n" + ("\n".join([f"👤 `{r[0]}`: {r[1]}" for r in rows]) if rows else "No feedback.")
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="admin_home")]]
        try: await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown'); return
        except error.BadRequest: pass

    if data == "admin_home" and uid in ADMIN_IDS: await admin_panel(update, context); return

    if data.startswith("ban_user_") and uid in ADMIN_IDS: await admin_ban_command(update, context); return
    if data.startswith("clear_user_") and uid in ADMIN_IDS:
        tid = int(data.split("_")[2]); conn = get_conn(); cur = conn.cursor(); cur.execute("UPDATE users SET report_count = 0 WHERE user_id = %s", (tid,)); conn.commit(); cur.close(); release_conn(conn)
        try: await q.edit_message_text(f"✅ Cleared {tid}.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_reports")]]), parse_mode='Markdown'); return
        except error.BadRequest: pass

    if data.startswith("unban_user_") and uid in ADMIN_IDS:
        tid = int(data.split("_")[2]); conn = get_conn(); cur = conn.cursor(); cur.execute("UPDATE users SET banned_until = NULL WHERE user_id = %s", (tid,)); conn.commit(); cur.close(); release_conn(conn)
        try: await q.edit_message_text(f"✅ Unbanned {tid}.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_banlist")]]), parse_mode='Markdown'); return
        except error.BadRequest: pass

    # Rate Logic
    if data.startswith("rate_"):
        act, target = data.split("_")[1], int(data.split("_")[2])
        if act == "report":
            await handle_report(update, context, uid, target)
            k = [[InlineKeyboardButton("👎 Dislike & Block", callback_data=f"rate_dislike_{target}")]]
            try: await q.edit_message_text("⚠️ Reported.", reply_markup=InlineKeyboardMarkup(k))
            except error.BadRequest: pass
        else:
            sc = 1 if act == "like" else -1
            conn = get_conn(); cur = conn.cursor(); cur.execute("INSERT INTO user_interactions (rater_id, target_id, score) VALUES (%s, %s, %s)", (uid, target, sc)); conn.commit(); cur.close(); release_conn(conn)
            try: await q.edit_message_text("✅ Feedback Sent.")
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
        
        print("🤖 PHASE 15 (FULL) BOT LIVE")
        app.run_polling()
