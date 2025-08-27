import os
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Set, Dict
import asyncio

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

# スレッドリンク保存先（JSON）
THREAD_LINKS_FILE = os.getenv("THREAD_LINKS_FILE", "data/thread_links.json")

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
intents.messages = True           # on_message 用
intents.members = True            # ロール判定（Dev Portal で Server Members Intent をON）
intents.message_content = False   # 内容は使わない
bot = commands.Bot(command_prefix="!", intents=intents)

JST = ZoneInfo("Asia/Tokyo")

# ========= 永続化（メッセージ→スレッド紐付け） =========
_links_lock = asyncio.Lock()
# 形式: { "<message_id>": [<thread_id>, ...] }
_links: Dict[str, List[int]] = {}

def _ensure_dir(path: str):
    dirpath = os.path.dirname(path)
    if dirpath:
        os.makedirs(dirpath, exist_ok=True)

def load_links():
    global _links
    try:
        if os.path.exists(THREAD_LINKS_FILE):
            with open(THREAD_LINKS_FILE, "r", encoding="utf-8") as f:
                _links = json.load(f)
        else:
            _links = {}
    except Exception:
        log.exception("リンクファイルの読み込みに失敗しました。初期化します。")
        _links = {}

def save_links():
    _ensure_dir(THREAD_LINKS_FILE)
    try:
        with open(THREAD_LINKS_FILE, "w", encoding="utf-8") as f:
            json.dump(_links, f, ensure_ascii=False, indent=2)
    except Exception:
        log.exception("リンクファイルの保存に失敗しました。")

async def add_link(message_id: int, thread_id: int):
    async with _links_lock:
        key = str(message_id)
        _links.setdefault(key, [])
        if thread_id not in _links[key]:
            _links[key].append(thread_id)
            save_links()

async def pop_links(message_id: int) -> List[int]:
    """削除時に対応スレッドID群を取り出す（なければ空）。"""
    async with _links_lock:
        key = str(message_id)
        ids = _links.pop(key, [])
        if ids:
            save_links()
        return ids

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
    # アクティブ
    for t in forum.threads:
        if name_belongs_to_user(t.name, display_name):
            return t
    # アーカイブ済み（※ private 引数は不要）
    try:
        async for t in forum.archived_threads(limit=200):
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
    load_links()
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log.info(f"監視対象テキスト: {SOURCE_TEXT_CHANNEL_IDS}")
    log.info(f"男性フォーラム: {MALE_FORUM_IDS} / 女性フォーラム: {FEMALE_FORUM_IDS} / デフォルト: {DEFAULT_FORUM_IDS}")
    if not SOURCE_TEXT_CHANNEL_IDS:
        log.warning("SOURCE_TEXT_CHANNEL_IDS が未設定です。")

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
    content = message.jump_url  # ← リンクのみ

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
            # create_thread は ThreadWithMessage を返す → thread を取り出す
            thread_obj = created.thread if hasattr(created, "thread") else created
            await add_link(message.id, thread_obj.id)

            log.info(f"[OK] Created thread: {thread_obj.name} (ID: {thread_obj.id}) in forum '{forum.name}'")
        except discord.Forbidden:
            log.exception(f"[NG] 権限不足で作成失敗: forum '{forum.name}'")
        except discord.HTTPException:
            log.exception(f"[NG] HTTPエラーで作成失敗: forum '{forum.name}'")
        except Exception:
            log.exception(f"[NG] 想定外のエラー: forum '{forum.name}'")

@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    """
    メッセージが削除されたら、紐付いたスレッドを削除。
    Raw イベントなので、キャッシュに無いメッセージでも反応可能。
    """
    msg_id = payload.message_id
    thread_ids = await pop_links(msg_id)
    if not thread_ids:
        return

    for tid in thread_ids:
        try:
            ch = await bot.fetch_channel(tid)  # Thread を取得
            if isinstance(ch, discord.Thread):
                await ch.delete(reason=f"Source message {msg_id} deleted; auto-clean thread.")
                log.info(f"[OK] Deleted thread {tid} due to source message deletion.")
        except discord.NotFound:
            log.info(f"[Skip] Thread {tid} not found (already deleted?).")
        except discord.Forbidden:
            log.exception(f"[NG] 権限不足でスレッド削除失敗: thread {tid}")
        except discord.HTTPException:
            log.exception(f"[NG] HTTPエラーでスレッド削除失敗: thread {tid}")
        except Exception:
            log.exception(f"[NG] 想定外のエラー: thread {tid}")

# ========= 起動 =========
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN が未設定です。")
        raise SystemExit(1)
    bot.run(DISCORD_TOKEN)
