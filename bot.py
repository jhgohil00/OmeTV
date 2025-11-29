import logging
import psycopg2
from psycopg2 import pool
import datetime
import asyncio
import os
import threading
import random  # <--- NEW
import time  # <--- THIS WAS MISSING
from game_data import GAME_DATA
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
from ghost_engine import GhostEngine

# ==============================================================================
# 🔐 SECURITY & CONFIGURATION
# ==============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
admin_env = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in admin_env.split(",") if x.strip().isdigit()]

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ==============================================================================
# 🚀 HIGH-PERFORMANCE ENGINE (RAM Cache & Connection Pool)
# ==============================================================================
# 1. RAM CACHE: Stores who is chatting with whom. Instant access. 0ms Latency.
ACTIVE_CHATS = {} 
# [NEW] Translation Map for Replies
MESSAGE_MAP = {}
# --- GAME STATE & DATA ---
GAME_STATES = {}       # {user_id: {'game': 'tod', 'turn': uid, 'partner': pid}}
GAME_COOLDOWNS = {}    # {user_id: timestamp}

# 2. DB POOL: Keeps connections open so we don't "dial" the DB every time.
DB_POOL = None
GHOST = None # Will init later

def init_db_pool():
    global DB_POOL
    if not DATABASE_URL: return
    try:
        DB_POOL = psycopg2.pool.SimpleConnectionPool(1, 20, dsn=DATABASE_URL)
        print("✅ CONNECTION POOL STARTED.")
    except Exception as e:
        print(f"❌ Pool Error: {e}")

def get_conn():
    # Grabs an open line from the pool
    if DB_POOL: return DB_POOL.getconn()
    return None

def release_conn(conn):
    # Puts the line back in the pool
    if DB_POOL and conn: DB_POOL.putconn(conn)

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
# 🛠️ DATABASE SETUP
# ==============================================================================
def init_db():
    init_db_pool() # Start the pool
    conn = get_conn()
    if not conn: return
    cur = conn.cursor()
    
    # Tables
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
    
    # Migration checks
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
    global GHOST
    GHOST = GhostEngine(DB_POOL)


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
    return ReplyKeyboardMarkup([[KeyboardButton("❌ Stop Searching")]], resize_keyboard=True)

def get_keyboard_chat():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🎮 Games")],
        [KeyboardButton("⏭️ Next"), KeyboardButton("🛑 Stop")]
    ], resize_keyboard=True)

def get_keyboard_game():
    return ReplyKeyboardMarkup([[KeyboardButton("🛑 Stop Game"), KeyboardButton("🛑 Stop Chat")]], resize_keyboard=True)


# ==============================================================================
# 🧠 MATCHMAKING ENGINE (Fixed Design + Performance)
# ==============================================================================
def find_match(user_id):
    conn = get_conn()
    cur = conn.cursor()
    
    # Fetch Me (Including Mood)
    cur.execute("SELECT language, interests, age_range, mood FROM users WHERE user_id = %s", (user_id,))
    me = cur.fetchone()
    if not me: release_conn(conn); return None, [], "Neutral", "English"
    my_lang, my_interests, my_age, my_mood = me
    my_tags = [t.strip().lower() for t in my_interests.split(',')] if my_interests else []

    # Fetch Dislikes
    cur.execute("SELECT target_id FROM user_interactions WHERE rater_id = %s AND score = -1", (user_id,))
    disliked_ids = {row[0] for row in cur.fetchall()}

    # Fetch Candidates (Including Mood)
    cur.execute("""
        SELECT user_id, language, interests, age_range, mood 
        FROM users 
        WHERE status = 'searching' AND user_id != %s
        AND (banned_until IS NULL OR banned_until < NOW())
    """, (user_id,))
    candidates = cur.fetchall()
    
    best_match, best_score, common_interests = None, -999999, []
    p_mood, p_lang = "Neutral", "English"

    for cand in candidates:
        cand_id, cand_lang, cand_interests, cand_age, cand_mood = cand
        cand_tags = [t.strip().lower() for t in cand_interests.split(',')] if cand_interests else []
        
        score = 0
        if cand_id in disliked_ids: score -= 1000
        
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
# ==============================================================================
# 🎮 GAME ENGINE LOGIC
# ==============================================================================
async def offer_game(update, context, user_id, game_name):
    partner_id = ACTIVE_CHATS.get(user_id)
    if not partner_id: return
    
    # [NEW] HANDLE AI PARTNER
    if isinstance(partner_id, str) and partner_id.startswith("AI_"):
        # 1. Ask the Ghost Engine (Roll Dice)
        accept, reply_text = GHOST.decide_game_offer(game_name)
        
        # 2. Simulate Delay (Thinking)
        await context.bot.send_chat_action(chat_id=user_id, action="typing")
        await asyncio.sleep(2)
        
        # 3. AI Replies
        await context.bot.send_message(user_id, reply_text)
        
        # 4. If Accepted, give instructions (But don't start the button engine)
        if accept:
            await asyncio.sleep(1)
            if "Truth" in game_name:
                await context.bot.send_message(user_id, "🎲 **Game On!**\nSince I can't click buttons, just type your Question or Dare here in the chat!", parse_mode='Markdown')
            elif "Rock" in game_name:
                await context.bot.send_message(user_id, "✂️ **Rock Paper Scissors**\n\nType your move: *Rock, Paper, or Scissors*", parse_mode='Markdown')
        return

    # [EXISTING] HUMAN PARTNER LOGIC
    last = GAME_COOLDOWNS.get(user_id, 0)
    if time.time() - last < 60:
        await context.bot.send_message(user_id, f"⏳ Wait {int(60 - (time.time() - last))}s before sending another request.")
        return
    GAME_COOLDOWNS[user_id] = time.time()

    rules_map = {
        "Truth or Dare": "• Be honest!\n• You can answer with Text, Voice, or Photos.\n• Use 'Ask Your Own' to get creative.",
        "Would You Rather": "• Vote silently first.\n• Discuss WHY you chose it.\n• Next round starts only after BOTH answer.",
        "Rock Paper Scissors": "• Pick your move.\n• Best of 3 or 5 wins.\n• Draws restart the round instantly."
    }
    
    rule_text = rules_map.get(game_name.split("|")[0], "Have fun!")
    kb = [
        [InlineKeyboardButton("✅ Accept", callback_data=f"game_accept_{game_name}"), InlineKeyboardButton("❌ Reject", callback_data="game_reject")]
    ]
    
    await context.bot.send_message(user_id, f"🎮 **Offered: {game_name}**\n⏳ Waiting...", parse_mode='Markdown')
    await context.bot.send_message(partner_id, f"🎮 **Game Request**\nPartner wants to play **{game_name}**.\n\n📜 **How to Play:**\n{rule_text}", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def start_game_session(update, context, game_raw, p1, p2):
    # Detect Rounds (Format: "RPS|3")
    rounds = 1
    game_name = game_raw
    if "|" in game_raw:
        game_name = "Rock Paper Scissors"
        rounds = int(game_raw.split("|")[1])

    # Init State with Scoreboard
    # s1 = P1's score, s2 = P2's score, cr = current round
    # Added: 'streak' (for WYR), 'explained' (set of who answered why)
    state = {"game": game_name, "turn": p2, "partner": p2, "status": "playing", "moves": {}, 
             "max_r": rounds, "cur_r": 1, "s1": 0, "s2": 0, "streak": 0, "explained": []}
    
    GAME_STATES[p1] = GAME_STATES[p2] = state
    
    kb = get_keyboard_game()
    await context.bot.send_message(p1, f"🎮 **Started: {game_name}**", reply_markup=kb, parse_mode='Markdown')
    await context.bot.send_message(p2, f"🎮 **Started: {game_name}**", reply_markup=kb, parse_mode='Markdown')
    
    if game_name == "Truth or Dare":
        # Turn starts with P2 (The one who accepted)
        await send_tod_turn(context, p2)
    elif game_name == "Would You Rather":
        await send_wyr_round(context, p1, p2)
    elif game_name == "Rock Paper Scissors":
        await send_rps_round(context, p1, p2)

async def send_tod_turn(context, turn_id):
    kb = [[InlineKeyboardButton("🟢 Truth", callback_data="tod_pick_truth"), InlineKeyboardButton("🔴 Dare", callback_data="tod_pick_dare")]]
    await context.bot.send_message(turn_id, "🫵 **Your Turn!** Choose:", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def send_tod_options(context, target_id, mode):
    # Select 5 random questions
    options = random.sample(GAME_DATA[f"tod_{mode}"], 5)
    
    # Create Menu Text
    msg_text = f"🎭 **Pick a {mode.upper()}:**\n\n"
    for i, opt in enumerate(options):
        msg_text += f"**{i+1}.** {opt}\n"
    
    # Create Buttons (1-5 and Manual)
    kb = [
        [InlineKeyboardButton("1️⃣", callback_data="tod_send_0"), InlineKeyboardButton("2️⃣", callback_data="tod_send_1"), InlineKeyboardButton("3️⃣", callback_data="tod_send_2")],
        [InlineKeyboardButton("4️⃣", callback_data="tod_send_3"), InlineKeyboardButton("5️⃣", callback_data="tod_send_4")],
        [InlineKeyboardButton("✍️ Ask Your Own", callback_data="tod_manual")]
    ]
    
    # Save options to the Asker's state (target_id)
    if target_id not in GAME_STATES: GAME_STATES[target_id] = {}
    GAME_STATES[target_id]["options"] = options
        
    # Send to the Partner (Asker)
    await context.bot.send_message(target_id, msg_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
async def send_wyr_round(context, p1, p2):
    q = random.choice(GAME_DATA["wyr"])
    
    # 1. Put the LONG text in the Message (No limits here)
    msg = f"⚖️ **Would You Rather...**\n\n🅰️ **{q[0]}**\n       ➖ OR ➖\n🅱️ **{q[1]}**"
    
    # 2. Keep the buttons simple so they never cut off
    kb = [
        [InlineKeyboardButton("🅰️ Choose Option A", callback_data="wyr_a")],
        [InlineKeyboardButton("🅱️ Choose Option B", callback_data="wyr_b")]
    ]
    
    await context.bot.send_message(p1, msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    await context.bot.send_message(p2, msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def send_rps_round(context, p1, p2):
    kb = [[InlineKeyboardButton("🪨", callback_data="rps_rock"), InlineKeyboardButton("📄", callback_data="rps_paper"), InlineKeyboardButton("✂️", callback_data="rps_scissors")]]
    await context.bot.send_message(p1, "✂️ **Shoot!**", reply_markup=InlineKeyboardMarkup(kb))
    await context.bot.send_message(p2, "✂️ **Shoot!**", reply_markup=InlineKeyboardMarkup(kb))

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    conn = get_conn(); cur = conn.cursor()
    
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE status != 'idle'")
    online = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE report_count > 0")
    flagged = cur.fetchone()[0]
    
    cur.execute("SELECT gender, COUNT(*) FROM users GROUP BY gender")
    g_stats = " | ".join([f"{r[0]}:{r[1]}" for r in cur.fetchall()])

    def get_stat(col):
        cur.execute(f"SELECT {col}, COUNT(*) FROM users GROUP BY {col} ORDER BY COUNT(*) DESC LIMIT 3")
        return " | ".join([f"{r[0]}:{r[1]}" for r in cur.fetchall()])

    msg = (f"👮 **CONTROL ROOM**\n"
           f"👥 Total: `{total}` | 🟢 Online: `{online}`\n"
           f"⚠️ Flagged: `{flagged}`\n"
           f"🚻 **Gender:** {g_stats}\n"
           f"🌍 {get_stat('region')}\n\n"
           f"🛠️ **COMMANDS:**\n"
           f"• `/ban ID HOURS` (e.g., /ban 12345 24)\n"
           f"• `/warn ID REASON` (e.g., /warn 12345 No spam)\n"
           f"• `/broadcast MESSAGE` (Send to all)\n"
           f"• `/unban ID` (Via button only)")
    
    kb = [[InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast_info"), InlineKeyboardButton("📜 Recent Users", callback_data="admin_users")],
          [InlineKeyboardButton("⚠️ Reports", callback_data="admin_reports"), InlineKeyboardButton("📨 Feedbacks", callback_data="admin_feedbacks")],
          [InlineKeyboardButton("🚫 Bans", callback_data="admin_banlist")]]
    
    try:
        if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        else: await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    except error.BadRequest: pass
    cur.close(); release_conn(conn)

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
        
        # Clear RAM cache if online
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
    if not feedback_text: await update.message.reply_text("❌ Usage: `/feedback message`", parse_mode='Markdown'); return
    conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO feedback (user_id, message) VALUES (%s, %s)", (user_id, feedback_text))
    conn.commit(); cur.close(); release_conn(conn)
    await update.message.reply_text("✅ **Feedback Sent!**", parse_mode='Markdown')

# ==============================================================================
# 📝 ONBOARDING
# ==============================================================================
async def send_onboarding_step(update, step):
    kb = []
    msg = ""
    
    if step == 1:
        msg = "1️⃣ **What's your gender?**"
        kb = [[InlineKeyboardButton("👨 Male", callback_data="set_gen_Male"), InlineKeyboardButton("👩 Female", callback_data="set_gen_Female")], 
              [InlineKeyboardButton("🌈 Other", callback_data="set_gen_Other"), InlineKeyboardButton("⏭️ Skip", callback_data="set_gen_Hidden")]]
    
    elif step == 2:
        msg = "2️⃣ **Age Group?**"
        kb = [[InlineKeyboardButton("👦 ~18", callback_data="set_age_~18"), InlineKeyboardButton("🧢 20-25", callback_data="set_age_20-25")], 
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
        kb = [[InlineKeyboardButton("🌏 Asia 🗻", callback_data="set_reg_Asia"), InlineKeyboardButton("🌍 Europe 🍷", callback_data="set_reg_Europe")],
              [InlineKeyboardButton("🌎 America 🗽", callback_data="set_reg_America"), InlineKeyboardButton("🌍 Africa 🌴", callback_data="set_reg_Africa")],
              [InlineKeyboardButton("⏭️ Skip", callback_data="set_reg_Hidden")]]
    
    elif step == 5:
        msg = "5️⃣ **Current Mood?**"
        kb = [[InlineKeyboardButton("😃 Happy", callback_data="set_mood_Happy"), InlineKeyboardButton("😔 Sad", callback_data="set_mood_Sad")],
              [InlineKeyboardButton("😴 Bored", callback_data="set_mood_Bored"), InlineKeyboardButton("🤔 Don't Know", callback_data="set_mood_Confused")],
              [InlineKeyboardButton("🥀 Lonely", callback_data="set_mood_Lonely"), InlineKeyboardButton("😰 Anxious", callback_data="set_mood_Anxious")],
              [InlineKeyboardButton("⏭️ Skip", callback_data="set_mood_Neutral")]]
    
    elif step == 6:
        msg = "6️⃣ **Final Step! Interests**\n\nType keywords (e.g., *Music, Movies,kdrama..*) or click Skip."
        kb = [[InlineKeyboardButton("⏭️ Skip & Finish", callback_data="onboarding_done")]]

    try:
        if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        else: await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
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
        await update.message.reply_text(f"🚫 Banned until {data[0]}."); cur.close(); release_conn(conn); return
    
    cur.execute("""INSERT INTO users (user_id, username, first_name) VALUES (%s, %s, %s) 
                   ON CONFLICT (user_id) DO UPDATE SET username = %s, first_name = %s""", 
                   (user.id, user.username, user.first_name, user.username, user.first_name))
    conn.commit(); cur.close(); release_conn(conn)

    welcome_msg = "👋 **Welcome to OmeTV Chatbot🤖**\n\nConnect with strangers worldwide 🌍\nNo names. No login.End to End encrypted\n\n*Let's vibe check.* 👇"
    if not data or data[1] == 'Hidden':
        await update.message.reply_text(welcome_msg, reply_markup=ReplyKeyboardRemove(), parse_mode='Markdown')
        await send_onboarding_step(update, 1)
    else:
        msg = await update.message.reply_text("🔄 Loading...", reply_markup=ReplyKeyboardRemove())
        try: await context.bot.delete_message(chat_id=user.id, message_id=msg.message_id)
        except: pass
        await show_main_menu(update)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🆘 **USER GUIDE**\n\n"
        "**1. How to Chat?**\n"
        "Click '🚀 Start Matching'. You will be connected to a random stranger. Say Hi!\n\n"
        "**2. The Games**\n"
        "Click '🎮 Games' inside a chat to challenge your partner. Both must accept to play.\n\n"
        "**3. Safety First**\n"
        "•End to End Encrypted, Your identity is hidden.\n"
        "• To leave: Click '🛑 Stop'.\n"
	"• To change Profile: Click '⚙️ Settings'.\n"
	"• View your Profile: Click '🪪 My ID'.\n"
        "• To report abuse: Click '⚠️ Report' after ending chat.\n"
	"• 🛑🛑Behave Respectful to avoid Permanent **BAN**.🛑🛑\n\n"
        "**4. Commands**\n"
        "/start - Restart Bot\n"
        "/feedback [msg] - Send your feedback to Admin about Bot"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    text = update.message.text
    user_id = update.effective_user.id

    # 1. GAME ANSWER LOGIC (The Answer)
    # Check if this user is supposed to be answering a question

    # 3. ONBOARDING
    if context.user_data.get("state") == "ONBOARDING_INTEREST":
        await update_user(user_id, "interests", text)
        context.user_data["state"] = None
        await update.message.reply_text("✅ **Ready!**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown'); return

    # 4. BUTTON TEXT TRIGGERS
    if text == "🚀 Start Matching": await start_search(update, context); return
    if text in ["🛑 Stop", "🛑 Stop Chat"]: await stop_chat(update, context); return
    if text == "⏭️ Next": await stop_chat(update, context, is_next=True); return
    if text == "❌ Stop Searching": await stop_search_process(update, context); return
    if text == "🎯 Change Interests": context.user_data["state"] = "ONBOARDING_INTEREST"; await update.message.reply_text("👇 Type interests:(separate two interest with coma)", reply_markup=ReplyKeyboardRemove()); return
    if text == "⚙️ Settings": 
        kb = [
            [InlineKeyboardButton("🚻 Gender", callback_data="set_gen_menu"), InlineKeyboardButton("🎂 Age", callback_data="set_age_menu")],
            [InlineKeyboardButton("🗣️ Lang", callback_data="set_lang_menu"), InlineKeyboardButton("🎭 Mood", callback_data="set_mood_menu")],
            [InlineKeyboardButton("🔙 Close", callback_data="close_settings")]
        ]
        await update.message.reply_text("⚙️ **Settings:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown'); return
    if text == "🪪 My ID": await show_profile(update, context); return
    if text == "🆘 Help": await help_command(update, context); return
    
    # 5. GAME MENU
    if text == "🎮 Games":
        kb = [[InlineKeyboardButton("😈 Truth or Dare", callback_data="game_offer_Truth or Dare")],
              [InlineKeyboardButton("🎲 Would You Rather", callback_data="game_offer_Would You Rather")],
              [InlineKeyboardButton("✂️ Rock Paper Scissors", callback_data="rps_mode_select")]]
        await update.message.reply_text("🎮 **Game Center**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown'); return
    
    if text == "🛑 Stop Game":
        pid = ACTIVE_CHATS.get(user_id)
        if user_id in GAME_STATES: del GAME_STATES[user_id]
        if pid and pid in GAME_STATES: del GAME_STATES[pid]
        await update.message.reply_text("🛑 Game Stopped.", reply_markup=get_keyboard_chat())
        if pid: await context.bot.send_message(pid, "🛑 Partner stopped game.", reply_markup=get_keyboard_chat())
        return

    # 6. COMMANDS (Robust & Clean)
    if text.startswith("/"):
        cmd = text.lower().strip() # Fixes "Stop" or "/stop "
        
        # User Commands
        if cmd == "/search":
            # 1. Check DB for 'searching' status (ACTIVE_CHATS only tracks active chats, not waiters)
            conn = get_conn(); cur = conn.cursor()
            cur.execute("SELECT status FROM users WHERE user_id = %s", (user_id,))
            status_row = cur.fetchone()
            cur.close(); release_conn(conn)
            
            # 2. Logic: If in RAM (Chatting) OR DB says Searching -> Block it
            if user_id in ACTIVE_CHATS or (status_row and status_row[0] == 'searching'):
                await update.message.reply_text("⚠️ **User are already in chat** (or connecting).", parse_mode='Markdown')
            else:
                await start_search(update, context)
            return

        if cmd == "/stop": 
            # 1. Logic: If NOT in RAM cache, you aren't chatting
            if user_id not in ACTIVE_CHATS:
                await update.message.reply_text("⚠️ **You aren't in any connection rn.**", parse_mode='Markdown')
            else:
                await stop_chat(update, context)
            return

        if cmd == "/next": 
            # 1. Logic: Works exactly like the button (Ends chat -> Starts Search)
            await stop_chat(update, context, is_next=True)
            return
        
        # Admin Commands
        if cmd == "/admin": await admin_panel(update, context); return
        if cmd.startswith("/ban"): await admin_ban_command(update, context); return
        if cmd.startswith("/warn"): await admin_warn_command(update, context); return
        if cmd.startswith("/broadcast"): await admin_broadcast_execute(update, context); return
        if cmd.startswith("/feedback"): await handle_feedback_command(update, context); return

    await relay_message(update, context)
# ==============================================================================
# 🔌 FAST CONNECTION LOGIC (RAM + DB)
# ==============================================================================
async def check_and_connect_ghost(context: ContextTypes.DEFAULT_TYPE):
    """Called after 15s. If still searching, connect AI (Shadow Mode)."""
    job_data = context.job.data
    user_id = job_data['uid']
    
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT status FROM users WHERE user_id = %s", (user_id,))
    status = cur.fetchone()
    cur.close(); release_conn(conn)
    
    # Only connect AI if user is STILL searching
    if status and status[0] == 'searching':
        persona = GHOST.pick_random_persona()
        user_ctx = {'gender': job_data['gender'], 'country': job_data['region']}
        
        # [FIXED LINE BELOW] Added "Hidden" as the 3rd argument (ai_gender)
        success = await GHOST.start_chat(user_id, persona, "Hidden", user_ctx)
        
        if success:
            # SHADOW MODE: We update RAM to AI, but keep DB as 'searching'.
            # This allows real humans to 'steal' this user later.
            ACTIVE_CHATS[user_id] = f"AI_{persona}"
            
            msg = (f"⚡ **PARTNER FOUND!**\n\n"
                   f"🎭 **Mood:** Random\n"
                   f"🗣️ **Lang:** Mixed\n\n"
                   f"⚠️ *Say Hi!*")
            await context.bot.send_message(user_id, msg, reply_markup=get_keyboard_chat(), parse_mode='Markdown')

async def connect_users(context, user_id, partner_id, common, p_mood, p_lang):
    """Connects two humans, interrupting AI if necessary."""
    # 1. Cleanup AI Shadow Sessions
    for uid in [user_id, partner_id]:
        if isinstance(ACTIVE_CHATS.get(uid), str):
            # Clean AI memory if they were talking to bot
            if uid in GAME_STATES: del GAME_STATES[uid]
            
    # 2. Update DB (Now officially chatting)
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET status='chatting', partner_id=%s WHERE user_id=%s", (partner_id, user_id))
    cur.execute("UPDATE users SET status='chatting', partner_id=%s WHERE user_id=%s", (user_id, partner_id))
    conn.commit(); cur.close(); release_conn(conn)
    
    # 3. Update RAM
    ACTIVE_CHATS[user_id] = partner_id
    ACTIVE_CHATS[partner_id] = user_id
    
    # 4. Notify
    common_str = ", ".join(common).title() if common else "Random"
    msg = (f"⚡ **PARTNER FOUND!**\n\n🎭 **Mood:** {p_mood}\n🔗 **Common:** {common_str}\n"
           f"🗣️ **Lang:** {p_lang}\n\n⚠️ *Say Hi!*")
    
    kb = get_keyboard_chat()
    try: await context.bot.send_message(user_id, msg, reply_markup=kb, parse_mode='Markdown')
    except: pass
    try: await context.bot.send_message(partner_id, msg, reply_markup=kb, parse_mode='Markdown')
    except: pass

async def stop_search_process(update, context):
    user_id = update.effective_user.id
    conn = get_conn(); cur = conn.cursor()
    # 1. Set Status to Idle
    cur.execute("UPDATE users SET status = 'idle' WHERE user_id = %s", (user_id,))
    conn.commit(); cur.close(); release_conn(conn)
    
    # 2. Send Feedback & Show Lobby
    try:
        if update.callback_query:
            await update.callback_query.message.reply_text("🛑 **Search Stopped.**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown')
        else:
            await update.message.reply_text("🛑 **Search Stopped.**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown')
    except: pass

async def start_search(update, context):
    user_id = update.effective_user.id
    if user_id in ACTIVE_CHATS:
        await update.message.reply_text("⛔ **Already in chat!**", parse_mode='Markdown'); return

    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET status = 'searching' WHERE user_id = %s", (user_id,))
    
    # Fetch details for AI Context
    cur.execute("SELECT gender, region, interests FROM users WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    u_gender = row[0] if row else "Hidden"
    u_region = row[1] if row else "Unknown"
    tags = row[2] or "Any"
    
    conn.commit(); cur.close(); release_conn(conn)
    
    # Notify User
    await update.message.reply_text(f"📡 **Scanning...**\nLooking for: `{tags}`...", parse_mode='Markdown', reply_markup=get_keyboard_searching())
    
    # 1. Try Instant Match
    partner_id, common, p_mood, p_lang = find_match(user_id)
    
    if partner_id:
        # [NEW] KICK LOGIC
        # Check if the found partner is currently chatting with an AI
        partner_chat_state = ACTIVE_CHATS.get(partner_id)
        
        if isinstance(partner_chat_state, str) and partner_chat_state.startswith("AI_"):
            # The partner is with AI. We FORCE DISCONNECT them.
            # 1. Remove AI from their RAM
            del ACTIVE_CHATS[partner_id]
            
            # 2. Set their DB to Idle
            conn = get_conn(); cur = conn.cursor()
            cur.execute("UPDATE users SET status='idle' WHERE user_id = %s", (partner_id,))
            conn.commit(); cur.close(); release_conn(conn)
            
            # 3. Send them the "Disconnected" screen with buttons
            # This makes them think the 'stranger' left. They must click Start again.
            kb_feedback = [
                [InlineKeyboardButton("👍", callback_data=f"rate_like_AI"), InlineKeyboardButton("👎", callback_data=f"rate_dislike_AI")],
                [InlineKeyboardButton("⚠️ Report", callback_data=f"rate_report_AI")]
            ]
            try:
                await context.bot.send_message(partner_id, "😶‍🌫️ **Partner Disconnected.**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown')
                await context.bot.send_message(partner_id, "Rate Stranger:", reply_markup=InlineKeyboardMarkup(kb_feedback))
            except: pass
            
            # 4. DO NOT CONNECT YET.
            # User B (You) stays in "Searching..." mode.
            # When User A clicks "Start Matching", they will find User B immediately.
            # We fall through to the 'else' block below to schedule the AI timer for User B just in case User A leaves.
            pass 
        else:
            # Partner is truly waiting. Connect immediately.
            await connect_users(context, user_id, partner_id, common, p_mood, p_lang)
            return # Exit function, we are connected.

    # If we are here, either no match found OR we just kicked a user and are waiting for them to re-join.
    # 2. Schedule AI Fallback (15s)
    context.job_queue.run_once(
        check_and_connect_ghost, 
        15, 
        data={'uid': user_id, 'gender': u_gender, 'region': u_region}
    )
        # 2. Schedule AI Fallback (15s)
        context.job_queue.run_once(
            check_and_connect_ghost, 
            15, 
            data={'uid': user_id, 'gender': u_gender, 'region': u_region}
        )
async def perform_match(update, context, user_id):
    partner_id, common, p_mood, p_lang = find_match(user_id)
    if partner_id:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("UPDATE users SET status='chatting', partner_id=%s WHERE user_id=%s", (partner_id, user_id))
        cur.execute("UPDATE users SET status='chatting', partner_id=%s WHERE user_id=%s", (user_id, partner_id))
        conn.commit(); cur.close(); release_conn(conn)
        
        # UPDATE RAM CACHE (Instant Relay)
        ACTIVE_CHATS[user_id] = partner_id
        ACTIVE_CHATS[partner_id] = user_id
        
        # DESIGN RESTORED
        common_str = ", ".join(common).title() if common else "Random"
        msg = (f"⚡ **YOU ARE CONNECTED!**\n\n🎭 **Mood:** {p_mood}\n🔗 **Interest:** {common_str}\n"
               f"🗣️ **Lang:** {p_lang}\n\n⚠️ *Tip: Say Hi! or Sent a meme*")
        
        kb = get_keyboard_chat()
        await context.bot.send_message(user_id, msg, reply_markup=kb, parse_mode='Markdown')
        try: await context.bot.send_message(partner_id, msg, reply_markup=kb, parse_mode='Markdown')
        except: pass

async def stop_chat(update, context, is_next=False):
    user_id = update.effective_user.id
    partner_id = ACTIVE_CHATS.pop(user_id, 0)
    
    # Cleanup
    keys_to_remove = [k for k in MESSAGE_MAP if k[0] in (user_id, partner_id)]
    for k in keys_to_remove: del MESSAGE_MAP[k]
    if user_id in GAME_STATES: del GAME_STATES[user_id]

    # IF PARTNER WAS HUMAN
    if isinstance(partner_id, int) and partner_id > 0:
        if partner_id in ACTIVE_CHATS: del ACTIVE_CHATS[partner_id]
        if partner_id in GAME_STATES: del GAME_STATES[partner_id]
        
        conn = get_conn(); cur = conn.cursor()
        cur.execute("UPDATE users SET status='idle', partner_id=0 WHERE user_id IN (%s, %s)", (user_id, partner_id))
        conn.commit(); cur.close(); release_conn(conn)
        
        # Send Feedback to Human Partner
        k_partner = [[InlineKeyboardButton("👍", callback_data=f"rate_like_{user_id}"), InlineKeyboardButton("👎", callback_data=f"rate_dislike_{user_id}")], [InlineKeyboardButton("⚠️ Report", callback_data=f"rate_report_{user_id}")]]
        try: 
            await context.bot.send_message(partner_id, "😶‍🌫️ **Partner Disconnected.**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown')
            await context.bot.send_message(partner_id, "Rate Stranger:", reply_markup=InlineKeyboardMarkup(k_partner))
        except: pass

    # IF PARTNER WAS AI
    elif isinstance(partner_id, str):
        conn = get_conn(); cur = conn.cursor()
        cur.execute("UPDATE users SET status='idle' WHERE user_id = %s", (user_id,))
        conn.commit(); cur.close(); release_conn(conn)

    # SEND FEEDBACK BUTTONS TO ME (Preserves Illusion for AI too)
    # If AI, we use target ID "AI"
    target_id = partner_id if isinstance(partner_id, int) else "AI"
    
    k_me = [[InlineKeyboardButton("👍", callback_data=f"rate_like_{target_id}"), InlineKeyboardButton("👎", callback_data=f"rate_dislike_{target_id}")], [InlineKeyboardButton("⚠️ Report", callback_data=f"rate_report_{target_id}")]]
    
    if is_next:
        await update.message.reply_text("⏭️ **Skipping...**", reply_markup=ReplyKeyboardRemove(), parse_mode='Markdown')
        await update.message.reply_text("Rate previous partner:", reply_markup=InlineKeyboardMarkup(k_me))
        await start_search(update, context)
    else:
        await update.message.reply_text("😶‍🌫️ **Partner Disconnect.**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown')
        await update.message.reply_text("Rate Stranger:", reply_markup=InlineKeyboardMarkup(k_me))
async def relay_message(update, context):
    user_id = update.effective_user.id
    partner_id = ACTIVE_CHATS.get(user_id)
    if not partner_id: return 

    # --- PARTNER IS AI ---
    if isinstance(partner_id, str) and partner_id.startswith("AI_"):
        msg_text = update.message.text
        
        # 1. SPECIAL: Handle Rock Paper Scissors via Text
        if msg_text and msg_text.lower() in ['rock', 'paper', 'scissors']:
            # AI plays randomly
            ai_move = random.choice(['rock', 'paper', 'scissors'])
            user_move = msg_text.lower()
            
            # Decide Winner
            result = "🤝 Draw!"
            if (user_move == 'rock' and ai_move == 'scissors') or \
               (user_move == 'paper' and ai_move == 'rock') or \
               (user_move == 'scissors' and ai_move == 'paper'):
                result = "🏆 You Win!"
            elif user_move != ai_move:
                result = "💀 You Lose!"
            
            await asyncio.sleep(1)
            await update.message.reply_text(f"I picked **{ai_move.title()}**.\n\n{result}", parse_mode='Markdown')
            return

        # 2. Normal Text Processing (The existing logic)
        if msg_text:
            # ... (Existing process_message logic) ...
            await context.bot.send_chat_action(chat_id=user_id, action="typing")
            result = await GHOST.process_message(user_id, msg_text)
            
            # (Keep your existing TRIGGER handling here)
            if result == "TRIGGER_SKIP" or result == "TRIGGER_INDIAN_MALE_BEG":
                # ... (Handle disconnect) ...
                await stop_chat(update, context)
                return

            if isinstance(result, dict) and result.get("type") == "text":
                reply_text = result['content']
                
                # [NEW] KEYWORD SCANNER (The Doorman)
                # If AI wants to leave, we execute the /stop command for them.
                triggers = ["bye", "skip", "stop", "boring", "bsdk", "hat", "leave", "gtg"]
                # Check if any trigger word is in the reply (word boundaries)
                is_leaving = any(f" {t} " in f" {reply_text.lower()} " for t in triggers)
                
                # Add a random 5% chance to just ghost without saying anything
                is_ghosting = random.random() < 0.05

                if is_leaving or is_ghosting:
                    # Send the "Bye" message first (if not ghosting)
                    if not is_ghosting:
                        await asyncio.sleep(result['delay'])
                        await update.message.reply_text(reply_text)
                    
                    # Then kill the chat
                    await asyncio.sleep(1) 
                    await stop_chat(update, context)
                    return

                # Normal Reply
                await asyncio.sleep(result['delay'])
                await update.message.reply_text(reply_text)
        return

    # --- PARTNER IS HUMAN ---
    # (Rest of your code remains unchanged)

    # --- PARTNER IS HUMAN ---
    # (Original Logic Below)
    if partner_id:
        if user_id in GAME_STATES and GAME_STATES[user_id].get("status") == "discussing":
            gd = GAME_STATES[user_id]
            try:
                await update.message.copy(chat_id=partner_id, caption=f"🗣️ **Because...**")
                await update.message.reply_text("✅ Explanation Sent.")
                if "explained" not in gd: gd["explained"] = []
                if user_id not in gd["explained"]: gd["explained"].append(user_id)
                if len(gd["explained"]) >= 2:
                    await context.bot.send_message(user_id, "✨ **Both explained! Next Round...**")
                    await context.bot.send_message(partner_id, "✨ **Both explained! Next Round...**")
                    gd["status"] = "playing"; gd["explained"] = []
                    await asyncio.sleep(1.5)
                    await send_wyr_round(context, user_id, partner_id)
            except Exception as e: print(f"WYR Error: {e}")
            return

        if user_id in GAME_STATES and GAME_STATES[user_id].get("status") == "answering" and GAME_STATES[user_id].get("turn") == user_id:
            try: 
                await update.message.copy(chat_id=partner_id, caption=f"🗣️ **Answer**")
                await update.message.reply_text("✅ Answer Sent.")
                GAME_STATES[user_id]["status"] = "playing"
                if partner_id in GAME_STATES: GAME_STATES[partner_id]["status"] = "playing"
                GAME_STATES[user_id]["turn"] = partner_id; GAME_STATES[partner_id]["turn"] = partner_id
                await send_tod_turn(context, partner_id)
                return 
            except: pass

        if update.message:
            if update.message.photo or update.message.video or update.message.video_note or update.message.voice:
                duration = 0
                caption = "📸 Photo"
                if update.message.video: caption = "📹 Video"; duration = update.message.video.duration or 0
                elif update.message.voice: caption = "🗣️ Voice"; duration = update.message.voice.duration or 0
                elif update.message.video_note: caption = "⏺ Circle Video"; duration = update.message.video_note.duration or 0
                
                callback_data = f"secret_{user_id}_{update.message.message_id}_{duration}"
                kb = [[InlineKeyboardButton(f"🔓 View {caption}", callback_data=callback_data)]]
                await context.bot.send_message(partner_id, f"🔒 **Secret {caption} Received!**\nTap below to view.\n_Self-destructing._", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
                await update.message.reply_text(f"🔒 **Sent as View Once.**")
                return 

            if update.message.text:
                conn = get_conn(); cur = conn.cursor()
                cur.execute("INSERT INTO chat_logs (sender_id, receiver_id, message) VALUES (%s, %s, %s)", (user_id, partner_id, update.message.text))
                conn.commit(); cur.close(); release_conn(conn)
            
            try:
                reply_target_id = None
                if update.message.reply_to_message:
                    reply_target_id = MESSAGE_MAP.get((user_id, update.message.reply_to_message.message_id))
                sent_msg = await update.message.copy(chat_id=partner_id, reply_to_message_id=reply_target_id)
                if sent_msg: MESSAGE_MAP[(partner_id, sent_msg.message_id)] = update.message.message_id
            except: await stop_chat(update, context)

# ==============================================================================
# 🧩 HELPERS & BUTTON HANDLER
# ==============================================================================
async def send_reroll_option(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT status FROM users WHERE user_id = %s", (user_id,))
    status = cur.fetchone()
    
    # Only show if STILL searching
    if status and status[0] == 'searching':
        kb = [
            [InlineKeyboardButton("🔔 Notify Me & Stop", callback_data="notify_me")],
            [InlineKeyboardButton("📡 Keep Searching", callback_data="keep_searching")]
        ]
        msg = (
            "🐢 **It's quiet right now.**\n\n"
            "Want me to notify you when someone joins?\n\n"
            "_This is temporary because our bot is in the initial stage. "
            "When userbase increases, you will get connected immediately. "
            "Thanks for supporting!_"
        )
        try: await context.bot.send_message(user_id, msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        except: pass
    cur.close(); release_conn(conn)

async def show_profile(update, context):
    user_id = update.effective_user.id
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT language, interests, karma_score, gender, age_range, region, mood FROM users WHERE user_id = %s", (user_id,))
    data = cur.fetchone(); cur.close(); release_conn(conn)
    text = f"👤 **IDENTITY**\n━━━━━━━━━━━━━━━━\n🗣️ {data[0]}\n🏷️ {data[1]}\n🚻 {data[3]}\n🎂 {data[4]}\n🌍 {data[5]}\n🎭 {data[6]}\n🛡️ {data[2]}%"
    await update.message.reply_text(text, parse_mode='Markdown')

async def show_main_menu(update):
    try: 
        if update.message: await update.message.reply_text("👋 **Welcome to OmeTV Chatbot 🤖**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown')
        elif update.callback_query: await update.callback_query.message.reply_text("⏳ **you are in waiting Lobby...Thanks for you patience **", reply_markup=get_keyboard_lobby(), parse_mode='Markdown')
    except: pass

async def handle_report(update, context, reporter, reported):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET report_count = report_count + 1 WHERE user_id = %s RETURNING report_count", (reported,))
    cnt = cur.fetchone()[0]
    cur.execute("INSERT INTO reports (reporter_id, reported_id, reason) VALUES (%s, %s, 'Report')", (reporter, reported))
    conn.commit()
    if cnt >= 3:
        cur.execute("SELECT message FROM chat_logs WHERE sender_id = %s ORDER BY timestamp DESC LIMIT 5", (reported,))
        logs = [l[0] for l in cur.fetchall()]
        msg = f"🚨 **REPORT (3+)**\nUser: `{reported}`\nLogs: {logs}"
        kb = [[InlineKeyboardButton(f"🔨 BAN {reported}", callback_data=f"ban_user_{reported}")]]
        for a in ADMIN_IDS:
            try: await context.bot.send_message(a, msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
            except: pass
    cur.close(); release_conn(conn)

async def update_user(user_id, col, val):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(f"UPDATE users SET {col} = %s WHERE user_id = %s", (val, user_id))
    conn.commit(); cur.close(); release_conn(conn)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
# RPS SUB-MENU
    if data == "rps_mode_select":
        kb = [[InlineKeyboardButton("Best of 3", callback_data="game_offer_Rock paper Scissors|3"), InlineKeyboardButton("Best of 5", callback_data="game_offer_Rock paper Scissors|5")]]
        await q.edit_message_text("🔢 **Select Rounds:**", reply_markup=InlineKeyboardMarkup(kb)); return
    uid = q.from_user.id
    # [NEW] SECRET MEDIA HANDLER
    if data.startswith("secret_"):
        try:
            _, sender_id, msg_id, duration_str = data.split("_")
            sender_id = int(sender_id)
            msg_id = int(msg_id)
            duration = int(duration_str)
            
            # Timeout: 15s for photos, (Duration + 30s) for videos
            timeout = 15 if duration == 0 else (duration + 30)
            
            await q.edit_message_text(f"🔓 **Open for {timeout}s...**")
            
            sent_media = await context.bot.copy_message(
                chat_id=uid, 
                from_chat_id=sender_id, 
                message_id=msg_id, 
                protect_content=True, # BLOCKS SCREENSHOTS
                caption=f"⏱️ **Self-Destructing in {timeout}s...**",
                parse_mode='Markdown'
            )
            
            # Non-blocking wait
            await asyncio.sleep(timeout)
            
            await context.bot.delete_message(chat_id=uid, message_id=sent_media.message_id)
            await context.bot.send_message(uid, "💣 **Media Destroyed.**")
        except Exception as e:
            try: await q.edit_message_text("❌ **Expired or Error.**")
            except: pass
        return
    # NEW SETTINGS REDIRECTS
    if data == "set_gen_menu": await send_onboarding_step(update, 1); return
    if data == "set_age_menu": await send_onboarding_step(update, 2); return
    if data == "set_lang_menu": await send_onboarding_step(update, 3); return
    if data == "set_mood_menu": await send_onboarding_step(update, 5); return
    if data == "force_random": await perform_match(update, context, uid); return
    if data == "close_settings": await q.delete_message(); return
    # NOTIFY ME LOGIC
    # NOTIFY ME LOGIC (Pause & Lobby)
    if data == "notify_me":
        conn = get_conn(); cur = conn.cursor()
        cur.execute("UPDATE users SET status = 'waiting_notify' WHERE user_id = %s", (uid,))
        conn.commit(); cur.close(); release_conn(conn)
        
        await q.edit_message_text("✅ **Paused.** I'll notify you when someone joins.", parse_mode='Markdown')
        await show_main_menu(update) # Force them back to Lobby so they are ready to click Start later
        return

    # KEEP SEARCHING LOGIC (Continue)
    if data == "keep_searching":
        await q.delete_message() # Just delete the warning, stay in queue
        return
    if data == "game_soon": await q.answer("🚧 Coming Soon!", show_alert=True); return
    
    # GAME HANDLERS
    if data.startswith("game_offer_"): await offer_game(update, context, uid, data.split("_", 2)[2]); return
    if data.startswith("game_accept_"): pid = ACTIVE_CHATS.get(uid); await start_game_session(update, context, data.split("_", 2)[2], pid, uid) if pid else None; return
    if data == "game_reject": pid = ACTIVE_CHATS.get(uid); await context.bot.send_message(pid, "❌ Declined.") if pid else None; await q.edit_message_text("❌ Declined."); return
    
    # TRUTH OR DARE LOGIC (Fixed Flow)
    if data.startswith("tod_pick_"):
        mode = data.split("_")[2] # truth or dare
        partner_id = ACTIVE_CHATS.get(uid)
        
        # 1. Notify the person who clicked (You)
        await q.edit_message_text(f"✅ You picked **{mode.upper()}**.\nWaiting for partner to ask...", parse_mode='Markdown')
        
        # 2. Send the Question Menu to the Partner (Asker)
        if partner_id:
            # FIX: Passing 'context' and 'partner_id' correctly
            await send_tod_options(context, partner_id, mode)
        return
    
    if data.startswith("tod_send_"): 
        gd = GAME_STATES.get(uid)
        if gd:
            q_text = gd["options"][int(data.split("_")[2])]
            pid = gd["partner"]
            
            # Send Question
            await context.bot.send_message(pid, f"🎲 **QUESTION:**\n{q_text}\n\n*Type your answer...*", parse_mode='Markdown')
            await q.edit_message_text(f"✅ Asked: {q_text}")
            
            # Update State: It is now PARTNER'S turn to answer. 
            # We DO NOT send the menu yet. We wait for text input.
            if pid in GAME_STATES: 
                GAME_STATES[pid]["status"] = "answering"
                GAME_STATES[pid]["turn"] = pid 
        return
    if data == "tod_manual": context.user_data["state"] = "GAME_MANUAL"; await q.edit_message_text("✍️ **Type your question now:**"); return
# ROCK PAPER SCISSORS LOGIC
    # ROCK PAPER SCISSORS (TOURNAMENT EDITION)
    if data.startswith("rps_"):
        move = data.split("_")[1]
        gd = GAME_STATES.get(uid)
        if not gd: return
        
        # 1. Save Move
        gd["moves"][uid] = move
        await q.edit_message_text(f"✅ You chose **{move.upper()}**.\nWaiting for partner...")
        
        # 2. Check if both played
        partner_id = ACTIVE_CHATS.get(uid)
        if partner_id and partner_id in gd["moves"]:
            p_move = gd["moves"][partner_id]
            
            # 3. Calculate ROUND Winner
            r_res = "🤝 Draw"
            winner = None # None, p1, or p2
            
            # Determine Offerer (p1) vs Accepter (p2) for scoring
            # We stored scores as s1 (for p1) and s2 (for p2)
            # We need to know who 'uid' is relative to the game owner
            # But simpler: Just track scores by ID? 
            # Let's use the 'turn' logic or just simple ID comparison? 
            # FAST FIX: Since both share the SAME dictionary object, we can just update gd['s1'] if uid < partner_id else...
            # Wait, easier: Just send the text.
            
            if move == p_move: r_res = "🤝 Draw"
            elif (move == "rock" and p_move == "scissors") or \
                 (move == "paper" and p_move == "rock") or \
                 (move == "scissors" and p_move == "paper"):
                 r_res = f"🏆 You ({move}) beat {p_move}!"
                 winner = uid
            else:
                 r_res = f"💀 You ({move}) lost to {p_move}!"
                 winner = partner_id

            # Update Scoreboard
            if winner == uid: gd[f"s_{uid}"] = gd.get(f"s_{uid}", 0) + 1
            elif winner == partner_id: gd[f"s_{partner_id}"] = gd.get(f"s_{partner_id}", 0) + 1
            
            # Get Current Scores
            sc_me = gd.get(f"s_{uid}", 0)
            sc_pa = gd.get(f"s_{partner_id}", 0)
            
            # 4. Check Tournament Status
            if gd["cur_r"] >= gd["max_r"]:
                # GAME OVER - FINAL RESULTS
                final_res = "aww...🤝 **MATCH DRAW!**"
                if sc_me > sc_pa: final_res = "🏆 **YOU WON THE MATCH!🍾**"
                elif sc_pa > sc_me: final_res = "💀 **YOU LOST THE MATCH!**"
                
                p_final = "🏆 **YOU WON THE MATCH!**" if "LOST" in final_res else ("💀 **YOU LOST THE MATCH!**" if "WON" in final_res else final_res)

                msg = f"🏁 **FINAL SCORE (Best of {gd['max_r']})**\n━━━━━━━━━━━━\nYou: {sc_me} | Partner: {sc_pa}\n\n{final_res}"
                p_msg = f"🏁 **FINAL SCORE (Best of {gd['max_r']})**\n━━━━━━━━━━━━\nYou: {sc_pa} | Partner: {sc_me}\n\n{p_final}"
                
                await context.bot.send_message(uid, msg, parse_mode='Markdown', reply_markup=get_keyboard_game())
                await context.bot.send_message(partner_id, p_msg, parse_mode='Markdown', reply_markup=get_keyboard_game())
                
                # Cleanup
                gd["moves"] = {}
                # Keep state briefly or delete? Let's delete to prevent glitches
                del GAME_STATES[uid]
                del GAME_STATES[partner_id]
                
            else:
                # NEXT ROUND
                p_r_res = f"🏆 You ({p_move}) beat {move}!" if winner == partner_id else (f"💀 You ({p_move}) lost to {move}!" if winner == uid else "🤝 Draw")
                
                msg = f"🔔 **Round {gd['cur_r']} Result:**\n{r_res}\n\n📊 Score: {sc_me} - {sc_pa}\n⏳ Next round..."
                p_msg = f"🔔 **Round {gd['cur_r']} Result:**\n{p_r_res}\n\n📊 Score: {sc_pa} - {sc_me}\n⏳ Next round..."

                await context.bot.send_message(uid, msg, parse_mode='Markdown')
                await context.bot.send_message(partner_id, p_msg, parse_mode='Markdown')
                
                # Setup Next Round
                gd["cur_r"] += 1
                gd["moves"] = {}
                await asyncio.sleep(2)
                await send_rps_round(context, uid, partner_id)
        return

    # WOULD YOU RATHER LOGIC
    # WOULD YOU RATHER (SOCIAL ENGINE 2.0)
    if data.startswith("wyr_") and data != "wyr_skip":
        choice = data.split("_")[1].upper() # A or B
        gd = GAME_STATES.get(uid)
        if not gd: return
        
        # 1. Save Vote
        gd["moves"][uid] = choice
        await q.edit_message_text(f"✅ You voted **Option {choice}**.\nWaiting for partner...")
        
        # 2. Check if both voted
        partner_id = ACTIVE_CHATS.get(uid)
        if partner_id and partner_id in gd["moves"]:
            p_choice = gd["moves"][partner_id]
            
            # 3. Analyze Compatibility
            match_text = ""
            if choice == p_choice:
                gd["streak"] = gd.get("streak", 0) + 1
                s = gd["streak"]
                match_text = f"🔥 **100% MATCH!** (Streak: {s})"
                if s == 2: match_text += "\n*2 in a row! Are you twins?* 👯"
                if s >= 3: match_text += "\n*PERFECT SYNC! Soulmates?* 💍"
            else:
                gd["streak"] = 0
                match_text = "⚡ **DIFFERENT POV!** (Streak Reset)"

            # 4. Announce & Trigger "Interrogation Phase"
            msg = f"📊 **RESULTS:**\n\n👤 You: **{choice}**\n👤 Partner: **{p_choice}**\n\n{match_text}\n\n👇 **Tell your partner WHY you chose that!**"
            p_msg = f"📊 **RESULTS:**\n\n👤 You: **{p_choice}**\n👤 Partner: **{choice}**\n\n{match_text}\n\n👇 **Tell your partner WHY you chose that!**"
            
            # Switch State to "Discussion"
            gd["status"] = "discussing"
            gd["explained"] = [] # Reset explanation tracker
            
            # Add a Skip Button (Emergency Exit)
            kb = [[InlineKeyboardButton("⏭️ Skip Discussion", callback_data="wyr_skip")]]
            
            await context.bot.send_message(uid, msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
            await context.bot.send_message(partner_id, p_msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
            
            # Reset moves for safety
            gd["moves"] = {}
        return
    
    # WYR SKIP HANDLER
    if data == "wyr_skip":
        gd = GAME_STATES.get(uid)
        pid = ACTIVE_CHATS.get(uid)
        
        # Only process if in discussing phase
        if gd and gd.get("status") == "discussing":
            # 1. Initialize 'explained' list if missing
            if "explained" not in gd: gd["explained"] = []
            
            # 2. Mark this user as DONE (Treat Skip as an 'Answer')
            if uid not in gd["explained"]:
                gd["explained"].append(uid)
                await q.edit_message_text("⏭️ **You skipped.** Waiting for partner...")
                if pid: await context.bot.send_message(pid, "⏭️ **Partner skipped discussion.**")
            else:
                await q.answer("⏳ Waiting for partner...", show_alert=True)
                return

            # 3. Check if BOTH are done (Meaning: Both Skipped, or 1 Skipped + 1 Answered)
            if len(gd["explained"]) >= 2:
                # Notify
                if pid: await context.bot.send_message(pid, "✨ **Next Round...**")
                await context.bot.send_message(uid, "✨ **Next Round...**")
                
                # Reset State & Start Next Round
                gd["status"] = "playing"
                await asyncio.sleep(1.5)
                if pid: await send_wyr_round(context, uid, pid)
        return

    # ONBOARDING
    if data.startswith("set_gen_"): await update_user(uid, "gender", data.split("_")[2]); await send_onboarding_step(update, 2); return
    if data.startswith("set_age_"): await update_user(uid, "age_range", data.split("_")[2]); await send_onboarding_step(update, 3); return
    if data.startswith("set_lang_"): await update_user(uid, "language", data.split("_")[2]); await send_onboarding_step(update, 4); return
    if data.startswith("set_reg_"): await update_user(uid, "region", data.split("_")[2]); await send_onboarding_step(update, 5); return
    if data.startswith("set_mood_"): await update_user(uid, "mood", data.split("_")[2]); context.user_data["state"] = "ONBOARDING_INTEREST"; await send_onboarding_step(update, 6); return
    if data == "onboarding_done": context.user_data["state"] = None; await show_main_menu(update); return
    if data == "restart_onboarding": await send_onboarding_step(update, 1); return

    # ADMIN
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
            conn = get_conn(); cur = conn.cursor(); cur.execute("SELECT user_id, banned_until FROM users WHERE banned_until > NOW() LIMIT 5"); users = cur.fetchall(); cur.close(); release_conn(conn)
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
            try: await q.edit_message_text(f"✅ Cleared.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_reports")]])); return
            except: pass
        if data.startswith("unban_user_"):
            tid = int(data.split("_")[2]); conn = get_conn(); cur = conn.cursor(); cur.execute("UPDATE users SET banned_until = NULL WHERE user_id = %s", (tid,)); conn.commit(); cur.close(); release_conn(conn)
            try: await q.edit_message_text("✅ Unbanned.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_banlist")]])); return
            except: pass

    # RATE & GENERAL
    if data.startswith("rate_"):
        parts = data.split("_")
        act = parts[1]
        target_str = parts[2]

        # [NEW] ILLUSION: Handle AI Rating
        if target_str == "AI":
            await q.edit_message_text("✅ Feedback Sent.")
            return

        # Handle Human Rating
        target = int(target_str)
        if act == "report": 
            await handle_report(update, context, uid, target)
            await q.edit_message_text("⚠️ Reported.")
        else:
            sc = 1 if act == "like" else -1
            conn = get_conn(); cur = conn.cursor()
            cur.execute("INSERT INTO user_interactions (rater_id, target_id, score) VALUES (%s, %s, %s)", (uid, target, sc))
            conn.commit(); cur.close(); release_conn(conn)
            await q.edit_message_text("✅ Sent.")
    
    if data == "action_search": await start_search(update, context); return
    if data == "main_menu": await show_main_menu(update); return
    if data == "stop_search": await stop_search_process(update, context); return
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
        app.add_handler(CommandHandler("broadcast", admin_broadcast_execute))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("feedback", handle_feedback_command))
        
        app.add_handler(MessageHandler(filters.TEXT, handle_text_input))
        
        app.add_handler(CallbackQueryHandler(button_handler))
        app.add_handler(MessageHandler(filters.ALL, relay_message))
        
        print("🤖 PHASE 20 BOT LIVE")
        app.run_polling()
