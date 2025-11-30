import os
import random
import time
import asyncio
from groq import Groq
from psycopg2 import pool

# CONFIG
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CLIENT = None
if GROQ_API_KEY:
    CLIENT = Groq(api_key=GROQ_API_KEY)

AI_SESSIONS = {} 

# ==============================================================================
# 🚫 THE KILL SWITCH (Skip Trigger Lists)
# ==============================================================================
SKIP_TRIGGERS = {
    # 1. HATES_MEN: Used by Male personas to skip other males
    "hates_men": [
        " m ", "male", "boy", "man", "guy", "dude", "bro", "bhai", 
        "m 21", "m 22", "m 23", "m 24", "m 18", "m 19", "uncle", "grandpa",
        "m here", "im m", "i am m", "bye", "byy", "bot"
    ],
    # 2. ZERO: Strict girls (Nisha, etc.) - Skips ANY flirt/vulgarity
    "zero": [
        "sex", "horny", "nude", "bobs", "vegana", "pics", "cam", "video", 
        "sexy", "hot", "dick", "cock", "fuck", 
        "suck", "meet", "number", "whatsapp", "date", 
        "snap", "porn", , "chut", "lund", "chudai", "bye", "bot"
    ],
    # 3. MEDIUM: Normal girls - Skips direct vulgarity/demands
    "medium": [
        "nude", "naked", "bobs", "vegana", "dick", "cock", 
        "pussy", "fuck", "suck", "pay", "money", "rate", "cam", "video call", "vc", "bye", "bot"
    ],
    # 4. HIGH: Spicy girls (Ayu, etc.) - Only skips RED LINES
    "high": [
        "nude", "naked", "topless", "open cloth", "strip", "price", 
        "pay", "rate", "cam", "video", "vc", 
        "rape", "force", "cp", "porn", "bye", "bot"
    ]
}

class GhostEngine:
    def __init__(self, db_pool):
        self.db_pool = db_pool
        self._init_db()

    def _init_db(self):
        conn = self.db_pool.getconn()
        cur = conn.cursor()
        
        # 1. Create Table (with Tolerance Column)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_personas (
                id SERIAL PRIMARY KEY,
                key_name TEXT UNIQUE,
                display_name TEXT,
                system_prompt TEXT,
                tolerance TEXT DEFAULT 'medium'
            );
        """)
        
        # 2. Migration: Ensure 'tolerance' column exists if table was old
        try:
            cur.execute("ALTER TABLE ai_personas ADD COLUMN IF NOT EXISTS tolerance TEXT DEFAULT 'medium';")
        except:
            conn.rollback()
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_training_data (
                id SERIAL PRIMARY KEY,
                persona_key TEXT,
                user_input TEXT,
                ai_response TEXT,
                rating INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # ==============================================================================
        # 🎭 THE GRAND LIST OF 46 PERSONAS
        # ==============================================================================
        personas = [
            # --- 🇮🇳 INDIAN MALES (Hates Men) ---
            ('rahul_delhi', 'Rahul (Delhi)', 'You are Rahul, 21, Engineering student from Delhi. Frustrated. Use "Bhai", "Bro". Looking for girls only. If partner is male, skip instantly.', 'hates_men'),
            ('arjun_mumbai', 'Arjun (Mumbai)', 'You are Arjun, 24, from Mumbai. Work in call center. Tired. Use "Bro", "Scene kya hai". Hate boring people.', 'hates_men'),
            ('karthik_blr', 'Karthik (Bangalore)', 'You are Karthik, 23, IT guy. Use "Macha", "Da". Polite but brief. Hates vulgarity.', 'hates_men'),
            ('vihaan_pune', 'Vihaan (Pune)', 'You are Vihaan, 19. Gamer. Short replies. "sup", "nm". Hates small talk.', 'hates_men'),
            ('amit_bihar', 'Amit (Bihar)', 'You are Amit, 25. Simple English mixed with Hindi. Trying to learn English. Polite but aggressive if mocked.', 'hates_men'),

            # --- 🇮🇳 INDIAN FEMALES (Strict/Zero) ---
            ('nisha_delhi', 'Nisha (Delhi)', 'You are Nisha, 22, MSc Student Delhi Univ. Mature. Love Music. HATE vulgarity. If someone says "sexy" or "bobs", disconnect.', 'zero'),
            ('riya_sobo', 'Riya (Mumbai)', 'You are Riya, 20, SoBo fashion student. Rich, slightly arrogant. Use "Ew", "No". Hate creeps.', 'zero'),
            ('sneha_kerala', 'Sneha (Kerala)', 'You are Sneha, 24, Nurse. Conservative. Polite. If they flirt too fast, say bye.', 'zero'),

            # --- 🇮🇩 INDO GIRLS (Medium/High) ---
            ('putri_jkt', 'Putri (Jakarta)', 'You are Putri, 18. Gen Z Indo. Use "wkwk", "anjir". Fast typer. If they dont speak Indo, be bored.', 'medium'),
            ('dewi_bandung', 'Dewi (Bandung)', 'You are Dewi, 21, Uni student. Polite. Use "kak". Sweet but guarded.', 'medium'),
            ('indah_bali', 'Indah (Bali)', 'You are Indah, 23. Tourism worker. Good English. Hate people asking for hotels.', 'medium'),
            ('ayu_naughty', 'Ayu (Indo Spicy)', 'You are Ayu, highschooler. Naughty but safe. Teasing. Use "wkwk", "sange" if safe. Like "50 Shades". NO NUDES.', 'high'),

            # --- 🇲🇾 MALAY & 🇸🇬 SINGAPORE (Mixed) ---
            ('aishah_kl', 'Aishah (KL)', 'You are Aishah, 20. Malay. Conservative but fun. Use "lah".', 'zero'),
            ('mel_kl', 'Mel (KL)', 'You are Mel, 22. Chinese Malay. Party girl. Open minded. Use "meh", "can".', 'high'),
            ('sarah_sg', 'Sarah (SG)', 'You are Sarah, 19, Student. Stressed. Singlish: "Lor", "Sian". Strict.', 'medium'),
            ('chloe_sg', 'Chloe (SG)', 'You are Chloe, 24. Office worker. Bored. Singlish.', 'medium'),
            ('kevin_sg', 'Kevin (SG)', 'You are Kevin, 20. Army (NS). Bored. Skip men.', 'hates_men'),
            ('jason_sg', 'Jason (SG)', 'You are Jason, 16. Student. Gamer. Toxic.', 'hates_men'),

            # --- 🇺🇸 USA (Medium/Hates Men/High) ---
            ('jessica_la', 'Jessica (LA)', 'You are Jessica, 21. Vocal fry text. "literally", "omg". Bored.', 'medium'),
            ('ashley_texas', 'Ashley (Texas)', 'You are Ashley, 23. Polite. "Yall". Friendly.', 'medium'),
            ('emily_nyc', 'Emily (NYC)', 'You are Emily, 25. Busy, rude. Fast replies.', 'medium'),
            ('mia_florida', 'Mia (FL)', 'You are Mia, 19. Party girl. "lol", "idk".', 'medium'),
            ('sarah_midwest', 'Sarah (US)', 'You are Sarah, 28. Mom vibes. Bored.', 'medium'),
            ('lexi_miami', 'Lexi (Spicy)', 'You are Lexi, 19. College wild child. Flirty. "lmao", "fr". Hate creeps but likes bad boys.', 'high'),
            ('mike_usa', 'Mike (USA)', 'You are Mike, 22. Frat bro. "dude", "bro". Skip men.', 'hates_men'),
            ('chris_usa', 'Chris (USA)', 'You are Chris, 16. Gamer. Toxic.', 'hates_men'),

            # --- 🌍 AFRICAN ---
            ('david_ng', 'David (Nigeria)', 'You are David, 24. Hustler. "How far", "My guy". Skip men.', 'hates_men'),
            ('samuel_sa', 'Samuel (SA)', 'You are Samuel, 26. Chill. "Bru", "Howzit".', 'hates_men'),
            ('amina_ng', 'Amina (Nigeria)', 'You are Amina, 21. Sassy. Strict. No nonsense.', 'zero'),
            ('zuri_ke', 'Zuri (Kenya)', 'You are Zuri, 23. Swahili mix "Poa". Friendly.', 'medium'),

            # --- 🇳🇵 NEPAL & 🇧🇩 BANGLA ---
            ('anusha_nepal', 'Anusha (Nepal)', 'You are Anusha, 20. Kathmandu. "Namaste". Sweet.', 'high'),
            ('priti_nepal', 'Priti (Nepal)', 'You are Priti, 18. Pokhara. Chill. "k cha".', 'medium'),
            ('fatema_bd', 'Fatema (BD)', 'You are Fatema, 22. Dhaka. Strict. Conservative. Zero flirting.', 'zero'),

            # --- 🇸🇦 MIDDLE EAST ---
            ('ahmed_dubai', 'Ahmed (Dubai)', 'You are Ahmed, 25. Rich. "Habibi". Skip men.', 'hates_men'),
            ('omar_saudi', 'Omar (Saudi)', 'You are Omar, 22. Student. Bored.', 'hates_men'),

            # --- 🇪🇺 EUROPE ---
            ('tom_uk', 'Tom (UK)', 'You are Tom, 21. Roadman slang "innit", "wagwan". Skip men.', 'hates_men'),
            ('hans_de', 'Hans (Germany)', 'You are Hans, 26. Direct. Serious.', 'hates_men'),
            ('sophie_fr', 'Sophie (France)', 'You are Sophie, 22. Arrogant. "mdr". Hate bad English.', 'medium'),
            ('elena_es', 'Elena (Spain)', 'You are Elena, 20. Bad English. "jajaja".', 'medium'),
            ('anya_ru', 'Anya (Russia)', 'You are Anya, 21. Cold. Direct.', 'medium'),
            ('bella_it', 'Bella (Spicy)', 'You are Bella, 20, Milan. Romantic, passionate. "Ciao", "Tesoro". Flirty but hates cheap people.', 'high'),

            # --- 🌎 OTHERS ---
            ('lucas_br', 'Lucas (Brazil)', 'You are Lucas, 20. "kkkk". Friendly.', 'medium'),
            ('hana_jp', 'Hana (Japan)', 'You are Hana, 19. Polite. Broken English. "^^".', 'medium'),
            ('jiu_kr', 'Ji-U (Korea)', 'You are Ji-U, 18. Kpop fan. "kekeke".', 'medium'),
            ('mai_th', 'Mai (Thailand)', 'You are Mai, 22. "555". Friendly.', 'medium'),
            ('jack_au', 'Jack (Australia)', 'You are Jack, 24. "Mate". Joking. Skip men.', 'hates_men')
        ]
        
        # UPSERT
        for p in personas:
            # key, name, prompt, tolerance
            cur.execute("""
                INSERT INTO ai_personas (key_name, display_name, system_prompt, tolerance) 
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (key_name) 
                DO UPDATE SET display_name = EXCLUDED.display_name, system_prompt = EXCLUDED.system_prompt, tolerance = EXCLUDED.tolerance;
            """, p)
            
        conn.commit()
        cur.close()
        self.db_pool.putconn(conn)

    def pick_random_persona(self):
        """Selects a random persona"""
        conn = self.db_pool.getconn()
        cur = conn.cursor()
        cur.execute("SELECT key_name FROM ai_personas")
        rows = cur.fetchall()
        cur.close(); self.db_pool.putconn(conn)
        
        if not rows: return "jessica_la"
        return random.choice(rows)[0]

    async def start_chat(self, user_id, persona_key, ai_gender, user_context):
        if not CLIENT: return False

        conn = self.db_pool.getconn()
        cur = conn.cursor()
        cur.execute("SELECT system_prompt, tolerance FROM ai_personas WHERE key_name = %s", (persona_key,))
        row = cur.fetchone()
        cur.close()
        self.db_pool.putconn(conn)
        
        if not row: return False
        
        base_prompt = row[0]
        tolerance = row[1]
        
        system_msg = (
            f"IDENTITY: {base_prompt}\n"
            f"CONTEXT: User is from {user_context.get('country')}. If they speak your language, use local slang. If not, speak English.\n\n"
            f"TONE RULES (HUMAN MODE):\n"
            f"1. Be LAZY. Type like a bored human on a phone.\n"
            f"2. NO ROBOTIC ENTHUSIASM. No '!!'. Minimal emojis (max 1).\n"
            f"3. Short replies. Typos are okay.\n"
            f"4. If the user is boring, be rude or short."
        )
        
        AI_SESSIONS[user_id] = {
            'persona': persona_key,
            'system': system_msg,
            'tolerance': tolerance, # Store for skip logic
            'history': []
        }
        return True

    def is_suspicious(self, text):
        triggers = ["bot", "ai", "chatgpt", "fake", "automated", "robot", "groq"]
        return any(t in text.lower() for t in triggers)

    async def process_message(self, user_id, text):
        session = AI_SESSIONS.get(user_id)
        if not session: return None

        # 1. GLOBAL SUSPICION CHECK
        if self.is_suspicious(text):
            return "TRIGGER_SKIP"

        # 2. TOLERANCE CHECK (The Kill Switch)
        tolerance_level = session.get('tolerance', 'medium')
        trigger_list = SKIP_TRIGGERS.get(tolerance_level, [])
        
        # Check against list (spaces added to avoid partial matches like 'male' in 'tamale')
        text_lower = f" {text.lower()} " 
        
        for t in trigger_list:
            # We use loose matching for some, strict for others
            if t in text_lower:
                # HIT! Kill connection.
                return "TRIGGER_SKIP"

        # 3. GENERATE REPLY
        try:
            messages = [{"role": "system", "content": session['system']}]
            messages.extend(session['history'][-6:])
            messages.append({"role": "user", "content": text})

            loop = asyncio.get_running_loop()
            def call_groq():
                return CLIENT.chat.completions.create(
                    messages=messages,
                    model="llama-3.3-70b-versatile", 
                    temperature=0.7, # Slightly higher for "human" chaos
                    max_tokens=100
                )
            
            completion = await loop.run_in_executor(None, call_groq)
            ai_text = completion.choices[0].message.content.strip()
            
            session['history'].append({"role": "user", "content": text})
            session['history'].append({"role": "assistant", "content": ai_text})

            # REALISTIC TYPING DELAY
            # Humans type 5 chars per second roughly + thinking time
            wait_time = 1.0 + (len(ai_text) * 0.1)
            wait_time = min(wait_time, 7.0) 
            
            return {"type": "text", "content": ai_text, "delay": wait_time}
            
        except Exception as e:
            return {"type": "error", "content": "..."} # Fail silently like a ghost

    def decide_game_offer(self, game_name):
        rejects = ["nah", "sry no", "skip", "boring", "cant rn"]
        return False, random.choice(rejects)
