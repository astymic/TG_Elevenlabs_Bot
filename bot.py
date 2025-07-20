import asyncio
import traceback
from pathlib import Path
import re

import aiofiles
import httpx
from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.bot import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, Audio, Voice, Document, Video, ReplyParameters
from loguru import logger
from pydub import AudioSegment

import config
import db

# --- Setup ---
logger.add(config.LOGS_PATH / "bot.log", rotation="10 MB", compression="zip", level="INFO")
config.setup_directories()

if config.API_SERVER_URL:
    # If a local server URL is specified, create a custom session
    session = AiohttpSession(api_url=config.API_SERVER_URL)
    bot = Bot(token=config.BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode="Markdown"))
else:
    # Otherwise, use the default session for the cloud Telegram API
    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))

dp = Dispatcher()

# --- User-facing Messages (in Russian) ---
MSG_ASK_PASSWORD = "🔐 Введите пароль для доступа к боту."
MSG_WELCOME = """Я голосовой бот, преобразующий ваш голос.

Отправьте мне аудиофайл, голосовое сообщение или видео **(до 2 ГБ)**.
Скорость обработки зависит от длины отправленого аудио."""
MSG_SERVER_ERROR = "Произошла ошибка на сервере. Пожалуйста, повторите попытку позже."

# --- ACCELERATED Audio Functions using FFMPEG ---

async def split_audio_with_ffmpeg(file_path: Path) -> list[Path]:
    """
    Accelerated audio splitting using ffmpeg.
    It first detects silences, then splits the audio based on them.
    """
    MAX_DURATION_S = 295  # 5 minutes with a safety margin, in seconds
    SILENCE_DURATION_S = 0.7  # Minimum silence duration to be detected
    SILENCE_THRESHOLD = "-25dB" # dB level to consider as silence

    # Step 1: Get file duration using ffprobe
    ffprobe_process = await asyncio.create_subprocess_exec(
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(file_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await ffprobe_process.communicate()
    try:
        duration = float(stdout.strip())
        if duration <= MAX_DURATION_S:
            return [] # No splitting needed
    except ValueError:
        raise IOError("Could not determine file duration using ffprobe.")

    # Step 2: Detect silences using ffmpeg's silencedetect filter
    silence_detect_cmd = [
        'ffmpeg', '-i', str(file_path), '-af', f'silencedetect=noise={SILENCE_THRESHOLD}:d={SILENCE_DURATION_S}', 
        '-f', 'null', '-'
    ]
    process = await asyncio.create_subprocess_exec(
        *silence_detect_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    stderr_str = stderr.decode('utf-8', errors='ignore')
    
    # Parse ffmpeg output to find silence end timestamps
    silence_end_re = re.compile(r'silence_end: (\d+\.?\d*)')
    silence_ends = [float(x) for x in silence_end_re.findall(stderr_str)]

    # Step 3: Plan the splits based on detected silences
    chunk_paths = []
    current_pos = 0.0
    chunk_counter = 1
    
    while (duration - current_pos) > MAX_DURATION_S:
        split_at = current_pos + MAX_DURATION_S
        
        # Find the last silence before the target split point for a clean cut
        best_split_point = None
        for end_time in silence_ends:
            if current_pos < end_time < split_at:
                best_split_point = end_time
        
        if best_split_point:
            split_at = best_split_point

        chunk_path = file_path.parent / f"{file_path.stem}_chunk_{chunk_counter}.wav"
        
        # Step 4: Split the chunk using ffmpeg 
        split_cmd = [
            'ffmpeg', '-i', str(file_path), '-ss', str(current_pos), '-to', str(split_at), 
            '-c', 'copy', str(chunk_path)
        ]
        split_process = await asyncio.create_subprocess_exec(*split_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await split_process.wait()
        
        if chunk_path.exists():
            chunk_paths.append(chunk_path)
        
        current_pos = split_at
        chunk_counter += 1

    # Add the last remaining chunk
    if current_pos < duration:
        last_chunk_path = file_path.parent / f"{file_path.stem}_chunk_{chunk_counter}.wav"
        split_cmd = [
            'ffmpeg', '-i', str(file_path), '-ss', str(current_pos),
            '-c', 'copy', str(last_chunk_path)
        ]
        split_process = await asyncio.create_subprocess_exec(*split_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await split_process.wait()
        if last_chunk_path.exists():
            chunk_paths.append(last_chunk_path)
            
    return chunk_paths


async def merge_audio_chunks(chunk_paths: list[Path], output_path: Path) -> Path:
    """Merges audio chunks into a single file (pydub is kept for its simplicity here)."""
    loop = asyncio.get_event_loop()
    
    def do_merge():
        combined = AudioSegment.empty()
        for path in chunk_paths:
            sound = AudioSegment.from_file(path)
            combined += sound
        combined.export(output_path, format="wav")

    return await loop.run_in_executor(None, do_merge)

# --- Middleware and Handlers ---
class AuthMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: types.Update, data: dict):
        if not event.message: return await handler(event, data)
        message = event.message
        if message.text and (message.text.startswith(('/start', '/logout')) or message.text == config.BOT_PASSWORD):
            return await handler(event, data)
        if not db.is_user_authorized(message.from_user.id):
            await message.answer(MSG_ASK_PASSWORD); return
        return await handler(event, data)

@dp.message(CommandStart())
async def handle_start(message: Message):
    if db.is_user_authorized(message.from_user.id): await message.answer(MSG_WELCOME)
    else: await message.answer(MSG_ASK_PASSWORD)

@dp.message(Command("logout"))
async def handle_logout(message: Message):
    db.remove_user(message.from_user.id); await message.answer("Вы успешно вышли из системы.")

@dp.message(F.text == config.BOT_PASSWORD)
async def handle_password(message: Message):
    if not db.is_user_authorized(message.from_user.id):
        db.add_user(message.from_user.id)
        await message.answer("✅ Пароль верный! Доступ предоставлен."); await message.answer(MSG_WELCOME)
    else: await message.answer("Вы уже авторизованы.")


# --- Main File Handler ---
@dp.message(F.audio | F.voice | F.document | F.video)
async def handle_files(message: Message):
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

        # 2. Extract audio if the source is a video
        if isinstance(file_to_process, Video) or (isinstance(file_to_process, Document) and "video" in file_to_process.mime_type):
            await status_msg.edit_text("Извлечение аудиодорожки из видео...")
            source_audio_path = config.STORAGE_PATH / f"{download_path.stem}.wav"
            
            process = await asyncio.create_subprocess_exec(
                'ffmpeg', '-i', str(download_path), '-vn', '-acodec', 'pcm_s16le', '-ar', '48000', '-ac', '2', str(source_audio_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await process.wait()
            # The downloaded video is a temp file, add it to the cleanup list
            temp_files_to_delete.append(download_path)
        else:
            source_audio_path = download_path # The original file is already audio

        # 3. Split audio into chunks
        await status_msg.edit_text("Анализ аудио и разделение на части...")
        original_chunks_paths = await split_audio_with_ffmpeg(source_audio_path)
        
        if original_chunks_paths:
            temp_files_to_delete.extend(original_chunks_paths)
            # If the file was split, the original full-length audio is also temporary
            if source_audio_path.exists():
                temp_files_to_delete.append(source_audio_path)
            chunks_to_process = original_chunks_paths
        else:
            # If the file was not split, we process it as a single chunk
            chunks_to_process = [source_audio_path]

        # 4. Process each chunk
        processed_chunks_paths = []
        total_chunks = len(chunks_to_process)
        for i, chunk_path in enumerate(chunks_to_process):
            await status_msg.edit_text(f"Обработка части {i+1}/{total_chunks}...")
            
            api_data = {"output_format": "pcm_48000"}
            async with httpx.AsyncClient(timeout=600.0) as client:
                with open(chunk_path, "rb") as f_audio:
                    response = await client.post(
                        config.ELEVEN_API_URL, headers={"xi-api-key": config.ELEVEN_API_KEY}, 
                        files={"audio": f_audio}, data=api_data
                    )
            
            if response.status_code != 200:
                raise Exception(f"ElevenLabs API error on chunk {i+1}: {response.json().get('detail', {}).get('message', 'Unknown error')}")

            processed_chunk_path = chunk_path.with_name(f"{chunk_path.stem}_processed.wav")
            async with aiofiles.open(processed_chunk_path, 'wb') as f: await f.write(response.content)
            
            processed_chunks_paths.append(processed_chunk_path)
            # Processed chunks are also temporary
            temp_files_to_delete.append(processed_chunk_path)
            
        # 5. Merge processed chunks into a final file
        await status_msg.edit_text(f"Сборка финального файла из {len(processed_chunks_paths)} частей...")
        final_filename = f"{Path(getattr(file_to_process, 'file_name', 'media')).stem}_voice_changed.wav"
        final_path = config.STORAGE_PATH / final_filename
        await merge_audio_chunks(processed_chunks_paths, final_path)

        # 6. Send the result
        await status_msg.edit_text("Отправка финального файла...")
        await message.answer_document(
            types.FSInputFile(final_path, filename=final_filename),
            reply_parameters=ReplyParameters(message_id=message.message_id)
        )
        await status_msg.edit_text("Готово: голос преобразован.")
        logger.info(f"Successfully processed file for user {message.from_user.id}")

    except Exception as e:
        logger.error(f"An error occurred for user {message.from_user.id}:\n{traceback.format_exc()}")
        await status_msg.edit_text(MSG_SERVER_ERROR)
        await bot.send_message(
            config.DEV_TG_ID, f"❗️ **An error occurred in the bot**\n\n**User:** `{message.from_user.id}`\n**Traceback:**\n```\n{traceback.format_exc()}\n```", parse_mode="Markdown"
        )
    finally:
        # 7. Final cleanup of all temporary files from the list
        logger.info(f"Cleaning up {len(temp_files_to_delete)} temporary files.")
        for path in temp_files_to_delete:
            if path.exists():
                try: path.unlink()
                except OSError as e: logger.warning(f"Could not delete temp file {path}: {e}")

async def main():
    db.init_db()
    dp.update.middleware(AuthMiddleware())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    logger.info("Bot is starting...")
    asyncio.run(main())