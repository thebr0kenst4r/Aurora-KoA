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
    print("CRITICAL: DISCORD_TOKEN is missing.")
if not raw_keys:
    st.error("Missing GEMINI_KEYS/GEMINI_KEY in Streamlit Secrets!")
    print("CRITICAL: No Gemini keys found.")

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

# --- DATABASE SETUP (MEMORY, PROFILES, EPISODES, STRATEGY, JOKES, & SILENCE) ---
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
    # 3. L1 Long-Term Profile Memory (Stable user facts)
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id TEXT PRIMARY KEY,
            facts TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 4. L2 Long-Term Episodic Memory (Events, plans, specific notes)
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

    # Seed / Upsert Comprehensive King of Avalon (Solar) Strategy Matrix
    EXPANDED_STRATEGY_DOCS = [
        # --- TROOP & BATTLE FORMATIONS ---
        (
            "battle formation",
            "Battle Formations & Combat Ratios",
            "Solar Combat Mechanics: Formations dictate target priority and casualty distribution. Standard Rally Ratio: 40-50% Infantry (Shield/frontline damage mitigation), 25-30% Cavalry (flank charge / backline puncture), and 25-30% Bowmen (sustained DPS). Never deploy naked backlines; if Infantry frontline collapses, Bowmen take amplified catastrophic direct damage.",
            "Aerith"
        ),
        (
            "pvp stats",
            "PvP Stats, Buffs & Spells",
            "Prioritize Combat Damage Reduction, Troop Attack, and Health multipliers over basic Defense stats. Active kingdom buffs, war talents, and deity spells should be timed to synchronize with rally impact windows to compound percentage stacking.",
            "Aerith"
        ),

        # --- DRAGON & ELDER DRAGON PROGRESSION ---
        (
            "dragon",
            "Dragon Talents & Skills",
            "Dragon Skill Allocation: Specialize trees strictly into Assault (March Attack/Lethality) or Guardian (Infantry Health/Defense/Damage Taken Reduction) rather than hybrid dispersion. Hoard Dragon Flames, Intensifiers, and Drake Essences for multiplier events.",
            "Aerith"
        ),
        (
            "dragon awakening",
            "Dragon Awakening",
            "Dragon Awakening elevates baseline stat ceiling. Focus awakening stones on your primary troop specialization first before balancing secondary branches.",
            "Aerith"
        ),
        (
            "elder dragons",
            "Elder Dragons & Awakening",
            "Elder Dragons provide passive realm-wide modifiers and combat prowess. Prioritize Elder Dragon Awakening materials during dedicated solar growth cycles to unlock advanced aura buffs.",
            "Aerith"
        ),

        # --- EQUIPMENT, HEROES & GEAR ---
        (
            "equipment",
            "Equipment & Gear Enhancement",
            "Equip complete gear sets to activate set resonance bonuses. Do not mix incomplete tier sets if the higher tier breaks your primary troop attack/defense resonance threshold. Focus enhancement on weapon and armor slots first.",
            "Aerith"
        ),
        (
            "gemstones",
            "Gemstones & Inlay Strategy",
            "Inlay gemstones aligned strictly with your primary rally focus (Infantry Health/Defense gems for tanks; Cav/Bow Attack and Lethality gems for damage). Refine low-tier gems in batches during spend/consume milestone events.",
            "Aerith"
        ),
        (
            "hero weapons",
            "Hero Weapons & Specialization",
            "Hero weapons significantly bolster hero council skill efficiency. Prioritize exclusive hero weapons matching your council leaders to trigger high-tier combat skill effects.",
            "Aerith"
        ),
        (
            "heroes",
            "Hero Council & Appointees",
            "Appoint heroes based on specific march utility and council passive synergies. Match front-row tank heroes with defensive council slots and offensive backline commanders with attack slots to maximize march scaling.",
            "Aerith"
        ),
        (
            "relics",
            "Relics & Lost Relics",
            "Lost Relics and permanent relics provide realm-wide passive multipliers. Target relics granting march capacity expansion, rally size boosts, and enemy troop debuffs.",
            "Aerith"
        ),
        (
            "artifacts",
            "Artifacts & Enhancement",
            "Enhance core artifacts that amplify troop lethality and damage received reduction. Ensure artifact power tracks with your current equipment tier.",
            "Aerith"
        ),
        (
            "emblems",
            "Emblems & Crests",
            "Socket emblems uniformly across equipment slots to unlock global tier perks. Focus on flat troop reduction and percentage attack boosts.",
            "Aerith"
        ),

        # --- DEITIES, BOONS & SPIRITUAL SYSTEMS ---
        (
            "deity",
            "Deity Systems & Devotion",
            "Deity shrines offer passive kingdom protections and offensive activation buffs. Maximize devotion tiers during growth stages to sustain combat aura efficiency.",
            "Aerith"
        ),
        (
            "divine boon",
            "Divine Boon & Favor",
            "Balance Divine Boon activation prior to major kingdom invasions or Coalition rallies. Divine Favor buffs compound directly with equipment stats.",
            "Aerith"
        ),
        (
            "mystic soul",
            "Mystic Souls & Resonance",
            "Infuse mystic souls to augment individual troop type vitality and lethality. Prioritize frontline survivability souls before investment into pure damage branches.",
            "Aerith"
        ),
        (
            "element rating",
            "Element Rating & Counters",
            "Element ratings introduce cyclic advantage mechanics. Study defending enemy element affixes before engaging to ensure counter bonuses are leveraged.",
            "Aerith"
        ),

        # --- BASE, REALM & DOMINION GROWTH ---
        (
            "castle",
            "Castle & Building Priorities",
            "Prioritize Stronghold rush milestones to unlock higher troop tiers (T10, T11, T12). Always keep Embassy, Hall of War, and Resource production buildings synchronized with core march caps.",
            "Aerith"
        ),
        (
            "solars evolution",
            "Solar Evolution & Growth",
            "Focus evolution trees on march capacity, training speed, and hospital capacity before luxury economic nodes. Hospital capacity must always exceed your largest single-march deployment.",
            "Aerith"
        ),
        (
            "beastling",
            "Beastling Realm",
            "Nurture and evolve beastlings to gain passive march speed, gathering acceleration, and auxiliary combat stats.",
            "Aerith"
        ),
        (
            "skyloft",
            "Skyloft Garden",
            "Tend Skyloft crops and flora on rotation to generate vital stat essences and temporary march vigor buffs.",
            "Aerith"
        ),
        (
            "banners",
            "Alliance & War Banners",
            "Place war banners strategically near contested Coalition structures or resource veins to expand territory buffs and reinforce rally speed.",
            "Aerith"
        ),

        # --- ALLIANCE & COALITION EVENTS ---
        (
            "alliance",
            "Alliance Coordination & Warfare",
            "Alliance unity is paramount. Keep reinforcements routed to frontline towers, maintain high donation levels for alliance tech milestones, and coordinate rally leader queues in advance.",
            "Aerith"
        ),
        (
            "alliance hunt",
            "Alliance Hunt & Treasure Hunt",
            "Coordinate rally calls during scheduled hunt hours. Maximize stamina replenishment items specifically when alliance drop multipliers are active.",
            "Aerith"
        ),
        (
            "fallen knights",
            "Fallen Knights Event",
            "Fallen Knights Protocol: Reinforce weaker alliance castles that cannot survive higher waves. Higher-power players should send spare defense marches out to maximize overall alliance survival points.",
            "Aerith"
        ),
        (
            "guardian challenge",
            "Guardian Challenge",
            "Deploy maximum sustained DPS march configurations against the Guardian. Rotate defense debuff heroes first before high-burst strikes.",
            "Aerith"
        ),

        # --- WAR, INVASION & REALM-WIDE EVENTS ---
        (
            "excalibur",
            "Excalibur Invasion",
            "Royal Excalibur Warfare: Synchronize Coalition rallies against the royal palace and sanity towers. Coordinate march speeds so rally impacts hit concurrently with swarm defense waves.",
            "Aerith"
        ),
        (
            "fae",
            "Fae Realm & Seasons",
            "Fae Realm Protocol: Secure sanctum occupations early for seasonal kingdom buffs. Focus seasonal research trees on gathering and processing speed nodes before combat militarization.",
            "Aerith"
        ),
        (
            "mystery of the dunes",
            "Mystery of the Dunes (Fae Season 7)",
            "Dunes Season Mechanics: Prioritize water and oasis capture points to stave off territorial attrition. Protect supply lines between occupied sanctums.",
            "Aerith"
        ),
        (
            "netherwar",
            "Nether War & Netherfall",
            "Nether Battlefield Strategy: Control portal checkpoints and capture central spires. Rapid repositioning and march speed-ups are crucial to contest point capture nodes.",
            "Aerith"
        ),
        (
            "uac",
            "Ultimate Alliance Conquest (UAC)",
            "UAC Tournament Operations: Strict comms discipline. Assign dedicated rally leads, fast-cavalry capture runners for unoccupied buildings, and anchor defense teams at core control towers.",
            "Aerith"
        ),
        (
            "kingdom tourney",
            "Kingdom Tourney & Raids",
            "Gold Event & Kingdom Raid: Keep shields active prior to portal readiness. Target unshielded resource-heavy castles and retreat troops before counter-swarms land.",
            "Aerith"
        ),
        (
            "spire",
            "Spire & Floor Progression",
            "Spire Tactics: Tailor troop compositions directly to counter the enemy formation displayed on each floor. Reset and adjust hero council alignments to counter specific boss immunities.",
            "Aerith"
        ),
        (
            "shadow planes",
            "Shadow Planes",
            "Shadow Planes expedition: Conserve stamina for elite shadow fiends. Clear floor stages systematically to gather high-tier artifact and gear upgrade catalysts.",
            "Aerith"
        ),
        (
            "antiquity tree",
            "Antiquity Tree Challenge",
            "Antiquity Tree: Clear branch nodes methodically. Reallocate talent points toward PvE monster damage and stamina recovery to maximize stage depth.",
            "Aerith"
        ),
        (
            "dragon dale",
            "Dragon Dale Clash",
            "Dale Clash: Secure mineral nodes and control central dragon sanctuaries. Defense swarms must be coordinated quickly against enemy rally leads.",
            "Aerith"
        ),
        (
            "dragonfire relay",
            "Dragonfire Relay",
            "Relay Strategy: Maintain uninterrupted torch transit across designated waypoints. Position escort marches along the transit lane to block intercepts.",
            "Aerith"
        ),
        (
            "ravager hunt",
            "Ravager Hunt",
            "Ravager Hunt: Pool alliance march stamina to burn down high-level ravagers during double-point bonus hours.",
            "Aerith"
        ),
        (
            "castle conquest",
            "Castle Conquest",
            "Conquest Strategy: Isolate and surround target objectives. Suppress incoming defensive reinforcements using staggered diversion marches.",
            "Aerith"
        ),
        (
            "path of conquest",
            "Path of Conquest",
            "Progression road: Fulfill daily milestone objectives to accumulate march speed-ups and enhancement crystals for seasonal milestones.",
            "Aerith"
        ),
        (
            "infinite realms",
            "Infinite Realms Dragon King",
            "Infinite Realms Warfare: Cross-realm deployment requires strict scouting. Never commit main rallies without confirming enemy reinforcement capacity.",
            "Aerith"
        ),
        (
            "knight of the lake",
            "Knight of the Lake",
            "Lake Trials: Deploy high-defense frontline marches to weather the Knight's initial burst phase, then follow up with high-lethality archer DPS.",
            "Aerith"
        ),
        (
            "scouting",
            "Scouting & Reconnaissance",
            "Scouting Protocol: Never attack blind. Thorough recon reveals enemy wall defenses, reinforcing garrisons, hero council setups, and hidden trap counts.",
            "Aerith"
        ),
        (
            "migration",
            "Kingdom Merge & Migration",
            "Migration Strategy: Ensure your resource totals are below vault protection thresholds or spent on research before migrating. Collect required migration passes during event discounts.",
            "Aerith"
        ),

        # =========================================================================
        # --- FAE REALM: UNIVERSAL LAWS & ALL-SEASON DOCTRINES ($0 TOKENS) ---
        # =========================================================================
        (
            "fae rules",
            "Fae Realm Universal Rules & Lifecycle",
            "Universal Fae System Mechanics: "
            "1. Seasonal Reset: Every Fae season resets seasonal research, seasonal buildings, and seasonal territory; permanent castle stats, main gear, and heroes are unaffected. "
            "2. Sanctum Prioritization: Early phase focus must be 100% on securing Gathering and Speed Sanctums before military checkpoints. Sanctum production buffs compound season-long growth. "
            "3. Research Economy Tree First: Never push combat nodes first. Prioritize 'Gathering Speed', 'Processing Efficiency', and 'Seasonal March Speed' to compound material generation before warfare phases open. "
            "4. Territory Adjacency: Rallies and passes can only be taken if adjacent to contiguous alliance territory banners. Banners must never be cut off or disconnected. "
            "5. Attrition Management: Troops wounded inside Fae battle zones consume seasonal medicine/essences rather than core kingdom healing resources; pace march losses to prevent seasonal hospital overflow.",
            "Aerith"
        ),
        (
            "fae seasons",
            "Fae Seasons Overview & Architecture",
            "Standard Fae Season Lifecycle: Phase 1: Land grab, border expansion, and initial resource point occupation. Phase 2: Sanctum unlock and regional pass battles. Phase 3: Central World Tree / Core Throne contention. Always reserve march speed-ups, high-tier stamina items, and peace shields specifically for Pass Openings and Core Battles.",
            "Aerith"
        ),

        # =========================================================================
        # --- FAE SEASONS: DEDICATED SEASON-BY-SEASON MATRICES ---
        # =========================================================================
        (
            "fae season 1",
            "Fae Season 1: Awakening of the Grove",
            "Season 1 Mechanics: Foundational Fae season. Focus: Establish baseline alliance territory lines toward Tier 1 Sanctums. Key Strategy: Maximize seasonal wood/iron gathering nodes early; maintain strict 45% Infantry frontlines for NPC Grove guardians.",
            "Aerith"
        ),
        (
            "fae season 2",
            "Fae Season 2: Whispering Mire",
            "Season 2 Mechanics: Swamp territory with march speed debuffs. Key Strategy: Prioritize environmental movement resistance research in the seasonal tree. Position alliance fortresses to bypass narrow choke points in the wetlands.",
            "Aerith"
        ),
        (
            "fae season 3",
            "Fae Season 3: Twilight Thorns",
            "Season 3 Mechanics: Defending thorns impose passive attrition on sustained rallies. Key Strategy: Rotate rally leads to spread bleed damage. High Bowman backline DPS is essential to burn down thorny fortress nodes before attrition stacks wipe Infantry.",
            "Aerith"
        ),
        (
            "fae season 4",
            "Fae Season 4: Crystal Shards",
            "Season 4 Mechanics: Introduction of seasonal crystal refining nodes. Key Strategy: Protect central crystal veins. Dedicate off-peak hours for gatherer protection; crystal tech yields significant seasonal march capacity upgrades.",
            "Aerith"
        ),
        (
            "fae season 5",
            "Fae Season 5: Ancient Canopy",
            "Season 5 Mechanics: Multi-elevation battlefields with canopy watchtowers. Key Strategy: Garrison towers immediately upon pass unlocking to claim aerial bombardment buffs against enemy field marches.",
            "Aerith"
        ),
        (
            "fae season 6",
            "Fae Season 6: Frozen Tundra",
            "Season 6 Mechanics: Severe cold attrition outside heated outposts. Key Strategy: Keep marches tethered to alliance warm-zones. Do not deploy naked marches across blizzard lanes without heat stones active.",
            "Aerith"
        ),
        (
            "fae season 7",
            "Fae Season 7: Mystery of the Dunes",
            "Season 7 Dunes Mechanics: Arid desert battlefield with water/oasis resource control and sandstorm territory locks. Key Strategy: Secure and reinforce water wells early to avoid troop dehydration debuffs. Choke enemy supply lines between contested oases prior to central temple unlocks.",
            "Aerith"
        ),
        (
            "fae season 8",
            "Fae Season 8: Obsidian Spires",
            "Season 8 Mechanics: Volcanic terrain with periodic lava surges disabling outer banners. Key Strategy: Fortify inner perimeter lines. Spire captures grant explosive rally buffs for 30 minutes; time major pushes around spire cooldowns.",
            "Aerith"
        ),
        (
            "fae season 9",
            "Fae Season 9: Sunken Glade",
            "Season 9 Mechanics: Periodic tide surges alter viable pathing lanes. Key Strategy: Pre-coordinate march timers so reinforcements do not get trapped when water barriers rise.",
            "Aerith"
        ),
        (
            "fae season 10",
            "Fae Season 10: Celestial Bloom",
            "Season 10 Mechanics: Dual-cycle Day/Night bloom states. Key Strategy: Defensive actions receive 25% stat boosts during Night; offensive rally operations should strictly launch during Peak Solar Day phases.",
            "Aerith"
        ),
        (
            "fae season 11",
            "Fae Season 11: Shattered Canopy",
            "Season 11 Mechanics: Aerial rift navigation and splintered sanctums. Key Strategy: Coordinate dual-front skirmish teams. Prevent enemy alliances from linking split sanctuary networks.",
            "Aerith"
        ),
        (
            "fae season 12",
            "Fae Season 12: Echoing Caverns",
            "Season 12 Mechanics: Subterranean tunnels where cavalry flank bonuses are heavily amplified. Key Strategy: Increase Cavalry ratios to 35% in choke-point skirmishes.",
            "Aerith"
        ),
        (
            "fae season 13",
            "Fae Season 13: Primal Roots",
            "Season 13 Mechanics: Root network regenerates defensive walls rapidly. Key Strategy: Use high-burst siege/cavalry timing to break gates in single concentrated rally waves.",
            "Aerith"
        ),
        (
            "fae season 14",
            "Fae Season 14: Radiant Mirage",
            "Season 14 Mechanics: Decoy sanctums and false territory markers. Key Strategy: Strict scouting protocol before allocating high-cost war banners; verify true core nodes via scouting coordinates.",
            "Aerith"
        ),
        (
            "fae season 15",
            "Fae Season 15: Phantom Mist",
            "Season 15 Mechanics: Fog of war obscures enemy march sizes until within 5km. Key Strategy: Establish radar perimeter garrisons along border outposts to trigger early warning flares.",
            "Aerith"
        ),
        (
            "fae season 16",
            "Fae Season 16: Eternal Eclipse",
            "Season 16 Mechanics: Extended shadow debuff reduces healing speed. Key Strategy: Minimize reckless attrition; hoard seasonal healing elixirs exclusively for Coalition gate assaults.",
            "Aerith"
        ),
        (
            "fae season 17",
            "Fae Season 17: Stormforged Peaks",
            "Season 17 Mechanics: Lightning hazards randomly strike march routes outside sanctum protective fields. Key Strategy: Leapfrog marches from sanctum to sanctum rather than using cross-map pathing.",
            "Aerith"
        ),
        (
            "fae season 18",
            "Fae Season 18: Verdant Bastion",
            "Season 18 Mechanics: Fortified super-sanctums requiring tiered multi-rally strikes. Key Strategy: Stagger frontline Infantry break-in teams 3 seconds ahead of main DPS swarms.",
            "Aerith"
        ),
        (
            "fae season 19",
            "Fae Season 19: Core Resonance & Seasonal Warfare",
            "Season 19 Tactical Matrix: "
            "1. Seasonal Architecture: Latest Solar progression framework incorporating advanced Fae tree militarization and high-yield sanctums. "
            "2. Core Priority: Maximize seasonal economic research nodes in days 1-5; transition to troop defense/health trees before Level 3 Passes open. "
            "3. Rally Formations: Maintain strict 45% Infantry / 30% Cav / 25% Bowmen. Frontline collapse in S19 leads to instant wipeout against specialized seasonal siege defenses. "
            "4. Sovereign Directives: Detailed tactical updates, specific map layout breakdowns, and milestone turn-in times are directly committed by Aerith.",
            "Aerith"
        )
        
    ]

    c.executemany(
        "INSERT OR REPLACE INTO strategy_docs (keyword, topic, content, verified_by, timestamp) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
        EXPANDED_STRATEGY_DOCS
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
    
    # 1. Fetch L1 Profile Facts
    c.execute("SELECT facts FROM user_profiles WHERE user_id = ?", (str(user_id),))
    p_row = c.fetchone()
    l1_profile = p_row[0] if p_row else ""
    
    # 2. Fetch L2 Episodic Memories matching prompt terms
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
    """Fetches user history within decay window, clipped for tokens, plus rolling summary & LTM."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # 1. Fetch rolling summary
    c.execute("SELECT summary FROM user_summaries WHERE user_id = ?", (str(user_id),))
    s_row = c.fetchone()
    summary = s_row[0] if s_row else ""

    # 2. Fetch recent turns inside decay window
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
    
    # Inject L1/L2 Long-Term Memory
    ltm_snippet = get_ltm_context(user_id, prompt)
    if ltm_snippet:
        formatted.append({
            "role": "model",
            "parts": [{"text": ltm_snippet}]
        })

    # Inject rolling summary
    if summary:
        formatted.append({
            "role": "model",
            "parts": [{"text": f"[Persistent Knowledge of User: {summary}]"}]
        })

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
    """Deep archive inspection for !remember / recall commands."""
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
    """Performs keyless web search and returns formatted snippets + domain sources."""
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
                
                if href:
                    parts = href.split("/")
                    domain = parts[2] if len(parts) > 2 else href
                    domains.append(domain)

            formatted_snippets = "[Live DuckDuckGo Web Results]:\n" + "\n".join(snippets)
            return formatted_snippets, list(set(domains))
    except Exception as e:
        print(f"DuckDuckGo search notice: {e}")
        return "", []

# --- VERIFIED KNOWLEDGE INGESTION PIPELINE ---
def process_knowledge_ingestion(prompt: str, content: str, domains: list, author=None):
    """Inspects text and auto-inscribes into strategy records if authoritative or 2+ domains agree."""
    clean_p = prompt.lower()
    
    # 1. Sovereign override: Direct input from Aerith
    if author and is_aerith(author) and any(k in clean_p for k in ["guide", "mechanic", "strategy", "formula", "fae"]):
        topic_key = prompt.replace("guide", "").replace("strategy", "").strip()[:30]
        add_verified_strategy(topic_key, topic_key, content, "Aerith (Direct)")
        return topic_key

    # 2. Multi-source web verification (2+ independent domains)
    if len(domains) >= 2:
        topic_snippet = prompt.replace("search", "").replace("lookup", "").replace("google", "").strip()[:30]
        add_verified_strategy(
            topic_snippet,
            topic_snippet,
            content[:500],
            f"Verified by {len(domains)} Web Domains ({', '.join(domains[:2])})"
        )
        return topic_snippet
        
    return None

# --- SILENCE PROTOCOL MANAGEMENT ---
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

# --- AURORA SYSTEM INSTRUCTION & DIRECTIVES ---
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
            return await active_client.aio.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=contents,
                config=config
            )
        raise e

# --- ASYNC ROLLING AUTO-SUMMARIZER ---
async def maybe_update_user_summary(user_id):
    """Summarizes history in background with a delay to prevent concurrent quota collisions."""
    await asyncio.sleep(6)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT role, content FROM user_chat WHERE user_id = ? ORDER BY id DESC LIMIT 8",
        (str(user_id),)
    )
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
                print(f"Updated rolling summary for user {user_id}")
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
    
    # Direct sovereign ingestion through web portal
    if is_authenticated and any(k in user_input.lower() for k in strat_triggers):
        topic_key = user_input.replace("guide", "").replace("strategy", "").strip()[:30]
        add_verified_strategy(topic_key, topic_key, user_input, "Aerith (Web Direct)")
        st.toast(f"✨ Inscribed into Permanent Strategy Records: {topic_key}")

    local_strat = query_local_strategy(user_input)
    prompt_payload = f"{local_strat}\n{user_input}" if local_strat else user_input

    # Dynamic DuckDuckGo search integration
    if any(s in user_input.lower() for s in SEARCH_TRIGGERS):
        ddg_results, ddg_domains = execute_ddg_search(user_input)
        if ddg_results:
            prompt_payload = f"{ddg_results}\n\n{prompt_payload}"
            inscribed = process_knowledge_ingestion(user_input, ddg_results, ddg_domains)
            if inscribed:
                st.toast(f"🌐 Inscribed verified web data: {inscribed}")

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
    print(f"Aurora online as {bot.user.name} (ID: {bot.user.id}). Running on DDG + Gemini 3.5 Flash Lite.")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    channel_id = str(message.channel.id)
    user_id = str(message.author.id)
    content = message.content.strip()
    clean_lower = content.lower()
    is_dm = isinstance(message.channel, discord.DMChannel)

    # 1. Zero-Token Direct Commands: L1 Profile & L2 Notes
    if content.startswith("!profile "):
        facts = content.replace("!profile ", "").strip()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO user_profiles (user_id, facts, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (user_id, facts)
        )
        conn.commit()
        conn.close()
        await message.reply(f"Celestial record inscribed into your profile: `{facts}`")
        return

    if content.startswith("!note "):
        note_content = content.replace("!note ", "").strip()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "INSERT INTO episodic_memory (user_id, topic, content) VALUES (?, ?, ?)",
            (user_id, "User Note", note_content)
        )
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

    # 3. Check for Silence Commands
    is_explicit_mention = bot.user.mentioned_in(message) or ("aurora" in clean_lower)
    is_silence_cmd = any(st in clean_lower for st in SILENCE_TRIGGERS)

    if is_explicit_mention and is_silence_cmd:
        set_silence(channel_id)
        await message.reply("Understood. I shall draw a veil over my resonance and remain silent.")
        return

    # 4. Check Active Silence State
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

    # 5. Defense Trigger (Priority Override)
    is_defense = contains_hostility_toward_aerith(content)

    # 6. Engagement Detection
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT id FROM user_chat WHERE channel_id = ? AND timestamp >= datetime('now', '-3 minutes') LIMIT 1",
        (channel_id,)
    )
    recently_engaged = c.fetchone() is not None
    conn.close()

    should_respond = is_explicit_mention or is_dm or is_defense or recently_engaged

    # 7. Passive Chatter Handling (5% Joke Roll)
    if not should_respond:
        if random.random() < 0.05:
            joke = get_random_joke()
            await message.channel.send(joke)
        return

    # Prepare Clean Prompt
    clean_prompt = message.clean_content.replace(f"@{bot.user.name}", "").strip()
    if is_defense and not clean_prompt:
        clean_prompt = f"Someone just said this about Aerith: '{content}'. Defend her."
    elif not clean_prompt:
        return

    # 8. Check Explicit Deep Memory Recall Trigger (!remember)
    if any(rt in clean_prompt.lower() for rt in REMEMBER_TRIGGERS):
        deep_context = get_deep_memory(user_id)
        clean_prompt = f"[User requested memory recall. Archived Records:\n{deep_context}\nAnswer user question: {clean_prompt}]"

    # 9. Check Local Strategy DB ($0 Token Lookup)
    local_strategy = query_local_strategy(clean_prompt)
    if local_strategy:
        clean_prompt = f"{local_strategy}\n{clean_prompt}"

    # 10. Keyless DuckDuckGo Search Integration (0 API quota)
    ddg_domains = []
    if any(st in clean_prompt.lower() for st in SEARCH_TRIGGERS):
        ddg_results, ddg_domains = execute_ddg_search(clean_prompt)
        if ddg_results:
            clean_prompt = f"{ddg_results}\n\n{clean_prompt}"
            # Multi-source ingestion pipeline: Auto-inscribes when 2+ domains agree
            process_knowledge_ingestion(clean_prompt, ddg_results, ddg_domains)

    # 11. Assemble Context (Short-Term Decay Window + LTM Context)
    user_history = get_active_user_context(user_id, clean_prompt, timeout_minutes=25, max_turns=2, char_limit=350)
    contents = user_history + [{"role": "user", "parts": [{"text": clean_prompt}]}]
    save_chat(user_id, channel_id, "user", clean_prompt)

    async with message.channel.typing():
        response_obj = None
        try:
            response_obj = await call_gemini_raw(contents)
            reply_text = response_obj.text if response_obj and response_obj.text else "The celestial threads waver... no response was formed."
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                reply_text = "The cosmic resonance stream across all keys is currently exhausted. Please give the ley lines a moment to replenish."
            else:
                print(f"Error during execution: {e}")
                reply_text = f"A rift occurred in the resonance stream: `{e}`"

        save_chat(user_id, channel_id, "model", reply_text)

        # Dynamically harvest jokes
        if "joke" in clean_prompt.lower() and len(reply_text) < 300:
            add_joke(reply_text)

        # Discord Message Delivery
        if len(reply_text) <= 2000:
            await message.reply(reply_text)
        else:
            for chunk in [reply_text[i:i+1990] for i in range(0, len(reply_text), 1990)]:
                await message.channel.send(chunk)

    # 12. Run auto-summarizer asynchronously (delayed to prevent concurrent quota collisions)
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
