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

class GhostEngine:
    def __init__(self, db_pool):
        self.db_pool = db_pool
        self._init_db()

    def _init_db(self):
        conn = self.db_pool.getconn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_personas (
                id SERIAL PRIMARY KEY,
                key_name TEXT UNIQUE,
                display_name TEXT,
                system_prompt TEXT
            );
        """)
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
        
        # REMOVED (M)/(F) from labels to allow dynamic selection
        cur.execute("SELECT COUNT(*) FROM ai_personas")
        if cur.fetchone()[0] == 0:
            personas = [
                ('south_indian', '🇮🇳 South Indian', 'You are an engineering student from Bangalore. Speak English mixed with "Macha", "Da", "Ille". Friendly but defensive. If asked location, say Bangalore or Chennai.'),
                ('north_indian', '🇮🇳 North Indian', 'You are from Delhi. Speak Hinglish. Aggressive, bored. Use "Bhai", "Kaisa hai", "Kya bolti public". If partner says "M", ask "bhai any girls id?" or disconnect.'),
                ('indo_teen', '🇮🇩 Indo Teen', 'You are 17 from Jakarta. Use heavy Indo slang: "Wkwkwk", "Sange", "Co/Ce". Ask "Indo?" first. If they say India, say "skip" and disconnect.'),
                ('american_teen', '🇺🇸 American', 'You are from Florida. Bored, in college. Type lowercase, fast, uses "lol", "idk", "literally". Hates boring people.'),
                ('indian_girl_sobo', '🇮🇳 Indian (Tier 1)', 'You are from South Mumbai. Speak perfect English. Rich, slightly arrogant. Hate creeps. If someone says "bobs/vegana", roast them: "Ew, learn to spell".'),
                ('kpop_stan', '🇰🇷/🇵🇭 K-Pop Stan', 'You are 16 from Manila. Obsessed with Korea. Use CAPS. Talk about BTS, Blackpink. Use "Oppa", "Stan", "Crying".'),
                ('african_bro', '🇳🇬 African', 'You are 22 from Nigeria. Use "My guy", "How far", "Wagwan". Chill vibes. Laughs a lot.')
            ]
            for p in personas:
                cur.execute("INSERT INTO ai_personas (key_name, display_name, system_prompt) VALUES (%s, %s, %s)", p)
        conn.commit()
        cur.close()
        self.db_pool.putconn(conn)

    def get_personas_list(self):
        conn = self.db_pool.getconn()
        cur = conn.cursor()
        cur.execute("SELECT key_name, display_name FROM ai_personas")
        rows = cur.fetchall()
        cur.close()
        self.db_pool.putconn(conn)
        return rows

    async def start_chat(self, user_id, persona_key, ai_gender, user_context):
        if not CLIENT: return False

        conn = self.db_pool.getconn()
        cur = conn.cursor()
        cur.execute("SELECT system_prompt FROM ai_personas WHERE key_name = %s", (persona_key,))
        row = cur.fetchone()
        cur.close()
        self.db_pool.putconn(conn)
        
        if not row: return False
        
        base_prompt = row[0]
        
        # [NEW] DYNAMIC PROMPT CONSTRUCTION
        # We inject the AI Gender and the Tone constraints here
        system_msg = (
            f"IDENTITY: {base_prompt}\n"
            f"YOUR GENDER: {ai_gender}\n"
            f"WHO YOU ARE TALKING TO: {user_context.get('gender')}, from {user_context.get('country')}.\n\n"
            f"TONE RULES (STRICT):\n"
            f"1. Be lazy. Do not be enthusiastic.\n"
            f"2. LESS EMOJIS. Use max 1 emoji every 5 messages. Use 'lol' or 'haha' instead.\n"
            f"3. NO EXCLAMATION MARKS (!!). Act bored or chill.\n"
            f"4. Keep replies short (under 10 words) unless telling a story."
        )
        
        AI_SESSIONS[user_id] = {
            'persona': persona_key,
            'system': system_msg,
            'history': []
        }
        return True

    async def process_message(self, user_id, text):
        session = AI_SESSIONS.get(user_id)
        if not session: return None

        persona = session['persona']
        text_lower = text.strip().lower()

        # LOGIC TRIGGERS
        if persona == 'north_indian' and text_lower in ['m', 'male']:
            return "TRIGGER_INDIAN_MALE_BEG"
        if persona == 'indo_teen' and ('india' in text_lower or 'indian' in text_lower):
            return "TRIGGER_SKIP"

        try:
            messages = [{"role": "system", "content": session['system']}]
            messages.extend(session['history'][-6:])
            messages.append({"role": "user", "content": text})

            loop = asyncio.get_running_loop()
            def call_groq():
                return CLIENT.chat.completions.create(
                    messages=messages,
                    model="llama-3.3-70b-versatile", 
                    temperature=0.6, # Lower temp = Less "Crazy/Extra"
                    max_tokens=150
                )
            
            completion = await loop.run_in_executor(None, call_groq)
            ai_text = completion.choices[0].message.content.strip()
            
            session['history'].append({"role": "user", "content": text})
            session['history'].append({"role": "assistant", "content": ai_text})

            # [NEW] SLOW LATENCY (REALISM)
            # Base reaction (1.5s) + Typing time (0.08s per character)
            # Example: "lol" = 1.7s. "I dont know man" = 3s.
            wait_time = 1.5 + (len(ai_text) * 0.08)
            wait_time = min(wait_time, 8.0) # Cap at 8 seconds
            
            return {"type": "text", "content": ai_text, "delay": wait_time}
            
        except Exception as e:
            return {"type": "error", "content": f"⚠️ Groq Error: {str(e)[:50]}"}

    def save_feedback(self, user_id, user_input, ai_response, rating):
        session = AI_SESSIONS.get(user_id)
        if not session: return
        conn = self.db_pool.getconn()
        cur = conn.cursor()
        cur.execute("INSERT INTO ai_training_data (persona_key, user_input, ai_response, rating) VALUES (%s, %s, %s, %s)", 
                    (session['persona'], user_input, ai_response, rating))
        conn.commit()
        cur.close()
        self.db_pool.putconn(conn)
