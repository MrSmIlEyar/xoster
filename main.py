import os
import re
import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telethon import TelegramClient, events, functions
from telethon.tl.types import (
    MessageEntityCustomEmoji,
    MessageEntityTextUrl,
    MessageMediaDocument,
    DocumentAttributeVideo,
)
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

TARGET_PEER = None  # выставим в main()


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

    # Удаляем упоминания, но сохраняем текст между ними
    safe_text = re.sub(r'@[\w_]+', "", text).strip()

    # Если после удаления упоминаний осталась пустая строка
    # или только спецсимволы, используем заглушку
    if not safe_text or len(safe_text.strip()) == 0:
        safe_text = "Новость"

    # Удаляем возможные множественные пробелы
    safe_text = re.sub(r'\s+', ' ', safe_text).strip()

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
    # Используем ту же логику, что и для текстовых сообщений
    return safe_text_for_message(text)


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


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def ffprobe_meta(path: str) -> tuple[int, int, int]:
    """
    duration(sec), width, height
    Если ffprobe не доступен или что-то пошло не так — вернём нули (Telegram переживёт).
    """
    if not shutil.which("ffprobe"):
        return 0, 0, 0
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json",
            path
        ]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        data = json.loads(p.stdout or "{}")
        streams = data.get("streams") or [{}]
        fmt = data.get("format") or {}
        w = int(streams[0].get("width") or 0)
        h = int(streams[0].get("height") or 0)
        dur = int(float(fmt.get("duration") or 0))
        return dur, w, h
    except Exception:
        return 0, 0, 0


def make_thumb(video_path: str, out_jpg: Path) -> Optional[Path]:
    """
    Делаем JPEG-превью (1 кадр).
    Важно: в альбомах Telethon может игнорировать thumb, поэтому для видео в альбомах мы шлём по одному.
    """
    if not shutil.which("ffmpeg"):
        return None
    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", "1",
            "-i", video_path,
            "-frames:v", "1",
            "-vf", "scale=320:-1",
            str(out_jpg)
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return out_jpg if out_jpg.exists() else None
    except Exception:
        return None


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
Вы — классификатор текстов. 
Определите, является ли предоставленный текст рекламой или новостью, используя строгие критерии.

Текст признаётся РЕКЛАМОЙ, если:
1. Содержит прямые или косвенные предложения товаров, услуг, приложений (например: «купите», «закажите», «скачайте», «воспользуйтесь», «подпишитесь», «оформите», «начните», «лучший сервис»).
2. Упоминает промо-акции: скидки, акции, промокоды, бонусы, специальные предложения, ограниченные по времени.
3. Продвигает событие с коммерческой или личной выгодой для автора: платные вебинары, курсы, тренинги, мастер-классы, марафоны.
4. Содержит призыв к финансовому действию в чьих-либо интересах: «инвестируйте в проект», «откройте счёт», «вложите средства», «купите акции».
5. Включает ссылки, коды или упоминания, явно указывающие на партнёрские или реферальные программы (реф-ссылки, реферальные коды, промо от блогеров).
6. Акцент сделан на преимуществах конкретного продукта/компании/бренда, а не на объективном информировании.
7. Имеет признаки спама: навязчивые повторяющиеся призывы, массовый характер, отсутствие конкретной новостной ценности.

Текст признаётся НОВОСТЬЮ, если:

1. Сообщает об объективном событии, факте, произошедшем или анонсированном (политика, экономика, происшествия, индустрия, наука, технологии).
2. Приводит финансовые или экономические данные, котировки, статистику, результаты компаний без прямого призыва к их покупке.
3. Содержит анализ, обсуждение или экспертное мнение по событию или тенденции.
4. Информирует о изменениях в законодательстве, работе госорганов, значимых общественных событиях.
5. Критерий принятия решения:
6. Если в тексте присутствует хотя бы один явный признак рекламы из перечисленных выше — классифицируйте его как РЕКЛАМА.
7. Если текст носит исключительно информационный, аналитический или новостной характер без коммерческих призывов и продвижения — классифицируйте как НОВОСТЬ.

Формат ответа:
Отвечайте строго одним словом, без кавычек, точек и любых других пояснений: РЕКЛАМА или НОВОСТЬ.""",
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
        print(f"⚠ Ошибка при обращении к API для проверки рекламы: {e}")
        return False


async def rewrite_text_with_ai(text: str, max_retries: int = 3) -> Optional[str]:
    """
    Переписывает текст с помощью AI с гарантией непустого результата.
    Делает до max_retries попыток, если API возвращает пустой текст.
    """
    if not text or len(text.strip()) < 10:
        return text

    original_text = text

    for attempt in range(max_retries):
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
                                "content": """
Вы — редактор новостного Telegram-канала.
Ваша задача — переработать исходный текст новости в лаконичный и динамичный пост.

Критерии обработки текста:

1. Стиль: Только сухие факты, изложенные энергично и кратко. Без вводных слов, оценок и рассуждений.
2. Длина: Строго не более 600 символов, включая пробелы.
3. Содержание: Извлекается и переформулируется исключительно суть события (кто, что, когда, где, основные обстоятельства). Все второстепенные детали, цитаты, контекст и «воду» — удалить.
4. Форматирование: Все эмодзи, смайлики, лишние переносы строк и HTML-разметку — удалить.
5. Любые упоминания источников («канал сообщает», «пишет РИА»), рекламные приписки и названия других каналов в начале или конце текста — удалить.
6. Выходные данные: Ваш ответ должен содержать только итоговый текст новости для поста, без пояснений, подписей или тегов.
7. Контекст: Учитывайте актуальность на 2026 год.
8. Правовой аспект: Если в тексте прямо упоминается организация, признанная в РФ экстремистской или террористической, либо иной запрещенный материал, после основного текста добавьте абзацем: «[Упомянутая организация/материал] запрещены на территории РФ».

Ваш ответ — это готовый к публикации пост, соответствующий всем пунктам выше.""",
                            },
                            {"role": "user", "content": f"Переписать в стиль Telegram поста:\n\n{text}"},
                        ],
                        "temperature": 0.6,
                        "max_tokens": 150,
                    },
                )

                if resp.status_code != 200:
                    print(f"⚠ DeepSeek API ошибка (попытка {attempt + 1}/{max_retries}): {resp.status_code}")
                    if attempt < max_retries - 1:
                        continue
                    return original_text

                data = resp.json()
                rewritten = data["choices"][0]["message"]["content"].strip()

                # Проверяем, что результат не пустой
                if rewritten and len(rewritten.strip()) > 0:
                    print(f"✓ AI переработала ({len(original_text)} -> {len(rewritten)} символов)")
                    return rewritten
                else:
                    print(f"⚠ AI вернула пустой текст (попытка {attempt + 1}/{max_retries})")
                    if attempt < max_retries - 1:
                        continue
                    # Если после всех попыток пусто, возвращаем исходный текст
                    return original_text

        except Exception as e:
            print(f"⚠ Ошибка при обращении к AI (попытка {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                continue
            return original_text

    # Если все попытки не удались, возвращаем исходный текст
    return original_text


state = load_map()
if "dedup_history" not in state:
    state["dedup_history"] = []
    save_map(state)


async def send_media_file(
        file_path: str,
        caption_text: str,
        caption_entities: list,
        is_video: bool,
):
    """
    Единая отправка файла (single). Для видео добавляем attrs + thumb.
    supports_streaming должен быть и параметром send_file, и флагом в DocumentAttributeVideo.
    """
    send_kwargs = dict(
        caption=caption_text,
        force_document=False,
        formatting_entities=caption_entities,
        supports_streaming=bool(is_video),
    )

    if is_video:
        dur, w, h = ffprobe_meta(file_path)
        send_kwargs["attributes"] = [DocumentAttributeVideo(
            duration=dur,
            w=w,
            h=h,
            supports_streaming=True
        )]  # ключевой момент для streamable видео

        if has_ffmpeg():
            thumb_path = make_thumb(file_path, WORKDIR / f"thumb_{Path(file_path).stem}.jpg")
            if thumb_path:
                send_kwargs["thumb"] = str(thumb_path)

    return await client.send_file(TARGET_CHANNEL_ID, file_path, **send_kwargs)


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

    # Гарантируем, что текст не пустой после обработки
    if not text or len(text.strip()) == 0:
        print(f"⚠️ Предупреждение: текст пустой после обработки, используем заглушку")
        return

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

        sent = await send_media_file(
            file_path=file_path,
            caption_text=caption_text,
            caption_entities=caption_entities,
            is_video=bool(msg.video),
        )

        if sent:
            cleanup_media(file_path)
        return sent

    message_text, entities = safe_text_for_message(text)
    return await client.send_message(TARGET_CHANNEL_ID, message_text, formatting_entities=entities, link_preview=False)


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

        # Гарантируем, что текст не пустой после обработки
        if not caption_src or len(caption_src.strip()) == 0:
            print(f"⚠️ Предупреждение: текст альбома пустой после обработки, используем заглушку")
            caption_src = "Новость"

        album_key = f"{source_channel}:{grouped_id}"
        if album_key in state["album"]:
            caption_msg_id = state["album"][album_key].get("caption_msg_id")
            if caption_msg_id:
                pass
            return

        print(f"📷 Новый альбом #{grouped_id} из {source_channel}")

        # скачиваем файлы
        media_msgs = [m for m in msgs if m.media]
        files: list[str] = []
        for m in media_msgs:
            fp = await client.download_media(m, file=str(WORKDIR))
            if fp:
                files.append(fp)

        caption_text, caption_entities = safe_caption_for_media(caption_src)

        if not files:
            sent = await client.send_message(
                TARGET_CHANNEL_ID,
                caption_text,
                formatting_entities=caption_entities,
                link_preview=False
            )
            state["album"][album_key] = {"target_msg_ids": [sent.id], "caption_msg_id": sent.id}
            save_map(state)
            return

        # Важный фикс: если в альбоме есть видео — отправляем по одному,
        # потому что с thumb/атрибутами в альбомах у Telethon бывают проблемы. [web:17]
        if any(m.video for m in media_msgs):
            print("🎬 В альбоме есть видео -> отправляем по одному (fix preview/streaming)")
            target_ids: list[int] = []
            caption_msg_id = None

            for idx, (m, fp) in enumerate(zip(media_msgs, files)):
                sent = await send_media_file(
                    file_path=fp,
                    caption_text=caption_text if idx == 0 else "",
                    caption_entities=caption_entities if idx == 0 else [],
                    is_video=bool(m.video),
                )
                if sent:
                    target_ids.append(sent.id)
                    if caption_msg_id is None:
                        caption_msg_id = sent.id

                cleanup_media(fp)

            state["album"][album_key] = {"target_msg_ids": target_ids, "caption_msg_id": caption_msg_id}
            save_map(state)
            cleanup_workdir()
            print(f"✅ Альбом отправлен по одному ({len(target_ids)} сообщений)")
            return

        # Если видео нет — можно слать настоящим альбомом (быстрее)
        sent_messages = await client.send_file(
            TARGET_CHANNEL_ID,
            files,
            caption=caption_text,
            force_document=False,
            formatting_entities=caption_entities,
            supports_streaming=False,
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

    global TARGET_PEER
    TARGET_PEER = await client.get_input_entity(TARGET_CHANNEL_ID)

    # проверим источники (не обязательно, но удобно)
    for ch in SOURCE_CHANNELS:
        try:
            await client.get_entity(ch)
        except Exception:
            pass

    await client.get_entity(TARGET_CHANNEL_ID)

    print("\n🚀 Mirror started (PRIVATE TARGET + clickable TITLE footer + dedup + AI + AD FILTER + VIDEO FIX)")
    print(f"   Sources: {', '.join(SOURCE_CHANNELS)}")
    print(f"   Target (private id): {TARGET_CHANNEL_ID}")
    print(f"   Footer title: {TARGET_TITLE or '-'}")
    print(f"   Footer link: {TARGET_LINK or '-'}")
    print(f"   Dedup threshold: {TRIGRAM_THRESHOLD:.0%}")
    print(f"   AI Model: {DEEPSEEK_MODEL}")
    print(f"   Premium emoji ID: {PREMIUM_EMOJI_ID}")
    print(f"   ffmpeg available: {'yes' if has_ffmpeg() else 'no'}")
    print(f"   AI retries: 3 (гарантия непустого текста)\n")

    await client.run_until_disconnected()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())