import asyncio
import traceback
from pathlib import Path

import aiofiles
import httpx
# ИЗМЕНЕНИЕ 1: Добавлен импорт BaseMiddleware
from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, Audio, Voice, Document, ReplyParameters
from loguru import logger

import config
import db

# --- Setup ---
logger.add(config.LOGS_PATH / "bot.log", rotation="10 MB", compression="zip", level="INFO")
config.setup_directories()

if config.API_SERVER_URL:
    session = AiohttpSession(api=Bot(token=config.BOT_TOKEN, server=config.API_SERVER_URL).session.api)
    bot = Bot(token=config.BOT_TOKEN, session=session)
else:
    bot = Bot(token=config.BOT_TOKEN)

dp = Dispatcher()

# --- Текстовые сообщения ---
MSG_ASK_PASSWORD = "🔐 Введите пароль для доступа к боту."
MSG_WELCOME = """Я голосовой бот, преобразующий ваш голос с помощью ElevenLabs Speech-to-Speech API.

Отправьте мне аудиофайл (форматы: mp3, wav, ogg и др., размер до 50 MB).
Вы можете отправить несколько файлов."""
MSG_SERVER_ERROR = "Произошла ошибка на сервере. Пожалуйста, повторите попытку позже."

# --- Middleware для проверки авторизации ---
# ИЗМЕНЕНИЕ 2: Заменен базовый класс
class AuthMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: types.Update, data: dict):
        # Проверяем, что обновление содержит сообщение
        if not event.message:
            return await handler(event, data)
        
        message = event.message

        # Пропускаем команды и ввод пароля для неавторизованных пользователей
        if message.text:
            if message.text.startswith(('/start', '/logout')) or message.text == config.BOT_PASSWORD:
                return await handler(event, data)

        if not db.is_user_authorized(message.from_user.id):
            await message.answer(MSG_ASK_PASSWORD)
            return

        return await handler(event, data)

# --- Обработчики команд и пароля ---
@dp.message(CommandStart())
async def handle_start(message: Message):
    if db.is_user_authorized(message.from_user.id):
        await message.answer(MSG_WELCOME)
    else:
        await message.answer(MSG_ASK_PASSWORD)

@dp.message(Command("logout"))
async def handle_logout(message: Message):
    db.remove_user(message.from_user.id)
    await message.answer("Вы успешно вышли из системы. Для повторного доступа снова введите пароль.")
    logger.info(f"User {message.from_user.id} logged out.")

@dp.message(F.text == config.BOT_PASSWORD)
async def handle_password(message: Message):
    if not db.is_user_authorized(message.from_user.id):
        db.add_user(message.from_user.id)
        logger.info(f"New user authorized: {message.from_user.id}")
        await message.answer("✅ Пароль верный! Доступ предоставлен.")
        await message.answer(MSG_WELCOME)
    else:
        await message.answer("Вы уже авторизованы.")

# --- Основной обработчик аудиофайлов ---
@dp.message(F.audio | F.voice | F.document)
async def handle_audio(message: Message):
    file_to_process = message.audio or message.voice or (message.document if message.document.mime_type and "audio" in message.document.mime_type else None)
    if not file_to_process:
        await message.reply("Пожалуйста, отправьте аудиофайл (голосовое сообщение, аудиофайл или документ с аудио).")
        return

    status_msg = await message.reply("Проверка файла...", disable_notification=True)

    try:
        # 1. Check file size
        if file_to_process.file_size > config.ELEVEN_FILE_SIZE_LIMIT:
            error_text = f"Ошибка: Файл слишком большой ({file_to_process.file_size / 1e6:.2f} MB). Лимит — 50 MB."
            await status_msg.edit_text(error_text)
            logger.warning(f"User {message.from_user.id} sent a file larger than the limit.")
            return

        # 2. Download the file to the server
        await status_msg.edit_text("Загрузка файла на сервер...")
        filename = getattr(file_to_process, 'file_name', 'audio.ogg')
        original_path = config.STORAGE_PATH / f"{file_to_process.file_unique_id}_{filename}"
        await bot.download(file_to_process, destination=original_path)

        # 3. Send to ElevenLabs
        await status_msg.edit_text("Отправка в ElevenLabs...")
        
        api_params = {
            "output_format": "pcm_48000"
        }
        
        async with httpx.AsyncClient(timeout=300.0) as client:
            with open(original_path, "rb") as f_audio:
                response = await client.post(
                    config.ELEVEN_API_URL, 
                    headers={"xi-api-key": config.ELEVEN_API_KEY}, 
                    files={"audio": f_audio},
                    data=api_params
                )
        
        # 4. Process the response
        await status_msg.edit_text("Ожидание ответа...")
        if response.status_code != 200:
            error_details = response.json().get("detail", {}).get("message", response.text)
            await status_msg.edit_text(f"Ошибка от ElevenLabs: {error_details}")
            logger.error(f"ElevenLabs API error: {response.status_code} - {response.text}")
            return
            
        await status_msg.edit_text("Получение результата...")
        
        # 5. Save and rename the result
        p = Path(original_path.name)
        
        base_name = p.stem.replace(f"{file_to_process.file_unique_id}_", "")
        new_filename = f"{base_name}_voice_changed.wav"
        processed_path = config.STORAGE_PATH / new_filename
        
        async with aiofiles.open(processed_path, 'wb') as f:
            await f.write(response.content)

        # 6. Send the result to the user
        await status_msg.edit_text("Отправка результата...")
        
        await message.answer_document(
            types.FSInputFile(processed_path, filename=new_filename),
            reply_parameters=ReplyParameters(message_id=message.message_id)
        )
        
        await status_msg.edit_text("Готово: голос преобразован.")
        logger.info(f"Successfully processed file for user {message.from_user.id}: {original_path.name}")

    except Exception as e:
        logger.error(f"An error occurred for user {message.from_user.id}:\n{traceback.format_exc()}")
        await status_msg.edit_text(MSG_SERVER_ERROR)
        # Send the full traceback to the developer
        await bot.send_message(
            config.DEV_TG_ID,
            f"❗️ **Произошла ошибка в боте**\n\n"
            f"**User:** `{message.from_user.id}`\n"
            f"**Traceback:**\n```\n{traceback.format_exc()}\n```",
            parse_mode="Markdown"
        )
    # Files are not deleted immediately; they will be removed after 24 hours by cleanup.py

async def main():
    db.init_db()
    # Регистрируем middleware для всех обновлений
    dp.update.middleware(AuthMiddleware())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    logger.info("Bot is starting...")
    asyncio.run(main())