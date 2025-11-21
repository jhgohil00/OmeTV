import logging
import psycopg2
import datetime
import asyncio
import os
import threading
from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update, error
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

# ==============================================================================
# ⚙️ CONFIGURATION
# ==============================================================================
BOT_TOKEN = "7392244323:AAEtT6oMRrAJAqkWaOEUumi51DTMIqkFKM4"
DATABASE_URL = "postgresql://neondb_owner:npg_bXwJRd61ZjUg@ep-winter-glitter-a1ic6wxx-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"

# 👑 YOUR ADMIN ID (Get this from @userinfobot)
ADMIN_IDS = [8315364356]  # <--- REPLACE WITH YOUR ID

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
    try:
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        print(f"❌ CONNECTION ERROR: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    
    # Core Tables
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
    print("\n✅ CLOUD PHASE READY: Production Mode.\n")

# ==============================================================================
# 🧠 MATCHMAKING ENGINE
# ==============================================================================
def find_match(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT language, interests, age_range FROM users WHERE user_id = %s", (user_id,))
    me = cur.fetchone()
    if not me: return None, []
    my_lang, my_interests, my_age = me[0], me[1], me[2]
    my_tags = [t.strip().lower() for t in my_interests.split(',')] if my_interests else []

    cur.execute("SELECT target_id FROM user_interactions WHERE rater_id = %s AND score = -1", (user_id,))
    disliked_ids = {row[0] for row in cur.fetchall()}

    cur.execute("""
        SELECT user_id, language, interests, age_range 
        FROM users 
        WHERE status = 'searching' 
        AND user_id != %s
        AND (banned_until IS NULL OR banned_until < NOW())
    """, (user_id,))
    candidates = cur.fetchall()
    
    best_match = None
    best_score = -999999
    common_interests = []

    for cand in candidates:
        cand_id, cand_lang, cand_interests, cand_age = cand
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

    conn.close()
    return best_match, common_interests

# ==============================================================================
# 👮 ADMIN SYSTEM
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
    
    def get_stats(column):
        cur.execute(f"SELECT {column}, COUNT(*) FROM users GROUP BY {column} ORDER BY COUNT(*) DESC LIMIT 3")
        return [f"{r[0]}: {r[1]}" for r in cur.fetchall()]

    regions = " | ".join(get_stats("region"))
    langs = " | ".join(get_stats("language"))
    ages = " | ".join(get_stats("age_range"))
    
    stats_msg = (
        f"👮 **CONTROL ROOM**\n"
        f"👥 Total: `{total}` | 🟢 Online: `{online}`\n"
        f"⚠️ Flagged: `{flagged}`\n\n"
        f"🌍 **Geo:** {regions}\n"
        f"🗣️ **Lang:** {langs}\n"
        f"🎂 **Age:** {ages}"
    )
    
    keyboard = [
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
         InlineKeyboardButton("📜 Recent Users", callback_data="admin_users")],
        [InlineKeyboardButton("⚠️ Reports", callback_data="admin_reports"),
         InlineKeyboardButton("🚫 Banned List", callback_data="admin_banlist")]
    ]
    
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(stats_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        else:
            await update.message.reply_text(stats_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except error.BadRequest: pass 
    conn.close()

async def admin_warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        target_id = int(context.args[0])
        reason = " ".join(context.args[1:])
        await context.bot.send_message(target_id, f"⚠️ **OFFICIAL WARNING**\n\nAdmin Message: {reason}\n\n*Continuing this behavior will result in a ban.*", parse_mode='Markdown')
        await update.message.reply_text(f"✅ Warning sent to `{target_id}`.")
    except:
        await update.message.reply_text("❌ Usage: `/warn {user_id} {message}`")

async def admin_ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        target_id = int(context.args[0])
        hours = int(context.args[1])
        conn = get_db_connection()
        cur = conn.cursor()
        ban_time = datetime.datetime.now() + datetime.timedelta(hours=hours)
        cur.execute("UPDATE users SET banned_until = %s WHERE user_id = %s", (ban_time, target_id))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"🔨 User `{target_id}` banned for {hours} hours.")
        try: await context.bot.send_message(target_id, f"🚫 You are banned for {hours} hours.")
        except: pass
    except:
        await update.message.reply_text("❌ Usage: `/ban {user_id} {hours}`")

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("❌ Usage: `/broadcast {message}`")
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    users = cur.fetchall()
    conn.close()
    await update.message.reply_text(f"📢 Sending to {len(users)} users...")
    for user in users:
        try: await context.bot.send_message(user[0], f"📢 **Announcement:**\n\n{msg}", parse_mode='Markdown')
        except: pass
    await update.message.reply_text("✅ Broadcast done.")

# ==============================================================================
# 📱 BOT INTERFACE & ONBOARDING
# ==============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT banned_until, gender FROM users WHERE user_id = %s", (user.id,))
    data = cur.fetchone()
    
    if data and data[0] and data[0] > datetime.datetime.now():
        await update.message.reply_text(f"🚫 You are banned until {data[0]}.")
        conn.close()
        return

    cur.execute("""
        INSERT INTO users (user_id, username, first_name) VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET username = %s, first_name = %s
    """, (user.id, user.username, user.first_name, user.username, user.first_name))
    conn.commit()
    conn.close()

    # --- THE GHOST BUTTON FIX ---
    # This checks for a specific flag or just sends a "Refresh" message
    # We send the Welcome Text with ReplyKeyboardRemove to kill the old buttons
    welcome_msg = (
        "👋 **Welcome to OmeTV Chatbot!**\n\n"
        "Connect with strangers worldwide. 🌍\n"
        "No names. No login. Just chat.\n\n"
        "*First, let's do a quick vibe check to find your best match.* 👇"
    )
    
    if not data or data[1] == 'Hidden':
        # New User: Kill keyboard AND show onboarding
        await update.message.reply_text(welcome_msg, reply_markup=ReplyKeyboardRemove(), parse_mode='Markdown')
        await send_onboarding_step(update, 1)
    else:
        # Existing User: Just kill keyboard then show menu
        # We send a tiny "Refreshing..." message to remove the keyboard, then show menu
        msg = await update.message.reply_text("🔄 Loading...", reply_markup=ReplyKeyboardRemove())
        try:
            await context.bot.delete_message(chat_id=user.id, message_id=msg.message_id)
        except: pass
        await show_main_menu(update)

async def send_onboarding_step(update, step):
    keyboard = []
    msg = ""

    if step == 1: # Gender
        msg = "1️⃣ **What's your gender?**"
        keyboard = [
            [InlineKeyboardButton("👨 Male", callback_data="set_gen_Male"), InlineKeyboardButton("👩 Female", callback_data="set_gen_Female")],
            [InlineKeyboardButton("🌈 Other", callback_data="set_gen_Other"), InlineKeyboardButton("⏭️ Skip", callback_data="set_gen_Hidden")]
        ]
    elif step == 2: # Age
        msg = "2️⃣ **Age Group?**"
        keyboard = [
            [InlineKeyboardButton("🐣 ~18", callback_data="set_age_~18"), InlineKeyboardButton("🧢 20-25", callback_data="set_age_20-25")],
            [InlineKeyboardButton("💼 25-30", callback_data="set_age_25-30"), InlineKeyboardButton("☕ 30-35", callback_data="set_age_30-35")],
            [InlineKeyboardButton("🍷 40+", callback_data="set_age_40+"), InlineKeyboardButton("⏭️ Skip", callback_data="set_age_Hidden")]
        ]
    elif step == 3: # Language
        msg = "3️⃣ **Primary Language?**"
        keyboard = [
            [InlineKeyboardButton("🇺🇸 English", callback_data="set_lang_English"), InlineKeyboardButton("🇮🇳 Hindi", callback_data="set_lang_Hindi")],
            [InlineKeyboardButton("🇫🇷 French", callback_data="set_lang_French"), InlineKeyboardButton("🇪🇸 Spanish", callback_data="set_lang_Spanish")],
            [InlineKeyboardButton("🇯🇵 Japanese", callback_data="set_lang_Japanese"), InlineKeyboardButton("🇮🇩 Indo", callback_data="set_lang_Indo")],
            [InlineKeyboardButton("🌍 Other", callback_data="set_lang_Other"), InlineKeyboardButton("⏭️ Skip", callback_data="set_lang_English")]
        ]
    elif step == 4: # Region
        msg = "4️⃣ **Region?**"
        keyboard = [
            [InlineKeyboardButton("🌏 Asia", callback_data="set_reg_Asia"), InlineKeyboardButton("🌍 Europe", callback_data="set_reg_Europe")],
            [InlineKeyboardButton("🌎 America", callback_data="set_reg_America"), InlineKeyboardButton("🌍 Africa", callback_data="set_reg_Africa")],
            [InlineKeyboardButton("⏭️ Skip", callback_data="set_reg_Hidden")]
        ]
    elif step == 5: # Mood
        msg = "5️⃣ **Current Mood?**"
        keyboard = [
            [InlineKeyboardButton("😃 Happy", callback_data="set_mood_Happy"), InlineKeyboardButton("😔 Sad", callback_data="set_mood_Sad")],
            [InlineKeyboardButton("😴 Bored", callback_data="set_mood_Bored"), InlineKeyboardButton("🥀 Lonely", callback_data="set_mood_Lonely")],
            [InlineKeyboardButton("😰 Anxious", callback_data="set_mood_Anxious"), InlineKeyboardButton("⏭️ Skip", callback_data="set_mood_Neutral")]
        ]
    elif step == 6: # Interest
        msg = "6️⃣ **Final Step! Interests**\n\nType keywords (e.g., *Cricket, Movies*) or click Skip."
        keyboard = [[InlineKeyboardButton("⏭️ Skip & Finish", callback_data="onboarding_done")]]

    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode='Markdown')
    except error.BadRequest: pass

async def show_main_menu(update: Update):
    keyboard = [
        [InlineKeyboardButton("🚀 Start Matching", callback_data="action_search")],
        [InlineKeyboardButton("✨ Setup My Vibe", callback_data="restart_onboarding"),
         InlineKeyboardButton("🪪 My ID", callback_data="action_profile")]
    ]
    msg = "👋 **Ready to meet someone new?**\n\nNo bots. No spam. Just real people.\n\n⚡ **Status:** Active & Waiting"
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        else:
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except error.BadRequest: pass

async def send_reroll_option(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id = job.data
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT status FROM users WHERE user_id = %s", (user_id,))
    res = cur.fetchone()
    conn.close()
    if res and res[0] == 'searching':
        keyboard = [[InlineKeyboardButton("🎲 Try Random Match", callback_data="force_random")]]
        try: await context.bot.send_message(user_id, "🐢 **Taking a while...**\nWe are still looking for your specific match.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        except: pass

# --- HANDLERS ---

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    
    # ONBOARDING
    if data.startswith("set_gen_"):
        val = data.split("_")[2]
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET gender = %s WHERE user_id = %s", (val, user_id))
        conn.commit()
        conn.close()
        await send_onboarding_step(update, 2)
        return

    if data.startswith("set_age_"):
        val = data.split("_")[2]
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET age_range = %s WHERE user_id = %s", (val, user_id))
        conn.commit()
        conn.close()
        await send_onboarding_step(update, 3)
        return

    if data.startswith("set_lang_"):
        val = data.split("_")[2]
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET language = %s WHERE user_id = %s", (val, user_id))
        conn.commit()
        conn.close()
        await send_onboarding_step(update, 4)
        return
    
    if data.startswith("set_reg_"):
        val = data.split("_")[2]
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET region = %s WHERE user_id = %s", (val, user_id))
        conn.commit()
        conn.close()
        await send_onboarding_step(update, 5)
        return

    if data.startswith("set_mood_"):
        val = data.split("_")[2]
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET mood = %s WHERE user_id = %s", (val, user_id))
        conn.commit()
        conn.close()
        context.user_data["state"] = "ONBOARDING_INTEREST"
        await send_onboarding_step(update, 6)
        return

    if data == "onboarding_done":
        context.user_data["state"] = None
        await show_main_menu(update)
        return
    
    if data == "restart_onboarding":
        await send_onboarding_step(update, 1)
        return

    # ADMIN
    if user_id in ADMIN_IDS:
        if data == "admin_home":
            await admin_panel(update, context)
            return
            
        if data == "admin_broadcast":
            try: await query.edit_message_text("📢 **Broadcast:**\nType `/broadcast Your Message`", 
                                          reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_home")]]), parse_mode='Markdown')
            except error.BadRequest: pass
            return

        if data == "admin_reports":
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT user_id, report_count FROM users WHERE report_count > 0 ORDER BY report_count DESC LIMIT 5")
            users = cur.fetchall()
            conn.close()
            
            if not users:
                try: await query.edit_message_text("✅ No active reports.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_home")]]))
                except error.BadRequest: pass
            else:
                buttons = []
                for u in users:
                    buttons.append([
                        InlineKeyboardButton(f"🔨 Ban {u[0]}", callback_data=f"ban_user_{u[0]}"),
                        InlineKeyboardButton(f"✅ Clear {u[0]}", callback_data=f"clear_user_{u[0]}")
                    ])
                buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin_home")])
                try: await query.edit_message_text("⚠️ **Flagged Users:**\nSelect Action:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode='Markdown')
                except error.BadRequest: pass
            return

        if data == "admin_banlist":
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM users WHERE banned_until > NOW() LIMIT 5")
            users = cur.fetchall()
            conn.close()
            
            if not users:
                try: await query.edit_message_text("✅ No banned users.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_home")]]))
                except error.BadRequest: pass
            else:
                buttons = []
                for u in users:
                    buttons.append([InlineKeyboardButton(f"✅ Unban {u[0]}", callback_data=f"unban_user_{u[0]}")])
                buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin_home")])
                try: await query.edit_message_text("🚫 **Click to Unban User:**", reply_markup=InlineKeyboardMarkup(buttons), parse_mode='Markdown')
                except error.BadRequest: pass
            return
            
        if data == "admin_users":
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT user_id, first_name FROM users ORDER BY joined_at DESC LIMIT 10")
            users = cur.fetchall()
            conn.close()
            msg = "📜 **Recent Users:**\n"
            for u in users: msg += f"• {u[1]} (`{u[0]}`)\n"
            try: await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_home")]]), parse_mode='Markdown')
            except error.BadRequest: pass
            return

        if data.startswith("clear_user_"):
            target_id = int(data.split("_")[2])
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE users SET report_count = 0 WHERE user_id = %s", (target_id,))
            conn.commit()
            conn.close()
            try: await query.edit_message_text(f"✅ Reports cleared for `{target_id}`.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_reports")]]), parse_mode='Markdown')
            except error.BadRequest: pass
            return

        if data.startswith("ban_user_"):
            target_id = int(data.split("_")[2])
            conn = get_db_connection()
            cur = conn.cursor()
            ban_time = datetime.datetime.now() + datetime.timedelta(hours=24)
            cur.execute("UPDATE users SET banned_until = %s WHERE user_id = %s", (ban_time, target_id))
            conn.commit()
            conn.close()
            try: await query.edit_message_text(f"🔨 Banned {target_id} for 24h.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_reports")]]))
            except error.BadRequest: pass
            try: await context.bot.send_message(target_id, "🚫 You have been banned for 24 hours.")
            except: pass
            return

        if data.startswith("unban_user_"):
            target_id = int(data.split("_")[2])
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE users SET banned_until = NULL WHERE user_id = %s", (target_id,))
            conn.commit()
            conn.close()
            try: await query.edit_message_text(f"✅ Unbanned {target_id}.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_banlist")]]))
            except error.BadRequest: pass
            return

    # MAIN MENU
    if data == "main_menu":
        await show_main_menu(update)

    elif data == "action_search" or data == "force_random":
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET status = 'searching' WHERE user_id = %s", (user_id,))
        conn.commit()
        conn.close()
        # JobQueue enabled for Cloud!
        if data == "action_search":
            context.job_queue.run_once(send_reroll_option, 15, data=user_id)
        await perform_match(update, context, user_id)

    elif data == "stop_search":
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET status = 'idle' WHERE user_id = %s", (user_id,))
        conn.commit()
        conn.close()
        await show_main_menu(update)
        
    elif data == "action_profile":
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT language, interests, karma_score FROM users WHERE user_id = %s", (user_id,))
        data = cur.fetchone()
        conn.close()
        text = f"👤 **IDENTITY CARD**\n━━━━━━━━━━━━━━━━\n🗣️ **Lang:** {data[0]}\n🏷️ **Tags:** {data[1]}\n🛡️ **Trust Score:** {data[2]}%\n━━━━━━━━━━━━━━━━"
        try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]), parse_mode='Markdown')
        except error.BadRequest: pass

    elif data == "set_interest_hub":
        context.user_data["state"] = "ONBOARDING_INTEREST"
        try: await query.edit_message_text("👇 **Type interests (comma separated):**", parse_mode='Markdown')
        except error.BadRequest: pass
        
    elif data.startswith("rate_"):
        action, target_id = data.split("_")[1], int(data.split("_")[2])
        
        if action == "report":
            await handle_report(update, context, user_id, target_id)
            k = [[InlineKeyboardButton("👎 Dislike & Block", callback_data=f"rate_dislike_{target_id}")]]
            try: await query.edit_message_text("⚠️ Report Sent.", reply_markup=InlineKeyboardMarkup(k))
            except error.BadRequest: pass
            return

        score = 1 if action == "like" else -1
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO user_interactions (rater_id, target_id, score) VALUES (%s, %s, %s)", (user_id, target_id, score))
        cur.execute("UPDATE users SET karma_score = karma_score + %s WHERE user_id = %s", (10 if score == 1 else -10, target_id))
        conn.commit()
        conn.close()
        
        try: await query.edit_message_text("✅ Feedback Sent!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Find New Match", callback_data="action_search")]]))
        except error.BadRequest: pass

async def perform_match(update, context, user_id):
    partner_id, common = find_match(user_id)
    
    if partner_id:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET status='chatting', partner_id=%s WHERE user_id=%s", (partner_id, user_id))
        cur.execute("UPDATE users SET status='chatting', partner_id=%s WHERE user_id=%s", (user_id, partner_id))
        conn.commit()
        conn.close()
        
        common_str = ", ".join(common).title() if common else "Random"
        msg = f"⚡ **YOU ARE CONNECTED!**\n\n🔗 Common Interest: **{common_str}**\n⚠️ Tip: Say 'Hi' or send a meme to break the ice!"
        
        try:
            if update.callback_query: await update.callback_query.edit_message_text(msg, parse_mode='Markdown')
            else: await context.bot.send_message(user_id, msg, parse_mode='Markdown')
        except error.BadRequest: pass
        try: await context.bot.send_message(partner_id, msg, parse_mode='Markdown')
        except: pass
    else:
        keyboard = [[InlineKeyboardButton("❌ Stop Searching", callback_data="stop_search")]]
        try:
            if update.callback_query: await update.callback_query.edit_message_text("📡 **Scanning Frequencies...**\nLooking for matches...", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        except error.BadRequest: pass

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = update.effective_user.id
    text = update.message.text
    
    if text:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT partner_id FROM users WHERE user_id = %s", (user_id,))
        res = cur.fetchone()
        pid = res[0] if res else 0
        cur.execute("INSERT INTO chat_logs (sender_id, receiver_id, message) VALUES (%s, %s, %s)", (user_id, pid, text))
        conn.commit()
        conn.close()

    if text == "/stop":
        await stop_chat(update, context)
        return
    if text == "/admin":
        await admin_panel(update, context)
        return
    if text and text.startswith("/ban"):
        await admin_ban_command(update, context)
        return
    if text and text.startswith("/warn"):
        await admin_warn_command(update, context)
        return
    if text and text.startswith("/broadcast"):
        await admin_broadcast(update, context)
        return

    if context.user_data.get("state") == "ONBOARDING_INTEREST":
        if not text: return
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET interests = %s WHERE user_id = %s", (text, user_id))
        conn.commit()
        conn.close()
        context.user_data["state"] = None
        await update.message.reply_text("✅ Vibe Check Complete!")
        await show_main_menu(update)
        return

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT status, partner_id FROM users WHERE user_id = %s", (user_id,))
    data = cur.fetchone()
    conn.close()
    
    if data and data[0] == 'chatting' and data[1] != 0:
        try: await update.message.copy(chat_id=data[1])
        except: await stop_chat(update, context)

async def stop_chat(update, context):
    user_id = update.effective_user.id
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT partner_id FROM users WHERE user_id = %s", (user_id,))
    res = cur.fetchone()
    partner_id = res[0] if res else 0
    
    cur.execute("UPDATE users SET status='idle', partner_id=0 WHERE user_id IN (%s, %s)", (user_id, partner_id))
    conn.commit()
    conn.close()
    
    k = [
        [InlineKeyboardButton("👍 Cool", callback_data=f"rate_like_{partner_id if partner_id else 0}"),
         InlineKeyboardButton("👎 Lame", callback_data=f"rate_dislike_{partner_id if partner_id else 0}")],
        [InlineKeyboardButton("⚠️ Report User", callback_data=f"rate_report_{partner_id if partner_id else 0}")],
        [InlineKeyboardButton("🚀 Find New Match", callback_data="action_search"),
         InlineKeyboardButton("🎯 Change Interest", callback_data="set_interest_hub")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
    ]
    
    await update.message.reply_text("🔌 **Connection Severed.**\n\n📊 **Experience Report:**\nHow was your partner?", reply_markup=InlineKeyboardMarkup(k))
    if partner_id:
        try: await context.bot.send_message(partner_id, "🔌 **Connection Severed.**\n\n📊 **Experience Report:**\nHow was your partner?", reply_markup=InlineKeyboardMarkup(k))
        except: pass

async def handle_report(update, context, reporter_id, reported_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET report_count = report_count + 1 WHERE user_id = %s RETURNING report_count", (reported_id,))
    res = cur.fetchone()
    new_count = res[0]
    cur.execute("INSERT INTO reports (reporter_id, reported_id, reason) VALUES (%s, %s, 'Report')", (reporter_id, reported_id))
    conn.commit()
    
    if new_count >= 3:
        cur.execute("SELECT message FROM chat_logs WHERE sender_id = %s ORDER BY timestamp DESC LIMIT 5", (reported_id,))
        logs = [l[0] for l in cur.fetchall()]
        keyboard = [[InlineKeyboardButton(f"🔨 BAN {reported_id}", callback_data=f"ban_user_{reported_id}")]]
        msg = f"🚨 **REPORT ALERT (3+)**\nUser: `{reported_id}`\nReports: {new_count}\n\nMessages: {logs}"
        for admin in ADMIN_IDS:
            try: await context.bot.send_message(admin, msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            except: pass
    conn.close()

if __name__ == '__main__':
    init_db()
    # Start Heartbeat for Render
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Start Bot with JobQueue ENABLED (Production Mode)
    request = HTTPXRequest(connect_timeout=60, read_timeout=60)
    app = ApplicationBuilder().token(BOT_TOKEN).request(request).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("ban", admin_ban_command))
    app.add_handler(CommandHandler("warn", admin_warn_command))
    app.add_handler(CommandHandler("broadcast", admin_broadcast))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    print("🤖 CLOUD BOT LAUNCHED...")
    app.run_polling()
