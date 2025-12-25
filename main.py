import os
import re
import json
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telethon import TelegramClient, events
import httpx

load_dotenv()

# .env
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
PHONE = os.getenv("PHONE")
SOURCE_CHANNELS = [ch.strip() for ch in os.getenv("SOURCE_CHANNELS", "").split(",")]
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL")
WORKDIR = Path(os.getenv("WORKDIR", "./_mirror_tmp"))
MAP_FILE = Path(os.getenv("MAP_FILE", "./mirror_map.json"))

# DeepSeek API
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# Параметры дедупликации
TRIGRAM_THRESHOLD = float(os.getenv("TRIGRAM_THRESHOLD", "0.2"))  # 20%
DEDUP_HISTORY_SIZE = int(os.getenv("DEDUP_HISTORY_SIZE", "100"))

if not API_ID or not API_HASH or not PHONE or not SOURCE_CHANNELS or not TARGET_CHANNEL:
    raise RuntimeError("Проверь .env: API_ID, API_HASH, PHONE, SOURCE_CHANNELS, TARGET_CHANNEL обязательны")

if not DEEPSEEK_API_KEY:
    raise RuntimeError("Проверь .env: DEEPSEEK_API_KEY обязателен для AI функционала")

WORKDIR.mkdir(parents=True, exist_ok=True)

client = TelegramClient("mirror_reupload", API_ID, API_HASH)


def safe_caption(text: str | None) -> str:
    """Заменяет подпись паблика на TARGET_CHANNEL"""
    if not text:
        return ""
    safe_text = text[:1024]
    safe_text = re.sub(r"@\w+", f"@{TARGET_CHANNEL}", safe_text)
    return safe_text


def get_trigrams(text: str) -> set:
    """Извлекает триграммы из текста (по 3 символа)"""
    text = text.lower().replace(" ", "")
    if len(text) < 3:
        return set()
    return {text[i:i + 3] for i in range(len(text) - 2)}


def calculate_similarity(text1: str, text2: str) -> float:
    """
    Вычисляет схожесть двух текстов на основе триграмм.
    Возвращает значение от 0 до 1 (1 = полностью одинаковые).
    """
    trigrams1 = get_trigrams(text1)
    trigrams2 = get_trigrams(text2)

    if not trigrams1 or not trigrams2:
        return 0.0

    intersection = len(trigrams1 & trigrams2)
    union = len(trigrams1 | trigrams2)

    return intersection / union if union > 0 else 0.0


def load_map() -> dict:
    if MAP_FILE.exists():
        return json.loads(MAP_FILE.read_text("utf-8"))
    return {
        "single": {},
        "album": {},
        "dedup_history": []  # История текстов для дедупликации
    }


def save_map(m: dict) -> None:
    MAP_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=2), "utf-8")


def is_duplicate(text: str, history: list) -> bool:
    """
    Проверяет, похожа ли новость на что-то из истории.
    Если сходство > TRIGRAM_THRESHOLD, считаем дубликатом.
    """
    if not text or len(text.strip()) < 20:
        return False

    for hist_text in history[-DEDUP_HISTORY_SIZE:]:
        similarity = calculate_similarity(text, hist_text)
        if similarity > TRIGRAM_THRESHOLD:
            print(f"⚠️  Дубликат! Сходство: {similarity:.1%}")
            return True

    return False


def add_to_history(text: str, history: list) -> None:
    """Добавляет текст в историю дедупликации"""
    if text and len(text.strip()) > 20:
        history.append(text)
        # Оставляем только последние N записей
        if len(history) > DEDUP_HISTORY_SIZE:
            history.pop(0)


def cleanup_media(file_path: str | Path) -> None:
    """
    Удаляет медиа файл после отправки.
    """
    try:
        file_path = Path(file_path)
        if file_path.exists():
            file_path.unlink()
            print(f"🗑️ Удалён медиа файл: {file_path.name}")
    except Exception as e:
        print(f"⚠️ Ошибка при удалении файла {file_path}: {e}")


def cleanup_workdir() -> None:
    """
    Удаляет все файлы из рабочей директории для экономии памяти.
    Вызывается периодически.
    """
    try:
        if WORKDIR.exists():
            for file_path in WORKDIR.glob("*"):
                if file_path.is_file():
                    file_path.unlink()
                    print(f"🗑️ Очищен файл: {file_path.name}")
    except Exception as e:
        print(f"⚠️ Ошибка при очистке директории {WORKDIR}: {e}")


async def is_advertisement(text: str) -> bool:
    """
    Проверяет через DeepSeek, является ли текст рекламой.
    Возвращает True если это реклама, False если это новость.
    """
    if not text or len(text.strip()) < 20:
        return False

    try:
        async with httpx.AsyncClient(timeout=20.0) as client_http:
            response = await client_http.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": DEEPSEEK_MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": """Ты анализируешь тексты и определяешь, является ли текст рекламой или новостью.

Реклама - это:
- Предложение услуг/товаров (купи, закажи, скачай, используй)
- Промо-коды и скидки
- Приглашение на вебинар/курс
- Ссылки на продукты (referral ссылки, реф коды)
- Призывы к действию в коммерческих целях (инвестируй в проект, откройте счёт)
- Спам и мусор

Новость - это:
- Информация о событиях, фактах, данных
- Финансовые новости, котировки
- Экономическая информация
- События в индустрии
- Аналитика и обсуждения

Отвечай ТОЛЬКО одним словом: "РЕКЛАМА" или "НОВОСТЬ"
Больше ничего не пиши!""",
                        },
                        {
                            "role": "user",
                            "content": f"Определи, что это - реклама или новость?\n\n{text}",
                        },
                    ],
                    "temperature": 0.3,
                    "max_tokens": 20,
                },
            )

            if response.status_code == 200:
                result = response.json()
                classification = result["choices"][0]["message"]["content"].strip().upper()

                is_ad = "РЕКЛАМА" in classification

                if is_ad:
                    print(f"🚫 Это реклама - пропускаем")
                else:
                    print(f"✓ Это новость - обрабатываем")

                return is_ad
            else:
                print(f"⚠ Ошибка при проверке рекламы: {response.status_code}")
                return False

    except Exception as e:
        print(f"⚠ Ошибка при обращении к API для проверки рекламы: {e}")
        return False


async def rewrite_text_with_ai(text: str) -> Optional[str]:
    """
    Переписывает текст новости для Telegram поста.
    Коротко, энергично, без воды. Максимум 300 символов.
    """
    if not text or len(text.strip()) < 10:
        return text

    try:
        async with httpx.AsyncClient(timeout=30.0) as client_http:
            response = await client_http.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": DEEPSEEK_MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": """Ты пишешь посты для Telegram канала новостей. 
Твои правила:
- Переформулируй новость кратко и энергично
- МАКСИМУМ 280 символов (чтобы умещалось в один пост)
- БЕЗ лишних деталей и воды
- БЕЗ "Как сообщает", "По словам", и подобного мусора
- Только суть и факты
- Можно добавить уместный эмодзи в начало (опционально)
- Отвечай ТОЛЬКО текстом поста, ничего больше""",
                        },
                        {
                            "role": "user",
                            "content": f"Переписать в стиль Telegram поста:\n\n{text}",
                        },
                    ],
                    "temperature": 0.6,
                    "max_tokens": 150,
                },
            )

            if response.status_code == 200:
                result = response.json()
                rewritten = result["choices"][0]["message"]["content"].strip()
                print(f"✓ AI переработала ({len(text)} -> {len(rewritten)} символов)")
                return rewritten
            else:
                print(f"⚠ DeepSeek API ошибка: {response.status_code}")
                return text

    except Exception as e:
        print(f"⚠ Ошибка при обращении к AI: {e}")
        return text


state = load_map()

if "dedup_history" not in state:
    state["dedup_history"] = []
    save_map(state)


async def reupload_single(msg, source_channel: str):
    text = msg.message or ""

    # ПРОВЕРКА: Пропускаем рекламу
    if text:
        if await is_advertisement(text):
            print(f"❌ Пропускаем рекламу из {source_channel}")
            return None

    # Проверяем на дубликат
    if is_duplicate(text, state["dedup_history"]):
        print(f"❌ Пропускаем дубликат из {source_channel}")
        return None

    # Добавляем в историю
    add_to_history(text, state["dedup_history"])
    save_map(state)

    # Переписываем текст через AI, если он есть
    if text:
        text = await rewrite_text_with_ai(text)

    if msg.media:
        file_path = await client.download_media(msg, file=str(WORKDIR))
        if not file_path:
            if text:
                sent = await client.send_message(TARGET_CHANNEL, text)
                return sent
            return None

        if msg.video:
            sent = await client.send_file(
                TARGET_CHANNEL,
                file_path,
                caption=safe_caption(text),
                supports_streaming=True,
                force_document=False,
            )
        else:
            sent = await client.send_file(
                TARGET_CHANNEL,
                file_path,
                caption=safe_caption(text),
            )

        # Удаляем медиа файл после отправки
        if sent:
            cleanup_media(file_path)

        return sent

    if text:
        sent = await client.send_message(TARGET_CHANNEL, text)
        return sent

    return None


async def edit_single(target_msg_id: int, new_text: str):
    """Редактируем текст/caption"""
    await client.edit_message(TARGET_CHANNEL, target_msg_id, new_text)


# Создаём обработчики для каждого канала
for source_channel in SOURCE_CHANNELS:
    @client.on(events.NewMessage(chats=source_channel))
    async def on_new_message(event, ch=source_channel):
        msg = event.message

        if msg.grouped_id:
            return

        print(f"📩 Новое сообщение #{msg.id} из {ch}")
        sent = await reupload_single(msg, ch)
        if sent:
            state["single"][str(msg.id)] = sent.id
            save_map(state)
            print(f"✅ Отправлено в целевой канал #{sent.id}")


    @client.on(events.MessageEdited(chats=source_channel))
    async def on_edited_message(event, ch=source_channel):
        msg = event.message

        if msg.grouped_id:
            return

        src_id = str(msg.id)
        tgt_id = state["single"].get(src_id)

        if not tgt_id:
            return

        new_text = msg.message or ""

        # Проверяем на рекламу при редактировании
        if new_text:
            if await is_advertisement(new_text):
                print(f"❌ Отредактировано в рекламу - удаляем пост")
                try:
                    await client.delete_messages(TARGET_CHANNEL, int(tgt_id))
                    del state["single"][src_id]
                    save_map(state)
                except:
                    pass
                return

        # Переписываем отредактированный текст через AI
        if new_text:
            new_text = await rewrite_text_with_ai(new_text)

        print(f"✏️ Редактирование #{msg.id} из {ch}")
        await edit_single(int(tgt_id), new_text)
        print(f"✅ Отредактировано #{tgt_id}")


    @client.on(events.Album(chats=source_channel))
    async def on_album(event, ch=source_channel):
        msgs = list(event.messages)
        if not msgs:
            return

        grouped_id = None
        for m in msgs:
            if m.grouped_id:
                grouped_id = m.grouped_id
                break
        if not grouped_id:
            return

        caption_src = ""
        for m in msgs:
            if m.message:
                caption_src = m.message
                break

        # Пропускаем рекламные альбомы
        if caption_src:
            if await is_advertisement(caption_src):
                print(f"❌ Пропускаем рекламный альбом из {ch}")
                return

        # Проверяем на дубликат
        if is_duplicate(caption_src, state["dedup_history"]):
            print(f"❌ Пропускаем дубликат альбома из {ch}")
            return

        # Добавляем в историю
        add_to_history(caption_src, state["dedup_history"])
        save_map(state)

        # Переписываем caption через AI
        if caption_src:
            caption_src = await rewrite_text_with_ai(caption_src)

        caption = safe_caption(caption_src)

        album_key = str(grouped_id)
        if album_key in state["album"]:
            caption_msg_id = state["album"][album_key].get("caption_msg_id")
            if caption_msg_id:
                print(f"✏️ Редактирование альбома #{grouped_id} из {ch}")
                await edit_single(int(caption_msg_id), caption)
                print(f"✅ Отредактировано #{caption_msg_id}")
            return

        print(f"📷 Новый альбом #{grouped_id} из {ch}")

        files = []
        any_video = False
        for m in msgs:
            if not m.media:
                continue
            fp = await client.download_media(m, file=str(WORKDIR))
            if fp:
                files.append(fp)
            if m.video:
                any_video = True

        if not files:
            if caption:
                sent = await client.send_message(TARGET_CHANNEL, caption)
                state["album"][album_key] = {"target_msg_ids": [sent.id], "caption_msg_id": sent.id}
                save_map(state)
            return

        sent_messages = await client.send_file(
            TARGET_CHANNEL,
            files,
            caption=caption,
            supports_streaming=any_video,
            force_document=False,
        )

        if isinstance(sent_messages, list):
            sent_list = sent_messages
        else:
            sent_list = [sent_messages]

        target_ids = [m.id for m in sent_list if m]
        caption_msg_id = target_ids[0] if target_ids else None

        state["album"][album_key] = {
            "target_msg_ids": target_ids,
            "caption_msg_id": caption_msg_id,
        }
        save_map(state)
        print(f"✅ Альбом отправлен ({len(target_ids)} сообщений)")

        # Удаляем все медиа файлы альбома после отправки
        for file_path in files:
            cleanup_media(file_path)

        # Очищаем директорию
        cleanup_workdir()


async def main():
    await client.start(phone=PHONE)

    # Прогреваем сущности
    for ch in SOURCE_CHANNELS:
        try:
            await client.get_entity(ch)
        except:
            pass

    await client.get_entity(TARGET_CHANNEL)

    print("\n🚀 Mirror started (2+ channels + deduplication + AI rewrite + AD FILTER + CLEANUP)")
    print(f"   Sources: {', '.join(SOURCE_CHANNELS)}")
    print(f"   Target: {TARGET_CHANNEL}")
    print(f"   Dedup threshold: {TRIGRAM_THRESHOLD:.0%}")
    print(f"   AI Model: {DEEPSEEK_MODEL}")
    print(f"   AD Filter: ENABLED")
    print(f"   Media Cleanup: ENABLED\n")

    await client.run_until_disconnected()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
