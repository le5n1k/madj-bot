"""
Discord-бот: компоновка фотографий из канала в сообщения по 10 штук.
Команда /скомпоновать — собирает все фото из текущего канала и отправляет в канал result.
"""

import json
import os
import io
from pathlib import Path
import aiohttp
from dotenv import load_dotenv

# .env ищем рядом с main.py — тогда запуск кнопкой в IDE работает без настроек
load_dotenv(Path(__file__).resolve().parent / ".env")
import discord
from discord.ext import commands

# Application ID (из Discord Developer Portal)
APPLICATION_ID = 1478116662468284517

# Размер пачки фото в одном сообщении
PHOTOS_PER_MESSAGE = 10

# Имя канала для результата
RESULT_CHANNEL_NAME = "result"

# Папки на Яндекс.Диске для команд заливки (задаются в .env: YA_FOLDER_1, YA_FOLDER_2, YA_FOLDER_3)
YA_DISK_RESOURCES_URL = "https://cloud-api.yandex.net/v1/disk/resources"
YA_DISK_UPLOAD_URL = "https://cloud-api.yandex.net/v1/disk/resources/upload"

# Расширения и MIME-типы изображений
IMAGE_EXTENSIONS = frozenset((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))
IMAGE_CONTENT_TYPES = frozenset(("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"))


def is_image_attachment(att: discord.Attachment) -> bool:
    """Проверяет, является ли вложение изображением."""
    if att.content_type and att.content_type.lower() in IMAGE_CONTENT_TYPES:
        return True
    if att.filename:
        return any(att.filename.lower().endswith(ext) for ext in IMAGE_EXTENSIONS)
    return False


def is_image_url(url: str) -> bool:
    """Проверяет по URL, похоже ли на изображение."""
    if not url or not url.startswith("http"):
        return False
    url_lower = url.lower()
    return any(ext in url_lower for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", "cdn.discordapp.com/attachments", "media.discordapp.net"))


def get_image_urls_from_message(message: discord.Message) -> list[str]:
    """Собирает все URL картинок из сообщения: вложения + картинки из эмбедов."""
    urls: list[str] = []
    for att in message.attachments:
        if is_image_attachment(att):
            urls.append(att.url)
    for embed in message.embeds:
        if embed.image and embed.image.url:
            urls.append(embed.image.url)
        if embed.thumbnail and embed.thumbnail.url:
            urls.append(embed.thumbnail.url)
    return urls


async def fetch_image_urls_via_api(channel_id: int, token: str) -> list[str]:
    """Запрашивает сообщения канала напрямую через Discord API (обходит кэш/ограничения библиотеки)."""
    urls: list[str] = []
    headers = {"Authorization": f"Bot {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"https://discord.com/api/v10/channels/{channel_id}/messages?limit=100",
            headers=headers,
        ) as resp:
            if resp.status != 200:
                return urls
            data = await resp.json()
    # API возвращает сообщения от новых к старым — разворачиваем для порядка "сначала старые"
    for msg in reversed(data):
        for att in msg.get("attachments", []):
            url = att.get("url") or att.get("proxy_url")
            if url and (is_image_url(url) or (att.get("content_type") or "").startswith("image/")):
                urls.append(url)
        for embed in msg.get("embeds", []):
            for key in ("image", "thumbnail"):
                part = embed.get(key, {})
                if isinstance(part, dict) and part.get("url"):
                    urls.append(part["url"])
    return urls


async def download_file(session: aiohttp.ClientSession, url: str) -> bytes | None:
    """Скачивает файл по URL и возвращает байты."""
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception:
        pass
    return None


async def ensure_yandex_folder(
    session: aiohttp.ClientSession,
    oauth_token: str,
    folder_path: str,
) -> tuple[bool, str | None]:
    """Создаёт папку на Яндекс.Диске, если её нет. Путь вида /КА или /отчеты. Возвращает (успех, ошибка)."""
    try:
        if not folder_path.startswith("/"):
            folder_path = "/" + folder_path
        # Передаём сырой путь — aiohttp сам закодирует; иначе двойная кодировка создаёт папки типа %D0%9A%D0%90
        params = {"path": folder_path}
        headers = {"Authorization": f"OAuth {oauth_token}"}
        async with session.put(YA_DISK_RESOURCES_URL, params=params, headers=headers) as resp:
            body = await resp.text()
            if resp.status in (201, 200):
                return True, None
            if resp.status == 409:
                return True, None
            try:
                err = json.loads(body)
                msg = err.get("description") or err.get("message") or body[:200]
            except Exception:
                msg = body[:200] if body else f"HTTP {resp.status}"
            return False, msg
    except Exception as e:
        return False, str(e)[:200]


async def upload_to_yandex_disk(
    session: aiohttp.ClientSession,
    oauth_token: str,
    disk_path: str,
    file_data: bytes,
) -> tuple[bool, str | None]:
    """Загружает файл на Яндекс.Диск. Возвращает (успех, текст_ошибки или None)."""
    try:
        # Путь от корня Диска (с ведущим слэшем); сырой путь — aiohttp сам закодирует
        if not disk_path.startswith("/"):
            disk_path = "/" + disk_path
        params = {"path": disk_path, "overwrite": "true"}
        headers = {"Authorization": f"OAuth {oauth_token}"}
        async with session.get(YA_DISK_UPLOAD_URL, params=params, headers=headers) as resp:
            body = await resp.text()
            if resp.status != 200:
                try:
                    err = json.loads(body)
                    msg = err.get("description") or err.get("message") or body[:200]
                except Exception:
                    msg = body[:200] if body else f"HTTP {resp.status}"
                return False, f"Запрос ссылки: {msg}"
            try:
                data = json.loads(body)
            except Exception:
                return False, "Не удалось разобрать ответ Диска"
        upload_href = data.get("href")
        if not upload_href:
            return False, "В ответе нет ссылки для загрузки"
        async with session.put(upload_href, data=file_data) as put_resp:
            if put_resp.status in (200, 201, 202):
                return True, None
            err_body = await put_resp.text()
            return False, f"Загрузка файла: HTTP {put_resp.status}, {err_body[:150]}"
    except Exception as e:
        return False, str(e)[:200]


def extension_from_url(url: str) -> str:
    """Определяет расширение по URL."""
    url_lower = url.lower()
    if ".png" in url_lower:
        return ".png"
    if ".jpg" in url_lower or ".jpeg" in url_lower:
        return ".jpg"
    if ".gif" in url_lower:
        return ".gif"
    if ".webp" in url_lower:
        return ".webp"
    return ".png"


async def collect_images_from_result_channel(result_channel: discord.TextChannel) -> list[tuple[bytes, str]]:
    """Собирает все изображения из канала result: список пар (байты, имя_файла)."""
    image_urls: list[str] = []
    try:
        async for message in result_channel.history(limit=1000, oldest_first=True):
            image_urls.extend(get_image_urls_from_message(message))
    except discord.Forbidden:
        return []
    if not image_urls:
        token = os.environ.get("DISCORD_BOT_TOKEN")
        if token:
            image_urls = await fetch_image_urls_via_api(result_channel.id, token)
    if not image_urls:
        return []
    files: list[tuple[bytes, str]] = []
    async with aiohttp.ClientSession() as session:
        for i, url in enumerate(image_urls):
            data = await download_file(session, url)
            if data is not None:
                ext = extension_from_url(url)
                files.append((data, f"image_{i}{ext}"))
    return files


# Message Content Intent нужен, чтобы при запросе истории канала Discord отдавал вложения и эмбеды
# Включи в портале: https://discord.com/developers/applications → приложение → Bot → Privileged Gateway Intents → Message Content Intent
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# Максимальное значение ID в Discord (64-bit snowflake)
DISCORD_SNOWFLAKE_MAX = 9223372036854775807


@bot.event
async def on_ready():
    print(f"Бот запущен: {bot.user} (ID: {bot.user.id})")
    try:
        guild_id_str = os.environ.get("DISCORD_GUILD_ID", "").strip()
        if guild_id_str and guild_id_str.isdigit():
            gid = int(guild_id_str)
            if gid <= DISCORD_SNOWFLAKE_MAX:
                guild = discord.Object(id=gid)
                synced = await bot.tree.sync(guild=guild)
                names = [c.name for c in synced]
                print(f"На сервер зарегистрированы команды: {names}")
            else:
                print(f"ВНИМАНИЕ: DISCORD_GUILD_ID слишком большой (макс. {DISCORD_SNOWFLAKE_MAX}). Синхронизация только глобально.")
        synced_global = await bot.tree.sync()
        print(f"Глобально команд: {len(synced_global)}")
    except Exception as e:
        print(f"Ошибка синхронизации команд: {e}")
        import traceback
        traceback.print_exc()


@bot.tree.command(name="скомпоновать", description="Собрать все фото из этого канала и отправить в канал result по 10 штук")
async def skomponovat(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    source_channel = interaction.channel
    if not isinstance(source_channel, discord.TextChannel):
        await interaction.followup.send("Команду можно использовать только в текстовом канале.", ephemeral=True)
        return

    guild = interaction.guild
    if not guild:
        await interaction.followup.send("Не удалось определить сервер.", ephemeral=True)
        return

    result_channel = discord.utils.get(guild.text_channels, name=RESULT_CHANNEL_NAME)
    if not result_channel:
        await interaction.followup.send(
            f"Канал с именем «{RESULT_CHANNEL_NAME}» не найден. Создайте текстовый канал с таким именем.",
            ephemeral=True,
        )
        return

    # Собираем все сообщения канала: вложения и картинки из эмбедов
    image_urls: list[str] = []
    try:
        async for message in source_channel.history(limit=1000, oldest_first=True):
            image_urls.extend(get_image_urls_from_message(message))
    except discord.Forbidden:
        await interaction.followup.send("У бота нет прав читать сообщения в этом канале.", ephemeral=True)
        return

    # Если через history() ничего не нашли — пробуем прямой запрос к API (иногда без Message Content Intent приходит пусто)
    if not image_urls:
        token = os.environ.get("DISCORD_BOT_TOKEN")
        if token:
            image_urls = await fetch_image_urls_via_api(source_channel.id, token)

    if not image_urls:
        await interaction.followup.send(
            "В этом канале не найдено ни одного изображения. Включи в Discord Developer Portal → приложение → Bot → "
            "Privileged Gateway Intents → **Message Content Intent**, перезапусти бота и попробуй снова.",
            ephemeral=True,
        )
        return

    # Скачиваем и отправляем пачками по 10
    sent_count = 0
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(image_urls), PHOTOS_PER_MESSAGE):
            batch_urls = image_urls[i : i + PHOTOS_PER_MESSAGE]
            files: list[discord.File] = []

            for url in batch_urls:
                data = await download_file(session, url)
                if data is not None:
                    # Имя файла для Discord (нужно расширение)
                    name = f"image_{sent_count + len(files)}.png"
                    if ".png" in url.lower() or "png" in url:
                        name = f"image_{sent_count + len(files)}.png"
                    elif ".jpg" in url.lower() or ".jpeg" in url.lower():
                        name = f"image_{sent_count + len(files)}.jpg"
                    elif ".gif" in url.lower():
                        name = f"image_{sent_count + len(files)}.gif"
                    elif ".webp" in url.lower():
                        name = f"image_{sent_count + len(files)}.webp"
                    files.append(discord.File(fp=io.BytesIO(data), filename=name))

            if files:
                try:
                    await result_channel.send(files=files)
                    sent_count += len(files)
                except discord.HTTPException as e:
                    await interaction.followup.send(
                        f"Ошибка при отправке в канал result: {e}. Проверьте права бота и размер файлов.",
                        ephemeral=True,
                    )
                    return

    await interaction.followup.send(
        f"Готово. Собрано изображений: {len(image_urls)}. Отправлено в канал «{RESULT_CHANNEL_NAME}»: {sent_count}.",
    )


@bot.tree.command(name="посчитать_скрины", description="Посчитать количество скринов/фото во всех сообщениях этого канала")
async def count_screens(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    source_channel = interaction.channel
    if not isinstance(source_channel, discord.TextChannel):
        await interaction.followup.send("Команду можно использовать только в текстовом канале.", ephemeral=True)
        return

    image_urls: list[str] = []
    try:
        async for message in source_channel.history(limit=1000, oldest_first=True):
            image_urls.extend(get_image_urls_from_message(message))
    except discord.Forbidden:
        await interaction.followup.send("У бота нет прав читать сообщения в этом канале.", ephemeral=True)
        return

    if not image_urls:
        token = os.environ.get("DISCORD_BOT_TOKEN")
        if token:
            image_urls = await fetch_image_urls_via_api(source_channel.id, token)

    count = len(image_urls)
    await interaction.followup.send(
        f"В этом канале во всех сообщениях найдено скринов/фото: **{count}**.",
        ephemeral=True,
    )


@bot.tree.command(name="очистить_result", description="Удалить все сообщения в канале result")
async def clear_result(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    if not guild:
        await interaction.followup.send("Не удалось определить сервер.", ephemeral=True)
        return

    result_channel = discord.utils.get(guild.text_channels, name=RESULT_CHANNEL_NAME)
    if not result_channel:
        await interaction.followup.send(
            f"Канал «{RESULT_CHANNEL_NAME}» не найден.",
            ephemeral=True,
        )
        return

    try:
        total = 0
        while True:
            # purge удаляет пачками (до 100), для старых сообщений — по одному
            deleted = await result_channel.purge(limit=100)
            if not deleted:
                break
            total += len(deleted)
        await interaction.followup.send(
            f"Канал «{RESULT_CHANNEL_NAME}» очищен. Удалено сообщений: {total}.",
            ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "У бота нет прав удалять сообщения в этом канале. Нужно право «Управление сообщениями» (Manage Messages).",
            ephemeral=True,
        )
    except discord.HTTPException as e:
        await interaction.followup.send(f"Ошибка при очистке: {e}.", ephemeral=True)


async def _upload_result_to_yandex_folder(
    interaction: discord.Interaction,
    folder_env_key: str,
    folder_label: str,
) -> None:
    """Общая логика: собрать картинки из result и залить в указанную папку на Яндекс.Диске."""
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("Не удалось определить сервер.", ephemeral=True)
        return
    result_channel = discord.utils.get(guild.text_channels, name=RESULT_CHANNEL_NAME)
    if not result_channel:
        await interaction.followup.send(f"Канал «{RESULT_CHANNEL_NAME}» не найден.", ephemeral=True)
        return
    oauth = os.environ.get("YA_DISK_TOKEN", "").strip()
    folder_name = os.environ.get(folder_env_key, "").strip()
    if not oauth:
        await interaction.followup.send(
            "Не задан токен Яндекс.Диска. Добавь в .env переменную YA_DISK_TOKEN.",
            ephemeral=True,
        )
        return
    if not folder_name:
        await interaction.followup.send(
            f"Не задана папка для этой команды. Добавь в .env переменную {folder_env_key}.",
            ephemeral=True,
        )
        return
    files = await collect_images_from_result_channel(result_channel)
    if not files:
        await interaction.followup.send(
            f"В канале «{RESULT_CHANNEL_NAME}» нет изображений.",
            ephemeral=True,
        )
        return
    uploaded = 0
    first_error: str | None = None
    async with aiohttp.ClientSession() as session:
        folder_ok, folder_err = await ensure_yandex_folder(session, oauth, f"/{folder_name}")
        if not folder_ok:
            await interaction.followup.send(
                f"Не удалось создать/найти папку «{folder_label}» на Диске: {folder_err}",
                ephemeral=True,
            )
            return
        for data, filename in files:
            disk_path = f"{folder_name}/{filename}"
            ok, err = await upload_to_yandex_disk(session, oauth, disk_path, data)
            if ok:
                uploaded += 1
            elif first_error is None:
                first_error = err
    msg = f"Залито в папку «{folder_label}» на Яндекс.Диске: **{uploaded}** из {len(files)} файлов."
    if first_error and uploaded == 0:
        msg += f"\n\nОшибка: {first_error}"
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="залить_ка", description="Залить содержимое канала result в папку КА на Яндекс.Диске")
async def upload_disk_ka(interaction: discord.Interaction):
    await _upload_result_to_yandex_folder(interaction, "YA_FOLDER_1", "КА")


@bot.tree.command(name="залить_отчеты", description="Залить содержимое канала result в папку «отчеты» на Яндекс.Диске")
async def upload_disk_otchety(interaction: discord.Interaction):
    await _upload_result_to_yandex_folder(interaction, "YA_FOLDER_2", "отчеты")


@bot.tree.command(name="залить_присяга", description="Залить содержимое канала result в папку «присяга» на Яндекс.Диске")
async def upload_disk_prisyaga(interaction: discord.Interaction):
    await _upload_result_to_yandex_folder(interaction, "YA_FOLDER_3", "присяга")


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("Задайте переменную окружения DISCORD_BOT_TOKEN (токен бота из Discord Developer Portal).")
        return
    bot.run(token)


if __name__ == "__main__":
    main()
