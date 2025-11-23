import logging
import psycopg2
from psycopg2 import pool
import datetime
import asyncio
import os
import threading
import random
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
# 1. CHAT CACHE: Stores who is chatting with whom. Instant access.
ACTIVE_CHATS = {} 

# 2. GAME CACHE (ISLAND A): Isolated memory for Game States.
# Structure: { user_id: { "partner": id, "game": "tod", "turn": "me", "mode": "answering" } }
ACTIVE_GAMES = {}
GAME_COOLDOWNS = {} # { user_id: datetime_object }

# 3. DB POOL
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
    if DB_POOL: return DB_POOL.getconn()
    return None

def release_conn(conn):
    if DB_POOL and conn: DB_POOL.putconn(conn)

# ==============================================================================
# 🎮 ISLAND B: GAME CONTENT & LOGIC (PHASE 15)
# ==============================================================================
GAME_LIST = ["Truth or Dare", "Would You Rather", "Rock Paper Scissors"]

GAME_CONTENT = {
    "truth": [
        "What is your biggest fear?", "What is the last lie you told?", 
        "Who is your secret crush?", "What is your most embarrassing moment?",
        "Have you ever cheated on a test?", "What is your biggest regret?",
        "What is the weirdest dream you've had?", "Do you believe in ghosts?",
        "What is a secret you've never told anyone?", "What is your worst habit?"
    ],
    "dare": [
        "Send a voice note singing 'Happy Birthday'.", "Send the 3rd photo in your gallery.",
        "Type a message with your nose.", "Send a sticker that describes you.",
        "Do 10 pushups and send a video note.", "Talk in emojis for the next 3 turns.",
        "Reveal your battery percentage.", "Send a voice note whispering a secret."
    ],
    "wyr": [
        ("Be invisible", "Be able to fly"), ("Always be cold", "Always be hot"),
        ("Rich but lonely", "Poor but loved"), ("Know how you die", "Know when you die")
    ]
}

# --- Helper: Check Cooldown ---
def check_cooldown(user_id):
    if user_id in GAME_COOLDOWNS:
        remaining = (GAME_COOLDOWNS[user_id] - datetime.datetime.now()).total_seconds()
        if remaining > 0: return int(remaining)
    return 0

# --- Helper: Generate Hybrid Menu (5 Random + 1 Manual) ---
def get_tod_menu(game_type):
    # Select 5 random questions from the library
    options = random.sample(GAME_CONTENT[game_type], 5)
    kb = []
    for opt in options:
        # Truncate long text for button label
        label = (opt[:30] + '..') if len(opt) > 30 else opt
        kb.append([InlineKeyboardButton(label, callback_data=f"game_pick_{game_type}_id")]) # In real app, use ID, here using generic
    
    # Add the Manual Option
    kb.append([InlineKeyboardButton("✍️ Ask Yourself (Type)", callback_data=f"game_manual_{game_type}")])
    # Store these options in RAM temporarily so we know what text to send when clicked? 
    # For Phase 15 simplified: We will embed the index or full text if short. 
    # To keep it robust: We will just pass the index of the random list if we stored it, 
    # but for statelessness, we will put the Text in the Callback (Warning: Telegram limit 64 bytes).
    # FIX: We will just use a simple lookup or just send "Random" for now. 
    # BETTER FIX for this file: Re-generate isn't an issue. We will just attach the text to the button if short.
    
    # Revised approach for Stability: 
    kb = []
    for i, opt in enumerate(options):
        # We map the button to the index 0-4. We need to save these 5 options to RAM?
        # No, let's just send the text directly if user clicks. 
        # Since we can't store per-turn state easily without DB, we will use a simplified flow:
        # The button sends "Pick Random" and server picks one. 
        # BUT User wants to SEE options.
        # Solution: We attach the hash or short ID. For now, let's just show "Option 1", "Option 2" 
        # No, User wants to see the question. 
        # Real Solution: Store the current 5 options in ACTIVE_GAMES[user_id]['options']
        pass 
    return options # We return the list, the handler will build the keyboard

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

def get_keyboard_game_active():
    return ReplyKeyboardMarkup([[KeyboardButton("🛑 Stop Game"), KeyboardButton("🛑 Stop Chat")]], resize_keyboard=True)

# ==============================================================================
# 🧠 MATCHMAKING ENGINE
# ==============================================================================
def find_match(user_id):
    conn = get_conn()
    cur = conn.cursor()
    
    cur.execute("SELECT language, interests, age_range, mood FROM users WHERE user_id = %s", (user_id,))
    me = cur.fetchone()
    if not me: release_conn(conn); return None, [], "Neutral", "English"
    my_lang, my_interests, my_age, my_mood = me
    my_tags = [t.strip().lower() for t in my_interests.split(',')] if my_interests else []

    cur.execute("SELECT target_id FROM user_interactions WHERE rater_id = %s AND score = -1", (user_id,))
    disliked_ids = {row[0] for row in cur.fetchall()}

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
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    conn = get_conn(); cur = conn.cursor()
    
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE status != 'idle'")
    online = cur.fetchone()[0]
    
    msg = (f"👮 **CONTROL ROOM**\n👥 Total: `{total}` | 🟢 Online: `{online}`")
    kb = [[InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast_info")]]
    
    try: await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    except: pass
    cur.close(); release_conn(conn)

# ==============================================================================
# 📝 ONBOARDING
# ==============================================================================
async def send_onboarding_step(update, step):
    kb = []
    msg = ""
    if step == 1: msg, kb = "1️⃣ **Gender?**", [[InlineKeyboardButton("Male", callback_data="set_gen_Male"), InlineKeyboardButton("Female", callback_data="set_gen_Female")], [InlineKeyboardButton("Skip", callback_data="set_gen_Hidden")]]
    elif step == 2: msg, kb = "2️⃣ **Age?**", [[InlineKeyboardButton("18-22", callback_data="set_age_18"), InlineKeyboardButton("23-30", callback_data="set_age_23")], [InlineKeyboardButton("Skip", callback_data="set_age_Hidden")]]
    elif step == 3: msg, kb = "3️⃣ **Lang?**", [[InlineKeyboardButton("English", callback_data="set_lang_English"), InlineKeyboardButton("Hindi", callback_data="set_lang_Hindi")], [InlineKeyboardButton("Skip", callback_data="set_lang_English")]]
    elif step == 4: msg, kb = "4️⃣ **Region?**", [[InlineKeyboardButton("Asia", callback_data="set_reg_Asia"), InlineKeyboardButton("Europe", callback_data="set_reg_Europe")], [InlineKeyboardButton("Skip", callback_data="set_reg_Hidden")]]
    elif step == 5: msg, kb = "5️⃣ **Mood?**", [[InlineKeyboardButton("Happy", callback_data="set_mood_Happy"), InlineKeyboardButton("Bored", callback_data="set_mood_Bored")], [InlineKeyboardButton("Skip", callback_data="set_mood_Neutral")]]
    elif step == 6: msg, kb = "6️⃣ **Interests?**\nType keywords or Skip.", [[InlineKeyboardButton("Skip", callback_data="onboarding_done")]]

    try:
        if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        else: await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    except: pass

# ==============================================================================
# 📱 MAIN CONTROLLER & GAME HOOKS
# ==============================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""INSERT INTO users (user_id, username, first_name) VALUES (%s, %s, %s) 
                   ON CONFLICT (user_id) DO UPDATE SET username = %s, first_name = %s""", 
                   (user.id, user.username, user.first_name, user.username, user.first_name))
    conn.commit(); cur.close(); release_conn(conn)

    welcome_msg = "👋 **Welcome to OmeTV Chatbot!**"
    await update.message.reply_text(welcome_msg, reply_markup=get_keyboard_lobby(), parse_mode='Markdown')

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    text = update.message.text
    user_id = update.effective_user.id

    # --- 🎮 GAME HOOK: Manual Input Trap ---
    if user_id in ACTIVE_GAMES:
        game = ACTIVE_GAMES[user_id]
        # If user is in "Ask Yourself" mode
        if game.get("mode") == "waiting_for_manual":
            partner_id = game["partner"]
            # 1. Send Question to Partner
            await context.bot.send_message(partner_id, f"❓ **Question:**\n{text}\n\n_(Type your answer)_", parse_mode='Markdown')
            await update.message.reply_text("✅ Sent.", reply_markup=get_keyboard_game_active())
            
            # 2. Update States
            ACTIVE_GAMES[user_id]["mode"] = "spectating"
            if partner_id in ACTIVE_GAMES:
                ACTIVE_GAMES[partner_id]["mode"] = "answering"
                ACTIVE_GAMES[partner_id]["turn"] = "me"
            return
    # ---------------------------------------

    if context.user_data.get("state") == "ONBOARDING_INTEREST":
        conn = get_conn(); cur = conn.cursor()
        cur.execute("UPDATE users SET interests = %s WHERE user_id = %s", (text, user_id))
        conn.commit(); cur.close(); release_conn(conn)
        context.user_data["state"] = None
        await update.message.reply_text("✅ **Ready!**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown'); return

    if text == "🚀 Start Matching": await start_search(update, context); return
    if text in ["🛑 Stop", "🛑 Stop Chat"]: await stop_chat(update, context); return
    if text == "⏭️ Next": await stop_chat(update, context, is_next=True); return
    if text == "❌ Stop Searching": await stop_search_process(update, context); return
    
    # --- 🎮 GAME HOOK: Game Lobby ---
    if text == "🎮 Games": await open_game_lobby(update, context); return
    if text == "🛑 Stop Game": await stop_game_only(update, context); return
    # --------------------------------

    if text == "/admin": await admin_panel(update, context); return
    await relay_message(update, context)

# ==============================================================================
# 🎮 GAME ENGINE LOGIC (PHASE 15)
# ==============================================================================
async def open_game_lobby(update, context):
    user_id = update.effective_user.id
    # 1. Check Cooldown
    wait = check_cooldown(user_id)
    if wait > 0:
        await update.message.reply_text(f"⏳ **Wait {wait}s** before asking again.", parse_mode='Markdown')
        return

    # 2. Show Lobby
    kb = [
        [InlineKeyboardButton("😈 Truth or Dare", callback_data="game_offer_ToD")],
        [InlineKeyboardButton("🎲 Would You Rather", callback_data="game_offer_WYR")],
        [InlineKeyboardButton("✂️ Rock Paper Scissors", callback_data="game_offer_RPS")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="game_cancel")]
    ]
    await update.message.reply_text("🎮 **Game Center**\nChoose a game to offer:", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def send_game_offer(update, context, game_code):
    user_id = update.effective_user.id
    partner_id = ACTIVE_CHATS.get(user_id)
    if not partner_id: 
        await update.message.reply_text("❌ You are not connected.")
        return

    game_name = "Truth or Dare" if game_code == "ToD" else "Would You Rather" if game_code == "WYR" else "Rock Paper Scissors"
    
    # Update Wait UI for Sender
    try: await update.callback_query.edit_message_text(f"⏳ Waiting for partner to accept **{game_name}**...", parse_mode='Markdown')
    except: pass

    # Smart Suggestion Logic (Remove current offer from suggestions)
    suggestions = [g for g in GAME_LIST if g != game_name]
    kb = [
        [InlineKeyboardButton("✅ Accept", callback_data=f"game_accept_{game_code}"), InlineKeyboardButton("❌ Reject", callback_data=f"game_reject_{game_code}")],
    ]
    # Add specific suggestion buttons
    suggest_row = []
    for sugg in suggestions:
        code = "ToD" if "Truth" in sugg else "WYR" if "Rather" in sugg else "RPS"
        suggest_row.append(InlineKeyboardButton(f"Suggest {sugg.split()[0]}", callback_data=f"game_suggest_{code}"))
    kb.append(suggest_row)

    try: await context.bot.send_message(partner_id, f"🎮 **Game Request**\nStranger wants to play **{game_name}**.", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    except: pass

async def start_game_session(update, context, game_code):
    # Initialize Game State for both
    # We assume 'q' is the accept button click
    q = update.callback_query
    user_B = q.from_user.id # The one who accepted
    user_A = ACTIVE_CHATS.get(user_B) # The one who offered

    if not user_A: return

    # Set RAM State
    ACTIVE_GAMES[user_A] = {"partner": user_B, "game": game_code, "turn": "me", "mode": "choosing"}
    ACTIVE_GAMES[user_B] = {"partner": user_A, "game": game_code, "turn": "them", "mode": "spectating"}

    # Notify Start
    kb_stop = get_keyboard_game_active()
    await context.bot.send_message(user_A, "✅ **Game Accepted!**\nIt is your turn.", reply_markup=kb_stop)
    await context.bot.send_message(user_B, "✅ **Game Started!**\nWaiting for partner...", reply_markup=kb_stop)

    # Trigger First Turn (Only implemented ToD for now as per request)
    if game_code == "ToD":
        await send_tod_menu(context, user_A)

async def send_tod_menu(context, user_id):
    # Generate 5 Random + 1 Manual
    options = random.sample(GAME_CONTENT["truth"] + GAME_CONTENT["dare"], 5)
    
    # Save these options in RAM so we know which text corresponds to which button index
    # ACTIVE_GAMES[user_id]['current_options'] = options 
    # ^ Simpler: We embed hash/id. For this phase, we put text in callback if short, or store index.
    # Let's store in RAM for safety.
    ACTIVE_GAMES[user_id]['options_cache'] = options

    kb = []
    for i, opt in enumerate(options):
        # Button format: "Truth: What is..."
        label = (opt[:25] + "..") 
        kb.append([InlineKeyboardButton(label, callback_data=f"game_pick_{i}")])
    
    kb.append([InlineKeyboardButton("✍️ Ask Yourself", callback_data="game_pick_manual")])
    
    await context.bot.send_message(user_id, "👇 **Your Turn: Choose a Question**", reply_markup=InlineKeyboardMarkup(kb))

async def handle_game_pick(update, context, pick_data):
    q = update.callback_query
    user_id = q.from_user.id
    game_state = ACTIVE_GAMES.get(user_id)
    if not game_state: return

    partner_id = game_state["partner"]

    if pick_data == "manual":
        game_state["mode"] = "waiting_for_manual"
        await q.edit_message_text("✍️ **Type your question below:**", parse_mode='Markdown')
        return
    
    # If picked from list
    try:
        idx = int(pick_data)
        question_text = game_state['options_cache'][idx]
        
        # Send to Partner
        await context.bot.send_message(partner_id, f"❓ **Question:**\n{question_text}\n\n_(Type your answer)_", parse_mode='Markdown')
        await q.edit_message_text(f"✅ You asked: {question_text}")

        # Update Turn State (Now waiting for partner to answer)
        game_state["mode"] = "spectating"
        if partner_id in ACTIVE_GAMES:
            ACTIVE_GAMES[partner_id]["mode"] = "answering"
            ACTIVE_GAMES[partner_id]["turn"] = "me"
            
    except:
        await q.edit_message_text("❌ Error picking.")

async def stop_game_only(update, context):
    user_id = update.effective_user.id
    partner_id = ACTIVE_CHATS.get(user_id)
    
    # Clear Game RAM
    if user_id in ACTIVE_GAMES: del ACTIVE_GAMES[user_id]
    if partner_id in ACTIVE_GAMES: del ACTIVE_GAMES[partner_id]
    
    # Reset Keyboard
    kb = get_keyboard_chat()
    await update.message.reply_text("🛑 **Game Stopped.**", reply_markup=kb, parse_mode='Markdown')
    if partner_id:
        try: await context.bot.send_message(partner_id, "🛑 **Partner stopped the game.**", reply_markup=kb, parse_mode='Markdown')
        except: pass

# ==============================================================================
# 🔌 FAST CONNECTION LOGIC
# ==============================================================================
async def start_search(update, context):
    user_id = update.effective_user.id
    if user_id in ACTIVE_CHATS:
        await update.message.reply_text("⛔ **Already in chat!**", parse_mode='Markdown'); return

    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET status = 'searching' WHERE user_id = %s", (user_id,))
    cur.execute("SELECT interests FROM users WHERE user_id = %s", (user_id,))
    tags = cur.fetchone()[0] or "Any"
    conn.commit(); cur.close(); release_conn(conn)
    
    await update.message.reply_text(f"📡 **Scanning...**\nLooking for: `{tags}`...", parse_mode='Markdown', reply_markup=get_keyboard_searching())
    await perform_match(update, context, user_id)

async def perform_match(update, context, user_id):
    partner_id, common, p_mood, p_lang = find_match(user_id)
    if partner_id:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("UPDATE users SET status='chatting', partner_id=%s WHERE user_id=%s", (partner_id, user_id))
        cur.execute("UPDATE users SET status='chatting', partner_id=%s WHERE user_id=%s", (user_id, partner_id))
        conn.commit(); cur.close(); release_conn(conn)
        
        ACTIVE_CHATS[user_id] = partner_id
        ACTIVE_CHATS[partner_id] = user_id
        
        common_str = ", ".join(common).title() if common else "Random"
        msg = (f"⚡ **YOU ARE CONNECTED!**\n\n🎭 **Mood:** {p_mood}\n🔗 **Interest:** {common_str}\n⚠️ *Tip: Say Hi!*")
        
        kb = get_keyboard_chat()
        await context.bot.send_message(user_id, msg, reply_markup=kb, parse_mode='Markdown')
        try: await context.bot.send_message(partner_id, msg, reply_markup=kb, parse_mode='Markdown')
        except: pass

async def stop_chat(update, context, is_next=False):
    user_id = update.effective_user.id
    partner_id = ACTIVE_CHATS.pop(user_id, 0)
    if partner_id and partner_id in ACTIVE_CHATS: del ACTIVE_CHATS[partner_id]
    
    # CLEANUP GAMES TOO
    if user_id in ACTIVE_GAMES: del ACTIVE_GAMES[user_id]
    if partner_id in ACTIVE_GAMES: del ACTIVE_GAMES[partner_id]

    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET status='idle', partner_id=0 WHERE user_id IN (%s, %s)", (user_id, partner_id))
    conn.commit(); cur.close(); release_conn(conn)
    
    kb = get_keyboard_lobby()
    await update.message.reply_text("🔌 **Disconnected.**", reply_markup=kb, parse_mode='Markdown')
    if partner_id:
        try: await context.bot.send_message(partner_id, "🔌 **Partner Disconnected.**", reply_markup=kb, parse_mode='Markdown')
        except: pass
    
    if is_next: await start_search(update, context)

async def stop_search_process(update, context):
    user_id = update.effective_user.id
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET status='idle' WHERE user_id = %s", (user_id,))
    conn.commit(); cur.close(); release_conn(conn)
    await update.message.reply_text("❌ **Stopped.**", reply_markup=get_keyboard_lobby(), parse_mode='Markdown')

async def relay_message(update, context):
    user_id = update.effective_user.id
    partner_id = ACTIVE_CHATS.get(user_id)
    
    if partner_id:
        try: await update.message.copy(chat_id=partner_id)
        except: await stop_chat(update, context)
        
        # 🎲 Game Logic: Detect Answers
        # If Partner was waiting for an answer (ToD), we might want to swap turn here automatically?
        # For simplicity in Phase 15: We assume any text sent is the answer, and we swap the menu manually 
        # or we just let them chat. 
        # To make it "Gamey": If user_id was in "answering" mode, we should trigger the next menu for Partner.
        if user_id in ACTIVE_GAMES and ACTIVE_GAMES[user_id].get("mode") == "answering":
            game = ACTIVE_GAMES[user_id]
            partner = game["partner"]
            # Swap turns
            ACTIVE_GAMES[user_id]["mode"] = "spectating"
            ACTIVE_GAMES[partner]["mode"] = "choosing"
            ACTIVE_GAMES[partner]["turn"] = "me"
            # Show menu to partner
            if game["game"] == "ToD":
                await send_tod_menu(context, partner)

# ==============================================================================
# 🧩 BUTTON HANDLER
# ==============================================================================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = q.from_user.id

    # --- 🎮 GAME ISLAND HOOKS ---
    if data.startswith("game_offer_"): await send_game_offer(update, context, data.split("_")[2]); return
    
    if data.startswith("game_accept_"): 
        await q.edit_message_text("✅ You accepted.")
        await start_game_session(update, context, data.split("_")[2]); return
        
    if data.startswith("game_reject_"):
        # Add Cooldown
        GAME_COOLDOWNS[uid] = datetime.datetime.now() + datetime.timedelta(seconds=60)
        partner_id = ACTIVE_CHATS.get(uid)
        await q.edit_message_text("❌ Rejected.")
        if partner_id:
            # Cooldown for sender too
            GAME_COOLDOWNS[partner_id] = datetime.datetime.now() + datetime.timedelta(seconds=60) 
            try: await context.bot.send_message(partner_id, "❌ **Offer Rejected.**\n(Wait 60s to ask again)")
            except: pass
        return

    if data.startswith("game_suggest_"):
        # Send suggestion back (A suggests to B, B suggests back)
        # For Phase 15, simplified: treat as a new offer but skipping the lobby
        await send_game_offer(update, context, data.split("_")[2]); return

    if data.startswith("game_pick_"): await handle_game_pick(update, context, data.split("_")[2]); return
    if data == "game_cancel": await q.delete_message(); return
    # ----------------------------

    if data == "force_random": await perform_match(update, context, uid); return
    if data == "close_settings": await q.delete_message(); return
    
    # Onboarding logic
    if data.startswith("set_gen_"): await send_onboarding_step(update, 2); return
    if data.startswith("set_age_"): await send_onboarding_step(update, 3); return
    if data.startswith("set_lang_"): await send_onboarding_step(update, 4); return
    if data.startswith("set_reg_"): await send_onboarding_step(update, 5); return
    if data.startswith("set_mood_"): context.user_data["state"] = "ONBOARDING_INTEREST"; await send_onboarding_step(update, 6); return
    if data == "onboarding_done": context.user_data["state"] = None; await update.message.reply_text("👋 Lobby", reply_markup=get_keyboard_lobby()); return

    # Admin Logic
    if data == "admin_broadcast_info" and uid in ADMIN_IDS:
        try: await q.edit_message_text("📢 **Broadcast:**\nType `/broadcast Msg`"); return
        except error.BadRequest: pass

    if data == "action_search": await start_search(update, context); return
    if data == "main_menu": await update.message.reply_text("👋 Lobby", reply_markup=get_keyboard_lobby()); return
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
        app.add_handler(CommandHandler("help", start)) # Reuse start as help for now
        
        app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_input))
        app.add_handler(CallbackQueryHandler(button_handler))
        app.add_handler(MessageHandler(filters.ALL, relay_message))
        
        print("🤖 PHASE 15 BOT LIVE (GAMES ENABLED)")
        app.run_polling()
