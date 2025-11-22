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
# 🔐 CONFIGURATION
# ==============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
admin_env = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in admin_env.split(",") if x.strip().isdigit()]

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ==============================================================================
# 🧠 RAM STATE
# ==============================================================================
ACTIVE_CHATS = {}
GAME_STATES = {}
GAME_COOLDOWNS = {}
DB_POOL = None

GAME_DATA = {
    "tod_truth": ["Biggest fear?", "Last lie?", "Secret crush?", "Embarrassing moment?", "Cheated on test?", "Worst gift?", "Biggest regret?", "Last time cried?", "Deepest secret?", "Switch lives with?"],
    "tod_dare": ["Sing voice note.", "Send 3rd photo.", "Type with nose.", "Send selfie sticker.", "10 pushups video.", "Emoji talk 3 turns.", "Describe crush.", "Home screen screenshot."],
    "wyr": [("Invisible", "Fly"), ("Cold", "Hot"), ("Rich", "Time"), ("How die", "When die"), ("Space", "Ocean"), ("Animals", "Languages")]
}

# ==============================================================================
# ❤️ HEARTBEAT
# ==============================================================================
app_flask = Flask(__name__)
@app_flask.route('/')
def health_check(): return "Alive", 200
def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app_flask.run(host="0.0.0.0", port=port)

# ==============================================================================
# 🛠️ DATABASE
# ==============================================================================
def init_db_pool():
    global DB_POOL
    if not DATABASE_URL: return
    try: DB_POOL = psycopg2.pool.SimpleConnectionPool(1, 20, dsn=DATABASE_URL)
    except: pass

def get_conn(): return DB_POOL.getconn() if DB_POOL else None
def release_conn(conn): 
    if DB_POOL and conn: DB_POOL.putconn(conn)

def init_db():
    init_db_pool(); conn = get_conn()
    if not conn: return
    cur = conn.cursor()
    tables = [
        """CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT, language TEXT DEFAULT 'English', gender TEXT DEFAULT 'Hidden', age_range TEXT DEFAULT 'Hidden', region TEXT DEFAULT 'Hidden', interests TEXT DEFAULT '', mood TEXT DEFAULT 'Neutral', karma_score INTEGER DEFAULT 100, status TEXT DEFAULT 'idle', partner_id BIGINT DEFAULT 0, report_count INTEGER DEFAULT 0, banned_until TIMESTAMP, joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);""",
        """CREATE TABLE IF NOT EXISTS chat_logs (id SERIAL PRIMARY KEY, sender_id BIGINT, receiver_id BIGINT, message TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP);""",
        """CREATE TABLE IF NOT EXISTS reports (id SERIAL PRIMARY KEY, reporter_id BIGINT, reported_id BIGINT, reason TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP);""",
        """CREATE TABLE IF NOT EXISTS user_interactions (id SERIAL PRIMARY KEY, rater_id BIGINT, target_id BIGINT, score INTEGER, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP);""",
        """CREATE TABLE IF NOT EXISTS feedback (id SERIAL PRIMARY KEY, user_id BIGINT, message TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP);"""
    ]
    for t in tables: cur.execute(t)
    try:
        cols = ["username TEXT", "first_name TEXT", "report_count INTEGER DEFAULT 0", "banned_until TIMESTAMP", "gender TEXT DEFAULT 'Hidden'", "age_range TEXT DEFAULT 'Hidden'", "region TEXT DEFAULT 'Hidden'"]
        for c in cols: cur.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {c};")
    except: pass
    conn.commit(); cur.close(); release_conn(conn)

# ==============================================================================
# ⌨️ KEYBOARDS
# ==============================================================================
def get_keyboard_lobby():
    return ReplyKeyboardMarkup([[KeyboardButton("🚀 Start Matching")], [KeyboardButton("🎯 Change Interests"), KeyboardButton("⚙️ Settings")], [KeyboardButton("🪪 My ID"), KeyboardButton("🆘 Help")]], resize_keyboard=True)
def get_keyboard_searching():
    return ReplyKeyboardMarkup([[KeyboardButton("❌ Stop Searching")]], resize_keyboard=True)
def get_keyboard_chat():
    return ReplyKeyboardMarkup([[KeyboardButton("🎮 Games")], [KeyboardButton("⏭️ Next"), KeyboardButton("🛑 Stop")]], resize_keyboard=True)
def get_keyboard_game():
    return ReplyKeyboardMarkup([[KeyboardButton("🛑 Stop Game"), KeyboardButton("🛑 Stop Chat")]], resize_keyboard=True)

# ==============================================================================
# 🧩 HELPER FUNCTIONS (Defined BEFORE use)
# ==============================================================================
async def show_main_menu(update):
    # Universal menu shower
    kb = get_keyboard_lobby()
    try:
        if hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.message.reply_text("👋 **Lobby**", reply_markup=kb, parse_mode='Markdown')
        elif hasattr(update, 'message') and update.message:
            await update.message.reply_text("👋 **Lobby**", reply_markup=kb, parse_mode='Markdown')
    except: pass

async def update_user(user_id, col, val):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(f"UPDATE users SET {col} = %s WHERE user_id = %s", (val, user_id))
    conn.commit(); cur.close(); release_conn(conn)

async def send_onboarding_step(update, step):
    kb, msg = [], ""
    if step == 1: msg, kb = "1️⃣ **Gender?**", [[InlineKeyboardButton("👨 Male", callback_data="set_gen_Male"), InlineKeyboardButton("👩 Female", callback_data="set_gen_Female")], [InlineKeyboardButton("🌈 Other", callback_data="set_gen_Other"), InlineKeyboardButton("⏭️ Skip", callback_data="set_gen_Hidden")]]
    elif step == 2: msg, kb = "2️⃣ **Age?**", [[InlineKeyboardButton("🐣 ~18", callback_data="set_age_~18"), InlineKeyboardButton("🧢 20-25", callback_data="set_age_20-25")], [InlineKeyboardButton("💼 25-30", callback_data="set_age_25-30"), InlineKeyboardButton("☕ 30+", callback_data="set_age_30+")], [InlineKeyboardButton("⏭️ Skip", callback_data="set_age_Hidden")]]
    elif step == 3: msg, kb = "3️⃣ **Lang?**", [[InlineKeyboardButton("🇺🇸 Eng", callback_data="set_lang_English"), InlineKeyboardButton("🇮🇳 Hin", callback_data="set_lang_Hindi"), InlineKeyboardButton("🇮🇩 Indo", callback_data="set_lang_Indo")], [InlineKeyboardButton("🇪🇸 Spa", callback_data="set_lang_Spanish"), InlineKeyboardButton("🇫🇷 Fre", callback_data="set_lang_French"), InlineKeyboardButton("🇯🇵 Jap", callback_data="set_lang_Japanese")], [InlineKeyboardButton("🌍 Other", callback_data="set_lang_Other"), InlineKeyboardButton("⏭️ Skip", callback_data="set_lang_English")]]
    elif step == 4: msg, kb = "4️⃣ **Region?**", [[InlineKeyboardButton("🌏 Asia", callback_data="set_reg_Asia"), InlineKeyboardButton("🌍 Europe", callback_data="set_reg_Europe")], [InlineKeyboardButton("🌎 America", callback_data="set_reg_America"), InlineKeyboardButton("🌍 Africa", callback_data="set_reg_Africa")], [InlineKeyboardButton("⏭️ Skip", callback_data="set_reg_Hidden")]]
    elif step == 5: msg, kb = "5️⃣ **Mood?**", [[InlineKeyboardButton("😃 Happy", callback_data="set_mood_Happy"), InlineKeyboardButton("😔 Sad", callback_data="set_mood_Sad")], [InlineKeyboardButton("😴 Bored", callback_data="set_mood_Bored"), InlineKeyboardButton("🤔 IDK", callback_data="set_mood_Confused")], [InlineKeyboardButton("🥀 Lonely", callback_data="set_mood_Lonely"), InlineKeyboardButton("⏭️ Skip", callback_data="set_mood_Neutral")]]
    elif step == 6: msg, kb = "6️⃣ **Interests?**\nType keywords or Skip.", [[InlineKeyboardButton("⏭️ Skip & Finish", callback_data="onboarding_done")]]

    try:
        if hasattr(update, 'callback_query') and update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        else: await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    except: pass

async def show_profile(update, context):
    user_id = update.effective_user.id
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT language, interests, karma_score, gender, age_range, region, mood FROM users WHERE user_id = %s", (user_id,))
    data = cur.fetchone(); cur.close(); release_conn(conn)
    text = f"👤 **IDENTITY**\n━━━━━━━━━━━━━━━━\n🗣️ {data[0]}\n🏷️ {data[1]}\n🚻 {data[3]}\n🎂 {data[4]}\n🌍 {data[5]}\n🎭 {data[6]}\n🛡️ {data[2]}%"
    await update.message.reply_text(text, parse_mode='Markdown')

async def send_reroll_option(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT status FROM users WHERE user_id = %s", (user_id,))
    if cur.fetchone()[0] == 'searching':
        kb = [[InlineKeyboardButton("🎲 Try Random", callback_data="force_random")]]
        try: await context.bot.send_message(user_id, "🐢 **Quiet...**", reply_markup=InlineKeyboardMarkup(kb))
        except: pass
    cur.close(); release_conn(conn)

def find_match(user_id):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT language, interests, age_range, mood FROM users WHERE user_id = %s", (user_id,))
    me = cur.fetchone()
    if not me: release_conn(conn); return None, [], "Neutral", "English"
    my_lang, my_interests, my_age, my_mood = me
    my_tags = [t.strip().lower() for t in my_interests.split(',')] if my_interests else []
    cur.execute("SELECT target_id FROM user_interactions WHERE rater_id = %s AND score = -1", (user_id,))
    disliked_ids = {row[0] for row in cur.fetchall()}
    cur.execute("""SELECT user_id, language, interests, age_range, mood FROM users WHERE status = 'searching' AND user_id != %s AND (banned_until IS NULL OR banned_until < NOW())""", (user_id,))
    candidates = cur.fetchall()
    best_match, best_score, common, p_mood, p_lang = None, -999999, [], "Neutral", "English"
    for cand in candidates:
        cand_id, cand_lang, cand_interests, cand_age, cand_mood = cand
        if cand_id in disliked_ids: continue # Skip disliked
        score = 0
        matched = list(set(my_tags) & set([t.strip().lower() for t in cand_interests.split(',')] if cand_interests else []))
        if matched: score += 40
        if cand_lang == my_lang: score += 20
        if cand_age == my_age: score += 10
        if score > best_score: best_score, best_match, common, p_mood, p_lang = score, cand_id, matched, cand_mood, cand_lang
    cur.close(); release_conn(conn)
    return best_match, common, p_mood, p_lang

# ==============================================================================
# 🎮 GAME & ADMIN LOGIC
# ==============================================================================
async def offer_game(update, context, user_id, game_name):
    partner_id = ACTIVE_CHATS.get(user_id)
    if not partner_id: return
    last = GAME_COOLDOWNS.get(user_id, 0)
    if time.time() - last < 60: await context.bot.send_message(user_id, f"⏳ Wait {int(60 - (time.time() - last))}s."); return
    GAME_COOLDOWNS[user_id] = time.time()
    all_games = ["Truth or Dare", "Would You Rather", "Rock Paper Scissors"]
    sugs = [g for g in all_games if g != game_name]
    kb = [[InlineKeyboardButton("✅ Accept", callback_data=f"game_accept_{game_name}"), InlineKeyboardButton("❌ Reject", callback_data="game_reject")],
          [InlineKeyboardButton(f"💡 Suggest {sugs[0]}", callback_data=f"game_offer_{sugs[0]}"), InlineKeyboardButton(f"💡 Suggest {sugs[1]}", callback_data=f"game_offer_{sugs[1]}")]]
    await context.bot.send_message(user_id, f"🎮 Offered {game_name}...", parse_mode='Markdown')
    await context.bot.send_message(partner_id, f"🎮 **Game Request**\nPartner wants to play **{game_name}**.", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def start_game_session(update, context, game_name, p1, p2):
    GAME_STATES[p1] = GAME_STATES[p2] = {"game": game_name, "turn": p1, "partner": p2}
    kb = get_keyboard_game()
    await context.bot.send_message(p1, f"🎮 **Started: {game_name}**", reply_markup=kb, parse_mode='Markdown')
    await context.bot.send_message(p2, f"🎮 **Started: {game_name}**", reply_markup=kb, parse_mode='Markdown')
    if game_name == "Truth or Dare": await send_tod_turn(context, p1)
    elif game_name == "Would You Rather": await send_wyr_round(context, p1, p2)
    elif game_name == "Rock Paper Scissors": await send_rps_round(context, p1, p2)

async def send_tod_turn(context, turn_id):
    kb = [[InlineKeyboardButton("🟢 Truth", callback_data="tod_pick_truth"), InlineKeyboardButton("🔴 Dare", callback_data="tod_pick_dare")]]
    await context.bot.send_message(turn_id, "🫵 **Your Turn!**", reply_markup=InlineKeyboardMarkup(kb))

async def send_tod_options(update, context, mode):
    user = update.effective_user
    options = random.sample(GAME_DATA[f"tod_{mode}"], 5)
    kb = [[InlineKeyboardButton(opt[:30]+"...", callback_data=f"tod_send_{i}")] for i, opt in enumerate(options)]
    kb.append([InlineKeyboardButton("✍️ Manual", callback_data="tod_manual")])
    GAME_STATES[user.id]["options"] = options
    await update.callback_query.edit_message_text(f"🎭 **Pick a {mode.upper()}:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def send_wyr_round(context, p1, p2):
    q = random.choice(GAME_DATA["wyr"])
    kb = [[InlineKeyboardButton(f"🅰️ {q[0]}", callback_data="wyr_a"), InlineKeyboardButton(f"🅱️ {q[1]}", callback_data="wyr_b")]]
    await context.bot.send_message(p1, "⚖️ **Would You Rather...**", reply_markup=InlineKeyboardMarkup(kb))
    await context.bot.send_message(p2, "⚖️ **Would You Rather...**", reply_markup=InlineKeyboardMarkup(kb))

async def send_rps_round(context, p1, p2):
    kb = [[InlineKeyboardButton("🪨", callback_data="rps_rock"), InlineKeyboardButton("📄", callback_data="rps_paper"), InlineKeyboardButton("✂️", callback_data="rps_scissors")]]
    await context.bot.send_message(p1, "✂️ **Shoot!**", reply_markup=InlineKeyboardMarkup(kb))
    await context.bot.send_message(p2, "✂️ **Shoot!**", reply_markup=InlineKeyboardMarkup(kb))

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users"); total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE status != 'idle'"); online = cur.fetchone()[0]
    msg = f"👮 **CONTROL**\n👥 {total} | 🟢 {online}"
    kb = [[InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast_info"), InlineKeyboardButton("📜 Users", callback_data="admin_users")],
          [InlineKeyboardButton("⚠️ Reports", callback_data="admin_reports"), InlineKeyboardButton("📨 Feedback", callback_data="admin_feedbacks")],
          [InlineKeyboardButton("🚫 Bans", callback_data="admin_banlist")]]
    try:
        if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        else: await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    except: pass
    cur.close(); release_conn(conn)

async def handle_report(update, context, reporter, reported):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET report_count = report_count + 1 WHERE user_id = %s RETURNING report_count", (reported,))
    cnt = cur.fetchone()[0]
    cur.execute("INSERT INTO reports (reporter_id, reported_id, reason) VALUES (%s, %s, 'Report')", (reporter, reported))
    conn.commit(); cur.close(); release_conn(conn)
    if cnt >= 3:
        msg = f"🚨 **REPORT (3+)**\nUser: `{reported}`"
        kb = [[InlineKeyboardButton(f"🔨 BAN {reported}", callback_data=f"ban_user_{reported}")]]
        for a in ADMIN_IDS:
            try: await context.bot.send_message(a, msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
            except: pass

async def admin_broadcast_execute(update, context):
    if update.effective_user.id not in ADMIN_IDS: return
    msg = " ".join(context.args)
    if not msg: await update.message.reply_text("Usage: /broadcast msg"); return
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM users"); users = cur.fetchall(); cur.close(); release_conn(conn)
    await update.message.reply_text(f"📢 Sending to {len(users)}...")
    for u in users:
        try: await context.bot.send_message(u[0], f"📢 **ANNOUNCEMENT**\n\n{msg}", parse_mode='Markdown')
        except: pass
    await update.message.reply_text("✅ Done.")

async def handle_feedback_command(update, context):
    uid = update.effective_user.id; text = update.message.text.replace("/feedback", "").strip()
    if not text: await update.message.reply_text("Usage: /feedback msg"); return
    conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO feedback (user_id, message) VALUES (%s, %s)", (uid, text))
    conn.commit(); cur.close(); release_conn(conn)
    await update.message.reply_text("✅ Sent.")

async def admin_ban_command(update, context):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        t, h = int(context.args[0]), int(context.args[1])
        conn = get_conn(); cur = conn.cursor()
        bu = datetime.datetime.now() + datetime.timedelta(hours=h)
        cur.execute("UPDATE users SET banned_until = %s WHERE user_id = %s", (bu, t))
        conn.commit(); cur.close(); release_conn(conn)
        await update.message.reply_text(f"🔨 Banned {t}")
        if t in ACTIVE_CHATS: del ACTIVE_CHATS[t]
    except: pass

async def admin_warn_command(update, context):
    if update.effective_user.id not in ADMIN_IDS: return
    try: await context.bot.send_message(int(context.args[0]), f"⚠️ **WARNING**\n{' '.join(context.args[1:])}", parse_mode='Markdown')
    except: pass

# ==============================================================================
# 📱 MAIN CONTROLLER
# ==============================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT banned_until, gender FROM users WHERE user_id = %s", (user.id,))
    data = cur.fetchone()
    if data and data[0] and data[0] > datetime.datetime.now():
        await update.message.reply_text(f"🚫 Banned."); cur.close(); release_conn(conn); return
    cur.execute("""INSERT INTO users (user_id, username, first_name) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET username = %s, first_name = %s""", (user.id, user.username, user.first_name, user.username, user.first_name))
    conn.commit(); cur.close(); release_conn(conn)
    
    welcome = "👋 **Welcome to OmeTV!**\nConnect globally. 🌍\n\n*Vibe Check:* 👇"
    if not data or data[1] == 'Hidden':
        await update.message.reply_text(welcome, reply_markup=ReplyKeyboardRemove(), parse_mode='Markdown')
        await send_onboarding_step(update, 1)
    else:
        msg = await update.message.reply_text("🔄 Loading...", reply_markup=ReplyKeyboardRemove())
        try: await context.bot.delete_message(chat_id=user.id, message_id=msg.message_id)
        except: pass
        await show_main_menu(update)

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    text = update.message.text
    user_id = update.effective_user.id

    if context.user_data.get("state") == "GAME_MANUAL":
        pid = ACTIVE_CHATS.get(user_id)
        if pid:
            await context.bot.send_message(pid, f"❓ **Question:** {text}", parse_mode='Markdown')
            await update.message.reply_text("✅ Sent.")
            GAME_STATES[user_id]["turn"] = pid; GAME_STATES[pid]["turn"] = pid
            if GAME_STATES[user_id]["game"] == "Truth or Dare": await send_tod_turn(context, pid)
        context.user_data["state"] = None; return

    if context.user_data.get("state") == "ONBOARDING_INTEREST":
        await update_user(user_id, "interests", text)
        context.user_data["state"] = None
        await update.message.reply_text("✅ **Ready!**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown'); return

    if text == "🚀 Start Matching": await start_search(update, context); return
    if text in ["🛑 Stop", "🛑 Stop Chat"]: await stop_chat(update, context); return
    if text == "⏭️ Next": await stop_chat(update, context, is_next=True); return
    if text == "❌ Stop Searching": await stop_search_process(update, context); return
    if text == "🎯 Change Interests": context.user_data["state"] = "ONBOARDING_INTEREST"; await update.message.reply_text("👇 Type interests:", reply_markup=ReplyKeyboardRemove()); return
    if text == "⚙️ Settings": 
        kb = [[InlineKeyboardButton("Lang", callback_data="set_lang_English"), InlineKeyboardButton("Mood", callback_data="set_mood_Neutral")], [InlineKeyboardButton("Close", callback_data="close_settings")]]
        await update.message.reply_text("⚙️ Settings:", reply_markup=InlineKeyboardMarkup(kb)); return
    if text == "🪪 My ID": await show_profile(update, context); return
    if text == "🆘 Help": await update.message.reply_text("🆘 **HELP**\n🚀 Start: Match\n🛑 Stop: End\n🎮 Games: Play"); return
    
    if text == "🎮 Games":
        kb = [[InlineKeyboardButton("😈 Truth or Dare", callback_data="game_offer_Truth or Dare")], [InlineKeyboardButton("🎲 Would You Rather", callback_data="game_offer_Would You Rather")], [InlineKeyboardButton("✂️ Rock Paper Scissors", callback_data="game_offer_Rock Paper Scissors")]]
        await update.message.reply_text("🎮 **Game Center**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown'); return
    if text == "🛑 Stop Game":
        pid = ACTIVE_CHATS.get(user_id)
        if user_id in GAME_STATES: del GAME_STATES[user_id]
        if pid and pid in GAME_STATES: del GAME_STATES[pid]
        await update.message.reply_text("🛑 Game Stopped.", reply_markup=get_keyboard_chat())
        if pid: await context.bot.send_message(pid, "🛑 Partner stopped game.", reply_markup=get_keyboard_chat())
        return

    if text.startswith("/"):
        if text == "/stop": await stop_chat(update, context); return
        if text == "/admin": await admin_panel(update, context); return
        if text.startswith("/ban"): await admin_ban_command(update, context); return
        if text.startswith("/warn"): await admin_warn_command(update, context); return
        if text.startswith("/broadcast"): await admin_broadcast_execute(update, context); return
        if text.startswith("/feedback"): await handle_feedback_command(update, context); return

    await relay_message(update, context)

async def start_search(update, context):
    user_id = update.effective_user.id
    if user_id in ACTIVE_CHATS: await update.message.reply_text("⛔ In chat."); return
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET status = 'searching' WHERE user_id = %s", (user_id,))
    conn.commit()
    cur.execute("SELECT interests FROM users WHERE user_id = %s", (user_id,))
    tags = cur.fetchone()[0] or "Any"
    cur.close(); release_conn(conn)
    await update.message.reply_text(f"📡 **Scanning...**\nTags: `{tags}`", parse_mode='Markdown', reply_markup=get_keyboard_searching())
    if context.job_queue: context.job_queue.run_once(send_reroll_option, 15, data=user_id)
    await perform_match(update, context, user_id)

async def stop_search_process(update, context):
    user_id = update.effective_user.id
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET status = 'idle' WHERE user_id = %s", (user_id,))
    conn.commit(); cur.close(); release_conn(conn)
    await update.message.reply_text("🛑 Stopped.", reply_markup=get_keyboard_lobby())

async def perform_match(update, context, user_id):
    pid, common, p_mood, p_lang = find_match(user_id)
    if pid:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("UPDATE users SET status='chatting', partner_id=%s WHERE user_id=%s", (pid, user_id))
        cur.execute("UPDATE users SET status='chatting', partner_id=%s WHERE user_id=%s", (user_id, pid))
        conn.commit(); cur.close(); release_conn(conn)
        ACTIVE_CHATS[user_id] = pid; ACTIVE_CHATS[pid] = user_id
        msg = f"⚡ **CONNECTED!**\n🎭 {p_mood}\n🔗 {', '.join(common) if common else 'Random'}\n🗣️ {p_lang}"
        kb = get_keyboard_chat()
        await context.bot.send_message(user_id, msg, reply_markup=kb, parse_mode='Markdown')
        try: await context.bot.send_message(pid, msg, reply_markup=kb, parse_mode='Markdown')
        except: pass

async def stop_chat(update, context, is_next=False):
    user_id = update.effective_user.id
    pid = ACTIVE_CHATS.pop(user_id, 0)
    if pid and pid in ACTIVE_CHATS: del ACTIVE_CHATS[pid]
    if user_id in GAME_STATES: del GAME_STATES[user_id]
    if pid in GAME_STATES: del GAME_STATES[pid]
    
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET status='idle', partner_id=0 WHERE user_id IN (%s, %s)", (user_id, pid))
    conn.commit(); cur.close(); release_conn(conn)
    
    k = [[InlineKeyboardButton("👍", callback_data=f"rate_like_{pid}"), InlineKeyboardButton("👎", callback_data=f"rate_dislike_{pid}")], [InlineKeyboardButton("⚠️ Report", callback_data=f"rate_report_{pid}")], [InlineKeyboardButton("🚀 New", callback_data="action_search"), InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]]
    
    if is_next:
        await update.message.reply_text("⏭️ Skipping...", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("📊 Feedback?", reply_markup=InlineKeyboardMarkup(k))
        await start_search(update, context)
    else:
        await update.message.reply_text("🔌 Disconnected.", reply_markup=get_keyboard_lobby())
        await update.message.reply_text("📊 Feedback?", reply_markup=InlineKeyboardMarkup(k))
    
    if pid:
        try: await context.bot.send_message(pid, "🔌 Partner Disconnected.", reply_markup=get_keyboard_lobby()); await context.bot.send_message(pid, "📊 Feedback?", reply_markup=InlineKeyboardMarkup(k))
        except: pass

async def relay_message(update, context):
    user_id = update.effective_user.id
    pid = ACTIVE_CHATS.get(user_id)
    if not pid: # Fallback
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT partner_id FROM users WHERE user_id = %s AND status='chatting'", (user_id,))
        row = cur.fetchone(); cur.close(); release_conn(conn)
        if row and row[0]: pid = row[0]; ACTIVE_CHATS[user_id] = pid
    if pid:
        if update.message.text:
            conn = get_conn(); cur = conn.cursor()
            cur.execute("INSERT INTO chat_logs (sender_id, receiver_id, message) VALUES (%s, %s, %s)", (user_id, pid, update.message.text))
            conn.commit(); cur.close(); release_conn(conn)
        try: await update.message.copy(chat_id=pid)
        except: await stop_chat(update, context)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); data = q.data; uid = q.from_user.id
    if data == "force_random": await perform_match(update, context, uid); return
    if data == "close_settings": await q.delete_message(); return
    
    if data.startswith("game_offer_"): await offer_game(update, context, uid, data.split("_", 2)[2]); return
    if data.startswith("game_accept_"): pid = ACTIVE_CHATS.get(uid); await start_game_session(update, context, data.split("_", 2)[2], pid, uid) if pid else None; return
    if data == "game_reject": pid = ACTIVE_CHATS.get(uid); await context.bot.send_message(pid, "❌ Declined.") if pid else None; await q.edit_message_text("❌ Declined."); return
    if data.startswith("tod_pick_"): await send_tod_options(update, context, data.split("_")[2]); return
    if data.startswith("tod_send_"): 
        gd = GAME_STATES.get(uid)
        if gd: await context.bot.send_message(gd["partner"], f"🎲 {gd['options'][int(data.split('_')[2])]}", parse_mode='Markdown'); await q.edit_message_text("✅ Sent."); GAME_STATES[uid]["turn"] = gd["partner"]; await send_tod_turn(context, gd["partner"]); return
    if data == "tod_manual": context.user_data["state"] = "GAME_MANUAL"; await q.edit_message_text("✍️ Type question:"); return

    # Onboarding
    if data.startswith("set_gen_"): await update_user(uid, "gender", data.split("_")[2]); await send_onboarding_step(update, 2); return
    if data.startswith("set_age_"): await update_user(uid, "age_range", data.split("_")[2]); await send_onboarding_step(update, 3); return
    if data.startswith("set_lang_"): await update_user(uid, "language", data.split("_")[2]); await send_onboarding_step(update, 4); return
    if data.startswith("set_reg_"): await update_user(uid, "region", data.split("_")[2]); await send_onboarding_step(update, 5); return
    if data.startswith("set_mood_"): await update_user(uid, "mood", data.split("_")[2]); context.user_data["state"] = "ONBOARDING_INTEREST"; await send_onboarding_step(update, 6); return
    if data == "onboarding_done": context.user_data["state"] = None; await show_main_menu(update); return
    if data == "restart_onboarding": await send_onboarding_step(update, 1); return

    # Admin
    if uid in ADMIN_IDS:
        if data == "admin_broadcast_info": 
            try: await q.edit_message_text("📢 Type `/broadcast msg`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_home")]])); return
            except: pass
        if data == "admin_home": await admin_panel(update, context); return
        if data == "admin_users":
            conn = get_conn(); cur = conn.cursor(); cur.execute("SELECT user_id, first_name FROM users ORDER BY joined_at DESC LIMIT 10"); users = cur.fetchall(); cur.close(); release_conn(conn)
            msg = "📜 **Recent:**\n" + "\n".join([f"• {u[1]} (`{u[0]}`)" for u in users])
            try: await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_home")]]), parse_mode='Markdown'); return
            except: pass
        if data == "admin_reports":
            conn = get_conn(); cur = conn.cursor(); cur.execute("SELECT user_id, report_count FROM users WHERE report_count > 0 LIMIT 5"); users = cur.fetchall(); cur.close(); release_conn(conn)
            kb = []; 
            for u in users: kb.append([InlineKeyboardButton(f"🔨 {u[0]}", callback_data=f"ban_user_{u[0]}"), InlineKeyboardButton(f"✅ {u[0]}", callback_data=f"clear_user_{u[0]}")])
            kb.append([InlineKeyboardButton("🔙", callback_data="admin_home")])
            try: await q.edit_message_text("⚠️ **Reports:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown'); return
            except: pass
        if data == "admin_banlist":
            conn = get_conn(); cur = conn.cursor(); cur.execute("SELECT user_id FROM users WHERE banned_until > NOW() LIMIT 5"); users = cur.fetchall(); cur.close(); release_conn(conn)
            kb = []; 
            for u in users: kb.append([InlineKeyboardButton(f"✅ Unban {u[0]}", callback_data=f"unban_user_{u[0]}")])
            kb.append([InlineKeyboardButton("🔙", callback_data="admin_home")])
            try: await q.edit_message_text("🚫 **Bans:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown'); return
            except: pass
        if data == "admin_feedbacks":
            conn = get_conn(); cur = conn.cursor(); cur.execute("SELECT message FROM feedback ORDER BY timestamp DESC LIMIT 5"); rows = cur.fetchall(); cur.close(); release_conn(conn)
            txt = "\n".join([r[0] for r in rows]) or "None"
            try: await q.edit_message_text(f"📨 **Feed:**\n{txt}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_home")]]), parse_mode='Markdown'); return
            except: pass
        
        if data.startswith("ban_user_"): await admin_ban_command(update, context); return
        if data.startswith("clear_user_"):
            tid = int(data.split("_")[2]); conn = get_conn(); cur = conn.cursor(); cur.execute("UPDATE users SET report_count = 0 WHERE user_id = %s", (tid,)); conn.commit(); cur.close(); release_conn(conn)
            try: await q.edit_message_text("✅ Cleared.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_reports")]])); return
            except: pass
        if data.startswith("unban_user_"):
            tid = int(data.split("_")[2]); conn = get_conn(); cur = conn.cursor(); cur.execute("UPDATE users SET banned_until = NULL WHERE user_id = %s", (tid,)); conn.commit(); cur.close(); release_conn(conn)
            try: await q.edit_message_text("✅ Unbanned.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_banlist")]])); return
            except: pass

    # Rate & General
    if data.startswith("rate_"):
        act, target = data.split("_")[1], int(data.split("_")[2])
        if act == "report": await handle_report(update, context, uid, target); await q.edit_message_text("⚠️ Reported.")
        else:
            sc = 1 if act == "like" else -1
            conn = get_conn(); cur = conn.cursor(); cur.execute("INSERT INTO user_interactions (rater_id, target_id, score) VALUES (%s, %s, %s)", (uid, target, sc)); conn.commit(); cur.close(); release_conn(conn)
            await q.edit_message_text("✅ Sent.")
    
    if data == "action_search": await start_search(update, context); return
    if data == "main_menu": await show_main_menu(update); return
    if data == "stop_search": await stop_search_process(update, context); return

if __name__ == '__main__':
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
