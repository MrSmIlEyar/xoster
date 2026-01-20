import os
import re
import json
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telethon import TelegramClient, events, functions
from telethon.tl.types import MessageEntityCustomEmoji, MessageEntityTextUrl, MessageMediaDocument
import httpx

load_dotenv()

# .env base
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
PHONE = os.getenv("PHONE")

SOURCE_CHANNELS = [ch.strip() for ch in os.getenv("SOURCE_CHANNELS", "").split(",") if ch.strip()]
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "0"))

WORKDIR = Path(os.getenv("WORKDIR", "./_mirror_tmp"))
MAP_FILE = Path(os.getenv("MAP_FILE", "./mirror_map.json"))

# footer: clickable TITLE -> LINK
TARGET_TITLE = os.getenv("TARGET_TITLE", "").strip()
TARGET_LINK = os.getenv("TARGET_LINK", "").strip()

# DeepSeek
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# dedup
TRIGRAM_THRESHOLD = float(os.getenv("TRIGRAM_THRESHOLD", "0.15"))
DEDUP_HISTORY_SIZE = int(os.getenv("DEDUP_HISTORY_SIZE", "100"))

# premium emoji
PREMIUM_EMOJI_ID = int(os.getenv("PREMIUM_EMOJI_ID", "0")) or 5323761960829862762

if not API_ID or not API_HASH or not PHONE or not SOURCE_CHANNELS or not TARGET_CHANNEL_ID:
    raise RuntimeError("Проверь .env: API_ID, API_HASH, PHONE, SOURCE_CHANNELS, TARGET_CHANNEL_ID обязательны")

if not DEEPSEEK_API_KEY:
    raise RuntimeError("Проверь .env: DEEPSEEK_API_KEY обязателен для AI функционала")

WORKDIR.mkdir(parents=True, exist_ok=True)
client = TelegramClient("mirror_reupload", API_ID, API_HASH)


def footer_text_and_entities(base_offset: int) -> tuple[str, list]:
    """
    Делает кликабельный TITLE, ведущий на TARGET_LINK.
    base_offset — смещение (offset) в общем тексте сообщения, где начинается TITLE.
    """
    if not TARGET_TITLE or not TARGET_LINK:
        return "", []

    ft = TARGET_TITLE
    ents = [
        MessageEntityTextUrl(
            offset=base_offset,
            length=len(ft),
            url=TARGET_LINK
        )
    ]
    return ft, ents


def safe_text_for_message(text: str | None) -> tuple[str, list]:
    text = text or ""
    safe_text = re.sub(r'@[\w_]+', "", text).strip()

    base = f"⚡ {safe_text}" if safe_text else "⚡"
    entities = [MessageEntityCustomEmoji(offset=0, length=1, document_id=PREMIUM_EMOJI_ID)]

    if TARGET_TITLE and TARGET_LINK:
        base_with_sep = base + "\n\n"
        ft, fent = footer_text_and_entities(base_offset=len(base_with_sep))
        result_text = base_with_sep + ft
        entities.extend(fent)
        return result_text, entities

    return base, entities


def safe_caption_for_media(text: str | None) -> tuple[str, list]:
    text = text or ""
    safe_text = re.sub(r'@[\w_]+', "", text).strip()

    base = f"⚡ {safe_text}" if safe_text else "⚡"
    entities = [MessageEntityCustomEmoji(offset=0, length=1, document_id=PREMIUM_EMOJI_ID)]

    if TARGET_TITLE and TARGET_LINK:
        base_with_sep = base + "\n\n"
        ft, fent = footer_text_and_entities(base_offset=len(base_with_sep))
        result_text = base_with_sep + ft
        entities.extend(fent)
        return result_text, entities

    return base, entities


def get_trigrams(text: str) -> set:
    text = text.lower().replace(" ", "")
    if len(text) < 3:
        return set()
    return {text[i:i + 3] for i in range(len(text) - 2)}


def calculate_similarity(text1: str, text2: str) -> float:
    trigrams1 = get_trigrams(text1)
    trigrams2 = get_trigrams(text2)
    if not trigrams1 or not trigrams2:
        return 0.0
    inter = len(trigrams1 & trigrams2)
    union = len(trigrams1 | trigrams2)
    return inter / union if union > 0 else 0.0


def load_map() -> dict:
    if MAP_FILE.exists():
        return json.loads(MAP_FILE.read_text("utf-8"))
    return {"single": {}, "album": {}, "dedup_history": []}


def save_map(m: dict) -> None:
    MAP_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=2), "utf-8")


def is_duplicate(text: str, history: list) -> bool:
    if not text or len(text.strip()) < 20:
        return False
    for hist_text in history[-DEDUP_HISTORY_SIZE:]:
        sim = calculate_similarity(text, hist_text)
        if sim > TRIGRAM_THRESHOLD:
            print(f"⚠️  Дубликат! Сходство: {sim:.1%}")
            return True
    return False


def add_to_history(text: str, history: list) -> None:
    if text and len(text.strip()) > 20:
        history.append(text)
        if len(history) > DEDUP_HISTORY_SIZE:
            history.pop(0)


def cleanup_media(file_path: str | Path) -> None:
    try:
        p = Path(file_path)
        if p.exists():
            p.unlink()
            print(f"🗑️ Удалён медиа файл: {p.name}")
    except Exception as e:
        print(f"⚠️ Ошибка при удалении файла {file_path}: {e}")


def cleanup_workdir() -> None:
    try:
        if WORKDIR.exists():
            for p in WORKDIR.glob("*"):
                if p.is_file():
                    p.unlink()
                    print(f"🗑️ Очищен файл: {p.name}")
    except Exception as e:
        print(f"⚠️ Ошибка при очистке директории {WORKDIR}: {e}")


async def is_advertisement(text: str) -> bool:
    if not text or len(text.strip()) < 20:
        return False

    try:
        async with httpx.AsyncClient(timeout=20.0) as client_http:
            resp = await client_http.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": DEEPSEEK_MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": """
Ты анализируешь тексты и определяешь, является ли текст рекламой или новостью.

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
                        {"role": "user", "content": f"Определи, что это - реклама или новость?\n\n{text}"},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 20,
                },
            )

            if resp.status_code != 200:
                print(f"⚠ Ошибка при проверке рекламы: {resp.status_code}")
                return False

            data = resp.json()
            classification = data["choices"][0]["message"]["content"].strip().upper()
            is_ad = "РЕКЛАМА" in classification

            print("🚫 Это реклама - пропускаем" if is_ad else "✓ Это новость - обрабатываем")
            return is_ad

    except Exception as e:
        print(e)
        print(f"⚠ Ошибка при обращении к API для проверки рекламы: {e}")
        return False


async def rewrite_text_with_ai(text: str) -> Optional[str]:
    if not text or len(text.strip()) < 10:
        return text

    try:
        async with httpx.AsyncClient(timeout=30.0) as client_http:
            resp = await client_http.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": DEEPSEEK_MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": """Ты пишешь посты для Telegram канала новостей.
Правила:
- Переформулируй новость кратко и энергично
- МАКСИМУМ 500 символов
- БЕЗ лишних деталей и воды
- Удаляя приписки названий каналов внизу поста
- Только суть и факты
- Эмодзи добавлять не нужно, а если есть эмодзи в исходном посте, то удаляешь его
- Сейчас 2026 год
- Отвечай ТОЛЬКО переформулированным текстом поста, ничего больше
- Если в тексте встречается запрещённое на территории РФ термин, то в конце новости добавь, что это запрещено на территории РФ""",
                        },
                        {"role": "user", "content": f"Переписать в стиль Telegram поста:\n\n{text}"},
                    ],
                    "temperature": 0.6,
                    "max_tokens": 150,
                },
            )

            if resp.status_code != 200:
                print(f"⚠ DeepSeek API ошибка: {resp.status_code}")
                return text

            data = resp.json()
            rewritten = data["choices"][0]["message"]["content"].strip()
            print(f"✓ AI переработала ({len(text)} -> {len(rewritten)} символов)")
            return rewritten

    except Exception as e:
        print(f"⚠ Ошибка при обращении к AI: {e}")
        return text


state = load_map()
if "dedup_history" not in state:
    state["dedup_history"] = []
    save_map(state)


async def reupload_single(msg, source_channel: str):
    text = msg.message or ""

    if text and await is_advertisement(text):
        print(f"❌ Пропускаем рекламу из {source_channel}")
        return None

    if is_duplicate(text, state["dedup_history"]):
        print(f"❌ Пропускаем дубликат из {source_channel}")
        return None

    add_to_history(text, state["dedup_history"])
    save_map(state)

    if text:
        text = await rewrite_text_with_ai(text) or ""

    if msg.media:
        file_path = await client.download_media(msg, file=str(WORKDIR))
        if not file_path:
            message_text, entities = safe_text_for_message(text)
            return await client.send_message(
                TARGET_CHANNEL_ID,
                message_text,
                formatting_entities=entities,
                link_preview=False
            )

        caption_text, caption_entities = safe_caption_for_media(text)

        send_kwargs = dict(
            caption=caption_text,
            force_document=False,
            formatting_entities=caption_entities,
        )

        if isinstance(msg.media, MessageMediaDocument) and msg.media.document:
            send_kwargs["attributes"] = msg.media.document.attributes

            send_kwargs["supports_streaming"] = True

        sent = await client.send_file(
            TARGET_CHANNEL_ID,
            file_path,
            **send_kwargs
        )

        if sent:
            cleanup_media(file_path)
        return sent

    message_text, entities = safe_text_for_message(text)
    return await client.send_message(TARGET_CHANNEL_ID, message_text, formatting_entities=entities, link_preview=False)


async def edit_single(target_msg_id: int, new_text: str, is_caption: bool = False):
    if is_caption:
        final_text, final_entities = safe_caption_for_media(new_text)
    else:
        final_text, final_entities = safe_text_for_message(new_text)

    await client(
        functions.messages.EditMessageRequest(
            peer=TARGET_PEER,
            id=int(target_msg_id),
            message=final_text,
            entities=final_entities,
            no_webpage=True
        )
    )


def register_handlers_for_source(source_channel: str):
    @client.on(events.NewMessage(chats=source_channel))
    async def on_new_message(event):
        msg = event.message
        if msg.grouped_id:
            return

        print(f"📩 Новое сообщение #{msg.id} из {source_channel}")
        sent = await reupload_single(msg, source_channel)
        if sent:
            state["single"][f"{source_channel}:{msg.id}"] = sent.id
            save_map(state)
            print(f"✅ Отправлено в приватный канал #{sent.id}")

    @client.on(events.MessageEdited(chats=source_channel))
    async def on_edited_message(event):
        msg = event.message
        if msg.grouped_id:
            return

        src_key = f"{source_channel}:{msg.id}"
        tgt_id = state["single"].get(src_key)
        if not tgt_id:
            return

        new_text = msg.message or ""

        if new_text and await is_advertisement(new_text):
            print("❌ Отредактировано в рекламу - удаляем пост")
            try:
                await client.delete_messages(TARGET_CHANNEL_ID, int(tgt_id))
                del state["single"][src_key]
                save_map(state)
            except Exception:
                pass
            return

        if new_text:
            new_text = await rewrite_text_with_ai(new_text) or ""

        print(f"✏️ Редактирование #{msg.id} из {source_channel}")
        await edit_single(int(tgt_id), new_text, is_caption=bool(msg.media))
        print(f"✅ Отредактировано #{tgt_id}")

    @client.on(events.Album(chats=source_channel))
    async def on_album(event):
        msgs = list(event.messages)
        if not msgs:
            return

        grouped_id = next((m.grouped_id for m in msgs if m.grouped_id), None)
        if not grouped_id:
            return

        caption_src = next((m.message for m in msgs if m.message), "") or ""

        if caption_src and await is_advertisement(caption_src):
            print(f"❌ Пропускаем рекламный альбом из {source_channel}")
            return

        if is_duplicate(caption_src, state["dedup_history"]):
            print(f"❌ Пропускаем дубликат альбома из {source_channel}")
            return

        add_to_history(caption_src, state["dedup_history"])
        save_map(state)

        if caption_src:
            caption_src = await rewrite_text_with_ai(caption_src) or ""

        album_key = f"{source_channel}:{grouped_id}"
        if album_key in state["album"]:
            caption_msg_id = state["album"][album_key].get("caption_msg_id")
            if caption_msg_id:
                print(f"✏️ Редактирование альбома #{grouped_id} из {source_channel}")
                await edit_single(int(caption_msg_id), caption_src, is_caption=True)
                print(f"✅ Отредактировано #{caption_msg_id}")
            return

        print(f"📷 Новый альбом #{grouped_id} из {source_channel}")

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

        caption_text, caption_entities = safe_caption_for_media(caption_src)

        if not files:
            sent = await client.send_message(TARGET_CHANNEL_ID, caption_text, formatting_entities=caption_entities, link_preview=False)
            state["album"][album_key] = {"target_msg_ids": [sent.id], "caption_msg_id": sent.id}
            save_map(state)
            return

        sent_messages = await client.send_file(
            TARGET_CHANNEL_ID,
            files,
            caption=caption_text,
            supports_streaming=any_video,
            force_document=False,
            formatting_entities=caption_entities,
        )

        sent_list = sent_messages if isinstance(sent_messages, list) else [sent_messages]
        target_ids = [m.id for m in sent_list if m]
        caption_msg_id = target_ids[0] if target_ids else None

        state["album"][album_key] = {"target_msg_ids": target_ids, "caption_msg_id": caption_msg_id}
        save_map(state)
        print(f"✅ Альбом отправлен ({len(target_ids)} сообщений)")

        for fp in files:
            cleanup_media(fp)
        cleanup_workdir()


for ch in SOURCE_CHANNELS:
    register_handlers_for_source(ch)


async def main():
    await client.start(phone=PHONE)

    for ch in SOURCE_CHANNELS:
        try:
            await client.get_entity(ch)
            global TARGET_PEER
            TARGET_PEER = await client.get_input_entity(TARGET_CHANNEL_ID)
        except Exception:
            pass

    await client.get_entity(TARGET_CHANNEL_ID)

    print("\n🚀 Mirror started (PRIVATE TARGET + clickable TITLE footer + dedup + AI + AD FILTER)")
    print(f"   Sources: {', '.join(SOURCE_CHANNELS)}")
    print(f"   Target (private id): {TARGET_CHANNEL_ID}")
    print(f"   Footer title: {TARGET_TITLE or '-'}")
    print(f"   Footer link: {TARGET_LINK or '-'}")
    print(f"   Dedup threshold: {TRIGRAM_THRESHOLD:.0%}")
    print(f"   AI Model: {DEEPSEEK_MODEL}")
    print(f"   Premium emoji ID: {PREMIUM_EMOJI_ID}\n")

    await client.run_until_disconnected()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
