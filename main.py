"""
Discord-бот: компоновка фотографий из канала в сообщения по 10 штук.
Команда /скомпоновать — собирает все фото из текущего канала и отправляет в канал result.
"""

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


# Message Content Intent нужен, чтобы при запросе истории канала Discord отдавал вложения и эмбеды
# Включи в портале: https://discord.com/developers/applications → приложение → Bot → Privileged Gateway Intents → Message Content Intent
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Бот запущен: {bot.user} (ID: {bot.user.id})")
    try:
        guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
        # Сначала синхронизируем на твой сервер — команды появятся сразу
        if guild_id and guild_id.isdigit():
            guild = discord.Object(id=int(guild_id))
            synced = await bot.tree.sync(guild=guild)
            names = [c.name for c in synced]
            print(f"На сервер {guild_id} зарегистрированы команды: {names}")
        # Потом глобально (чтобы работало на других серверах и обновился кэш Discord)
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


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("Задайте переменную окружения DISCORD_BOT_TOKEN (токен бота из Discord Developer Portal).")
        return
    bot.run(token)


if __name__ == "__main__":
    main()
