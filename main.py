import os
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

# ========= 環境変数 =========
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")  # 必須
# 複数チャンネルIDをカンマ区切りで指定
SOURCE_TEXT_CHANNEL_IDS = [
    int(x.strip()) for x in os.getenv("SOURCE_TEXT_CHANNEL_IDS", "").split(",") if x.strip().isdigit()
]

MALE_ROLE_ID = int(os.getenv("MALE_ROLE_ID", "1399390214295785623"))     # 男ロール
FEMALE_ROLE_ID = int(os.getenv("FEMALE_ROLE_ID", "1399390384756363264")) # 女ロール
MALE_FORUM_ID = int(os.getenv("MALE_FORUM_ID", "0"))                     # 男用フォーラムch ID
FEMALE_FORUM_ID = int(os.getenv("FEMALE_FORUM_ID", "0"))                 # 女用フォーラムch ID
DEFAULT_FORUM_ID = int(os.getenv("DEFAULT_FORUM_ID", "0"))               # 任意：どちらでもない場合のフォーラム
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ========= ログ =========
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="(%(asctime)s) [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("forum-post-maker")

# ========= Bot/Intents =========
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True   # on_message 用
intents.members = True    # ロール判定に必要（Server Members Intent をDev PortalでONに）
bot = commands.Bot(command_prefix="!", intents=intents)

JST = ZoneInfo("Asia/Tokyo")

# ========= ユーティリティ =========
def make_thread_name(display_name: str, base_time: datetime) -> str:
    """スレッド名: ユーザー名/月/日"""
    due = (base_time + timedelta(days=10)).astimezone(JST)
    date_label = f"{due.month}/{due.day}"  # 例: 8/20
    return f"{display_name}/{date_label}"[:95]

async def pick_forum_for_member(guild: discord.Guild, member: discord.Member) -> discord.ForumChannel | None:
    has_male = any(r.id == MALE_ROLE_ID for r in member.roles)
    has_female = any(r.id == FEMALE_ROLE_ID for r in member.roles)

    target_id = None
    if has_male and MALE_FORUM_ID:
        target_id = MALE_FORUM_ID
    elif has_female and FEMALE_FORUM_ID:
        target_id = FEMALE_FORUM_ID
    elif DEFAULT_FORUM_ID:
        target_id = DEFAULT_FORUM_ID

    ch = guild.get_channel(target_id) if target_id else None
    if isinstance(ch, discord.ForumChannel):
        return ch
    return None

def name_belongs_to_user(thread_name: str, display_name: str) -> bool:
    return thread_name.startswith(f"{display_name}/")

async def find_existing_user_thread(forum: discord.ForumChannel, display_name: str) -> discord.Thread | None:
    for t in forum.threads:
        if name_belongs_to_user(t.name, display_name):
            return t

    try:
        async for t in forum.archived_threads(limit=200, private=False):
            if name_belongs_to_user(t.name, display_name):
                return t
    except Exception:
        log.exception("archived_threads の取得に失敗しました。")
    return None

# ========= イベント =========
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log.info(f"監視対象チャンネル: {SOURCE_TEXT_CHANNEL_IDS}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    # 複数指定対応
    if message.channel.id not in SOURCE_TEXT_CHANNEL_IDS:
        return

    member: discord.Member = message.author
    forum = await pick_forum_for_member(message.guild, member)
    if not forum:
        log.error("対象フォーラムが見つかりません。")
        return

    display_name = member.display_name
    base_time = message.created_at or datetime.now(tz=JST)
    if base_time.tzinfo is None:
        base_time = base_time.replace(tzinfo=JST)

    existing = await find_existing_user_thread(forum, display_name)
    if existing:
        log.info(f"Skip: 既に同ユーザー名のスレッドが存在 → {existing.name}")
        return

    thread_name = make_thread_name(display_name, base_time)
    content = (
        f"自動作成: {member.mention} さん用のスレッドです。\n"
        f"ソースメッセージ: {message.jump_url}\n"
        f"期限（目安）: 投稿日+10日＝スレッド名参照"
    )

    try:
        created = await forum.create_thread(
            name=thread_name,
            content=content,
            reason=f"Triggered by message in #{message.channel.name} from {member} ({member.id})",
        )
        log.info(f"Created thread: {created.name} (ID: {created.id}) in forum '{forum.name}'")
    except Exception:
        log.exception("スレッド作成に失敗しました。")

# ========= 起動 =========
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN が未設定です。")
        raise SystemExit(1)
    bot.run(DISCORD_TOKEN)
