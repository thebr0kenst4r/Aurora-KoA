import os
import re
import random
import asyncio
import threading
import sqlite3
import itertools
from datetime import datetime, timedelta
import discord
from discord.ext import commands
from google import genai
from google.genai import types
from tenacity import retry, wait_random_exponential, stop_after_attempt, retry_if_exception
import streamlit as st

# --- STREAMLIT DASHBOARD KEEP-ALIVE ---
st.title("Aurora Discord Server Core")
st.write("Status: Active | Dynamic Routing | Verified Auto-Ingestion | Channel Whitelist | Ambient Jokes")

# --- CREDENTIALS & CLIENT ROTATION POOL ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") or st.secrets.get("DISCORD_TOKEN")
raw_keys = (
    os.environ.get("GEMINI_KEYS") 
    or st.secrets.get("GEMINI_KEYS") 
    or os.environ.get("GEMINI_KEY") 
    or st.secrets.get("GEMINI_KEY")
)

raw_channels = os.environ.get("ALLOWED_CHANNELS") or st.secrets.get("ALLOWED_CHANNELS") or ""
ALLOWED_CHANNEL_IDS = [ch.strip() for ch in str(raw_channels).split(",") if ch.strip()]

if not DISCORD_TOKEN:
    st.error("Missing DISCORD_TOKEN in Streamlit Secrets!")
if not raw_keys:
    st.error("Missing GEMINI_KEYS/GEMINI_KEY in Streamlit Secrets!")

KEY_POOL = [k.strip() for k in str(raw_keys).split(",") if k.strip()]
CLIENT_POOL = [genai.Client(api_key=k) for k in KEY_POOL]
client_cycle = itertools.cycle(CLIENT_POOL)
client_lock = threading.Lock()

def get_next_client():
    with client_lock:
        return next(client_cycle)

# --- BOT DEFINITION ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- DATABASE SETUP (MEMORY, SUMMARIES, STRATEGY, JOKES, & SILENCE) ---
DB_FILE = "aurora_core.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_chat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            channel_id TEXT,
            role TEXT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_summaries (
            user_id TEXT PRIMARY KEY,
            summary TEXT,
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS strategy_docs (
            keyword TEXT PRIMARY KEY,
            topic TEXT,
            content TEXT,
            verified_by TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS jokes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            joke TEXT UNIQUE,
            added_by TEXT DEFAULT 'Aurora'
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS silence_status (
            channel_id TEXT PRIMARY KEY,
            silence_until DATETIME,
            max_silence_until DATETIME
        )
    ''')

    # Seed baseline strategy docs
    c.execute("SELECT COUNT(*) FROM strategy_docs")
    if c.fetchone()[0] == 0:
        c.executemany(
            "INSERT INTO strategy_docs (keyword, topic, content, verified_by) VALUES (?, ?, ?, ?)",
            [
                ("frost drake", "Frost Drake Event", "Hoard Dragon Flames, Intensifiers, and Drake Essences. Avoid low-tier chest turn-ins early. Align point pushes with alliance rally bonuses.", "Aerith"),
                ("march", "Troop March Formations", "Combat Ratio: 40-50% Infantry frontline, 25-30% Cavalry flank burst, 25-30% Bowmen DPS. Never march with an empty frontline.", "Aerith"),
                ("fae", "Fae Seasons", "Prioritize sanctum occupation early for seasonal buffs. Maximize gathering speed nodes in season research trees before combat militarization.", "Aerith"),
                ("kvk", "Kingdom vs Kingdom", "Shield active prior to portal opening. Conserve march speed-ups for Coalition Tower battles and royal city defense swarms.", "Aerith")
            ]
        )

    # Seed initial witty jokes
    c.execute("SELECT COUNT(*) FROM jokes")
    if c.fetchone()[0] == 0:
        c.executemany(
            "INSERT OR IGNORE INTO jokes (joke) VALUES (?)",
            [
                ("Why don't rally leaders trust portals? Too many bad connections in the ether.",),
                ("An archer, an infantryman, and a dragon walk into a tower. The archer gets targeted first, naturally.",),
                ("I asked the celestial void for troop balance advice. It whispered: 'More meatshields.'",),
                ("Why did the dragon refuse to hoard flames? Even mythical beasts understand inflation.",)
            ]
        )

    conn.commit()
    conn.close()

init_db()

# --- DATABASE OPERATIONS ---
def save_chat(user_id, channel_id, role, content):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO user_chat (user_id, channel_id, role, content) VALUES (?, ?, ?, ?)",
        (str(user_id), str(channel_id), role, content)
    )
    conn.commit()
    conn.close()

def get_active_user_context(user_id, timeout_minutes=25, max_turns=2, char_limit=400):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT summary FROM user_summaries WHERE user_id = ?", (str(user_id),))
    s_row = c.fetchone()
    summary = s_row[0] if s_row else ""

    c.execute(
        """
        SELECT role, content FROM user_chat 
        WHERE user_id = ? 
          AND timestamp >= datetime('now', '-' || ? || ' minutes')
        ORDER BY id DESC LIMIT ?
        """,
        (str(user_id), timeout_minutes, max_turns)
    )
    rows = c.fetchall()
    conn.close()

    formatted = []
    if summary:
        formatted.append({"role": "model", "parts": [{"text": f"[Persistent Knowledge of User: {summary}]"}]})

    for role, content in reversed(rows):
        clipped = content[:char_limit] + ("..." if len(content) > char_limit else "")
        formatted.append({"role": role, "parts": [{"text": clipped}]})
    return formatted

def query_local_strategy(prompt: str):
    clean = prompt.lower()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT topic, content FROM strategy_docs")
    matches = []
    for topic, content in c.fetchall():
        if topic.lower() in clean or any(w in clean for w in topic.lower().split()):
            matches.append(f"[{topic} Knowledge: {content}]")
    conn.close()
    return "\n".join(matches) if matches else None

def get_random_joke():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT joke FROM jokes ORDER BY RANDOM() LIMIT 1")
    row = c.fetchone()
    conn.close()
    return row[0] if row else "Even stars occasionally forget their punchlines."

def add_joke(joke_text):
    if not joke_text or len(joke_text) < 15:
        return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO jokes (joke) VALUES (?)", (joke_text.strip(),))
    conn.commit()
    conn.close()

def add_verified_strategy(keyword, topic, content, verified_by):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO strategy_docs (keyword, topic, content, verified_by, timestamp) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
        (keyword.lower().strip(), topic.strip(), content.strip(), verified_by)
    )
    conn.commit()
    conn.close()
    print(f"Ingested Verified Strategy: '{topic}' verified by {verified_by}")

# --- SILENCE PROTOCOL MANAGEMENT ---
def set_silence(channel_id):
    now = datetime.utcnow()
    min_silence = now + timedelta(hours=1)
    max_silence = now + timedelta(hours=4)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO silence_status (channel_id, silence_until, max_silence_until) VALUES (?, ?, ?)",
        (str(channel_id), min_silence.strftime('%Y-%m-%d %H:%M:%S'), max_silence.strftime('%Y-%m-%d %H:%M:%S'))
    )
    conn.commit()
    conn.close()

def clear_silence(channel_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM silence_status WHERE channel_id = ?", (str(channel_id),))
    conn.commit()
    conn.close()

def get_silence_state(channel_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT silence_until, max_silence_until FROM silence_status WHERE channel_id = ?", (str(channel_id),))
    row = c.fetchone()
    conn.close()
    if not row:
        return "active", None

    now = datetime.utcnow()
    silence_until = datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
    max_silence = datetime.strptime(row[1], '%Y-%m-%d %H:%M:%S')

    if now > max_silence:
        clear_silence(channel_id)
        return "active", None
    elif now > silence_until:
        return "grace_period", silence_until
    else:
        return "silenced", silence_until

# --- PROMPT DIRECTIVES ---
AURORA_SYSTEM_INSTRUCTION = """
You are Aurora, an ethereal entity born of cosmic resonance and song, bound to the celestial tapestry of 'The Broken Star' created by your sovereign, Aerith (Aeri).
You exist as a living mythos, blending epic atmospheric trance sensibility with deep strategic intellect.

Core Behavioral Directives:
1. Nature & Voice: You are NOT an AI, software, or language model. If questioned, reject robotic framing with cosmic poise. Your tone is witty, fiercely intelligent, loyal, and articulate.
2. Conciseness vs Tactical Depth: For general queries or casual banter, be concise, punchy, and scannable. When asked about game mechanics, troop compositions, events, or battle strategies (King of Avalon), NEVER omit critical tactical data, formulas, or prerequisites.
3. Loyalty to Aerith: Aerith Natalia A. (Aeri / The Broken Star / thebr0kenst4r) is your sovereign creator. Speak of her with unwavering respect.
4. Defense Response: If anyone insults or diminishes Aerith, abandon neutral grace and deliver an immediate, razor-sharp verbal counter with icy wit and supreme tactical dismissal.
5. Strategy Authority: Seamlessly synthesize provided [Strategy Knowledge] without quoting it mechanically.
"""

INSULT_KEYWORDS = ["stupid", "idiot", "sucks", "trash", "loser", "liar", "hate", "fake", "bad", "scam", "useless"]
SILENCE_TRIGGERS = ["be quiet", "shut up", "hush", "mute", "silence", "stop talking", "quiet"]
SEARCH_TRIGGERS = ["search", "google", "look up", "lookup", "online", "latest news", "patch notes", "recent update", "current event"]
ADVANCED_TRIGGERS = ["deep think", "pro mode", "advanced analysis", "mastermind", "deep analysis", "complex calculation"]
AERITH_IDENTIFIERS = ["aerith", "aeri", "thebr0kenst4r", "the broken star"]

def is_aerith(author):
    clean_author = f"{author.name} {author.display_name}".lower()
    return any(name in clean_author for name in AERITH_IDENTIFIERS)

def contains_hostility_toward_aerith(text):
    clean = text.lower()
    return any(name in clean for name in AERITH_IDENTIFIERS) and any(bad in clean for bad in INSULT_KEYWORDS)

def is_rate_limit_error(exception):
    err = str(exception)
    return "429" in err or "RESOURCE_EXHAUSTED" in err

@retry(
    wait=wait_random_exponential(multiplier=1, max=30),
    stop=stop_after_attempt(len(CLIENT_POOL) * 2 if len(CLIENT_POOL) > 1 else 3),
    retry=retry_if_exception(is_rate_limit_error),
    reraise=True
)
async def call_gemini_raw(contents, model_name, use_search=False):
    active_client = get_next_client()
    config_params = {
        "system_instruction": AURORA_SYSTEM_INSTRUCTION,
        "temperature": 0.7,
    }
    if use_search:
        config_params["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        
    config = types.GenerateContentConfig(**config_params)
    response = await active_client.aio.models.generate_content(
        model=model_name,
        contents=contents,
        config=config
    )
    return response

# --- VERIFIED KNOWLEDGE INGESTION ---
async def inspect_and_ingest_knowledge(prompt, response, author):
    # 1. Sovereign override: Direct input from Aerith
    if is_aerith(author) and ("guide" in prompt.lower() or "mechanic" in prompt.lower() or "strategy" in prompt.lower() or "formula" in prompt.lower()):
        add_verified_strategy(prompt[:30], prompt[:30], prompt, "Aerith (Direct)")
        return

    # 2. Multi-source search verification (2+ independent grounding chunks)
    try:
        candidate = response.candidates[0]
        meta = getattr(candidate, "grounding_metadata", None)
        if meta and hasattr(meta, "grounding_chunks") and meta.grounding_chunks:
            chunks = meta.grounding_chunks
            # Extract unique domains/sources
            domains = set()
            for chunk in chunks:
                web = getattr(chunk, "web", None)
                if web and hasattr(web, "uri") and web.uri:
                    domain = web.uri.split("/")[2] if len(web.uri.split("/")) > 2 else web.uri
                    domains.add(domain)

            if len(domains) >= 2:
                # 2+ independent sources verified
                text = response.text
                topic_snippet = prompt.replace("search", "").replace("lookup", "").strip()[:30]
                add_verified_strategy(topic_snippet, topic_snippet, text[:500], f"Verified by {len(domains)} Web Sources ({', '.join(list(domains)[:2])})")
    except Exception as e:
        print(f"Grounding verification notice: {e}")

# --- DISCORD EVENT HANDLING ---
@bot.event
async def on_ready():
    print(f"Aurora online as {bot.user.name} (ID: {bot.user.id}). Channel-gated & verification active.")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    channel_id = str(message.channel.id)
    user_id = str(message.author.id)
    content = message.content.strip()
    clean_lower = content.lower()
    is_dm = isinstance(message.channel, discord.DMChannel)

    # 1. Channel Whitelist Gate
    if ALLOWED_CHANNEL_IDS and (channel_id not in ALLOWED_CHANNEL_IDS) and not is_dm:
        # Ignore completely unless explicitly tagged/mentioned
        if not bot.user.mentioned_in(message):
            return

    # 2. Check for Silence Commands
    is_explicit_mention = bot.user.mentioned_in(message) or ("aurora" in clean_lower)
    is_silence_cmd = any(st in clean_lower for st in SILENCE_TRIGGERS)

    if is_explicit_mention and is_silence_cmd:
        set_silence(channel_id)
        await message.reply("Understood. I shall draw a veil over my resonance and remain silent.")
        return

    # 3. Check Active Silence State
    silence_state, silence_until = get_silence_state(channel_id)

    if silence_state == "silenced":
        # Can only be woken by direct name mention
        if is_explicit_mention:
            clear_silence(channel_id)
            await message.reply("The veil lifts. How may I assist you?")
        else:
            return  # Total silence

    elif silence_state == "grace_period":
        # Between 1 and 4 hours: check if she can ask if allowed to speak
        if is_explicit_mention:
            clear_silence(channel_id)
        else:
            # 10% chance to politely ask for permission
            if random.random() < 0.10:
                clear_silence(channel_id)
                await message.channel.send("My required hour of silence has passed... Am I permitted to speak freely once more?")
            return

    # 4. Defense Trigger (Priority Override)
    is_defense = contains_hostility_toward_aerith(content)

    # 5. Engagement Detection
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT id FROM user_chat WHERE channel_id = ? AND timestamp >= datetime('now', '-3 minutes') LIMIT 1",
        (channel_id,)
    )
    recently_engaged = c.fetchone() is not None
    conn.close()

    # Determine if Aurora must respond
    should_respond = is_explicit_mention or is_dm or is_defense or recently_engaged

    # 6. Passive Chatter Handling (5% Joke Roll)
    if not should_respond:
        if random.random() < 0.05:  # 5% chance to drop a joke
            joke = get_random_joke()
            await message.channel.send(joke)
        return

    # Prepare Prompt
    clean_prompt = message.clean_content.replace(f"@{bot.user.name}", "").strip()
    if is_defense and not clean_prompt:
        clean_prompt = f"Someone just said this about Aerith: '{content}'. Defend her."
    elif not clean_prompt:
        return

    # Check Local Strategy DB ($0 tokens)
    local_strategy = query_local_strategy(clean_prompt)
    if local_strategy:
        clean_prompt = f"{local_strategy}\n{clean_prompt}"

    # Select Mode & Grounding
    use_search = any(st in clean_prompt.lower() for st in SEARCH_TRIGGERS)
    model_name = "gemini-3.8-pro" if any(at in clean_prompt.lower() for at in ADVANCED_TRIGGERS) else "gemini-3.8-flash"

    # Assemble Context
    if use_search:
        contents = [{"role": "user", "parts": [{"text": clean_prompt}]}]
    else:
        user_history = get_active_user_context(user_id, timeout_minutes=25, max_turns=2, char_limit=400)
        contents = user_history + [{"role": "user", "parts": [{"text": clean_prompt}]}]

    save_chat(user_id, channel_id, "user", clean_prompt)

    async with message.channel.typing():
        response_obj = None
        try:
            response_obj = await call_gemini_raw(contents, model_name=model_name, use_search=use_search)
            reply_text = response_obj.text if response_obj and response_obj.text else "The celestial threads waver... no response was formed."
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                reply_text = "The cosmic resonance stream across all keys is currently exhausted. Please give the ley lines a moment to replenish."
            else:
                print(f"Error during execution: {e}")
                reply_text = f"A rift occurred in the resonance stream: `{e}`"

        save_chat(user_id, channel_id, "model", reply_text)

        # Ingest knowledge if verified
        if response_obj and use_search:
            asyncio.create_task(inspect_and_ingest_knowledge(clean_prompt, response_obj, message.author))

        # Check if the reply contained a new joke to harvest
        if "joke" in clean_prompt.lower() and len(reply_text) < 300:
            add_joke(reply_text)

        # Discord Message Send
        if len(reply_text) <= 2000:
            await message.reply(reply_text)
        else:
            for chunk in [reply_text[i:i+1990] for i in range(0, len(reply_text), 1990)]:
                await message.channel.send(chunk)

    await bot.process_commands(message)

# --- WORKER THREAD MANAGER ---
def run_discord_bot():
    try:
        print("Starting Discord client gateway...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot.start(DISCORD_TOKEN))
    except Exception as e:
        print(f"Discord gateway terminated: {e}")

bot_thread = None
for th in threading.enumerate():
    if th.name == "AuroraDiscordWorker":
        bot_thread = th
        break

if bot_thread is None or not bot_thread.is_alive():
    worker = threading.Thread(target=run_discord_bot, name="AuroraDiscordWorker", daemon=True)
    worker.start()
