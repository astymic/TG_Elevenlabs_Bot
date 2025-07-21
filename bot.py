import asyncio
import json
import re
import traceback
import wave
from pathlib import Path
from typing import Dict

import aiofiles
import httpx
from aiogram import BaseMiddleware, Bot, Dispatcher, F, types
from aiogram.client.bot import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Audio, Document, KeyboardButton, Message,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove,
                           ReplyParameters, Video, Voice)
from loguru import logger
from pydub import AudioSegment

import config
import db


# ==================================================================================================
# II. INITIALIZATION AND SETUP
# ==================================================================================================
# Configure logger to write to a file with rotation
logger.add(config.LOGS_PATH / "bot.log", rotation="10 MB", compression="zip", level="INFO")

# Ensure all necessary directories exist before starting
config.setup_directories()

# Initialize the Bot instance, using a local Bot API server if configured
if config.API_SERVER_URL:
    telegram_api_server = TelegramAPIServer.from_base(config.API_SERVER_URL, is_local=True)
    session = AiohttpSession(api=telegram_api_server)
    bot = Bot(token=config.BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode="Markdown"))
else:
    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))

# Initialize the Dispatcher
dp = Dispatcher()


# ==================================================================================================
# III. FSM & KEYBOARDS
# ==================================================================================================
# Define states for the Text-to-Speech (TTS) feature
class TtsStates(StatesGroup):
    waiting_for_text = State()

# Define reply keyboards for the user interface
main_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="TTS")]], resize_keyboard=True)
cancel_tts_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отменить")]], resize_keyboard=True)


# ==================================================================================================
# IV. IN-MEMORY STORAGE FOR TTS
# ==================================================================================================
# These dictionaries are used to temporarily store user input for multi-message TTS.
# They are not persistent and will be cleared on bot restart.
user_texts: Dict[int, str] = {}
user_timers: Dict[int, asyncio.Task] = {}


# ==================================================================================================
# V. USER-FACING STRINGS (in Russian)
# ==================================================================================================
MSG_ASK_PASSWORD = "🔐 Введите пароль для доступа к боту."
MSG_WELCOME = """Я голосовой бот, синтезирую или преобразующий ваш голос.

- Отправьте аудио, голосовое сообщение или видео для изменения голоса (до 2ГБ).
- Нажмите кнопку 'TTS' для синтеза речи из текста."""
MSG_SERVER_ERROR = "Произошла ошибка на сервере. Пожалуйста, повторите попытку позже."
MSG_TTS_PROMPT = "Отправьте мне текст для озвучки (до 5000 символов)."
MSG_TTS_CANCELLED = "Действие отменено. Вы снова в обычном режиме."
MSG_TTS_PROCESSING = "Текст получен. Начинаю синтез речи..."
MSG_TTS_LIMIT_EXCEEDED = "Ошибка: Суммарная длина текста превышает 5000 символов."


# ==================================================================================================
# VI. CORE HELPER FUNCTIONS (AUDIO PROCESSING)
# ==================================================================================================
async def split_audio_with_ffmpeg(file_path: Path) -> list[Path]:
    """
    Splits a long audio file into chunks of max ~5 minutes using ffmpeg for high performance.
    It first detects silences and then uses them as preferred split points.
    Returns a list of paths to the created chunk files. If splitting is not needed, returns an empty list.
    """
    MAX_DURATION_S = 295
    SILENCE_DURATION_S = 0.7
    SILENCE_THRESHOLD = "-25dB"

    ffprobe_process = await asyncio.create_subprocess_exec(
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(file_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await ffprobe_process.communicate()
    try:
        duration = float(stdout.strip())
        if duration <= MAX_DURATION_S:
            return []
    except ValueError:
        raise IOError("Could not determine file duration using ffprobe.")

    silence_detect_cmd = ['ffmpeg', '-i', str(file_path), '-af', f'silencedetect=noise={SILENCE_THRESHOLD}:d={SILENCE_DURATION_S}', '-f', 'null', '-']
    process = await asyncio.create_subprocess_exec(*silence_detect_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, stderr = await process.communicate()
    stderr_str = stderr.decode('utf-8', errors='ignore')
    
    silence_end_re = re.compile(r'silence_end: (\d+\.?\d*)')
    silence_ends = [float(x) for x in silence_end_re.findall(stderr_str)]

    chunk_paths = []
    current_pos = 0.0
    chunk_counter = 1
    
    while (duration - current_pos) > MAX_DURATION_S:
        split_at = current_pos + MAX_DURATION_S
        best_split_point = None
        for end_time in silence_ends:
            if current_pos < end_time < split_at:
                best_split_point = end_time
        if best_split_point:
            split_at = best_split_point

        chunk_path = file_path.parent / f"{file_path.stem}_chunk_{chunk_counter}.wav"
        split_cmd = ['ffmpeg', '-i', str(file_path), '-ss', str(current_pos), '-to', str(split_at), '-c', 'copy', str(chunk_path)]
        split_process = await asyncio.create_subprocess_exec(*split_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await split_process.wait()
        if chunk_path.exists():
            chunk_paths.append(chunk_path)
        
        current_pos = split_at
        chunk_counter += 1

    if current_pos < duration:
        last_chunk_path = file_path.parent / f"{file_path.stem}_chunk_{chunk_counter}.wav"
        split_cmd = ['ffmpeg', '-i', str(file_path), '-ss', str(current_pos), '-c', 'copy', str(last_chunk_path)]
        split_process = await asyncio.create_subprocess_exec(*split_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await split_process.wait()
        if last_chunk_path.exists():
            chunk_paths.append(last_chunk_path)
            
    return chunk_paths


async def merge_audio_chunks(chunk_paths: list[Path], output_path: Path) -> Path:
    """Merges multiple audio chunks into a single audio file using pydub."""
    loop = asyncio.get_event_loop()
    
    def do_merge():
        combined = AudioSegment.empty()
        for path in chunk_paths:
            sound = AudioSegment.from_file(path)
            combined += sound
        combined.export(output_path, format="wav")

    return await loop.run_in_executor(None, do_merge)


def save_pcm_to_wav(output_path: Path, pcm_data: bytes):
    """Correctly saves raw PCM data to a WAV file by adding a valid header."""
    with wave.open(str(output_path), 'wb') as wf:
        # These parameters are for the pcm_48000 format from ElevenLabs
        wf.setnchannels(1)      # 1 = Mono
        wf.setsampwidth(2)      # 2 bytes = 16-bit audio
        wf.setframerate(48000)  # 48kHz sample rate
        wf.writeframes(pcm_data)


# ==================================================================================================
# VII. MIDDLEWARE
# ==================================================================================================
class AuthMiddleware(BaseMiddleware):
    """
    A custom middleware to check if the user is authorized before processing any update.
    It ignores commands like /start, /logout, and password entry for unauthorized users.
    """
    async def __call__(self, handler, event: types.Update, data: dict):
        if not event.message:
            return await handler(event, data)
        
        message = event.message
        if message.text and (message.text.startswith(('/start', '/logout')) or message.text == config.BOT_PASSWORD):
            return await handler(event, data)

        if not db.is_user_authorized(message.from_user.id):
            await message.answer(MSG_ASK_PASSWORD, reply_markup=ReplyKeyboardRemove())
            return
        
        return await handler(event, data)


# ==================================================================================================
# VIII. HANDLERS
# ==================================================================================================

# --------------------------------------------------------------------------------------------------
# A. Basic Command Handlers
# --------------------------------------------------------------------------------------------------
@dp.message(CommandStart())
async def handle_start(message: Message):
    """Handles the /start command, showing the welcome message or asking for a password."""
    if db.is_user_authorized(message.from_user.id):
        await message.answer(MSG_WELCOME, reply_markup=main_kb)
    else:
        await message.answer(MSG_ASK_PASSWORD, reply_markup=ReplyKeyboardRemove())


@dp.message(Command("logout"))
async def handle_logout(message: Message, state: FSMContext):
    """Handles the /logout command, clearing user state and authorization."""
    await state.clear()
    db.remove_user(message.from_user.id)
    await message.answer("Вы успешно вышли из системы.", reply_markup=ReplyKeyboardRemove())


@dp.message(F.text == config.BOT_PASSWORD)
async def handle_password(message: Message):
    """Handles password entry, granting access on correct password."""
    if not db.is_user_authorized(message.from_user.id):
        db.add_user(message.from_user.id)
        await message.answer("✅ Пароль верный! Доступ предоставлен.")
        await message.answer(MSG_WELCOME, reply_markup=main_kb)
    else:
        await message.answer("Вы уже авторизованы.", reply_markup=main_kb)

# --------------------------------------------------------------------------------------------------
# B. Text-to-Speech (TTS) Workflow Handlers
# --------------------------------------------------------------------------------------------------
async def process_tts_request(message: Message, state: FSMContext):
    """
    This is the final step in the TTS workflow, triggered by a timer.
    It takes the collected text, sends it to the ElevenLabs API, and returns the audio.
    """
    user_id = message.from_user.id
    status_msg = await message.answer(MSG_TTS_PROCESSING)

    # Clean up timer, collected text, and FSM state
    if user_id in user_timers:
        user_timers.pop(user_id)
    full_text = user_texts.pop(user_id, "")
    await state.clear()

    if not full_text:
        return
    if len(full_text) > 5000:
        await status_msg.edit_text(MSG_TTS_LIMIT_EXCEEDED)
        await message.answer(MSG_WELCOME, reply_markup=main_kb)
        return
    
    # Prepare data for the ElevenLabs TTS API
    tts_url = f"https://api.elevenlabs.io/v1/text-to-speech/{config.ELEVEN_VOICE_ID}"
    api_data = {
        "text": full_text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.55,
            "similarity_boost": 0.75,
            "use_speaker_boost": True
        }
    }
    
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                tts_url, headers={"xi-api-key": config.ELEVEN_API_KEY}, json=api_data,
                params={"output_format": "pcm_48000"}
            )
        
        if response.status_code != 200:
            raise Exception(f"ElevenLabs TTS API Error: Status {response.status_code}, Response: {response.text[:500]}")
        
        # Save the received raw PCM data as a proper WAV file
        final_filename = f"tts_output_{message.message_id}.wav"
        final_path = config.STORAGE_PATH / final_filename
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, save_pcm_to_wav, final_path, response.content)

        # Send the final audio file as a document to preserve quality
        await message.answer_document(types.FSInputFile(final_path, filename=final_filename))
        await status_msg.delete()
        
    except Exception as e:
        logger.error(f"TTS processing failed for user {user_id}: {e}")
        await status_msg.edit_text(MSG_SERVER_ERROR)
    finally:
        await message.answer("Вы снова в обычном режиме.", reply_markup=main_kb)


@dp.message(StateFilter(TtsStates.waiting_for_text), F.text == "Отменить")
async def cancel_tts_handler(message: Message, state: FSMContext):
    """Allows the user to cancel the TTS process."""
    user_id = message.from_user.id
    # Cancel any pending timer and clear collected text
    if user_id in user_timers:
        user_timers[user_id].cancel()
        user_timers.pop(user_id)
    if user_id in user_texts:
        user_texts.pop(user_id)
    
    await state.clear()
    await message.answer(MSG_TTS_CANCELLED, reply_markup=main_kb)


@dp.message(F.text == "TTS", StateFilter(None))
async def start_tts_handler(message: Message, state: FSMContext):
    """Enters the TTS mode when the user clicks the 'TTS' button."""
    await state.set_state(TtsStates.waiting_for_text)
    await message.answer(MSG_TTS_PROMPT, reply_markup=cancel_tts_kb)


@dp.message(StateFilter(TtsStates.waiting_for_text), F.text)
async def tts_text_collector(message: Message, state: FSMContext):
    """
    Collects text from one or more messages.
    Uses a timer to wait for the user to finish sending all parts of the text.
    """
    user_id = message.from_user.id
    
    # Append the new text part
    user_texts[user_id] = user_texts.get(user_id, "") + message.text + "\n"
    
    # Cancel the previous timer if it exists
    if user_id in user_timers:
        user_timers[user_id].cancel()
    
    # Set a new timer. If no new message arrives within 2.5 seconds, start processing.
    task = asyncio.create_task(asyncio.sleep(2.5), name=f"tts_timer_{user_id}")
    task.add_done_callback(
        lambda _: asyncio.create_task(process_tts_request(message, state))
    )
    user_timers[user_id] = task

# --------------------------------------------------------------------------------------------------
# C. Speech-to-Speech (STS) Workflow Handler
# --------------------------------------------------------------------------------------------------
@dp.message(StateFilter(None), F.audio | F.voice | F.document | F.video)
async def handle_files(message: Message):
    """
    Handles all incoming media files (audio, voice, video) for the Speech-to-Speech feature.
    This handler only works when the bot is not in any specific state (e.g., waiting for TTS text).
    """
    status_msg = await message.reply("Получение файла...", disable_notification=True)
    temp_files_to_delete = []
    try:
        # 1. Identify and download the file
        file_to_process = message.audio or message.voice or message.video or \
                          (message.document if message.document.mime_type and ("audio" in message.document.mime_type or "video" in message.document.mime_type) else None)
        if not file_to_process:
            await status_msg.edit_text("Пожалуйста, отправьте аудио, голосовое сообщение или видео."); return
        
        await status_msg.edit_text("Загрузка файла на сервер...")
        download_path = config.STORAGE_PATH / f"{file_to_process.file_unique_id}_{getattr(file_to_process, 'file_name', 'media')}"
        await bot.download(file_to_process, destination=download_path)

        # 2. Extract audio from video if necessary
        if isinstance(file_to_process, Video) or (isinstance(file_to_process, Document) and "video" in file_to_process.mime_type):
            await status_msg.edit_text("Извлечение аудиодорожки из видео...")
            source_audio_path = config.STORAGE_PATH / f"{download_path.stem}.wav"
            process = await asyncio.create_subprocess_exec('ffmpeg', '-i', str(download_path), '-vn', '-acodec', 'pcm_s16le', '-ar', '48000', '-ac', '2', str(source_audio_path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await process.wait()
            temp_files_to_delete.append(download_path) # Mark original video for deletion
        else:
            source_audio_path = download_path

        # 3. Split the audio into manageable chunks
        await status_msg.edit_text("Анализ аудио и разделение на части...")
        original_chunks_paths = await split_audio_with_ffmpeg(source_audio_path)
        
        if original_chunks_paths:
            temp_files_to_delete.extend(original_chunks_paths)
            if source_audio_path.exists():
                temp_files_to_delete.append(source_audio_path) # Mark original full audio for deletion
            chunks_to_process = original_chunks_paths
        else:
            chunks_to_process = [source_audio_path]

        # 4. Process each chunk via ElevenLabs STS API
        processed_chunks_paths = []
        total_chunks = len(chunks_to_process)
        for i, chunk_path in enumerate(chunks_to_process):
            await status_msg.edit_text(f"Обработка части {i+1}/{total_chunks}...")
            api_data = {"output_format": "pcm_48000"}
            timeouts = httpx.Timeout(10.0, read=120.0, write=60.0, pool=300.0)
            async with httpx.AsyncClient(timeout=timeouts) as client:
                with open(chunk_path, "rb") as f_audio:
                    response = await client.post(config.ELEVEN_API_URL, headers={"xi-api-key": config.ELEVEN_API_KEY}, files={"audio": f_audio}, data=api_data)
            
            if response.status_code != 200:
                error_details = ""
                try: error_details = response.json().get("detail", {}).get("message", str(response.json()))
                except json.JSONDecodeError: error_details = response.text[:500]
                raise Exception(f"ElevenLabs STS API Error on chunk {i+1} (Status: {response.status_code}): {error_details}")
            
            processed_chunk_path = chunk_path.with_name(f"{chunk_path.stem}_processed.wav")
            async with aiofiles.open(processed_chunk_path, 'wb') as f:
                await f.write(response.content)
            processed_chunks_paths.append(processed_chunk_path)
            temp_files_to_delete.append(processed_chunk_path) # Mark processed chunk for deletion

        # 5. Merge processed chunks into a final audio file
        await status_msg.edit_text(f"Сборка финального файла из {len(processed_chunks_paths)} частей...")
        final_filename = f"{Path(getattr(file_to_process, 'file_name', 'media')).stem}_voice_changed.wav"
        final_path = config.STORAGE_PATH / final_filename
        await merge_audio_chunks(processed_chunks_paths, final_path)

        # 6. Send the final result to the user
        await status_msg.edit_text("Отправка финального файла...")
        await message.answer_document(types.FSInputFile(final_path, filename=final_filename), reply_parameters=ReplyParameters(message_id=message.message_id))
        await status_msg.edit_text("Готово: голос преобразован.")
        logger.info(f"Successfully processed STS file for user {message.from_user.id}")

    except Exception as e:
        # Gracefully handle any error, log it, and inform the developer
        error_traceback = traceback.format_exc()
        truncated_traceback = error_traceback[-2000:]
        error_text = f"❗️ **An error occurred in the bot**\n\n**User:** `{message.from_user.id}`\n**Traceback (last 2000 chars):**\n```\n{truncated_traceback}\n```"
        logger.error(f"An error occurred for user {message.from_user.id}:\n{error_traceback}")
        await status_msg.edit_text(MSG_SERVER_ERROR)
        await bot.send_message(config.DEV_TG_ID, error_text)
    finally:
        # Clean up all temporary files created during this request
        logger.info(f"Cleaning up {len(temp_files_to_delete)} temporary files.")
        for path in temp_files_to_delete:
            if path.exists():
                try:
                    path.unlink()
                except OSError as e:
                    logger.warning(f"Could not delete temp file {path}: {e}")


# ==================================================================================================
# IX. APPLICATION ENTRY POINT
# ==================================================================================================
async def main():
    """Initializes the database and starts the bot polling."""
    db.init_db()
    dp.update.middleware(AuthMiddleware())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    logger.info("Bot is starting...")
    asyncio.run(main())