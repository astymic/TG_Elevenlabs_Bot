import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from the .env file
# This allows storing secrets securely outside of the code
load_dotenv()

# --- Telegram Bot ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
# URL for your local Bot API server. If None, the default Telegram API will be used.
API_SERVER_URL = os.getenv("API_SERVER_URL")

# --- Access ---
BOT_PASSWORD = os.getenv("BOT_PASSWORD")
# Developer's Telegram ID to receive error tracebacks
DEV_TG_ID = int(os.getenv("DEV_TG_ID"))

# --- ElevenLabs ---
ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY")
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID")
# The final endpoint URL is constructed here using the voice_id
ELEVEN_API_URL = f"https://api.elevenlabs.io/v1/speech-to-speech/{ELEVEN_VOICE_ID}?remove_background_noise=true"

# --- Paths and Constants ---
BASE_DIR = Path(__file__).parent
# Directory for storing uploaded and generated audio files
STORAGE_PATH = BASE_DIR / "tmp_files"
# Directory for log files
LOGS_PATH = BASE_DIR / "logs"
# Directory for persistent data, e.g., the database
DB_STORAGE_PATH = BASE_DIR / "storage"
DB_PATH = DB_STORAGE_PATH / "bot_users.db"

# File lifetime in seconds (24 hours) for the cleanup script
FILE_LIFETIME_SECONDS = 24 * 60 * 60

def setup_directories():
    """Creates all necessary directories on startup."""
    STORAGE_PATH.mkdir(exist_ok=True)
    LOGS_PATH.mkdir(exist_ok=True)
    DB_STORAGE_PATH.mkdir(exist_ok=True)