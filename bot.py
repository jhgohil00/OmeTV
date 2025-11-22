import logging
import psycopg2
import datetime
import asyncio
import os
import threading
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
    ADMIN_IDS = [int(x) for x in admin_env.split(",") if x.strip().isdigit()]

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ==============================================================================
# ❤️ THE HEARTBEAT (Prevent Render from sleeping)
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
def get_db_connection():
    if not DATABASE_URL: return None
    try: return psycopg2.connect(DATABASE_URL)
    except Exception as e: print(f"❌ DB Error: {e}"); return None

def init_db():
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    
    # Core Users Table
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
    # Logging Tables
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_logs (
            id SERIAL PRIMARY KEY,
            sender_id BIGINT,
            receiver_id BIGINT,
            message TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id SERIAL PRIMARY KEY,
            reporter_id BIGINT,
            reported_id BIGINT,
            reason TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_interactions (
            id SERIAL PRIMARY KEY,
            rater_id BIGINT,
            target_id BIGINT,
            score INTEGER,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    # Migration: Add columns if they don't exist
    try:
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS report_count INTEGER DEFAULT 0;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS banned_until TIMESTAMP;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS gender TEXT DEFAULT 'Hidden';")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS age_range TEXT DEFAULT 'Hidden';")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS region TEXT DEFAULT 'Hidden';")
    except: pass

    conn.commit()
    cur.close()
    conn.close()
    print("✅ DATABASE READY.")

# ==============================================================================
# ⌨️ KEYBOARD LAYOUTS
# ==============================================================================
def get_keyboard_lobby():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🚀 Start Matching")],
        [KeyboardButton("🎯 Change Interests"), KeyboardButton("⚙️ Settings")],
        [KeyboardButton("🪪 My ID"), KeyboardButton("🆘 Help")]
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
# 🧠 MATCHMAKING ENGINE
# ==============================================================================
def find_match(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Get User Profile
    cur.execute("SELECT language, interests, age_range FROM users WHERE user_id = %s", (user_id,))
    me = cur.fetchone()
    if not me: return None, []
    my_lang, my_interests, my_age = me[0], me[1], me[2]
    my_tags = [t.strip().lower() for t in my_interests.split(',')] if my_interests else []

    # Get Disliked List
    cur.execute("SELECT target_id FROM user_interactions WHERE rater_id = %s AND score = -1", (user_id,))
    disliked_ids = {row[0] for row in cur.fetchall()}

    # Find Candidates
    cur.execute("""
        SELECT user_id, language, interests, age_range 
        FROM users 
        WHERE status = 'searching' 
        AND user_id != %s
        AND (banned_until IS NULL OR banned_until < NOW())
    """, (user_id,))
    candidates = cur.fetchall()
    
    best_match, best_score, common_interests = None, -999999, []

    for cand in candidates:
        cand_id, cand_lang, cand_interests, cand_age = cand
        cand_tags = [t.strip().lower() for t in cand_interests.split(',')] if cand_interests else []
        
        score = 0
        if cand_id in disliked_ids: score -= 1000
        
        # Logic: Interest > Language > Age
        matched_tags = list(set(my_tags) & set(cand_tags))
        if matched_tags: score += 40
        if cand_lang == my_lang: score += 20
        if cand_age == my_age and cand_age != 'Hidden': score += 10
            
        if score > best_score:
            best_score, best_match, common_interests = score, cand_id, matched_tags

    conn.close()
    return best_match, common_interests

# ==============================================================================
# 👮 ADMIN COMMANDS
# ==============================================================================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE status != 'idle'")
    online = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE report_count > 0")
    flagged = cur.fetchone()[0]
    
    # Geo Stats
    def get_stat(col):
        cur.execute(f"SELECT {col}, COUNT(*) FROM users GROUP BY {col} ORDER BY COUNT(*) DESC LIMIT 3")
        return " | ".join([f"{r[0]}:{r[1]}" for r in cur.fetchall()])

    msg = (f"👮 **CONTROL ROOM**\n👥 Total: `{total}` | 🟢 Online: `{online}`\n⚠️ Flagged: `{flagged}`\n\n"
           f"🌍 {get_stat('region')}\n🗣️ {get_stat('language')}")
    
    kb = [[InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"), InlineKeyboardButton("📜 Users", callback_data="admin_users")],
          [InlineKeyboardButton("⚠️ Reports", callback_data="admin_reports"), InlineKeyboardButton("🚫 Bans", callback_data="admin_banlist")]]
    
    try:
        if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        else: await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    except error.BadRequest: pass
    conn.close()

async def admin_ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        target = int(context.args[0])
        hours = int(context.args[1])
        conn = get_db_connection(); cur = conn.cursor()
        ban_until = datetime.datetime.now() + datetime.timedelta(hours=hours)
        cur.execute("UPDATE users SET banned_until = %s WHERE user_id = %s", (ban_until, target))
        conn.commit(); conn.close()
        await update.message.reply_text(f"🔨 Banned {target} for {hours}h.")
        try: await context.bot.send_message(target, f"🚫 Banned for {hours}h.")
        except: pass
    except: await update.message.reply_text("Usage: /ban ID HOURS")

async def admin_warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        target = int(context.args[0])
        reason = " ".join(context.args[1:])
        await context.bot.send_message(target, f"⚠️ **WARNING:** {reason}", parse_mode='Markdown')
        await update.message.reply_text(f"✅ Warned {target}.")
    except: await update.message.reply_text("Usage: /warn ID REASON")

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    msg = " ".join(context.args)
    if not msg: return await update.message.reply_text("Usage: /broadcast MSG")
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    users = cur.fetchall(); conn.close()
    
    await update.message.reply_text(f"📢 Sending to {len(users)} users...")
    for u in users:
        try: await context.bot.send_message(u[0], f"📢 **ANNOUNCEMENT:**\n\n{msg}", parse_mode='Markdown')
        except: pass
    await update.message.reply_text("✅ Done.")

# ==============================================================================
# 📱 USER COMMANDS & ONBOARDING
# ==============================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_db_connection(); cur = conn.cursor()
    
    # Ban Check
    cur.execute("SELECT banned_until, gender FROM users WHERE user_id = %s", (user.id,))
    data = cur.fetchone()
    if data and data[0] and data[0] > datetime.datetime.now():
        await update.message.reply_text(f"🚫 Banned until {data[0]}.")
        conn.close(); return

    # Register
    cur.execute("""
        INSERT INTO users (user_id, username, first_name) VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET username = %s, first_name = %s
    """, (user.id, user.username, user.first_name, user.username, user.first_name))
    conn.commit(); conn.close()

    if not data or data[1] == 'Hidden':
        await update.message.reply_text("👋 **Welcome!**\nLet's set up your vibe.", reply_markup=ReplyKeyboardRemove(), parse_mode='Markdown')
        await send_onboarding_step(update, 1)
    else:
        msg = await update.message.reply_text("🔄 Loading...", reply_markup=ReplyKeyboardRemove())
        try: await context.bot.delete_message(chat_id=user.id, message_id=msg.message_id)
        except: pass
        await show_main_menu(update)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🆘 **HELP**\n\n🚀 Start: Find partner\n🛑 Stop: End chat\n⏭️ Next: Skip", parse_mode='Markdown')

async def send_onboarding_step(update, step):
    kb = []
    msg = ""
    if step == 1:
        msg = "1️⃣ **Gender?**"
        kb = [[InlineKeyboardButton("Male", callback_data="set_gen_Male"), InlineKeyboardButton("Female", callback_data="set_gen_Female")], [InlineKeyboardButton("Skip", callback_data="set_gen_Hidden")]]
    elif step == 2:
        msg = "2️⃣ **Age?**"
        kb = [[InlineKeyboardButton("18-22", callback_data="set_age_18-22"), InlineKeyboardButton("23-30", callback_data="set_age_23-30")], [InlineKeyboardButton("Skip", callback_data="set_age_Hidden")]]
    elif step == 3:
        msg = "3️⃣ **Language?**"
        kb = [[InlineKeyboardButton("English", callback_data="set_lang_English"), InlineKeyboardButton("Hindi", callback_data="set_lang_Hindi")], [InlineKeyboardButton("Skip", callback_data="set_lang_English")]]
    elif step == 4:
        msg = "4️⃣ **Region?**"
        kb = [[InlineKeyboardButton("Asia", callback_data="set_reg_Asia"), InlineKeyboardButton("Europe", callback_data="set_reg_Europe")], [InlineKeyboardButton("Skip", callback_data="set_reg_Hidden")]]
    elif step == 5:
        msg = "5️⃣ **Mood?**"
        kb = [[InlineKeyboardButton("Happy", callback_data="set_mood_Happy"), InlineKeyboardButton("Bored", callback_data="set_mood_Bored")], [InlineKeyboardButton("Skip", callback_data="set_mood_Neutral")]]
    elif step == 6:
        msg = "6️⃣ **Interests?**\nType keywords (Movies, Tech) or click Skip."
        kb = [[InlineKeyboardButton("Skip", callback_data="onboarding_done")]]

    try:
        if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        else: await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    except: pass

# ==============================================================================
# 🎮 MAIN LOGIC
# ==============================================================================
async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    text = update.message.text
    user_id = update.effective_user.id

    # Onboarding Interest Input
    if context.user_data.get("state") == "ONBOARDING_INTEREST":
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("UPDATE users SET interests = %s WHERE user_id = %s", (text, user_id))
        conn.commit(); conn.close()
        context.user_data["state"] = None
        await update.message.reply_text("✅ Saved!", reply_markup=get_keyboard_lobby())
        return

    # Buttons
    if text == "🚀 Start Matching": await start_search(update, context); return
    if text in ["🛑 Stop", "🛑 Stop Chat"]: await stop_chat(update, context); return
    if text == "⏭️ Next": await stop_chat(update, context, is_next=True); return
    if text == "🎯 Change Interests":
        context.user_data["state"] = "ONBOARDING_INTEREST"
        await update.message.reply_text("👇 Type new interests:", reply_markup=ReplyKeyboardRemove()); return
    
    if text == "⚙️ Settings":
        kb = [[InlineKeyboardButton("Lang", callback_data="set_lang"), InlineKeyboardButton("Mood", callback_data="set_mood")], [InlineKeyboardButton("Close", callback_data="close_settings")]]
        await update.message.reply_text("⚙️ Settings:", reply_markup=InlineKeyboardMarkup(kb)); return

    if text == "🪪 My ID": await show_profile(update, context); return
    if text == "🆘 Help": await help_command(update, context); return
    if text == "🎮 Games": await update.message.reply_text("🎮 Games coming soon!", reply_markup=get_keyboard_game()); return
    if text == "🛑 Stop Game": await update.message.reply_text("🎮 Game Ended.", reply_markup=get_keyboard_chat()); return

    # Commands
    if text == "/stop": await stop_chat(update, context); return
    if text == "/admin": await admin_panel(update, context); return
    if text.startswith("/ban"): await admin_ban_command(update, context); return
    if text.startswith("/warn"): await admin_warn_command(update, context); return
    if text.startswith("/broadcast"): await admin_broadcast(update, context); return

    await relay_message(update, context)

# ==============================================================================
# 🔌 CONNECTION LOGIC
# ==============================================================================
async def start_search(update, context):
    user_id = update.effective_user.id
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT status FROM users WHERE user_id = %s", (user_id,))
    if cur.fetchone()[0] == 'chatting':
        await update.message.reply_text("⛔ Already in chat!"); conn.close(); return
    cur.execute("UPDATE users SET status = 'searching' WHERE user_id = %s", (user_id,))
    conn.commit(); conn.close()
    
    await update.message.reply_text("📡 **Scanning...**", parse_mode='Markdown', reply_markup=ReplyKeyboardRemove())
    if context.job_queue: context.job_queue.run_once(send_reroll_option, 15, data=user_id)
    await perform_match(update, context, user_id)

async def perform_match(update, context, user_id):
    partner_id, common = find_match(user_id)
    if partner_id:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("UPDATE users SET status='chatting', partner_id=%s WHERE user_id=%s", (partner_id, user_id))
        cur.execute("UPDATE users SET status='chatting', partner_id=%s WHERE user_id=%s", (user_id, partner_id))
        conn.commit(); conn.close()
        
        msg = f"⚡ **CONNECTED!**\nMatch: {', '.join(common) if common else 'Random'}"
        await context.bot.send_message(user_id, msg, reply_markup=get_keyboard_chat(), parse_mode='Markdown')
        try: await context.bot.send_message(partner_id, msg, reply_markup=get_keyboard_chat(), parse_mode='Markdown')
        except: pass

async def stop_chat(update, context, is_next=False):
    user_id = update.effective_user.id
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT partner_id, status FROM users WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    partner, status = (row[0], row[1]) if row else (0, 'idle')
    
    if status != 'chatting':
        if is_next: await start_search(update, context)
        else: await update.message.reply_text("⛔ Not in chat.", reply_markup=get_keyboard_lobby())
        conn.close(); return

    cur.execute("UPDATE users SET status='idle', partner_id=0 WHERE user_id IN (%s, %s)", (user_id, partner))
    conn.commit(); conn.close()
    
    if is_next:
        await update.message.reply_text("⏭️ Skipping...", reply_markup=ReplyKeyboardRemove())
        await start_search(update, context)
    else:
        k = [[InlineKeyboardButton("👍", callback_data=f"rate_like_{partner}"), InlineKeyboardButton("👎", callback_data=f"rate_dislike_{partner}")], [InlineKeyboardButton("⚠️ Report", callback_data=f"rate_report_{partner}")]]
        await update.message.reply_text("🔌 Disconnected.", reply_markup=get_keyboard_lobby())
        await update.message.reply_text("Feedback?", reply_markup=InlineKeyboardMarkup(k))

    if partner:
        try: await context.bot.send_message(partner, "🔌 Partner disconnected.", reply_markup=get_keyboard_lobby())
        except: pass

async def relay_message(update, context):
    user_id = update.effective_user.id
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT status, partner_id FROM users WHERE user_id = %s", (user_id,))
    data = cur.fetchone()
    conn.close()
    if data and data[0] == 'chatting' and data[1] != 0:
        if update.message.text:
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("INSERT INTO chat_logs (sender_id, receiver_id, message) VALUES (%s, %s, %s)", (user_id, data[1], update.message.text))
            conn.commit(); conn.close()
        try: await update.message.copy(chat_id=data[1])
        except: await stop_chat(update, context)

# ==============================================================================
# 🧩 HELPERS
# ==============================================================================
async def send_reroll_option(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT status FROM users WHERE user_id = %s", (user_id,))
    if cur.fetchone()[0] == 'searching':
        kb = [[InlineKeyboardButton("🎲 Try Random", callback_data="force_random")]]
        try: await context.bot.send_message(user_id, "🐢 Taking a while...", reply_markup=InlineKeyboardMarkup(kb))
        except: pass
    conn.close()

async def show_profile(update, context):
    user_id = update.effective_user.id
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT language, interests, karma_score FROM users WHERE user_id = %s", (user_id,))
    data = cur.fetchone()
    conn.close()
    await update.message.reply_text(f"👤 **ID CARD**\nLang: {data[0]}\nTags: {data[1]}\nKarma: {data[2]}%", parse_mode='Markdown')

async def show_main_menu(update):
    await update.message.reply_text("👋 **Lobby**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown')

async def handle_report(update, context, reporter, reported):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE users SET report_count = report_count + 1 WHERE user_id = %s RETURNING report_count", (reported,))
    cnt = cur.fetchone()[0]
    cur.execute("INSERT INTO reports (reporter_id, reported_id, reason) VALUES (%s, %s, 'Report')", (reporter, reported))
    conn.commit()
    if cnt >= 3:
        cur.execute("SELECT message FROM chat_logs WHERE sender_id = %s ORDER BY timestamp DESC LIMIT 5", (reported,))
        logs = [l[0] for l in cur.fetchall()]
        msg = f"🚨 **REPORT (3+)**\nUser: {reported}\nLogs: {logs}"
        for a in ADMIN_IDS:
            try: await context.bot.send_message(a, msg)
            except: pass
    conn.close()

async def update_user(user_id, col, val):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute(f"UPDATE users SET {col} = %s WHERE user_id = %s", (val, user_id))
    conn.commit(); conn.close()

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = q.from_user.id

    if data == "force_random": await perform_match(update, context, uid); return
    if data == "close_settings": await q.delete_message(); return
    
    # Onboarding Logic (Fully Expanded)
    if data.startswith("set_gen_"): await update_user(uid, "gender", data.split("_")[2]); await send_onboarding_step(update, 2); return
    if data.startswith("set_age_"): await update_user(uid, "age_range", data.split("_")[2]); await send_onboarding_step(update, 3); return
    if data.startswith("set_lang_"): await update_user(uid, "language", data.split("_")[2]); await send_onboarding_step(update, 4); return
    if data.startswith("set_reg_"): await update_user(uid, "region", data.split("_")[2]); await send_onboarding_step(update, 5); return
    if data.startswith("set_mood_"): await update_user(uid, "mood", data.split("_")[2]); context.user_data["state"] = "ONBOARDING_INTEREST"; await send_onboarding_step(update, 6); return
    
    if data == "onboarding_done":
        context.user_data["state"] = None
        await show_main_menu(update); return
    
    if data == "restart_onboarding": await send_onboarding_step(update, 1); return

    # Admin Logic
    if data == "admin_broadcast" and uid in ADMIN_IDS: await admin_broadcast(update, context); return
    if data == "admin_users" and uid in ADMIN_IDS: await admin_panel(update, context); return
    if data == "admin_banlist" and uid in ADMIN_IDS: await admin_panel(update, context); return
    
    # Rate Logic
    if data.startswith("rate_"):
        act, target = data.split("_")[1], int(data.split("_")[2])
        if act == "report": await handle_report(update, context, uid, target); await q.edit_message_text("⚠️ Reported.")
        else:
            sc = 1 if act == "like" else -1
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("INSERT INTO user_interactions (rater_id, target_id, score) VALUES (%s, %s, %s)", (uid, target, sc))
            conn.commit(); conn.close()
            await q.edit_message_text("✅ Feedback Sent.")

if __name__ == '__main__':
    if not BOT_TOKEN: print("ERROR: Config missing")
    else:
        init_db()
        flask_thread = threading.Thread(target=run_flask); flask_thread.daemon = True; flask_thread.start()
        req = HTTPXRequest(connect_timeout=60, read_timeout=60)
        app = ApplicationBuilder().token(BOT_TOKEN).request(req).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("admin", admin_panel))
        app.add_handler(CommandHandler("ban", admin_ban_command))
        app.add_handler(CommandHandler("warn", admin_warn_command))
        app.add_handler(CommandHandler("broadcast", admin_broadcast))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_input))
        app.add_handler(CallbackQueryHandler(button_handler))
        app.add_handler(MessageHandler(filters.ALL, relay_message))
        print("🤖 BOT LIVE")
        app.run_polling()
