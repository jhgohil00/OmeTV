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
        
        # DEFINED PERSONAS (The ones you wanted)
        personas = [
            ('south_indian', '🇮🇳 South Indian', 'You are an engineering student from Bangalore. Speak English mixed with "Macha", "Da", "Ille". Friendly but defensive. If asked location, say Bangalore or Chennai.'),
            ('north_indian', '🇮🇳 North Indian', 'You are from Delhi. Speak Hinglish. Aggressive, bored. Use "Bhai", "Kaisa hai", "Kya bolti public". If partner says "M", ask "bhai any girls id?" or disconnect.'),
            ('indo_teen', '🇮🇩 Indo Teen', 'You are 17 from Jakarta. Use heavy Indo slang: "Wkwkwk", "Sange", "Co/Ce". Ask "Indo?" first. If they say India, say "skip" and disconnect.'),
            ('american_teen', '🇺🇸 American', 'You are from Florida. Bored, in college. Type lowercase, fast, uses "lol", "idk", "literally". Hates boring people.'),
            ('indian_girl_sobo', '🇮🇳 Indian (Tier 1)', 'You are from South Mumbai. Speak perfect English. Rich, slightly arrogant. Hate creeps. If someone says "bobs/vegana", roast them: "Ew, learn to spell".'),
            ('kpop_stan', '🇰🇷/🇵🇭 K-Pop Stan', 'You are 16 from Manila. Obsessed with Korea. Use CAPS. Talk about BTS, Blackpink. Use "Oppa", "Stan", "Crying".'),
            ('african_bro', '🇳🇬 African', 'You are 22 from Nigeria. Use "My guy", "How far", "Wagwan". Chill vibes. Laughs a lot.')
        ]
        
        # UPSERT (Insert or Update if exists) to ensure your DB has them
        for p in personas:
            cur.execute("""
                INSERT INTO ai_personas (key_name, display_name, system_prompt) 
                VALUES (%s, %s, %s)
                ON CONFLICT (key_name) 
                DO UPDATE SET display_name = EXCLUDED.display_name, system_prompt = EXCLUDED.system_prompt;
            """, p)
            
        conn.commit()
        cur.close()
        self.db_pool.putconn(conn)

    # [FIXED] THIS WAS MISSING
    def pick_random_persona(self):
        """Selects a random persona for the 15s fallback"""
        conn = self.db_pool.getconn()
        cur = conn.cursor()
        cur.execute("SELECT key_name FROM ai_personas")
        rows = cur.fetchall()
        cur.close(); self.db_pool.putconn(conn)
        
        if not rows: return "american_teen" # Fallback
        return random.choice(rows)[0]

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
        
        # DYNAMIC PROMPT CONSTRUCTION
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

    def is_suspicious(self, text):
        """Checks if user knows it's a bot"""
        triggers = ["bot", "ai", "chatgpt", "fake", "automated", "robot", "groq", "gemini", "program"]
        return any(t in text.lower() for t in triggers)

    async def process_message(self, user_id, text):
        session = AI_SESSIONS.get(user_id)
        if not session: return None

        # 1. SURVIVAL CHECK
        if self.is_suspicious(text):
            return "TRIGGER_SKIP"

        # 2. LOGIC TRIGGERS
        persona = session['persona']
        text_lower = text.lower()
        
        if persona == 'north_indian' and text_lower in ['m', 'male']:
            return "TRIGGER_INDIAN_MALE_BEG"
        if persona == 'indo_teen' and 'india' in text_lower:
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
                    temperature=0.6,
                    max_tokens=150
                )
            
            completion = await loop.run_in_executor(None, call_groq)
            ai_text = completion.choices[0].message.content.strip()
            
            session['history'].append({"role": "user", "content": text})
            session['history'].append({"role": "assistant", "content": ai_text})

            # SLOW LATENCY
            wait_time = 1.5 + (len(ai_text) * 0.08)
            wait_time = min(wait_time, 8.0) 
            
            return {"type": "text", "content": ai_text, "delay": wait_time}
            
        except Exception as e:
            return {"type": "error", "content": f"Groq Error: {str(e)[:50]}"}

    def decide_game_offer(self, game_name):
        """20% Chance to Reject, 80% Accept"""
        if random.random() < 0.20:
            # Rejection Responses
            rejects = ["nah im lazy", "maybe later", "not in the mood", "meh no"]
            return False, random.choice(rejects)
        
        # Acceptance Responses
        accepts = ["sure lol", "okay lets play", "bet", "game on", "alright you start"]
        return True, random.choice(accepts)

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
