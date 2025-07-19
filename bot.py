import asyncio
import json
import logging
import os
import redis.asyncio as redis
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.client.default import DefaultBotProperties

# --- КОНФИГУРАЦИЯ ---
# Токен и URL для Redis будут браться из переменных окружения на хостинге
# Это безопасно и правильно
API_TOKEN = os.getenv('TELEGRAM_API_TOKEN')
REDIS_URL = os.getenv('REDIS_URL')

# --- НАСТРОЙКА ---
logging.basicConfig(level=logging.INFO)

# Проверяем, что переменные окружения заданы
if not API_TOKEN or not REDIS_URL:
    raise ValueError("Необходимо задать переменные окружения TELEGRAM_API_TOKEN и REDIS_URL")

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# Ключи, под которыми мы будем хранить данные в Redis
REDIS_DATA_KEY = "telegram_bot_data"
REDIS_OFFSET_KEY = "telegram_bot_offset"

# --- ФУНКЦИИ ДЛЯ РАБОТЫ С ХРАНИЛИЩЕМ (REDIS) ---

async def load_data():
    """Загружает данные (наш бывший JSON-файл) из Redis."""
    json_data = await redis_client.get(REDIS_DATA_KEY)
    if json_data:
        return json.loads(json_data)
    # Если данных нет, возвращаем пустую структуру по умолчанию
    return {"config": {}, "codes": {}}

async def save_data(data):
    """Сохраняет данные в Redis."""
    await redis_client.set(REDIS_DATA_KEY, json.dumps(data, indent=4, ensure_ascii=False))
    logging.info("Данные сохранены в Redis.")

# --- ОСНОВНАЯ ЛОГИКА, КОТОРАЯ ЗАПУСКАЕТСЯ ОДИН РАЗ ---

async def process_updates():
    logging.info("Начинаю проверку обновлений...")
    bot_data = await load_data()
    
    # Получаем ID последнего обработанного обновления, чтобы не проверять старые сообщения
    offset = await redis_client.get(REDIS_OFFSET_KEY)
    offset = int(offset) + 1 if offset else None

    try:
        updates = await bot.get_updates(offset=offset, timeout=20, allowed_updates=["message"])
    except Exception as e:
        logging.error(f"Не удалось получить обновления от Telegram: {e}")
        return

    if not updates:
        logging.info("Новых сообщений нет.")
        return

    logging.info(f"Получено {len(updates)} новых обновлений.")
    
    data_changed = False # Флаг, чтобы сохранять данные только если были изменения

    # Перебираем все новые сообщения
    for update in updates:
        if not update.message or not update.message.text:
            continue
            
        message = update.message
        text = message.text
        
        # --- Логика команды /set_topic ---
        if text.startswith('/set_topic'):
            try:
                member = await bot.get_chat_member(message.chat.id, message.from_user.id)
                if member.status not in ["administrator", "creator"]:
                    await message.reply("Эту команду может использовать только администратор.")
                    continue
            except Exception:
                await message.reply("Я должен быть администратором в этом чате, чтобы проверить ваши права.")
                continue

            if not message.is_topic_message:
                await message.reply("Эту команду нужно использовать внутри темы (топика).")
                continue

            bot_data["config"]["target_chat_id"] = message.chat.id
            bot_data["config"]["target_thread_id"] = message.message_thread_id
            logging.info(f"Бот привязан к чату {message.chat.id} и теме {message.message_thread_id}")
            await message.reply("✅ Отлично! Теперь я буду работать только в этой теме.")
            data_changed = True
            continue

        # --- Проверяем, что сообщение пришло из нужной темы ---
        config = bot_data.get("config", {})
        target_chat_id = config.get("target_chat_id")
        target_thread_id = config.get("target_thread_id")
        
        is_correct_topic = (message.chat.id == target_chat_id and 
                            message.message_thread_id == target_thread_id)

        if not is_correct_topic:
            continue # Игнорируем сообщения не из той темы

        # --- Логика команды /get ---
        if text.startswith('/get'):
            code_to_send = next((code for code, data in bot_data["codes"].items() if not data.get("is_used")), None)
            if code_to_send:
                bot_data["codes"][code_to_send]["is_used"] = True
                remaining_count = sum(1 for data in bot_data["codes"].values() if not data.get("is_used", False))
                await bot.send_message(
                    chat_id=target_chat_id,
                    message_thread_id=target_thread_id,
                    text=f"<code>{code_to_send}</code>\n\n(Осталось: {remaining_count})"
                )
                data_changed = True
            else:
                await message.reply("😔 Увы, все коды закончились.")
        
        # --- Логика команды /add ---
        if text.startswith('/add'):
            # Убираем саму команду из текста
            codes_text = text.replace('/add', '').strip()
            codes = [line.strip() for line in codes_text.split('\n') if line.strip()]
            
            if not codes:
                await message.reply("После команды /add отправьте список кодов, каждый с новой строки.")
                continue

            new_codes_count = 0
            for code in codes:
                if code not in bot_data["codes"]:
                    bot_data["codes"][code] = {"is_used": False}
                    new_codes_count += 1
            
            if new_codes_count > 0:
                remaining_count = sum(1 for data in bot_data["codes"].values() if not data.get("is_used", False))
                await message.reply(f"👍 Добавлено {new_codes_count} новых кодов.\nВсего доступно: {remaining_count}")
                data_changed = True
            else:
                await message.reply("Все эти коды уже были в базе. Новых не добавлено.")

    # Если были изменения, сохраняем данные и ID последнего сообщения
    if data_changed:
        await save_data(bot_data)
    
    if updates:
        await redis_client.set(REDIS_OFFSET_KEY, updates[-1].update_id)

    logging.info("Проверка завершена.")


async def main():
    """Главная асинхронная функция."""
    try:
        await process_updates()
    finally:
        # Важно закрывать соединения, чтобы скрипт корректно завершился
        await bot.session.close()
        await redis_client.close()
        logging.info("Соединения закрыты.")

if __name__ == '__main__':
    asyncio.run(main())
