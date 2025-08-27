import os
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Set

import discord
from discord.ext import commands

# ========= 環境変数 =========
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")  # 必須

# 監視テキストch（複数可）
SOURCE_TEXT_CHANNEL_IDS = [
    int(x.strip()) for x in os.getenv("SOURCE_TEXT_CHANNEL_IDS", "").split(",") if x.strip().isdigit()
]

# ロールID
MALE_ROLE_ID = int(os.getenv("MALE_ROLE_ID", "1399390214295785623"))
FEMALE_ROLE_ID = int(os.getenv("FEMALE_ROLE_ID", "1399390384756363264"))

# フォーラムch ID（複数可・カンマ区切り）
def parse_id_list(env_name: str) -> List[int]:
    return [int(x.strip()) for x in os.getenv(env_name, "").split(",") if x.strip().isdigit()]

MALE_FORUM_IDS: List[int] = parse_id_list("MALE_FORUM_IDS")
FEMALE_FORUM_IDS: List[int] = parse_id_list("FEMALE_FORUM_IDS")
DEFAULT_FORUM_IDS: List[int] = parse_id_list("DEFAULT_FORUM_IDS")  # 任意

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
intents.members = True    # ロール判定に必要（Dev Portalで Server Members Intent をON）
bot = commands.Bot(command_prefix="!", intents=intents)

JST = ZoneInfo("Asia/Tokyo")

# ========= ユーティリティ =========
def make_thread_name(display_name: str, base_time: datetime) -> str:
    """スレッド名: ユーザー名/月/日（投稿日+10日基準）"""
    due = (base_time + timedelta(days=10)).astimezone(JST)
    date_label = f"{due.month}/{due.day}"  # 例: 8/20
    return f"{display_name}/{date_label}"[:95]

def name_belongs_to_user(thread_name: str, display_name: str) -> bool:
    # 「ユーザー名/xxxx」で始まっていれば同一ユーザー扱い
    return thread_name.startswith(f"{display_name}/")

async def find_existing_user_thread(forum: discord.ForumChannel, display_name: str) -> discord.Thread | None:
    """同ユーザー名先頭のスレッドがそのフォーラムにあるか（アクティブ＋アーカイブ）"""
    # アクティブスレッド
    for t in forum.threads:
        if name_belongs_to_user(t.name, display_name):
            return t

    # アーカイブ済み
    try:
        async for t in forum.archived_threads(limit=200, private=False):
            if name_belongs_to_user(t.name, display_name):
                return t
    except Exception:
        log.exception("archived_threads の取得に失敗しました。")
    return None

def gather_target_forums(guild: discord.Guild, member: discord.Member) -> List[discord.ForumChannel]:
    """メンバーのロールに応じて、作成先フォーラム（複数）を収集"""
    has_male = any(r.id == MALE_ROLE_ID for r in member.roles)
    has_female = any(r.id == FEMALE_ROLE_ID for r in member.roles)

    id_candidates: List[int] = []
    if has_male:
        id_candidates += MALE_FORUM_IDS
    if has_female:
        id_candidates += FEMALE_FORUM_IDS
    if not id_candidates:
        id_candidates += DEFAULT_FORUM_IDS

    # 重複除去しつつ、ForumChannel のみ返す
    seen: Set[int] = set()
    forums: List[discord.ForumChannel] = []
    for fid in id_candidates:
        if fid in seen:
            continue
        seen.add(fid)
        ch = guild.get_channel(fid)
        if isinstance(ch, discord.ForumChannel):
            forums.append(ch)
    return forums

# ========= イベント =========
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log.info(f"監視対象テキスト: {SOURCE_TEXT_CHANNEL_IDS}")
    log.info(f"男性フォーラム: {MALE_FORUM_IDS} / 女性フォーラム: {FEMALE_FORUM_IDS} / デフォルト: {DEFAULT_FORUM_IDS}")

@bot.event
async def on_message(message: discord.Message):
    # Bot・DMは無視
    if message.author.bot or message.guild is None:
        return
    if not SOURCE_TEXT_CHANNEL_IDS or message.channel.id not in SOURCE_TEXT_CHANNEL_IDS:
        return

    member: discord.Member = message.author
    forums = gather_target_forums(message.guild, member)
    if not forums:
        log.error("対象フォーラムが見つかりません（ロール→フォーラム対応 or DEFAULT_FORUM_IDS を確認）。")
        return

    display_name = member.display_name
    base_time = message.created_at or datetime.now(tz=JST)
    if base_time.tzinfo is None:
        base_time = base_time.replace(tzinfo=JST)

    thread_name = make_thread_name(display_name, base_time)
    content = message.jump_url  # ← ご要望どおり「リンクのみ」

    for forum in forums:
        try:
            # フォーラムごとに「同ユーザー名先頭のスレ」が既にあるか確認
            existing = await find_existing_user_thread(forum, display_name)
            if existing:
                log.info(f"[Skip] 既存スレあり: {existing.name} (forum: {forum.name})")
                continue

            created = await forum.create_thread(
                name=thread_name,
                content=content,
                reason=f"Triggered by message in #{message.channel.name} from {member} ({member.id})",
            )
            log.info(f"[OK] Created thread: {created.name} in forum '{forum.name}'")
        except discord.Forbidden:
            log.exception(f"[NG] 権限不足で作成失敗: forum '{forum.name}'")
        except discord.HTTPException:
            log.exception(f"[NG] HTTPエラーで作成失敗: forum '{forum.name}'")
        except Exception:
            log.exception(f"[NG] 想定外のエラー: forum '{forum.name}'")

# ========= 起動 =========
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN が未設定です。")
        raise SystemExit(1)
    bot.run(DISCORD_TOKEN)
