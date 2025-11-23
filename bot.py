import logging
import psycopg2
from psycopg2 import pool
import datetime
import asyncio
import os
import threading
import random  # <--- NEW
import time  # <--- THIS WAS MISSING
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
# 🚀 HIGH-PERFORMANCE ENGINE (RAM Cache & Connection Pool)
# ==============================================================================
# 1. RAM CACHE: Stores who is chatting with whom. Instant access. 0ms Latency.
ACTIVE_CHATS = {} 
# --- GAME STATE & DATA ---
GAME_STATES = {}       # {user_id: {'game': 'tod', 'turn': uid, 'partner': pid}}
GAME_COOLDOWNS = {}    # {user_id: timestamp}

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

# 2. DB POOL: Keeps connections open so we don't "dial" the DB every time.
DB_POOL = None

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
    
    # Cooldown
    last = GAME_COOLDOWNS.get(user_id, 0)
    if time.time() - last < 60:
        await context.bot.send_message(user_id, f"⏳ Wait {int(60 - (time.time() - last))}s before sending another request.")
        return
    GAME_COOLDOWNS[user_id] = time.time()

    # Suggestion Logic
    all_games = ["Truth or Dare", "Would You Rather", "Rock Paper Scissors"]
    suggestions = [g for g in all_games if g != game_name]
    
    kb = [
        [InlineKeyboardButton("✅ Accept", callback_data=f"game_accept_{game_name}"), InlineKeyboardButton("❌ Reject", callback_data="game_reject")],
        [InlineKeyboardButton(f"💡 Suggest {suggestions[0]}", callback_data=f"game_offer_{suggestions[0]}"),
         InlineKeyboardButton(f"💡 Suggest {suggestions[1]}", callback_data=f"game_offer_{suggestions[1]}")]
    ]
    
    await context.bot.send_message(user_id, f"🎮 **Offered {game_name}**\nWaiting for partner...", parse_mode='Markdown')
    await context.bot.send_message(partner_id, f"🎮 **Game Request**\nPartner wants to play **{game_name}**.", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def start_game_session(update, context, game_name, p1, p2):
    # P1 = Offerer, P2 = Accepter
    # P1 = Offerer, P2 = Accepter
    GAME_STATES[p1] = GAME_STATES[p2] = {"game": game_name, "turn": p2, "partner": p2, "status": "playing", "moves": {}}
    
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
    kb = [[InlineKeyboardButton(f"🅰️ {q[0]}", callback_data="wyr_a"), InlineKeyboardButton(f"🅱️ {q[1]}", callback_data="wyr_b")]]
    await context.bot.send_message(p1, "⚖️ **Would You Rather...**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    await context.bot.send_message(p2, "⚖️ **Would You Rather...**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

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

    msg = (f"👮 **CONTROL ROOM**\n👥 Total: `{total}` | 🟢 Online: `{online}`\n⚠️ Flagged: `{flagged}`\n\n"
           f"🚻 **Gender:** {g_stats}\n🌍 {get_stat('region')}\n🗣️ {get_stat('language')}")
    
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
        msg = "6️⃣ **Final Step! Interests**\n\nType keywords (e.g., *Cricket, Movies*) or click Skip."
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

    welcome_msg = "👋 **Welcome to OmeTV Chatbot!**\n\nConnect with strangers worldwide. 🌍\nNo names. No login.\n\n*Let's vibe check.* 👇"
    if not data or data[1] == 'Hidden':
        await update.message.reply_text(welcome_msg, reply_markup=ReplyKeyboardRemove(), parse_mode='Markdown')
        await send_onboarding_step(update, 1)
    else:
        msg = await update.message.reply_text("🔄 Loading...", reply_markup=ReplyKeyboardRemove())
        try: await context.bot.delete_message(chat_id=user.id, message_id=msg.message_id)
        except: pass
        await show_main_menu(update)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🆘 **HELP**\n\n🚀 Start: Match\n🛑 Stop: End\n📨 Feedback: `/feedback msg`", parse_mode='Markdown')

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    text = update.message.text
    user_id = update.effective_user.id

    # 1. GAME ANSWER LOGIC (The Answer)
    # Check if this user is supposed to be answering a question
    if user_id in GAME_STATES and GAME_STATES[user_id].get("status") == "answering":
        partner_id = ACTIVE_CHATS.get(user_id)
        if partner_id:
            # Send Answer to Partner
            await context.bot.send_message(partner_id, f"🗣️ **Answer:** {text}", parse_mode='Markdown')
            await update.message.reply_text("✅ Sent.")
            
            # Reset Status
            GAME_STATES[user_id]["status"] = "playing"
            GAME_STATES[partner_id]["status"] = "playing"
            
            # SWAP TURNS (Now Partner picks Truth/Dare)
            await send_tod_turn(context, partner_id)
        return

    # 2. MANUAL QUESTION INPUT (The Asker)
    if context.user_data.get("state") == "GAME_MANUAL":
        partner_id = ACTIVE_CHATS.get(user_id)
        if partner_id:
            await context.bot.send_message(partner_id, f"🎲 **QUESTION:**\n{text}\n\n*Type your answer...*", parse_mode='Markdown')
            await update.message.reply_text("✅ Question Sent. Waiting for answer...")
            
            # Set Partner to Answering Mode
            if partner_id in GAME_STATES:
                GAME_STATES[partner_id]["status"] = "answering"
        context.user_data["state"] = None
        return

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
    if text == "🎯 Change Interests": context.user_data["state"] = "ONBOARDING_INTEREST"; await update.message.reply_text("👇 Type interests:", reply_markup=ReplyKeyboardRemove()); return
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
              [InlineKeyboardButton("✂️ Rock Paper Scissors", callback_data="game_offer_Rock Paper Scissors")]]
        await update.message.reply_text("🎮 **Game Center**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown'); return
    
    if text == "🛑 Stop Game":
        pid = ACTIVE_CHATS.get(user_id)
        if user_id in GAME_STATES: del GAME_STATES[user_id]
        if pid and pid in GAME_STATES: del GAME_STATES[pid]
        await update.message.reply_text("🛑 Game Stopped.", reply_markup=get_keyboard_chat())
        if pid: await context.bot.send_message(pid, "🛑 Partner stopped game.", reply_markup=get_keyboard_chat())
        return

    # 6. COMMANDS
    if text.startswith("/"):
        if text == "/stop": await stop_chat(update, context); return
        if text == "/admin": await admin_panel(update, context); return
        if text.startswith("/ban"): await admin_ban_command(update, context); return
        if text.startswith("/warn"): await admin_warn_command(update, context); return
        if text.startswith("/broadcast"): await admin_broadcast_execute(update, context); return
        if text.startswith("/feedback"): await handle_feedback_command(update, context); return

    await relay_message(update, context)
# ==============================================================================
# 🔌 FAST CONNECTION LOGIC (RAM + DB)
# ==============================================================================
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
    # Fast Check RAM First
    if user_id in ACTIVE_CHATS:
        await update.message.reply_text("⛔ **Already in chat!**", parse_mode='Markdown'); return

    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET status = 'searching' WHERE user_id = %s", (user_id,))
    conn.commit(); 
    
    # Fetch interests for UI display
    cur.execute("SELECT interests FROM users WHERE user_id = %s", (user_id,))
    tags = cur.fetchone()[0] or "Any"
    cur.close(); release_conn(conn)
    
    await update.message.reply_text(f"📡 **Scanning...**\nLooking for: `{tags}`...", parse_mode='Markdown', reply_markup=get_keyboard_searching())
    if context.job_queue: context.job_queue.run_once(send_reroll_option, 15, data=user_id)
    await perform_match(update, context, user_id)

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
               f"🗣️ **Lang:** {p_lang}\n\n⚠️ *Tip: Say Hi!*")
        
        kb = get_keyboard_chat()
        await context.bot.send_message(user_id, msg, reply_markup=kb, parse_mode='Markdown')
        try: await context.bot.send_message(partner_id, msg, reply_markup=kb, parse_mode='Markdown')
        except: pass

async def stop_chat(update, context, is_next=False):
    user_id = update.effective_user.id
    
    # Clear RAM Cache immediately
    partner_id = ACTIVE_CHATS.pop(user_id, 0)
    if partner_id and partner_id in ACTIVE_CHATS: del ACTIVE_CHATS[partner_id]

    # Clear Game States on Disconnect
    if user_id in GAME_STATES: del GAME_STATES[user_id]
    if partner_id in GAME_STATES: del GAME_STATES[partner_id]

    # Clear DB
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET status='idle', partner_id=0 WHERE user_id IN (%s, %s)", (user_id, partner_id))
    conn.commit(); cur.close(); release_conn(conn)
    
    # 1. KEYBOARD FOR ME (Rate Partner)
    k_me = [[InlineKeyboardButton("👍", callback_data=f"rate_like_{partner_id}"), InlineKeyboardButton("👎", callback_data=f"rate_dislike_{partner_id}")],
            [InlineKeyboardButton("⚠️ Report", callback_data=f"rate_report_{partner_id}")],
            [InlineKeyboardButton("🚀 New Match", callback_data="action_search"), InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]]
    
    # 2. KEYBOARD FOR PARTNER (Rate Me) - NEW
    k_partner = [[InlineKeyboardButton("👍", callback_data=f"rate_like_{user_id}"), InlineKeyboardButton("👎", callback_data=f"rate_dislike_{user_id}")],
                 [InlineKeyboardButton("⚠️ Report", callback_data=f"rate_report_{user_id}")],
                 [InlineKeyboardButton("🚀 New Match", callback_data="action_search"), InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]]
    
    if is_next:
        await update.message.reply_text("⏭️ **Skipping...**", reply_markup=ReplyKeyboardRemove(), parse_mode='Markdown')
        await update.message.reply_text("📊 Feedback?", reply_markup=InlineKeyboardMarkup(k_me))
        await start_search(update, context)
    else:
        await update.message.reply_text("🔌 **Disconnected.**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown')
        await update.message.reply_text("📊 Feedback?", reply_markup=InlineKeyboardMarkup(k_me))

    if partner_id:
        try: 
            await context.bot.send_message(partner_id, "🔌 **Partner Disconnected.**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown')
            await context.bot.send_message(partner_id, "📊 Feedback?", reply_markup=InlineKeyboardMarkup(k_partner))
        except: pass

async def relay_message(update, context):
    user_id = update.effective_user.id
    
    # FAST PATH: Check RAM First
    partner_id = ACTIVE_CHATS.get(user_id)
    
    # SLOW PATH: Check DB (If bot restarted)
    if not partner_id:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT partner_id FROM users WHERE user_id = %s AND status='chatting'", (user_id,))
        row = cur.fetchone()
        cur.close(); release_conn(conn)
        if row and row[0]:
            partner_id = row[0]
            ACTIVE_CHATS[user_id] = partner_id # Repopulate RAM

    if partner_id:
        # 🟢 GAME ANSWER LOGIC (MOVED HERE TO SUPPORT MEDIA)
        # Check if this user is supposed to be answering a question
        if user_id in GAME_STATES and GAME_STATES[user_id].get("status") == "answering":
            # Forward the content (Text, Voice, Photo, Video, etc.)
            try: 
                await update.message.copy(chat_id=partner_id, caption=f"🗣️ **Answer** (from Game)")
                await update.message.reply_text("✅ Answer Sent.")
                
                # Reset Status
                GAME_STATES[user_id]["status"] = "playing"
                if partner_id in GAME_STATES: GAME_STATES[partner_id]["status"] = "playing"
                
                # SWAP TURNS (Now Partner picks Truth/Dare)
                await send_tod_turn(context, partner_id)
                return # Stop here, don't double send
            except Exception as e:
                print(f"Game Relay Error: {e}")

        # NORMAL CHAT RELAY
        if update.message.text:
            # Log in background (simplified here as synchronous for safety)
            conn = get_conn(); cur = conn.cursor()
            cur.execute("INSERT INTO chat_logs (sender_id, receiver_id, message) VALUES (%s, %s, %s)", (user_id, partner_id, update.message.text))
            conn.commit(); cur.close(); release_conn(conn)
        
        try: await update.message.copy(chat_id=partner_id)
        except: await stop_chat(update, context)

# ==============================================================================
# 🧩 HELPERS & BUTTON HANDLER
# ==============================================================================
async def send_reroll_option(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT status FROM users WHERE user_id = %s", (user_id,))
    if cur.fetchone()[0] == 'searching':
        kb = [[InlineKeyboardButton("🎲 Try Random", callback_data="force_random")]]
        try: await context.bot.send_message(user_id, "🐢 **Quiet...**", reply_markup=InlineKeyboardMarkup(kb))
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
        if update.message: await update.message.reply_text("👋 **Lobby**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown')
        elif update.callback_query: await update.callback_query.message.reply_text("👋 **Lobby**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown')
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
    uid = q.from_user.id
    # NEW SETTINGS REDIRECTS
    if data == "set_gen_menu": await send_onboarding_step(update, 1); return
    if data == "set_age_menu": await send_onboarding_step(update, 2); return
    if data == "set_lang_menu": await send_onboarding_step(update, 3); return
    if data == "set_mood_menu": await send_onboarding_step(update, 5); return
    if data == "force_random": await perform_match(update, context, uid); return
    if data == "close_settings": await q.delete_message(); return
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
        if gd and "options" in gd:
            q_text = gd["options"][int(data.split("_")[2])]
            partner_id = ACTIVE_CHATS.get(uid) # The Answerer
            
            if partner_id:
                await context.bot.send_message(partner_id, f"🎲 **QUESTION:**\n{q_text}\n\n*Type your answer...*", parse_mode='Markdown')
                await q.edit_message_text(f"✅ Asked: {q_text}")
                # Mark partner as answering
                if partner_id in GAME_STATES: GAME_STATES[partner_id]["status"] = "answering"
        return

# ROCK PAPER SCISSORS LOGIC
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
            
            # 3. Calculate Winner
            res = "🤝 **DRAW!**"
            if move == p_move: res = "🤝 **DRAW!**"
            elif (move == "rock" and p_move == "scissors") or \
                 (move == "paper" and p_move == "rock") or \
                 (move == "scissors" and p_move == "paper"):
                res = "🏆 **YOU WON!**"
            else:
                res = "💀 **YOU LOST!**"
            
            # Mirror result for partner
            p_res = "🏆 **YOU WON!**" if "LOST" in res else ("💀 **YOU LOST!**" if "WON" in res else "🤝 **DRAW!**")
            
            # 4. Send Results
            await context.bot.send_message(uid, f"You: {move} | Partner: {p_move}\n\n{res}", parse_mode='Markdown')
            await context.bot.send_message(partner_id, f"You: {p_move} | Partner: {move}\n\n{p_res}", parse_mode='Markdown')
            
            # 5. Reset for Next Round
            gd["moves"] = {}
            await asyncio.sleep(2) # Breathing room
            await send_rps_round(context, uid, partner_id)
        return

    # WOULD YOU RATHER LOGIC
    if data.startswith("wyr_"):
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
            
            # 3. Send Results
            msg = f"📊 **RESULTS**\n\n👤 You: **Option {choice}**\n👤 Partner: **Option {p_choice}**"
            p_msg = f"📊 **RESULTS**\n\n👤 You: **Option {p_choice}**\n👤 Partner: **Option {choice}**"
            
            await context.bot.send_message(uid, msg, parse_mode='Markdown')
            await context.bot.send_message(partner_id, p_msg, parse_mode='Markdown')
            
            # 4. Next Round
            gd["moves"] = {}
            await asyncio.sleep(2)
            await send_wyr_round(context, uid, partner_id)
        return

    if data == "tod_manual": context.user_data["state"] = "GAME_MANUAL"; await q.edit_message_text("✍️ **Type your question now:**"); return

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
        
        print("🤖 PHASE 15 BOT LIVE")
        app.run_polling()
