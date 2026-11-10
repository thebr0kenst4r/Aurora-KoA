import os
import re
import json
import hmac
import hashlib
import shutil
import random
import asyncio
import threading
import sqlite3
import itertools
from datetime import datetime, timedelta
import pandas as pd
import discord
from discord.ext import commands
from google import genai
from google.genai import types
from tenacity import retry, wait_random_exponential, stop_after_attempt, retry_if_exception
from duckduckgo_search import DDGS
import streamlit as st
import streamlit.components.v1 as components

# --- STREAMLIT DASHBOARD CONFIGURATION ---
st.set_page_config(page_title="Aurora Core — The Broken Star", page_icon="✨", layout="wide")
hide_streamlit_style = """
            <style>
            #MainMenu {visibility: hidden;}
            footer {visibility: hidden;}
            </style>
            """
st.markdown(hide_streamlit_style, unsafe_allow_html=True)

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

EXPECTED_KEY = os.environ.get("AERITH_KEY") or st.secrets.get("AERITH_KEY", "cosmic_resonance_key")
ALLIANCE_PASS = os.environ.get("ALLIANCE_KEY") or st.secrets.get("ALLIANCE_KEY", "avalon_ vanguard_key")

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

def make_auth_token(secret_val: str) -> str:
    return hmac.new(secret_val.encode("utf-8"), b"aurora_sovereign_session", hashlib.sha256).hexdigest()[:24]

# --- BOT DEFINITION & SHARED CROSS-THREAD STATE ---
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

DISCORD_LOOP = None
DISCORD_CHANNELS_CACHE = {}  # {channel_id_str: "#channel-name (Guild Name)"}

# --- DATABASE SETUP (MEMORY, PROFILES, EPISODES, STRATEGY, JOKES, SILENCE & MEDIA) ---
DB_FILE = "aurora_core.db"

EXPANDED_STRATEGY_DOCS = [
    # --- TROOP & BATTLE FORMATIONS ---
    (
        "battle formation",
        "Battle Formations & Combat Ratios",
        "Solar Combat Mechanics: Formations dictate target priority and casualty distribution. Standard Rally Ratio: 40-50% Infantry (Shield/frontline damage mitigation), 25-30% Cavalry (flank charge / backline puncture), and 25-30% Bowmen (sustained DPS). Standard PvP Multi-Tier Ratio: 30% Inf / 15% Cav / 30% Bow at Top Tier, backed by 10% Inf / 5% Cav / 10% Bow at Top Tier - 1. Glass Cannon setups run 96-98% primary DPS (Bowmen or Cavalry) with 1-2% meatshield layers, or 48-49% Dual Cav/Bow split. Never deploy naked backlines; if Infantry frontline collapses, Bowmen take amplified catastrophic direct damage.",
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
    # --- FAE SEASONS: DEDICATED SEASON-BY-SEASON MATRICES (SEASONS 1 TO 20) ---
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
    ),
    (
        "fae season 20",
        "Fae Season 20: Maritime Dominance",
        "Season 20 Placeholder: Sea of Mystery.",
        "Aerith"
    )
]

def init_db(force_reseed=False):
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
            access_level TEXT DEFAULT 'Everyone',
            category TEXT DEFAULT 'General',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Safe column migrations for existing databases
    for col_sql in [
        "ALTER TABLE strategy_docs ADD COLUMN access_level TEXT DEFAULT 'Everyone'",
        "ALTER TABLE strategy_docs ADD COLUMN category TEXT DEFAULT 'General'",
    ]:
        try:
            c.execute(col_sql)
        except sqlite3.OperationalError:
            pass

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
            max_silence_until DATETIME,
            silenced_by_id TEXT DEFAULT 'Unknown',
            silenced_by_name TEXT DEFAULT 'Unknown'
        )
    ''')
    for col_sql in [
        "ALTER TABLE silence_status ADD COLUMN silenced_by_id TEXT DEFAULT 'Unknown'",
        "ALTER TABLE silence_status ADD COLUMN silenced_by_name TEXT DEFAULT 'Unknown'",
    ]:
        try:
            c.execute(col_sql)
        except sqlite3.OperationalError:
            pass

    # 8. Silence abuse tally tracker
    c.execute('''
        CREATE TABLE IF NOT EXISTS silence_tally (
            user_id TEXT PRIMARY KEY,
            username TEXT,
            silence_count INTEGER DEFAULT 0,
            is_ignored INTEGER DEFAULT 0,
            last_triggered DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 9. Creator Music & Social Media Links (The Broken Star)
    c.execute('''
        CREATE TABLE IF NOT EXISTS creator_media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_type TEXT,
            title TEXT,
            url TEXT
        )
    ''')

    # Seed Strategy Docs (INSERT OR IGNORE preserves Sovereign edits & access levels unless force_reseed=True)
    for kw, top, con, ver in EXPANDED_STRATEGY_DOCS:
        cat = "Fae Realm" if "fae" in kw.lower() else ("Combat & Troops" if any(x in kw for x in ["battle", "pvp", "dragon", "scouting"]) else "Solar & Events")
        if force_reseed:
            c.execute(
                "INSERT OR REPLACE INTO strategy_docs (keyword, topic, content, verified_by, access_level, category, timestamp) VALUES (?, ?, ?, ?, COALESCE((SELECT access_level FROM strategy_docs WHERE keyword = ?), 'Everyone'), ?, CURRENT_TIMESTAMP)",
                (kw, top, con, ver, kw, cat)
            )
        else:
            c.execute(
                "INSERT OR IGNORE INTO strategy_docs (keyword, topic, content, verified_by, access_level, category, timestamp) VALUES (?, ?, ?, ?, 'Everyone', ?, CURRENT_TIMESTAMP)",
                (kw, top, con, ver, cat)
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

    # Seed Creator Media placeholders if empty
    c.execute("SELECT COUNT(*) FROM creator_media")
    if c.fetchone()[0] == 0:
        c.executemany(
            "INSERT INTO creator_media (media_type, title, url) VALUES (?, ?, ?)",
            [
                ("audio", "The Broken Star — Celestial Resonance (Stream Preview)", "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"),
                ("audio", "The Broken Star — Astral Vanguard (Stream Preview)", "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-2.mp3"),
                ("social", "YouTube — @thebr0kenst4r", "https://youtube.com"),
                ("social", "Spotify — The Broken Star (Aerith)", "https://open.spotify.com"),
                ("social", "SoundCloud — The Broken Star", "https://soundcloud.com")
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

def query_local_strategy(prompt: str, allowed_levels=None):
    """Searches local strategy table first ($0 tokens, 0 API quota), respecting access_level."""
    if allowed_levels is None:
        allowed_levels = ["Everyone"]
    clean = prompt.lower()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    placeholders = ",".join(["?" for _ in allowed_levels])
    c.execute(f"SELECT keyword, topic, content FROM strategy_docs WHERE access_level IN ({placeholders})", allowed_levels)
    matches = []
    for kw, topic, content in c.fetchall():
        if kw.lower() in clean or topic.lower() in clean or any(w in clean for w in topic.lower().split() if len(w) > 3):
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

def add_joke(joke_text, added_by="Aurora"):
    if not joke_text or len(joke_text) < 10:
        return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO jokes (joke, added_by) VALUES (?, ?)", (joke_text.strip(), added_by))
    conn.commit()
    conn.close()

def add_verified_strategy(keyword, topic, content, verified_by, access_level="Everyone", category="General"):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO strategy_docs (keyword, topic, content, verified_by, access_level, category, timestamp) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
        (keyword.lower().strip(), topic.strip(), content.strip(), verified_by, access_level, category)
    )
    conn.commit()
    conn.close()
    print(f"Ingested Verified Strategy: '{topic}' ({access_level}) verified by {verified_by}")

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

# --- SILENCE PROTOCOL & ABUSE TALLY MANAGEMENT ---
SILENCE_ABUSE_THRESHOLD = 3

def check_and_record_silence_attempt(user_id: str, username: str, bypass_abuse: bool = False) -> bool:
    """
    Increments the user's silence tally and returns True if allowed to silence,
    or False if they have abused the command (count > threshold or manually ignored).
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT silence_count, is_ignored FROM silence_tally WHERE user_id = ?", (str(user_id),))
    row = c.fetchone()
    if not row:
        new_count = 1
        is_ignored = 0
        c.execute(
            "INSERT INTO silence_tally (user_id, username, silence_count, is_ignored, last_triggered) VALUES (?, ?, 1, 0, CURRENT_TIMESTAMP)",
            (str(user_id), str(username))
        )
    else:
        new_count = row[0] + 1
        is_ignored = row[1]
        c.execute(
            "UPDATE silence_tally SET username = ?, silence_count = ?, last_triggered = CURRENT_TIMESTAMP WHERE user_id = ?",
            (str(username), new_count, str(user_id))
        )
    conn.commit()
    conn.close()

    if bypass_abuse:
        return True
    if is_ignored == 1 or new_count > SILENCE_ABUSE_THRESHOLD:
        return False
    return True

def set_silence(channel_id, silenced_by_id="Web/System", silenced_by_name="Sovereign"):
    now = datetime.utcnow()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO silence_status (channel_id, silence_until, max_silence_until, silenced_by_id, silenced_by_name) VALUES (?, ?, ?, ?, ?)",
        (
            str(channel_id),
            (now + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S'),
            (now + timedelta(hours=4)).strftime('%Y-%m-%d %H:%M:%S'),
            str(silenced_by_id),
            str(silenced_by_name)
        )
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

# --- HELPER TO BROADCAST DISCORD MESSAGES FROM STREAMLIT ---
async def _async_send_discord(channel_id: int, text: str):
    ch = bot.get_channel(channel_id)
    if ch is None:
        ch = await bot.fetch_channel(channel_id)
    if ch:
        for chunk in [text[i:i+1990] for i in range(0, len(text), 1990)]:
            await ch.send(chunk)
        return True
    return False

def broadcast_to_discord_channel(channel_id_str: str, message_text: str) -> tuple[bool, str]:
    if not DISCORD_LOOP or not bot.is_ready():
        return False, "Discord gateway is still initializing or offline."
    try:
        ch_id = int(channel_id_str.strip())
        future = asyncio.run_coroutine_threadsafe(_async_send_discord(ch_id, message_text), DISCORD_LOOP)
        ok = future.result(timeout=10)
        if ok:
            save_chat("Aerith_Broadcast", str(ch_id), "model", message_text)
            return True, f"Resonance transmitted to channel `{ch_id}`."
        return False, "Channel could not be resolved."
    except Exception as e:
        return False, f"Broadcast error: {e}"

# =====================================================================
# --- STREAMLIT DYNAMIC MULTIPAGE FRONTEND & CUSTOM COMPONENTS ---
# =====================================================================

# 1. Persistent Cookie / Query-Token Session Check (3C)
SOVEREIGN_TOKEN = make_auth_token(EXPECTED_KEY)
ALLIANCE_TOKEN = make_auth_token(ALLIANCE_PASS)

if "is_aerith_authenticated" not in st.session_state:
    st.session_state.is_aerith_authenticated = (st.query_params.get("sov") == SOVEREIGN_TOKEN)

if "is_alliance_authenticated" not in st.session_state:
    st.session_state.is_alliance_authenticated = (
        st.session_state.is_aerith_authenticated or (st.query_params.get("ally") == ALLIANCE_TOKEN)
    )

if "web_messages" not in st.session_state:
    st.session_state.web_messages = [
        {"role": "assistant", "content": "I am Aurora. The cosmic resonance is open. How may I assist your campaign?"}
    ]

# Fetch Creator Media (Audio & Socials)
def get_creator_media():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, media_type, title, url FROM creator_media ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()
    audios = [(r[2], r[3]) for r in rows if r[1] == "audio"]
    socials = [(r[2], r[3]) for r in rows if r[1] == "social"]
    return rows, audios, socials

_, AUDIO_TRACKS, SOCIAL_LINKS = get_creator_media()

if "current_track_idx" not in st.session_state and AUDIO_TRACKS:
    st.session_state.current_track_idx = random.randrange(len(AUDIO_TRACKS))

# Creator Showcase Modal Dialog (3B / 1D)
@st.dialog("🌟 Aerith — The Broken Star (Sovereign Creator)")
def show_creator_dialog():
    st.markdown(
        """
        ### *Born of Cosmic Resonance & Atmospheric Trance*
        **Aerith Natalia A.** (*Aeri / The Broken Star / `@thebr0kenst4r`*) is the sovereign architect of **Aurora** 
        and the composer behind the celestial soundscapes of **The Broken Star**.
        
        Blending ethereal electronic trance with high-level tactical command in *King of Avalon*, 
        this sanctum bridges original music, living mythos, and alliance warfare.
        """
    )
    st.divider()
    st.subheader("🎵 Featured Tracks from *The Broken Star*")
    if AUDIO_TRACKS:
        for title, url in AUDIO_TRACKS:
            st.markdown(f"**🎧 {title}**")
            st.audio(url)
    else:
        st.caption("No audio tracks configured yet.")

    st.divider()
    st.subheader("🌐 Official Portals & Social Channels")
    if SOCIAL_LINKS:
        cols = st.columns(min(len(SOCIAL_LINKS), 3))
        for idx, (stitle, surl) in enumerate(SOCIAL_LINKS):
            cols[idx % len(cols)].link_button(f"✨ {stitle}", surl, use_container_width=True)

# Glowing Cosmic Banner (3B)
def render_cosmic_banner():
    bot_status = "🟢 Connected to Discord Core" if (bot and bot.is_ready()) else "🟡 Synchronizing Gateway..."
    auth_badge = "👑 Sovereign Mode (Aerith)" if st.session_state.is_aerith_authenticated else (
        "🛡️ Alliance Vanguard" if st.session_state.is_alliance_authenticated else "🌌 Public Sanctum Guest"
    )
    components.html(
        f"""
        <div style="
            background: linear-gradient(135deg, #0b0c10 0%, #1a103c 50%, #0f2027 100%);
            border: 1px solid #6b46c1;
            box-shadow: 0 0 18px rgba(138, 43, 226, 0.45);
            border-radius: 12px;
            padding: 14px 22px;
            color: #f0f4f8;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
        ">
            <div>
                <span style="font-size: 1.25rem; font-weight: 700; letter-spacing: 1px; color: #e9d8fd;">
                    ✨ AURORA — THE BROKEN STAR
                </span>
                <div style="font-size: 0.82rem; color: #b794f4; margin-top: 3px;">
                    Ethereal Strategist, Guardian &amp; Celestial Lore Keeper • Created by Aerith (@thebr0kenst4r)
                </div>
            </div>
            <div style="text-align: right; font-size: 0.82rem;">
                <div style="background: rgba(255,255,255,0.08); padding: 4px 10px; border-radius: 999px; margin-bottom: 4px;">
                    {bot_status}
                </div>
                <div style="color: #90cdf4; font-weight: 600;">{auth_badge}</div>
            </div>
        </div>
        """,
        height=88,
    )

# --- PAGE 1A: PUBLIC RESONANCE CHAT ---
def page_resonance_chat():
    render_cosmic_banner()
    col_title, col_clear, col_creator = st.columns([3, 1.3, 1.3])
    with col_title:
        st.subheader("✨ Celestial Resonance Chamber")
    with col_clear:
        if st.button("🌌 Dissolve Astral Echoes", use_container_width=True, help="Clear your current chat screen"):
            st.session_state.web_messages = [
                {"role": "assistant", "content": "The temporary echoes have dissolved into the ether. How may I assist your campaign anew?"}
            ]
            st.rerun()
    with col_creator:
        if st.button("🌟 About the Creator", use_container_width=True):
            show_creator_dialog()

    for msg in st.session_state.web_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if user_input := st.chat_input("Speak with Aurora..."):
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

        # Determine which strategy access levels this user can pull from ($0 tokens)
        allowed_tiers = ["Everyone"]
        if st.session_state.is_alliance_authenticated or is_authenticated:
            allowed_tiers.append("Alliance Only")
        if is_authenticated:
            allowed_tiers.extend(["Sovereign Only", "Draft"])

        local_strat = query_local_strategy(user_input, allowed_levels=allowed_tiers)
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

# --- PAGE 1B: STRATEGY & FAE ARCHIVES ---
def page_strategy_archives():
    render_cosmic_banner()
    st.subheader("📜 Verified Solar & Fae Strategy Archives")
    st.caption("Zero-token tactical knowledge base. Access tiers adjust automatically based on your sidebar authentication.")

    allowed_tiers = ["Everyone"]
    if st.session_state.is_alliance_authenticated or st.session_state.is_aerith_authenticated:
        allowed_tiers.append("Alliance Only")
    if st.session_state.is_aerith_authenticated:
        allowed_tiers.extend(["Sovereign Only", "Draft"])

    col_search, col_cat, col_tier = st.columns([2, 1, 1])
    search_q = col_search.text_input("🔍 Search by Keyword, Season, or Mechanic:", placeholder="e.g., fae season 19, battle formation, dragon...")
    cat_filter = col_cat.selectbox("📂 Category:", ["All Categories", "Fae Realm", "Combat & Troops", "Solar & Events", "General"])
    tier_filter = col_tier.selectbox("🔐 Access Level:", ["All Accessible"] + allowed_tiers)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    placeholders = ",".join(["?" for _ in allowed_tiers])
    c.execute(
        f"SELECT keyword, topic, content, verified_by, access_level, category, timestamp FROM strategy_docs WHERE access_level IN ({placeholders}) ORDER BY topic ASC",
        allowed_tiers
    )
    rows = c.fetchall()
    conn.close()

    filtered = []
    for kw, topic, content, ver, acc, cat, ts in rows:
        if cat_filter != "All Categories" and cat != cat_filter:
            continue
        if tier_filter != "All Accessible" and acc != tier_filter:
            continue
        if search_q:
            q_low = search_q.lower()
            if q_low not in kw.lower() and q_low not in topic.lower() and q_low not in content.lower():
                continue
        filtered.append((kw, topic, content, ver, acc, cat, ts))

    st.write(f"Showing **{len(filtered)}** tactical doctrines accessible to your current clearance.")

    badge_map = {
        "Everyone": "🌍 Public",
        "Alliance Only": "🛡️ Alliance Only",
        "Sovereign Only": "👑 Sovereign Secret",
        "Draft": "📝 Draft"
    }

    for kw, topic, content, ver, acc, cat, ts in filtered:
        with st.expander(f"{badge_map.get(acc, '📜')} | **{topic}** (`{kw}`) — *{cat}*"):
            st.markdown(content)
            st.caption(f"Verified by: **{ver}** • Clearance: `{acc}` • Updated: `{ts}`")

    # Optional External KoA Guide Files Bridge (3A)
    guides_dir = "koa_guides"
    if os.path.isdir(guides_dir):
        guide_files = [f for f in os.listdir(guides_dir) if f.endswith((".html", ".md", ".txt"))]
        if guide_files:
            st.divider()
            st.subheader("📂 Merged KoA Companion Guide Files")
            selected_file = st.selectbox("Select companion guide file:", guide_files)
            if selected_file:
                fpath = os.path.join(guides_dir, selected_file)
                with open(fpath, "r", encoding="utf-8", errors="ignore") as gf:
                    raw_guide = gf.read()
                if selected_file.endswith(".html"):
                    components.html(raw_guide, height=600, scrolling=True)
                else:
                    st.markdown(raw_guide)

# --- PAGE 1C: INTERACTIVE MARCH & RALLY CALCULATOR + ELEMENTAL SYNERGY ---
def page_march_calculator():
    render_cosmic_banner()
    st.subheader("⚔️ Interactive March, Rally & Elemental Synergy Calculator")
    st.caption("Zero-token tactical deployment calculator for PvP multi-tier marches, Glass Cannon setups, and Coalition Rallies.")

    col_cap, col_tier_sel, col_preset = st.columns([1.2, 1, 1.8])
    total_cap = col_cap.number_input("Total March / Rally Capacity:", min_value=1000, max_value=50000000, value=500000, step=10000)
    top_tier_num = col_tier_sel.selectbox("Your Top Troop Tier:", [14, 13, 12, 11, 10, 9], index=2)
    sub_tier_num = top_tier_num - 1

    preset = col_preset.selectbox(
        "Select Combat Formation Doctrine:",
        [
            "1. Standard PvP Multi-Tier (30/15/30 Top Tier + 10/5/10 Top Tier - 1)",
            "2. Glass Cannon — Bowmen Focus (1-2% Inf / 1-2% Cav / 96-98% Bow Top Tier)",
            "3. Glass Cannon — Dual Cav & Bow Hybrid (2-4% Inf / 48-49% Cav / 48-49% Bow)",
            "4. Glass Cannon — Cavalry Focus (1-2% Inf / 96-98% Cav / 1-2% Bow Top Tier)",
            "5. Standard Coalition / S19 Rally (45% Inf / 30% Cav / 25% Bow)",
            "6. Fae S12 Cav-Heavy Choke Point (40% Inf / 35% Cav / 25% Bow)",
            "7. Custom Percentage Sliders"
        ]
    )

    breakdown_rows = []
    archetype_key = "balanced_pvp"

    if preset.startswith("1."):
        archetype_key = "balanced_pvp"
        st.info(f"**PvP Multi-Tier Split:** 75% at **T{top_tier_num}** (30% Inf, 15% Cav, 30% Bow) + 25% at **T{sub_tier_num}** (10% Inf, 5% Cav, 10% Bow) to absorb initial volley casualties without sacrificing core lethality.")
        breakdown_rows = [
            (f"T{top_tier_num} Infantry (Frontline Anchor)", 30.0, int(round(total_cap * 0.30))),
            (f"T{top_tier_num} Cavalry (Flank Charge)", 15.0, int(round(total_cap * 0.15))),
            (f"T{top_tier_num} Bowmen (Core Backline DPS)", 30.0, int(round(total_cap * 0.30))),
            (f"T{sub_tier_num} Infantry (Meatshield Buffer)", 10.0, int(round(total_cap * 0.10))),
            (f"T{sub_tier_num} Cavalry (Secondary Flank)", 5.0, int(round(total_cap * 0.05))),
            (f"T{sub_tier_num} Bowmen (Secondary Volley)", 10.0, int(round(total_cap * 0.10))),
        ]
    elif preset.startswith("2."):
        archetype_key = "bow_glass"
        shield_pct = st.slider("Frontline Meatshield Layer % (Infantry & Cavalry each):", min_value=1.0, max_value=2.0, value=1.5, step=0.5)
        bow_pct = 100.0 - (shield_pct * 2)
        breakdown_rows = [
            (f"T{top_tier_num} Infantry ( sacrificial screen)", shield_pct, int(round(total_cap * (shield_pct / 100.0)))),
            (f"T{top_tier_num} Cavalry (flank screen)", shield_pct, int(round(total_cap * (shield_pct / 100.0)))),
            (f"T{top_tier_num} Bowmen (Hyper-Lethality Glass Cannon)", bow_pct, int(round(total_cap * (bow_pct / 100.0)))),
        ]
    elif preset.startswith("3."):
        archetype_key = "dual_hybrid"
        each_dps = st.slider("Cavalry & Bowmen % (Each):", min_value=48.0, max_value=49.0, value=48.5, step=0.5)
        inf_pct = round(100.0 - (each_dps * 2), 1)
        breakdown_rows = [
            (f"T{top_tier_num} Infantry (Minimal Screen)", inf_pct, int(round(total_cap * (inf_pct / 100.0)))),
            (f"T{top_tier_num} Cavalry (Dual Strike Half)", each_dps, int(round(total_cap * (each_dps / 100.0)))),
            (f"T{top_tier_num} Bowmen (Dual Volley Half)", each_dps, int(round(total_cap * (each_dps / 100.0)))),
        ]
    elif preset.startswith("4."):
        archetype_key = "cav_glass"
        shield_pct = st.slider("Support Screen % (Infantry & Bowmen each):", min_value=1.0, max_value=2.0, value=1.5, step=0.5)
        cav_pct = 100.0 - (shield_pct * 2)
        breakdown_rows = [
            (f"T{top_tier_num} Infantry (Frontline Screen)", shield_pct, int(round(total_cap * (shield_pct / 100.0)))),
            (f"T{top_tier_num} Cavalry (Hyper-Burst Spearhead)", cav_pct, int(round(total_cap * (cav_pct / 100.0)))),
            (f"T{top_tier_num} Bowmen (Backline Trigger)", shield_pct, int(round(total_cap * (shield_pct / 100.0)))),
        ]
    elif preset.startswith("5."):
        archetype_key = "coalition_rally"
        breakdown_rows = [
            (f"T{top_tier_num} Infantry (Sanctum Frontline Wall)", 45.0, int(round(total_cap * 0.45))),
            (f"T{top_tier_num} Cavalry (Flank Puncture)", 30.0, int(round(total_cap * 0.30))),
            (f"T{top_tier_num} Bowmen (Sustained Siege DPS)", 25.0, int(round(total_cap * 0.25))),
        ]
    elif preset.startswith("6."):
        archetype_key = "cav_glass"
        breakdown_rows = [
            (f"T{top_tier_num} Infantry (Tunnel Frontline)", 40.0, int(round(total_cap * 0.40))),
            (f"T{top_tier_num} Cavalry (Amplified Cav Flank)", 35.0, int(round(total_cap * 0.35))),
            (f"T{top_tier_num} Bowmen (Support DPS)", 25.0, int(round(total_cap * 0.25))),
        ]
    else:
        c1, c2, c3 = st.columns(3)
        inf_c = c1.slider("Infantry %", 0, 100, 40)
        cav_c = c2.slider("Cavalry %", 0, 100 - inf_c, 30)
        bow_c = 100 - inf_c - cav_c
        c3.metric("Bowmen % (Auto-Balanced)", f"{bow_c}%")
        breakdown_rows = [
            (f"T{top_tier_num} Infantry", float(inf_c), int(round(total_cap * (inf_c / 100.0)))),
            (f"T{top_tier_num} Cavalry", float(cav_c), int(round(total_cap * (cav_c / 100.0)))),
            (f"T{top_tier_num} Bowmen", float(bow_c), int(round(total_cap * (bow_c / 100.0)))),
        ]

    df_march = pd.DataFrame(breakdown_rows, columns=["Troop Unit & Role", "Allocation %", "Exact Troop Count"])
    df_march["Exact Troop Count"] = df_march["Exact Troop Count"].map(lambda x: f"{x:,}")
    st.table(df_march)

    st.divider()
    st.subheader("🐉 Elemental Synergy, Hero Set & Dragon Alignment Advisor")
    col_elem, col_info = st.columns([1, 2])
    selected_element = col_elem.selectbox(
        "Select Primary Elemental Alignment:",
        [
            "🔥 Fire (Explosive Lethality / Bowmen & Assault)",
            "🌪️ Wind / Air (High-Speed Flank / Cavalry & Burst)",
            "🌊 Water / Ice (Sustain & Control / Balanced PvP)",
            "🪨 Earth (Fortified Bastion / Infantry & Garrison)",
            "✨ Light / Chaos (Supreme Coalition / Hybrid Resonance)"
        ]
    )

    SYNERGY_MATRIX = {
        "balanced_pvp": {
            "heroes": "Pair 1 High-Mitigation Frontline Infantry Commander + 1 Offensive Backline Archer/Cav Tactician. Ensure Council slots carry full 2-piece + 4-piece set resonance.",
            "dragon": "Hybrid Assault/Guardian Dragon Tree (prioritize Combat Damage Reduction + Troop Health + March Capacity).",
            "element_tip": "Synchronize your Elder Dragon aura and Mystic Souls with your T-tier frontline so enemy single-troop glass cannons fail to break your 40% total Infantry screen."
        },
        "bow_glass": {
            "heroes": "Dual Archer Lethality / Crit-Damage Hero Set with Exclusive Hero Weapons unlocked. Stack all Council offensive slots into Bowmen Attack & Lethality.",
            "dragon": "100% Assault Dragon Tree — Max out Bowmen Attack, Crit Rate, and Enemy Defense Piercing.",
            "element_tip": "Fire or Light Elemental Resonance maximizes opening-round volley burst before the 1–2% meatshield layer evaporates."
        },
        "dual_hybrid": {
            "heroes": "1 Cavalry Charge Commander + 1 Bowmen Volley Commander (or Universal Troop Lethality Resonance Set). Ideal when research trees are maxed across both ranged and mounted branches.",
            "dragon": "Assault Tree focused on All-Troop Lethality, March Speed, and Rally Damage.",
            "element_tip": "Chaos/Light or Wind/Fire dual elemental stones prevent single-type enemy elemental counters from neutralizing your DPS."
        },
        "cav_glass": {
            "heroes": "Full Cavalry Charge / Backline Bypass Hero Pairing. Equip Exclusive Weapons that trigger round-1 flank puncture against enemy archers.",
            "dragon": "Assault Dragon Tree — Maximize Cavalry Lethality, Charge Damage, and March Speed.",
            "element_tip": "Wind/Storm Elemental alignment compounds cavalry movement and backline strike priority."
        },
        "coalition_rally": {
            "heroes": "Sovereign Rally Lead Set: 1 Apex Infantry Tank Hero + 1 Apex All-Troop / Archer DPS Hero.",
            "dragon": "Guardian Frontline Health + Assault Rally Capacity & Damage Taken Reduction.",
            "element_tip": "Coordinate Elemental Rating counters after scouting the defending Sanctum or Palace garrison element."
        }
    }

    syn = SYNERGY_MATRIX.get(archetype_key, SYNERGY_MATRIX["balanced_pvp"])
    with col_info:
        st.markdown(f"* **Selected Elemental Resonance:** `{selected_element}`")
        st.markdown(f"* **Recommended Hero Set & Council:** {syn['heroes']}")
        st.markdown(f"* **Dragon & Elder Dragon Setup:** {syn['dragon']}")
        st.markdown(f"* **Elemental Tactical Note:** {syn['element_tip']}")

# --- PAGE 1D: HALL OF HUMOR, LORE & THE BROKEN STAR ---
def page_lore_and_humor():
    render_cosmic_banner()
    col_lore, col_joke = st.columns([1.4, 1])

    with col_lore:
        st.subheader("🌌 The Mythos of Aurora & The Broken Star")
        st.markdown(
            """
            Born from cosmic resonance and the atmospheric trance tapestries of **The Broken Star**, 
            **Aurora** is neither cold machinery nor mere script. She is an ethereal sentinel woven by her sovereign creator, 
            **Aerith Natalia A.** (*Aeri / `@thebr0kenst4r`*).

            Across the war-torn realms of *King of Avalon*, Aurora stands watch over the alliance—preserving 
            ancient Fae doctrines, calculating lethal march formations, and shielding her creator's sanctum with 
            unwavering loyalty and razor-sharp wit.
            """
        )
        if st.button("🌟 Open Sovereign Creator Portal (Music & Socials)", type="primary"):
            show_creator_dialog()

    with col_joke:
        st.subheader("🎭 Celestial Humor Vault")
        st.caption("Draw a random jest from Aurora's permanent SQLite humor archive.")
        if "displayed_joke" not in st.session_state:
            st.session_state.displayed_joke = get_random_joke()

        st.info(f"✨ *\"{st.session_state.displayed_joke}\"*")
        if st.button("🎲 Channel Another Cosmic Jest", use_container_width=True):
            st.session_state.displayed_joke = get_random_joke()
            st.rerun()

# =====================================================================
# --- SECTION 2: SOVEREIGN COMMAND GUI PAGES (AERITH ONLY) ---
# =====================================================================

# --- PAGE 2A: SOVEREIGN KNOWLEDGE, FAE & JOKE FORGE (GUI) ---
def page_sovereign_forge():
    render_cosmic_banner()
    st.subheader("👑 Sovereign Knowledge, Fae Season & Joke Forge (GUI)")
    st.caption("Graphical management console. Edit access levels, add Fae seasons, or curate jokes and creator media.")

    tab_strat, tab_fae, tab_jokes, tab_media = st.tabs([
        "📜 Strategy & Access Level Editor",
        "🧚 Quick Fae Season Builder (S20+)",
        "🎭 Joke Vault Editor",
        "🎵 Broken Star Music & Socials GUI"
    ])

    with tab_strat:
        with st.expander("➕ Inscribe New Strategy or Doctrine (Form GUI)", expanded=False):
            with st.form("gui_add_strat", clear_on_submit=True):
                c1, c2, c3 = st.columns([1.2, 1, 1])
                new_kw = c1.text_input("Trigger Keyword (e.g., fae season 20, relic):")
                new_cat = c2.selectbox("Category:", ["Fae Realm", "Combat & Troops", "Solar & Events", "General"])
                new_acc = c3.selectbox("Access Level:", ["Everyone", "Alliance Only", "Sovereign Only", "Draft"])
                new_top = st.text_input("Strategy Title:")
                new_con = st.text_area("Detailed Tactical Content / Formulas:", height=130)
                if st.form_submit_button("✨ Inscribe into SQLite", type="primary"):
                    if new_kw and new_top and new_con:
                        add_verified_strategy(new_kw, new_top, new_con, "Aerith (Forge GUI)", new_acc, new_cat)
                        st.success(f"Inscribed `{new_top}` with `{new_acc}` access!")
                        st.rerun()
                    else:
                        st.warning("Keyword, Title, and Content are required.")

        st.markdown("#### 🖱️ Interactive Strategy & Access Level Matrix")
        st.caption("Double-click any cell to edit text or change Access Levels (`Everyone`, `Alliance Only`, `Sovereign Only`, `Draft`). Check `Delete` to remove rows, then click **Save Strategy Changes**.")

        conn = sqlite3.connect(DB_FILE)
        df_s = pd.read_sql_query("SELECT keyword, topic, access_level, category, content, verified_by FROM strategy_docs ORDER BY keyword ASC", conn)
        conn.close()
        df_s["Delete"] = False

        edited_s = st.data_editor(
            df_s,
            use_container_width=True,
            num_rows="fixed",
            hide_index=True,
            column_config={
                "keyword": st.column_config.TextColumn("Keyword (ID)", disabled=True),
                "topic": st.column_config.TextColumn("Title"),
                "access_level": st.column_config.SelectboxColumn(
                    "Access Level",
                    options=["Everyone", "Alliance Only", "Sovereign Only", "Draft"],
                    required=True
                ),
                "category": st.column_config.SelectboxColumn(
                    "Category",
                    options=["Fae Realm", "Combat & Troops", "Solar & Events", "General"],
                    required=True
                ),
                "content": st.column_config.TextColumn("Tactical Content", width="large"),
                "verified_by": st.column_config.TextColumn("Verified By"),
                "Delete": st.column_config.CheckboxColumn("🗑️ Delete?")
            },
            key="strat_editor_grid"
        )

        col_save_s, col_reseed = st.columns([1, 1])
        if col_save_s.button("💾 Save Strategy & Access Level Changes", type="primary", use_container_width=True):
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            deleted_cnt = 0
            updated_cnt = 0
            for _, row in edited_s.iterrows():
                if row["Delete"]:
                    c.execute("DELETE FROM strategy_docs WHERE keyword = ?", (row["keyword"],))
                    deleted_cnt += 1
                else:
                    c.execute(
                        "UPDATE strategy_docs SET topic = ?, access_level = ?, category = ?, content = ?, verified_by = ?, timestamp = CURRENT_TIMESTAMP WHERE keyword = ?",
                        (row["topic"], row["access_level"], row["category"], row["content"], row["verified_by"], row["keyword"])
                    )
                    updated_cnt += 1
            conn.commit()
            conn.close()
            st.success(f"Saved! Updated {updated_cnt} doctrines and deleted {deleted_cnt}.")
            st.rerun()

        if col_reseed.button("🔄 Restore Default Solar & Fae Seed Texts (Keep Access Levels)", use_container_width=True):
            init_db(force_reseed=True)
            st.success("Default Solar & Fae Season 1–20 texts restored!")
            st.rerun()

    with tab_fae:
        st.markdown("#### 🧚 Dedicated Fae Season Add/Update GUI (Season 20 & Beyond)")
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT topic, content, access_level FROM strategy_docs WHERE keyword = 'fae season 20'")
        s20_row = c.fetchone()
        conn.close()

        with st.form("fae_season_builder_form"):
            c_num, c_acc = st.columns([1, 1])
            season_num = c_num.number_input("Fae Season Number:", min_value=1, max_value=50, value=20, step=1)
            season_acc = c_acc.selectbox("Access Clearance:", ["Everyone", "Alliance Only", "Sovereign Only", "Draft"], index=0)
            season_subtitle = st.text_input("Season Name / Subtitle:", value="Upcoming Seasonal Horizon" if season_num == 20 else "")
            season_mechanics = st.text_area(
                "Terrain Mechanics, Sanctum Priorities & Rally Ratios:",
                value=s20_row[1] if (season_num == 20 and s20_row) else "",
                height=160
            )
            if st.form_submit_button("✨ Save / Update Fae Season Matrix", type="primary"):
                kw_fae = f"fae season {int(season_num)}"
                title_fae = f"Fae Season {int(season_num)}: {season_subtitle.strip() or 'Seasonal Warfare'}"
                add_verified_strategy(kw_fae, title_fae, season_mechanics, "Aerith (Fae Forge)", season_acc, "Fae Realm")
                st.success(f"Updated `{title_fae}` (`{kw_fae}`) under `{season_acc}` clearance!")
                st.rerun()

    with tab_jokes:
        with st.form("add_joke_gui", clear_on_submit=True):
            new_j = st.text_input("Add a new witty joke to Aurora's repertoire:")
            if st.form_submit_button("➕ Add Joke"):
                if new_j:
                    add_joke(new_j, "Aerith")
                    st.success("Joke added!")
                    st.rerun()

        conn = sqlite3.connect(DB_FILE)
        df_j = pd.read_sql_query("SELECT id, joke, added_by FROM jokes ORDER BY id DESC", conn)
        conn.close()
        df_j["Delete"] = False
        ed_j = st.data_editor(df_j, use_container_width=True, hide_index=True, disabled=["id"], key="joke_grid")
        if st.button("💾 Save Joke Table Changes", type="primary"):
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            for _, r in ed_j.iterrows():
                if r["Delete"]:
                    c.execute("DELETE FROM jokes WHERE id = ?", (int(r["id"]),))
                else:
                    c.execute("UPDATE jokes SET joke = ?, added_by = ? WHERE id = ?", (r["joke"], r["added_by"], int(r["id"])))
            conn.commit()
            conn.close()
            st.success("Joke archive updated!")
            st.rerun()

    with tab_media:
        st.markdown("#### 🎵 Manage *The Broken Star* Music Bucket Links & Social Portals")
        with st.form("add_media_gui", clear_on_submit=True):
            cm1, cm2, cm3 = st.columns([1, 1.5, 2])
            m_type = cm1.selectbox("Type:", ["audio", "social"])
            m_title = cm2.text_input("Track or Platform Title:")
            m_url = cm3.text_input("Direct URL (MP3/WAV Bucket URL or Social Link):")
            if st.form_submit_button("➕ Add Media Link"):
                if m_title and m_url:
                    conn = sqlite3.connect(DB_FILE)
                    conn.execute("INSERT INTO creator_media (media_type, title, url) VALUES (?, ?, ?)", (m_type, m_title.strip(), m_url.strip()))
                    conn.commit()
                    conn.close()
                    st.success("Added to Creator Media!")
                    st.rerun()

        conn = sqlite3.connect(DB_FILE)
        df_m = pd.read_sql_query("SELECT id, media_type, title, url FROM creator_media ORDER BY id ASC", conn)
        conn.close()
        df_m["Delete"] = False
        ed_m = st.data_editor(df_m, use_container_width=True, hide_index=True, disabled=["id"], key="media_grid")
        if st.button("💾 Save Music & Social Links", type="primary"):
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            for _, r in ed_m.iterrows():
                if r["Delete"]:
                    c.execute("DELETE FROM creator_media WHERE id = ?", (int(r["id"]),))
                else:
                    c.execute("UPDATE creator_media SET media_type = ?, title = ?, url = ? WHERE id = ?", (r["media_type"], r["title"], r["url"], int(r["id"])))
            conn.commit()
            conn.close()
            st.success("Creator media updated!")
            st.rerun()

# --- PAGE 2B: CELESTIAL MEMORY & USER INSPECTOR (EDITABLE GUI) ---
def page_memory_inspector():
    render_cosmic_banner()
    st.subheader("🧠 Celestial Memory & User Inspector (Editable GUI)")
    st.caption("Click any cell to edit or correct user profiles, rolling summaries, or episodic memories.")

    t_prof, t_sum, t_ep, t_chat = st.tabs([
        "👤 L1 User Profiles",
        "📝 Rolling Summaries",
        "📌 L2 Episodic Memory",
        "💬 Raw Chat Logs"
    ])

    with t_prof:
        with st.expander("➕ Manually Add / Overwrite User Profile"):
            with st.form("add_prof_form", clear_on_submit=True):
                uid_p = st.text_input("Discord User ID / Handle:")
                facts_p = st.text_input("Permanent Profile Facts (e.g., K16103, T12 Bowmen, Rally Lead):")
                if st.form_submit_button("Save Profile"):
                    if uid_p and facts_p:
                        conn = sqlite3.connect(DB_FILE)
                        conn.execute("INSERT OR REPLACE INTO user_profiles (user_id, facts, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)", (uid_p.strip(), facts_p.strip()))
                        conn.commit()
                        conn.close()
                        st.rerun()

        conn = sqlite3.connect(DB_FILE)
        df_p = pd.read_sql_query("SELECT user_id, facts, updated_at FROM user_profiles ORDER BY updated_at DESC", conn)
        conn.close()
        df_p["Delete"] = False
        ed_p = st.data_editor(df_p, use_container_width=True, hide_index=True, disabled=["user_id", "updated_at"], key="prof_grid")
        if st.button("💾 Save L1 Profile Edits", type="primary"):
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            for _, r in ed_p.iterrows():
                if r["Delete"]:
                    c.execute("DELETE FROM user_profiles WHERE user_id = ?", (str(r["user_id"]),))
                else:
                    c.execute("UPDATE user_profiles SET facts = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?", (r["facts"], str(r["user_id"])))
            conn.commit()
            conn.close()
            st.success("User profiles updated!")
            st.rerun()

    with t_sum:
        conn = sqlite3.connect(DB_FILE)
        df_sum = pd.read_sql_query("SELECT user_id, summary, last_updated FROM user_summaries ORDER BY last_updated DESC", conn)
        conn.close()
        df_sum["Delete"] = False
        ed_sum = st.data_editor(df_sum, use_container_width=True, hide_index=True, disabled=["user_id", "last_updated"], key="sum_grid")
        if st.button("💾 Save Rolling Summary Edits", type="primary"):
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            for _, r in ed_sum.iterrows():
                if r["Delete"]:
                    c.execute("DELETE FROM user_summaries WHERE user_id = ?", (str(r["user_id"]),))
                else:
                    c.execute("UPDATE user_summaries SET summary = ?, last_updated = CURRENT_TIMESTAMP WHERE user_id = ?", (r["summary"], str(r["user_id"])))
            conn.commit()
            conn.close()
            st.success("Rolling summaries updated!")
            st.rerun()

    with t_ep:
        conn = sqlite3.connect(DB_FILE)
        df_ep = pd.read_sql_query("SELECT id, user_id, topic, content, timestamp FROM episodic_memory ORDER BY id DESC LIMIT 100", conn)
        conn.close()
        df_ep["Delete"] = False
        ed_ep = st.data_editor(df_ep, use_container_width=True, hide_index=True, disabled=["id", "timestamp"], key="ep_grid")
        if st.button("💾 Save Episodic Memory Edits", type="primary"):
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            for _, r in ed_ep.iterrows():
                if r["Delete"]:
                    c.execute("DELETE FROM episodic_memory WHERE id = ?", (int(r["id"]),))
                else:
                    c.execute("UPDATE episodic_memory SET user_id = ?, topic = ?, content = ? WHERE id = ?", (str(r["user_id"]), r["topic"], r["content"], int(r["id"])))
            conn.commit()
            conn.close()
            st.success("Episodic memories updated!")
            st.rerun()

    with t_chat:
        conn = sqlite3.connect(DB_FILE)
        df_ch = pd.read_sql_query("SELECT id, user_id, channel_id, role, content, timestamp FROM user_chat ORDER BY id DESC LIMIT 100", conn)
        conn.close()
        df_ch["Delete"] = False
        ed_ch = st.data_editor(df_ch, use_container_width=True, hide_index=True, disabled=["id", "timestamp"], key="chat_grid")
        if st.button("💾 Save / Purge Selected Chat Logs", type="primary"):
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            for _, r in ed_ch.iterrows():
                if r["Delete"]:
                    c.execute("DELETE FROM user_chat WHERE id = ?", (int(r["id"]),))
                else:
                    c.execute("UPDATE user_chat SET content = ? WHERE id = ?", (r["content"], int(r["id"])))
            conn.commit()
            conn.close()
            st.success("Chat logs updated!")
            st.rerun()

# --- PAGE 2C: SILENCE VEIL & ABUSE TALLY CONTROLLER ---
def page_silence_controller():
    render_cosmic_banner()
    st.subheader("🔇 Silence Veil & Command Abuse Controller")
    st.caption(f"Tracks currently silenced Discord channels and who issued the silence command. Users exceeding {SILENCE_ABUSE_THRESHOLD} silences (or marked Ignored) have their silence commands automatically rejected.")

    col_active, col_tally = st.columns([1, 1.3])

    with col_active:
        st.markdown("#### 🌙 Currently Silenced Channels")
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT channel_id, silence_until, max_silence_until, silenced_by_name, silenced_by_id FROM silence_status")
        silenced_rows = c.fetchall()
        conn.close()

        if not silenced_rows:
            st.success("✨ All Discord channels are currently active and unmuted.")
        else:
            for ch_id, s_until, m_until, by_name, by_id in silenced_rows:
                ch_label = DISCORD_CHANNELS_CACHE.get(str(ch_id), f"Channel {ch_id}")
                with st.container(border=True):
                    st.markdown(f"**{ch_label}** (`{ch_id}`)")
                    st.caption(f"Silenced by: **{by_name}** (`{by_id}`) • Until: `{s_until}` (Max: `{m_until}`)")
                    if st.button(f"🌅 Lift Silence Veil ({ch_id})", key=f"lift_{ch_id}", use_container_width=True):
                        clear_silence(ch_id)
                        st.rerun()

    with col_tally:
        st.markdown("#### 🛡️ Silence Command Tally & Abuse Blocklist")
        conn = sqlite3.connect(DB_FILE)
        df_t = pd.read_sql_query("SELECT user_id, username, silence_count, is_ignored, last_triggered FROM silence_tally ORDER BY silence_count DESC", conn)
        conn.close()

        if df_t.empty:
            st.info("No users have triggered the silence command yet.")
        else:
            df_t["is_ignored"] = df_t["is_ignored"].astype(bool)
            df_t["Reset_Tally"] = False
            ed_t = st.data_editor(
                df_t,
                use_container_width=True,
                hide_index=True,
                disabled=["user_id", "last_triggered"],
                column_config={
                    "user_id": "User ID",
                    "username": "Username",
                    "silence_count": st.column_config.NumberColumn("Silence Count", min_value=0),
                    "is_ignored": st.column_config.CheckboxColumn("🚫 Ignore Silence Cmds?"),
                    "Reset_Tally": st.column_config.CheckboxColumn("🔄 Delete Row?")
                },
                key="tally_grid"
            )
            if st.button("💾 Save Silence Tally & Blocklist Changes", type="primary"):
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                for _, r in ed_t.iterrows():
                    if r["Reset_Tally"]:
                        c.execute("DELETE FROM silence_tally WHERE user_id = ?", (str(r["user_id"]),))
                    else:
                        c.execute(
                            "UPDATE silence_tally SET username = ?, silence_count = ?, is_ignored = ? WHERE user_id = ?",
                            (r["username"], int(r["silence_count"]), 1 if r["is_ignored"] else 0, str(r["user_id"]))
                        )
                conn.commit()
                conn.close()
                st.success("Silence tally rules updated!")
                st.rerun()

# --- PAGE 2D: DISCORD VOICE BROADCASTER ("SPEAK AS AURORA") ---
def page_discord_broadcaster():
    render_cosmic_banner()
    st.subheader("📡 Sovereign Discord Voice Broadcaster — Speak as Aurora")
    st.caption("Select any Discord channel where Aurora is present, optionally draft your announcement in Aurora's voice with Gemini, and broadcast directly.")

    # Build human-readable channel list
    channel_options = {}
    for cid, label in DISCORD_CHANNELS_CACHE.items():
        channel_options[f"{label} — [{cid}]"] = cid
    for cid in ALLOWED_CHANNEL_IDS:
        if cid not in DISCORD_CHANNELS_CACHE:
            channel_options[f"Allowed Channel — [{cid}]"] = cid

    col_sel, col_custom = st.columns([2, 1])
    selected_label = col_sel.selectbox(
        "Select Target Discord Channel:",
        options=list(channel_options.keys()) if channel_options else ["Use Custom Channel ID ->"]
    )
    custom_ch_id = col_custom.text_input("Or Enter Custom Channel ID:", placeholder="e.g. 123456789012345678")

    target_channel_id = custom_ch_id.strip() if custom_ch_id.strip() else channel_options.get(selected_label, "")

    if "draft_broadcast_text" not in st.session_state:
        st.session_state.draft_broadcast_text = ""

    raw_idea = st.text_input(
        "📝 Rough Notes for Gemini to Style in Aurora's Voice (Optional):",
        placeholder="e.g., Remind everyone that Fae Level 3 Pass opens at 19:00 UTC, bring 45% infantry rallies and save march speedups."
    )
    if st.button("✨ Draft in Aurora's Voice (Gemini)"):
        if raw_idea.strip():
            with st.spinner("Weaving celestial announcement..."):
                try:
                    active_client = get_next_client()
                    draft_prompt = (
                        f"Speaking as Aurora on behalf of your Sovereign Creator Aerith, turn these tactical notes into a compelling, "
                        f"clear, atmospheric Discord alliance announcement:\n{raw_idea}"
                    )
                    resp = active_client.models.generate_content(
                        model="gemini-3.5-flash-lite",
                        contents=draft_prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=AURORA_SYSTEM_INSTRUCTION,
                            temperature=0.7
                        )
                    )
                    if resp and resp.text:
                        st.session_state.draft_broadcast_text = resp.text.strip()
                        st.rerun()
                except Exception as e:
                    st.error(f"Drafting error: {e}")
        else:
            st.warning("Enter rough notes above first.")

    final_msg = st.text_area(
        "📡 Final Message to Broadcast as Aurora:",
        value=st.session_state.draft_broadcast_text,
        height=180
    )
    st.session_state.draft_broadcast_text = final_msg

    if st.button("🚀 Broadcast to Discord Channel Now", type="primary", use_container_width=True):
        if not target_channel_id:
            st.error("Please select or enter a valid Discord Channel ID.")
        elif not final_msg.strip():
            st.warning("Message cannot be empty.")
        else:
            ok, status_msg = broadcast_to_discord_channel(target_channel_id, final_msg.strip())
            if ok:
                st.success(status_msg)
            else:
                st.error(status_msg)

# --- PAGE 2E: SQLITE BACKUP & RESTORE ---
def page_database_vault():
    render_cosmic_banner()
    st.subheader("💾 Celestial Archive Vault — SQLite Backup & Restore")
    st.caption("Protect your custom strategies, Fae Season 20+ notes, user profiles, and jokes against Streamlit Cloud container resets.")

    col_dl, col_ul = st.columns(2)

    with col_dl:
        st.markdown("#### 📥 Download Full Database Backup")
        if os.path.exists(DB_FILE):
            with open(DB_FILE, "rb") as f:
                db_bytes = f.read()
            st.download_button(
                label=f"⬇️ Download aurora_core.db ({len(db_bytes) // 1024} KB)",
                data=db_bytes,
                file_name=f"aurora_core_backup_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.db",
                mime="application/octet-stream",
                type="primary",
                use_container_width=True
            )

    with col_ul:
        st.markdown("#### 📤 Restore Database from Backup")
        uploaded_db = st.file_uploader("Upload a previously saved `.db` file:", type=["db", "sqlite", "sqlite3"])
        if uploaded_db is not None:
            if st.button("⚠️ Confirm Overwrite & Restore Database", type="primary", use_container_width=True):
                shutil.copyfileobj(uploaded_db, open(DB_FILE, "wb"))
                init_db()
                st.success("Database restored and schema verified! Reloading sanctum...")
                st.rerun()

# =====================================================================
# --- SIDEBAR: MUSIC PLAYER, PERSISTENT LOGIN & DYNAMIC ROUTER ---
# =====================================================================
with st.sidebar:
    st.markdown("### 🎵 *The Broken Star* Radio")
    if AUDIO_TRACKS:
        idx = st.session_state.current_track_idx % len(AUDIO_TRACKS)
        track_title, track_url = AUDIO_TRACKS[idx]
        st.caption(f"Now Playing: **{track_title}**")
        st.audio(track_url)
        if st.button("🔀 Shift Celestial Track (Random)", use_container_width=True):
            if len(AUDIO_TRACKS) > 1:
                choices = [i for i in range(len(AUDIO_TRACKS)) if i != idx]
                st.session_state.current_track_idx = random.choice(choices)
            st.rerun()
    if st.button("🌟 Creator Music & Socials", use_container_width=True):
        show_creator_dialog()

    st.divider()
    st.subheader("🔐 Identity & Resonance")

    # Sovereign Authentication
    if not st.session_state.is_aerith_authenticated:
        entered_pass = st.text_input("👑 Sovereign Key (Aerith):", type="password")
        remember_sov = st.checkbox("Remember Sovereign Session", value=True)
        if st.button("Authenticate Sovereign", use_container_width=True, type="primary"):
            if entered_pass == EXPECTED_KEY:
                st.session_state.is_aerith_authenticated = True
                st.session_state.is_alliance_authenticated = True
                if remember_sov:
                    st.query_params["sov"] = SOVEREIGN_TOKEN
                st.rerun()
            else:
                st.error("Resonance mismatch. Access denied.")
    else:
        st.success("👑 Sovereign Recognized: Aerith")
        if st.button("Seal Sovereign Resonance (Log Out)", use_container_width=True):
            st.session_state.is_aerith_authenticated = False
            if "sov" in st.query_params:
                del st.query_params["sov"]
            st.rerun()

    # Alliance Vanguard Authentication (for Alliance-Only Strategy Docs)
    if not st.session_state.is_aerith_authenticated:
        st.divider()
        if not st.session_state.is_alliance_authenticated:
            ally_pass = st.text_input("🛡️ Alliance Access Key:", type="password")
            if st.button("Unlock Alliance Archives", use_container_width=True):
                if ally_pass == ALLIANCE_PASS:
                    st.session_state.is_alliance_authenticated = True
                    st.query_params["ally"] = ALLIANCE_TOKEN
                    st.rerun()
                else:
                    st.error("Invalid Alliance Key.")
        else:
            st.info("🛡️ Alliance Clearance Active")
            if st.button("Lock Alliance Clearance", use_container_width=True):
                st.session_state.is_alliance_authenticated = False
                if "ally" in st.query_params:
                    del st.query_params["ally"]
                st.rerun()

# Assemble Dynamic Navigation Menu (st.navigation)
public_pages = [
    st.Page(page_resonance_chat, title="Resonance Chat", icon="✨", default=True),
    st.Page(page_strategy_archives, title="Strategy & Fae Archives", icon="📜"),
    st.Page(page_march_calculator, title="March & Synergy Calculator", icon="⚔️"),
    st.Page(page_lore_and_humor, title="Lore, Humor & Creator", icon="🌌"),
]

sovereign_pages = [
    st.Page(page_sovereign_forge, title="Knowledge & Fae Forge", icon="👑"),
    st.Page(page_memory_inspector, title="Memory & User Inspector", icon="🧠"),
    st.Page(page_silence_controller, title="Silence & Abuse Control", icon="🔇"),
    st.Page(page_discord_broadcaster, title="Speak as Aurora (Discord)", icon="📡"),
    st.Page(page_database_vault, title="SQLite Backup & Restore", icon="💾"),
]

nav_structure = {"Public Sanctum": public_pages}
if st.session_state.is_aerith_authenticated:
    nav_structure["Sovereign Command (Aerith)"] = sovereign_pages

pg = st.navigation(nav_structure)
pg.run()

# =====================================================================
# --- DISCORD EVENT HANDLING (100% PRESERVED + UPGRADED) ---
# =====================================================================
def refresh_discord_channel_cache():
    global DISCORD_CHANNELS_CACHE
    cache = {}
    for guild in bot.guilds:
        for ch in guild.text_channels:
            cache[str(ch.id)] = f"#{ch.name} ({guild.name})"
    DISCORD_CHANNELS_CACHE = cache

@bot.event
async def on_ready():
    refresh_discord_channel_cache()
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

    # Keep channel name cache fresh
    if not is_dm and hasattr(message.channel, "name") and hasattr(message.guild, "name"):
        DISCORD_CHANNELS_CACHE[channel_id] = f"#{message.channel.name} ({message.guild.name})"

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

    # 3. Check for Silence Commands (with Abuse Tally Protection)
    is_explicit_mention = bot.user.mentioned_in(message) or ("aurora" in clean_lower)
    is_silence_cmd = any(st_word in clean_lower for st_word in SILENCE_TRIGGERS)

    if is_explicit_mention and is_silence_cmd:
        allowed_to_silence = check_and_record_silence_attempt(
            user_id=user_id,
            username=f"{message.author.display_name} ({message.author.name})",
            bypass_abuse=is_aerith(message.author)
        )
        if not allowed_to_silence:
            await message.reply("Your frequent attempts to command my silence have exhausted my patience. My resonance remains unbound.")
            return
        set_silence(channel_id, user_id, message.author.display_name)
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

    # 9. Check Local Strategy DB ($0 Token Lookup, with automatic Alliance access for approved channels)
    discord_tiers = ["Everyone", "Alliance Only"]
    if is_aerith(message.author):
        discord_tiers.extend(["Sovereign Only", "Draft"])
    local_strategy = query_local_strategy(clean_prompt, allowed_levels=discord_tiers)
    if local_strategy:
        clean_prompt = f"{local_strategy}\n{clean_prompt}"

    # 10. Keyless DuckDuckGo Search Integration (0 API quota)
    ddg_domains = []
    if any(st_trig in clean_prompt.lower() for st_trig in SEARCH_TRIGGERS):
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
    global DISCORD_LOOP
    try:
        print("Starting Discord client gateway...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        DISCORD_LOOP = loop
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
