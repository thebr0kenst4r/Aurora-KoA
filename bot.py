import os
import asyncio
import threading
import discord
from discord.ext import commands
import google.generativeai as genai
import streamlit as st

# --- STREAMLIT DUMMY FRONTEND ---
# Streamlit requires a web page rendering to keep the server alive.
st.title("Gemini Discord Bot Server")
st.write("Status: Online and running 24/7")

# --- BOT CONFIGURATION ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_KEY")

genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel("gemini-1.5-pro")

intents = discord.Intents.default()
intents.message_content = True  
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} and actively listening!")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel):
        clean_prompt = message.clean_content.replace(f"@{bot.user.name}", "").strip()
        if not clean_prompt:
            return

        async with message.channel.typing():
            try:
                response = model.generate_content(clean_prompt)
                reply_text = response.text

                if len(reply_text) <= 2000:
                    await message.reply(reply_text)
                else:
                    for i in range(0, len(reply_text), 2000):
                        await message.channel.send(reply_text[i:i+2000])
            except Exception as e:
                await message.reply(f"Error: {e}")

    await bot.process_commands(message)

# --- BACKGROUND WORKER RUNNER ---
# Streamlit runs in cycles; threading keeps the Discord loop alive forever.
def run_discord_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.start(DISCORD_TOKEN))

if "bot_started" not in st.session_state:
    st.session_state.bot_started = True
    threading.Thread(target=run_discord_bot, daemon=True).start()
  
