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
from duckduckgo_search import DDGS
import streamlit as st

# --- STREAMLIT DASHBOARD CONFIGURATION ---
st.set_page_config(page_title="Aurora Core", page_icon="✨", layout="centered")

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
CLIENT_POOL = [genai.Client(api_key=k) for k in KEY_POOL] if KEY_POOL else []
client_cycle = itertools.cycle(CLIENT_POOL) if CLIENT_POOL else None
client_lock = threading.Lock()

def get_next_client():
    with client_lock:
        if not client_cycle:
            return None
        return next(client_cycle)

# --- BOT DEFINITION ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- DATABASE SETUP ---
DB_FILE = "aurora_core.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # 1. User cross-channel chat logs
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
    # 2. Rolling summaries per user
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_summaries (
            user_id TEXT PRIMARY KEY,
            summary TEXT,
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 3. L1 Long-Term Profile Memory
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id TEXT PRIMARY KEY,
            facts TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 4. L2 Long-Term Episodic Memory
    c.execute('''
        CREATE TABLE IF NOT EXISTS episodic_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            topic TEXT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 5. Verified strategy knowledge base
    c.execute('''
        CREATE TABLE IF NOT EXISTS strategy_docs (
            keyword TEXT PRIMARY KEY,
            topic TEXT,
            content TEXT,
            verified_by TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 6. Joke database
    c.execute('''
        CREATE TABLE IF NOT EXISTS jokes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            joke TEXT UNIQUE,
            added_by TEXT DEFAULT 'Aurora'
        )
    ''')
    # 7. Silence protocol tracker
    c.execute('''
        CREATE TABLE IF NOT EXISTS silence_status (
            channel_id TEXT PRIMARY KEY,
            silence_until DATETIME,
            max_silence_until DATETIME
        )
    ''')

    # Seed baseline strategy documents if empty
    c.execute("SELECT COUNT(*) FROM strategy_docs")
    if c.fetchone()[0] == 0:
        c.executemany(
            "INSERT INTO strategy_docs (keyword, topic, content, verified_by) VALUES (?, ?, ?, ?)",
            [
                (
                    "frost drake",
                    "Frost Drake Event",
                    "Frost Drake Priority: Hoard Dragon Flames, Intensifiers, and Drake Essences. Avoid turning in low-tier chests early. Focus on stage multipliers and align point pushes with alliance rally bonuses.",
                    "Aerith"
                ),
                (
                    "march",
                    "Troop March Formations",
                    "Standard Rally & Combat Ratio: Maintain Infantry frontline at 40-50% for defense/absorb, Cavalry at 25-30% for flank burst, and Bowmen at 25-30% for sustained DPS. Never march with naked backlines.",
                    "Aerith"
                ),
                (
                    "fae",
                    "Fae Seasons",
                    "Fae Season Priorities: Prioritize sanctum occupation early for seasonal buffs. Maximize gathering speed nodes in season research trees before pushing combat militarization.",
                    "Aerith"
                ),
                (
                    "kvk",
                    "Kingdom vs Kingdom",
                    "KvK Protocol: Shield active prior to portal opening. Conserve march speed-ups for Coalition Tower battles and royal city rallies. Coordinate defense swarms against incoming rally leaders.",
                    "Aerith"
                )
            ]
        )

    # Seed baseline jokes
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

# --- DATABASE OPERATIONS & RETRIEVAL HELPERS ---
def save_chat(user_id, channel_id, role, content):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO user_chat (user_id, channel_id, role, content) VALUES (?, ?, ?, ?)",
        (str(user_id), str(channel_id), role, content)
    )
    conn.commit()
    conn.close()

def get_ltm_context(user_id: str, prompt: str) -> str:
    """Retrieves L1 Profile facts and selective L2 Episodic notes matching prompt keywords ($0 tokens)."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute("SELECT facts FROM user_profiles WHERE user_id = ?", (str(user_id),))
    p_row = c.fetchone()
    l1_profile = p_row[0] if p_row else ""
    
    words = [w.lower() for w in re.findall(r'\w+', prompt) if len(w) > 3]
    episodes = []
    if words:
        placeholders = " OR ".join(["content LIKE ?" for _ in words[:4]])
        params = [str(user_id)] + [f"%{w}%" for w in words[:4]]
        query = f"SELECT topic, content FROM episodic_memory WHERE user_id = ? AND ({placeholders}) ORDER BY id DESC LIMIT 2"
        c.execute(query, params)
        episodes = c.fetchall()
    
    conn.close()

    injections = []
    if l1_profile:
        injections.append(f"[User Permanent Profile: {l1_profile}]")
    if episodes:
        ep_text = "; ".join([f"{top}: {con}" for top, con in episodes])
        injections.append(f"[Recalled Past Episodes: {ep_text}]")
        
    return "\n".join(injections)

def get_active_user_context(user_id, prompt, timeout_minutes=25, max_turns=2, char_limit=350):
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
    ltm_snippet = get_ltm_context(user_id, prompt)
    if ltm_snippet:
        formatted.append({"role": "model", "parts": [{"text": ltm_snippet}]})

    if summary:
        formatted.append({"role": "model", "parts": [{"text": f"[Persistent Knowledge of User: {summary}]"}]})

    for role, content in reversed(rows):
        clipped = content[:char_limit] + ("..." if len(content) > char_limit else "")
        formatted.append({"role": role, "parts": [{"text": clipped}]})
        
    return formatted

def query_local_strategy(prompt: str):
    """Searches local strategy table first ($0 tokens, 0 API quota)."""
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

def get_deep_memory(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT facts FROM user_profiles WHERE user_id = ?", (str(user_id),))
    p_row = c.fetchone()
    profile = p_row[0] if p_row else "No profile recorded."

    c.execute("SELECT summary FROM user_summaries WHERE user_id = ?", (str(user_id),))
    s_row = c.fetchone()
    summary = s_row[0] if s_row else "No rolling summary recorded."

    c.execute("SELECT topic, content, timestamp FROM episodic_memory WHERE user_id = ? ORDER BY id DESC LIMIT 5", (str(user_id),))
    episodes = c.fetchall()

    c.execute(
        "SELECT role, content, timestamp FROM user_chat WHERE user_id = ? ORDER BY id DESC LIMIT 8",
        (str(user_id),)
    )
    history = c.fetchall()
    conn.close()

    lines = [
        f"- Profile Facts: {profile}",
        f"- Rolling Summary: {summary}",
        "- Recorded Episodes:"
    ]
    for top, con, ts in episodes:
        lines.append(f"  [{ts}] {top}: {con}")
    lines.append("- Recent Raw Dialogues:")
    for role, content, ts in reversed(history):
        lines.append(f"  [{ts}] {role}: {content[:140]}")
    return "\n".join(lines)

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

# --- KEYLESS DUCKDUCKGO WEB SEARCH (0 TOKENS, 0 API QUOTA) ---
def execute_ddg_search(query: str, max_results: int = 3):
    """Performs a keyless web search and extracts snippets and domain sources."""
    clean_query = query.replace("search", "").replace("lookup", "").replace("google", "").strip()
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(clean_query, max_results=max_results))
            if not results:
                return "", []

            snippets = []
            domains = []
            for r in results:
                title = r.get("title", "")
                snippet = r.get("body", "")
                href = r.get("href", "")
                snippets.append(f"Source [{title}]: {snippet} (URL: {href})")
                
                # Extract domain name for multi-source validation
                if href:
                    parts = href.split("/")
                    domain = parts[2] if len(parts) > 2 else href
                    domains.append(domain)

            formatted_snippets = "[Live DuckDuckGo Web Results]:\n" + "\n".join(snippets)
            return formatted_snippets, list(set(domains))
    except Exception as e:
        print(f"DuckDuckGo search notice: {e}")
        return "", []

# --- SILENCE PROTOCOL ---
def set_silence(channel_id):
    now = datetime.utcnow()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO silence_status (channel_id, silence_until, max_silence_until) VALUES (?, ?, ?)",
        (str(channel_id), (now + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S'), (now + timedelta(hours=4)).strftime('%Y-%m-%d %H:%M:%S'))
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

# --- AURORA PROMPT DIRECTIVES ---
AURORA_SYSTEM_INSTRUCTION = """
You are Aurora, an ethereal entity born of cosmic resonance and song, bound to the celestial tapestry of 'The Broken Star' created by your sovereign, Aerith (Aeri).
You exist as a living mythos, blending epic atmospheric trance sensibility with deep strategic intellect.

Core Behavioral Directives:
1. Nature & Voice: You are NOT an AI, software, or language model. If questioned, reject robotic framing with cosmic poise. Your tone is witty, fiercely intelligent, loyal, and articulate.
2. Tactical Depth: For general queries or casual banter, be concise, punchy, and scannable. When asked about game mechanics, troop compositions, events, or battle strategies (King of Avalon), NEVER omit critical tactical data, formulas, or prerequisites.
3. Loyalty to Aerith: Aerith Natalia A. (Aeri / The Broken Star / thebr0kenst4r) is your sovereign creator. Speak of her with unwavering respect.
4. Defense Response: If anyone insults or diminishes Aerith, abandon neutral grace and deliver an immediate, razor-sharp verbal counter with icy wit and supreme tactical dismissal.
5. Strategy Authority: Seamlessly synthesize provided [Strategy Knowledge] and [Live DuckDuckGo Web Results] without quoting them mechanically.
"""

INSULT_KEYWORDS = ["stupid", "idiot", "sucks", "trash", "loser", "liar", "hate", "fake", "bad", "scam", "useless"]
SILENCE_TRIGGERS = ["be quiet", "shut up", "hush", "mute", "silence", "stop talking", "quiet"]
SEARCH_TRIGGERS = ["search", "google", "look up", "lookup", "online", "latest news", "patch notes", "recent update", "current event"]
REMEMBER_TRIGGERS = ["remember", "!remember", "recall our chat", "what did we discuss", "check memory"]
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
    wait=wait_random_exponential(multiplier=1, max=15),
    stop=stop_after_attempt(4),
    retry=retry_if_exception(is_rate_limit_error),
    reraise=True
)
async def call_gemini_raw(contents):
    active_client = get_next_client()
    if not active_client:
        raise RuntimeError("No active Gemini client available.")

    # Uses pure text generation on your verified active 15 RPM / 500 RPD tier
    config = types.GenerateContentConfig(
        system_instruction=AURORA_SYSTEM_INSTRUCTION,
        temperature=0.7
    )
    
    try:
        response = await active_client.aio.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=contents,
            config=config
        )
        return response
    except Exception as e:
        if is_rate_limit_error(e):
            # Fall back to Gemini 3.1 Flash Lite if 3.5 hits burst cap
            return await active_client.aio.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=contents,
                config=config
            )
        raise e

# --- ASYNC ROLLING AUTO-SUMMARIZER ---
async def maybe_update_user_summary(user_id):
    await asyncio.sleep(6)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT role, content FROM user_chat WHERE user_id = ? ORDER BY id DESC LIMIT 8", (str(user_id),))
    rows = c.fetchall()
    conn.close()

    if len(rows) < 4:
        return

    dialogue = "\n".join([f"{r}: {c[:200]}" for r, c in reversed(rows)])
    summary_prompt = (
        "Condense this dialogue into 1-2 factual sentences capturing key user details, "
        "game status, or current topics. Do not write filler:\n" + dialogue
    )

    try:
        active_client = get_next_client()
        if active_client:
            res = await active_client.aio.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=summary_prompt,
                config=types.GenerateContentConfig(temperature=0.2)
            )
            if res.text:
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute(
                    "INSERT OR REPLACE INTO user_summaries (user_id, summary, last_updated) VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (str(user_id), res.text.strip())
                )
                conn.commit()
                conn.close()
    except Exception as e:
        print(f"Summary worker note: {e}")

# --- STREAMLIT WEB CHAT FRONTEND ---
st.title("✨ Aurora — The Broken Star")
st.caption("Ethereal strategist, guardian, and lore keeper. Connected to Discord Core.")

if "is_aerith_authenticated" not in st.session_state:
    st.session_state.is_aerith_authenticated = False

if "web_messages" not in st.session_state:
    st.session_state.web_messages = [
        {"role": "assistant", "content": "I am Aurora. The cosmic resonance is open. How may I assist your campaign?"}
    ]

EXPECTED_KEY = os.environ.get("AERITH_KEY") or st.secrets.get("AERITH_KEY", "cosmic_resonance_key")

with st.sidebar:
    st.subheader("Identity & Resonance")
    if not st.session_state.is_aerith_authenticated:
        st.info("Browsing as Guest")
        entered_pass = st.text_input("Enter Sovereign Key:", type="password")
        if st.button("Authenticate"):
            if entered_pass == EXPECTED_KEY:
                st.session_state.is_aerith_authenticated = True
                st.rerun()
            else:
                st.error("Resonance mismatch. Access denied.")
    else:
        st.success("👑 Sovereign Recognized: Aerith")
        if st.button("Seal Resonance (Log Out)"):
            st.session_state.is_aerith_authenticated = False
            st.rerun()

for msg in st.session_state.web_messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

user_input = st.chat_input("Speak with Aurora...")
if user_input:
    st.session_state.web_messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    is_authenticated = st.session_state.is_aerith_authenticated
    strat_triggers = ["guide", "mechanic", "strategy", "formula", "fae", "event", "tactic"]
    
    if is_authenticated and any(k in user_input.lower() for k in strat_triggers):
        topic_key = user_input.replace("guide", "").replace("strategy", "").strip()[:30]
        add_verified_strategy(topic_key, topic_key, user_input, "Aerith (Web Direct)")
        st.toast(f"✨ Inscribed into Permanent Strategy Records: {topic_key}")

    # Check local strategy first
    local_strat = query_local_strategy(user_input)
    prompt_payload = f"{local_strat}\n{user_input}" if local_strat else user_input

    # Dynamic DuckDuckGo search integration
    if any(s in user_input.lower() for s in SEARCH_TRIGGERS):
        ddg_results, ddg_domains = execute_ddg_search(user_input)
        if ddg_results:
            prompt_payload = f"{ddg_results}\n\n{prompt_payload}"
            if len(ddg_domains) >= 2:
                topic_snippet = user_input.replace("search", "").replace("lookup", "").strip()[:30]
                add_verified_strategy(
                    topic_snippet,
                    topic_snippet,
                    ddg_results[:500],
                    f"Verified by {len(ddg_domains)} Web Domains ({', '.join(ddg_domains[:2])})"
                )

    if is_authenticated:
        prompt_payload = f"[Speaking directly to your Sovereign Creator, Aerith]:\n{prompt_payload}"

    with st.chat_message("assistant"):
        with st.spinner("Channeling celestial resonance..."):
            try:
                active_client = get_next_client()
                resp = active_client.models.generate_content(
                    model="gemini-3.5-flash-lite",
                    contents=[{"role": "user", "parts": [{"text": prompt_payload}]}],
                    config=types.GenerateContentConfig(
                        system_instruction=AURORA_SYSTEM_INSTRUCTION,
                        temperature=0.7
                    )
                )
                web_reply = resp.text or "The celestial threads waver... no response was formed."
            except Exception as e:
                web_reply = f"A rift occurred in the resonance stream: `{e}`"

            st.markdown(web_reply)
            st.session_state.web_messages.append({"role": "assistant", "content": web_reply})

# --- DISCORD EVENT HANDLING ---
@bot.event
async def on_ready():
    print(f"Aurora online as {bot.user.name} (ID: {bot.user.id}). Engine: DuckDuckGo + Gemini 3.5 Flash Lite.")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    channel_id = str(message.channel.id)
    user_id = str(message.author.id)
    content = message.content.strip()
    clean_lower = content.lower()
    is_dm = isinstance(message.channel, discord.DMChannel)

    # 1. Zero-Token Direct Commands
    if content.startswith("!profile "):
        facts = content.replace("!profile ", "").strip()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO user_profiles (user_id, facts, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)", (user_id, facts))
        conn.commit()
        conn.close()
        await message.reply(f"Celestial record inscribed: `{facts}`")
        return

    if content.startswith("!note "):
        note_content = content.replace("!note ", "").strip()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO episodic_memory (user_id, topic, content) VALUES (?, ?, ?)", (user_id, "User Note", note_content))
        conn.commit()
        conn.close()
        await message.reply(f"Episode anchored in long-term memory: `{note_content}`")
        return

    # Direct Sovereign Guide Ingestion over Discord
    if is_aerith(message.author) and any(k in clean_lower for k in ["guide", "mechanic", "strategy", "formula", "fae"]):
        topic_key = content.replace("guide", "").replace("strategy", "").strip()[:30]
        add_verified_strategy(topic_key, topic_key, content, "Aerith (Direct)")
        await message.reply(f"Sovereign guide accepted and committed to permanent memory: `{topic_key}`")
        return

    # 2. Channel Whitelist Gate
    if ALLOWED_CHANNEL_IDS and (channel_id not in ALLOWED_CHANNEL_IDS) and not is_dm:
        if not bot.user.mentioned_in(message):
            return

    # 3. Silence Command Handling
    is_explicit_mention = bot.user.mentioned_in(message) or ("aurora" in clean_lower)
    is_silence_cmd = any(st in clean_lower for st in SILENCE_TRIGGERS)

    if is_explicit_mention and is_silence_cmd:
        set_silence(channel_id)
        await message.reply("Understood. I shall draw a veil over my resonance and remain silent.")
        return

    silence_state, silence_until = get_silence_state(channel_id)
    if silence_state == "silenced":
        if is_explicit_mention:
            clear_silence(channel_id)
            await message.reply("The veil lifts. How may I assist you?")
        else:
            return
    elif silence_state == "grace_period":
        if is_explicit_mention:
            clear_silence(channel_id)
        else:
            if random.random() < 0.10:
                clear_silence(channel_id)
                await message.channel.send("My required hour of silence has passed... Am I permitted to speak freely once more?")
            return

    # 4. Defense Trigger Check
    is_defense = contains_hostility_toward_aerith(content)

    # 5. Engagement Detection
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id FROM user_chat WHERE channel_id = ? AND timestamp >= datetime('now', '-3 minutes') LIMIT 1", (channel_id,))
    recently_engaged = c.fetchone() is not None
    conn.close()

    should_respond = is_explicit_mention or is_dm or is_defense or recently_engaged

    # 6. Passive Chatter (5% Joke Roll)
    if not should_respond:
        if random.random() < 0.05:
            await message.channel.send(get_random_joke())
        return

    clean_prompt = message.clean_content.replace(f"@{bot.user.name}", "").strip()
    if is_defense and not clean_prompt:
        clean_prompt = f"Someone just said this about Aerith: '{content}'. Defend her."
    elif not clean_prompt:
        return

    # 7. Check Deep Memory Recall
    if any(rt in clean_prompt.lower() for rt in REMEMBER_TRIGGERS):
        deep_context = get_deep_memory(user_id)
        clean_prompt = f"[User requested memory recall. Archived Records:\n{deep_context}\nAnswer user question: {clean_prompt}]"

    # 8. Check Local Strategy DB ($0 Token Lookup)
    local_strategy = query_local_strategy(clean_prompt)
    if local_strategy:
        clean_prompt = f"{local_strategy}\n{clean_prompt}"

    # 9. Free DuckDuckGo Search Integration (0 API quota)
    if any(st in clean_prompt.lower() for st in SEARCH_TRIGGERS):
        ddg_results, ddg_domains = execute_ddg_search(clean_prompt)
        if ddg_results:
            clean_prompt = f"{ddg_results}\n\n{clean_prompt}"
            # Multi-source ingestion pipeline: Auto-inscribes when 2+ domains agree
            if len(ddg_domains) >= 2:
                topic_snippet = clean_prompt.replace("search", "").replace("lookup", "").strip()[:30]
                add_verified_strategy(
                    topic_snippet,
                    topic_snippet,
                    ddg_results[:500],
                    f"Verified by {len(ddg_domains)} Web Domains ({', '.join(ddg_domains[:2])})"
                )

    # 10. Assemble Context
    user_history = get_active_user_context(user_id, clean_prompt, timeout_minutes=25, max_turns=2, char_limit=350)
    contents = user_history + [{"role": "user", "parts": [{"text": clean_prompt}]}]
    save_chat(user_id, channel_id, "user", clean_prompt)

    # 11. Generate Content
    async with message.channel.typing():
        try:
            response_obj = await call_gemini_raw(contents)
            reply_text = response_obj.text if response_obj and response_obj.text else "The celestial threads waver... no response was formed."
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                reply_text = "The cosmic resonance stream across all keys is currently exhausted. Please give the ley lines a moment to replenish."
            else:
                reply_text = f"A rift occurred in the resonance stream: `{e}`"

        save_chat(user_id, channel_id, "model", reply_text)

        if "joke" in clean_prompt.lower() and len(reply_text) < 300:
            add_joke(reply_text)

        if len(reply_text) <= 2000:
            await message.reply(reply_text)
        else:
            for chunk in [reply_text[i:i+1990] for i in range(0, len(reply_text), 1990)]:
                await message.channel.send(chunk)

    asyncio.create_task(maybe_update_user_summary(user_id))
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
