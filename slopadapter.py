import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import time
import asyncio
import aiosqlite
import statistics
import math
import re
import datetime
import hashlib
from typing import Optional, Dict, Any, List, Tuple, Set

# =====================================================================
# 1. GLOBAL CONFIGURATION & PERMISSION MATRIX
# =====================================================================

BOT_TOKEN = "YOUR_TOKEN"
PROXY_URL = None

# Authorized Platform IDs (guild_id -> platform name).
# The bot will strictly refuse to operate on any Discord server not listed here.
REGISTERED_PLATFORM_ID: int = 0
REGISTERED_PLATFORMS: Dict[int, str] = {
    # 123456789012345678: "General",
}

POLLS_CHANNEL_IDS: List[int] = [
    # 123456789012345679,
    # 123456789012345685,
]

ELECTIONS_CHANNEL_ID = 0
APPEALS_CHANNEL_ID = 0
MOD_LOG_CHANNEL_ID = 0

MEMBER_ROLE_ID = 0
ISOLATION_ROLE_ID = 0

MANAGER_ROLES: Dict[int, str] = {
    # 111111111111111111: "moderator",
    # 222222222222222222: "admin",
    # 333333333333333333: "general admin"
}

COMMAND_PERMISSIONS: Dict[str, List[int]] = {
    "rreject":        [],
    "rconfirm":       [],
    "risolation":     [],
    "runisolation":   [],
    "relection":      [],
    "runexecutable":  [],
    "rapprove_twink": [],
    "rstatistics":    []
}

ACI_CROSS_CONTROL_WINDOW = 86400
ACI_CRIT_DISPUTE_WINDOW = 172800  # 48 hours for Tier IV (Crit) ACI dispute (SVA V. RH)
RELECTION_DELAY_SECONDS = 1800
THIRTY_DAYS_SECONDS = 2592000

DAILY_ACTION_LIMIT = 3
DAILY_LIMIT_WINDOW = 86400

# =====================================================================
# 2. SVA V. RH & ACI STANDARD PARAMETERS
# =====================================================================

# Quorum presets: SVA V. RH active table (20/30/40/50) and high-scale reference (15/30/50/75)
USE_HIGH_SCALE_QUORUM = False

TIERS: Dict[str, Dict[str, int]] = {
    "Low": {
        "win_pct": 50,
        "cooldown": 0,
        "quorum_pct": 15 if USE_HIGH_SCALE_QUORUM else 20,
        "duration": 86400
    },
    "Mid": {
        "win_pct": 50,
        "cooldown": 10800,
        "quorum_pct": 30,
        "duration": 172800
    },
    "High": {
        "win_pct": 60,
        "cooldown": 43200,
        "quorum_pct": 50 if USE_HIGH_SCALE_QUORUM else 40,
        "duration": 259200
    },
    "Crit": {
        "win_pct": 70,
        "cooldown": 86400,
        "quorum_pct": 75 if USE_HIGH_SCALE_QUORUM else 50,
        "duration": 604800
    }
}

TIER_ROMAN: Dict[str, str] = {
    "Low":  "I. Low",
    "Mid":  "II. Mid",
    "High": "III. High",
    "Crit": "IV. Crit"
}

CATEGORIES: Dict[str, str] = {
    # I. Low
    "Косметика": "Low",
    "Ивенты": "Low",
    "Опросы": "Low",
    # II. Mid
    "Каналы": "Mid",
    "Роли": "Mid",
    "Формат": "Mid",
    # III. High
    "Правосудие": "High",
    "Локальные правила": "High",
    "Стафф": "High",
    "Подконтрольные Ресурсы": "High",
    "Интеграции": "High",
    "Локальное лого": "High",
    # IV. Crit
    "Название": "Crit",
    "Лого": "Crit",
    "Система голосования": "Crit",
    "Площадки и отдельные Ресурсы": "Crit",
    "Фундамент": "Crit"
}

DURATION_UNITS: Dict[str, int] = {
    "sec": 1,
    "s": 1,
    "min": 60,
    "hr": 3600,
    "h": 3600,
    "day": 86400,
    "d": 86400,
    "mon": 2592000,
    "mo": 2592000,
    "m": 2592000,
    "yr": 31536000,
    "y": 31536000
}
DURATION_TOKEN_RE = re.compile(r"(\d+)\s*(sec|min|mon|day|hr|mo|yr|s|h|d|m|y)\b")
WHITESPACE_RE = re.compile(r"\s+")
SNOWFLAKE_RE = re.compile(r"\d{15,22}")
DISCORD_MAX_TIMEOUT_SECONDS = 2419200

# =====================================================================
# 3. STORAGE, CACHE, DATABASE & BOT INITIALIZATION
# =====================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POLLS_DIR = os.path.join(BASE_DIR, "polls_data")
ARCHIVE_DIR = os.path.join(BASE_DIR, "polls_archive")
ELECTIONS_DIR = os.path.join(BASE_DIR, "elections_jsons")
STATE_DIR = os.path.join(BASE_DIR, "system_state")
DB_FILE = os.path.join(BASE_DIR, "activity.db")
DASHBOARD_STATE_FILE = os.path.join(STATE_DIR, "dashboard.json")

for directory in [POLLS_DIR, ARCHIVE_DIR, ELECTIONS_DIR, STATE_DIR]:
    os.makedirs(directory, exist_ok=True)

file_lock = asyncio.Lock()
POLLS_CACHE: Dict[str, Dict[str, Any]] = {}
DASHBOARD_CACHE: Optional[Dict[str, Any]] = None

MESSAGE_LOG_BUFFER: List[Tuple[int, int, float]] = []
buffer_lock = asyncio.Lock()

ACTIVE_STATS_CACHE: Dict[int, Tuple[float, int, float, List[Tuple[int, int]]]] = {}
ACTIVE_STATS_TTL = 30.0

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")


def get_registered_platform_ids() -> Set[int]:
    """Returns the set of explicitly authorized Platform (guild) IDs."""
    allowed: Set[int] = {int(gid) for gid in REGISTERED_PLATFORMS.keys() if int(gid) > 0}
    if REGISTERED_PLATFORM_ID and REGISTERED_PLATFORM_ID > 0:
        allowed.add(int(REGISTERED_PLATFORM_ID))
    return allowed


def is_authorized_platform(guild_id: Optional[int]) -> bool:
    """Checks whether the given guild_id belongs to REGISTERED_PLATFORMS / REGISTERED_PLATFORM_ID."""
    if guild_id is None:
        return False
    allowed = get_registered_platform_ids()
    return int(guild_id) in allowed


def get_authorized_guilds() -> List[discord.Guild]:
    """Returns all connected Discord guilds that are authorized in REGISTERED_PLATFORMS."""
    allowed = get_registered_platform_ids()
    return [g for g in bot.guilds if g.id in allowed]


def write_json_atomic(filepath: str, data: Dict[str, Any]) -> None:
    """Atomically writes JSON data via a temporary file."""
    tmp_path = f"{filepath}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp_path, filepath)


def load_polls_into_cache() -> None:
    """Loads all active poll JSON files from disk into memory."""
    POLLS_CACHE.clear()
    for fname in os.listdir(POLLS_DIR):
        if not fname.endswith(".json"):
            continue
        poll_id = fname[:-5]
        fpath = os.path.join(POLLS_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                POLLS_CACHE[poll_id] = json.load(f)
        except Exception as e:
            print(f"[Cache Load Error on {fname}]: {e}")


def get_poll_data(poll_id: str, include_archive: bool = False) -> Optional[Dict[str, Any]]:
    """Retrieves poll data from memory cache, disk, or archive."""
    poll_id = str(poll_id).strip()
    if poll_id in POLLS_CACHE:
        return POLLS_CACHE[poll_id]

    filepath = os.path.join(POLLS_DIR, f"{poll_id}.json")
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            POLLS_CACHE[poll_id] = data
            return data
        except Exception:
            return None

    if include_archive:
        archive_path = os.path.join(ARCHIVE_DIR, f"{poll_id}.json")
        if os.path.exists(archive_path):
            try:
                with open(archive_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return None
    return None


def save_poll_data(poll_id: str, data: Dict[str, Any]) -> None:
    """Updates poll data in memory cache and persists it to disk."""
    poll_id_str = str(poll_id)
    POLLS_CACHE[poll_id_str] = data
    filepath = os.path.join(POLLS_DIR, f"{poll_id_str}.json")
    write_json_atomic(filepath, data)


def ensure_sample_election_template() -> None:
    """Generates a commented-out sample election template if missing."""
    sample_path = os.path.join(ELECTIONS_DIR, "example.json")
    if not os.path.exists(sample_path):
        commented_template = (
            "/*\n"
            "=====================================================================\n"
            "ПРИМЕР ШАБЛОНА АНКЕТЫ ДЛЯ ВЫБОРОВ (/rpollelection) — НЕРАБОЧИЙ ФАЙЛ\n"
            "=====================================================================\n"
            "Чтобы создать рабочую анкету для конкретной роли:\n"
            "1. Скопируйте этот файл и назовите его по ID роли (например: 123456789012345678.json)\n"
            "2. Удалите строки комментариев /* и */ в начале и конце файла, а также комментарии //\n"
            "3. Укажите реальный role_id и role_name вашей должности.\n"
            "\n"
            "{\n"
            '    "role_id": 0, // Цифровой ID роли в Discord\n'
            '    "role_name": "Модератор", // Точное название роли\n'
            '    "tier": "High", // Уровень значимости: Low, Mid, High или Crit\n'
            '    "win_pct": 60, // Процент одобрения для победы\n'
            '    "cooldown": 43200, // Время охлаждения в секундах (43200 = 12 часов)\n'
            '    "quorum_pct": 40, // Кворум от активных пользователей в %\n'
            '    "duration": 259200, // Длительность голосования в секундах (259200 = 3 дня)\n'
            '    "description": "Квалификационная анкета кандидата на должность Модератора сообщества «Горизонт» (п. 7.1, 7.2 Регламента).",\n'
            '    "questions": [\n'
            '        "1. Ваш часовой пояс и среднее время онлайна в сутки?",\n'
            '        "2. Был ли у вас опыт модерации и работы со Стандартом ACI?",\n'
            '        "3. Как вы поступите, если другой Управленец нарушит п. 4.3 (скрытая модерация)?",\n'
            '        "4. В каких случаях допустимо применение Изоляции (п. 3.5)?",\n'
            '        "5. Почему ваша деятельность не приведёт к злоупотреблению привилегиями (Раздел 4)?",\n'
            '        "6. Опишите, как работает перекрёстный контроль валидации (п. 3.3.1).",\n'
            '        "7. Ваша главная цель на посту Управленца в ближайший месяц?"\n'
            '    ]\n'
            "}\n"
            "*/\n"
        )
        with open(sample_path, "w", encoding="utf-8") as f:
            f.write(commented_template)


async def setup_db() -> None:
    """Initializes SQLite schema and indices."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")

        await db.execute('''CREATE TABLE IF NOT EXISTS message_logs (
            user_id INTEGER,
            guild_id INTEGER,
            timestamp REAL
        )''')
        await db.execute('''CREATE INDEX IF NOT EXISTS idx_guild_time 
            ON message_logs(guild_id, timestamp)''')

        await db.execute('''CREATE TABLE IF NOT EXISTS isolations (
            user_id INTEGER,
            guild_id INTEGER,
            moderator_id INTEGER,
            reason TEXT,
            start_time INTEGER,
            end_time INTEGER,
            saved_roles TEXT,
            is_twink INTEGER,
            PRIMARY KEY (user_id, guild_id)
        )''')

        await db.execute('''CREATE TABLE IF NOT EXISTS pending_appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            target_user_id INTEGER,
            role_id INTEGER,
            initiator_id INTEGER,
            reason TEXT,
            execute_at INTEGER,
            executed INTEGER DEFAULT 0
        )''')

        await db.execute('''CREATE TABLE IF NOT EXISTS command_usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            guild_id INTEGER,
            command_name TEXT,
            timestamp INTEGER
        )''')
        await db.execute('''CREATE INDEX IF NOT EXISTS idx_cmd_usage 
            ON command_usage_logs(user_id, guild_id, command_name, timestamp)''')

        # Officially approved technical secondary accounts without voting rights (Rule 2.6 exception)
        await db.execute('''CREATE TABLE IF NOT EXISTS approved_twinks (
            twink_user_id INTEGER,
            guild_id INTEGER,
            owner_user_id INTEGER,
            poll_id TEXT,
            approved_at INTEGER,
            PRIMARY KEY (twink_user_id, guild_id)
        )''')

        await db.commit()


async def flush_message_logs() -> None:
    """Flushes buffered message activity metadata to SQLite."""
    global MESSAGE_LOG_BUFFER
    async with buffer_lock:
        if not MESSAGE_LOG_BUFFER:
            return
        batch = MESSAGE_LOG_BUFFER
        MESSAGE_LOG_BUFFER = []

    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.executemany(
                "INSERT INTO message_logs (user_id, guild_id, timestamp) VALUES (?, ?, ?)",
                batch
            )
            await db.commit()
    except Exception as e:
        print(f"[Activity Flush Error]: {e}")


# =====================================================================
# 4. PERMISSIONS, CHANNELS, LIMITS & QUORUM UTILITIES
# =====================================================================

async def is_approved_non_voting_twink(user_id: int, guild_id: int) -> Optional[int]:
    """Checks if the account is registered as a technical secondary account without voting rights (Rule 2.6)."""
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT owner_user_id FROM approved_twinks WHERE twink_user_id = ? AND guild_id = ?",
                (user_id, guild_id)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return int(row[0])
    except Exception:
        pass
    return None


def resolve_poll_destination_channel(
    guild: discord.Guild,
    fallback_channel: Optional[discord.abc.Messageable],
    explicit_channel_id: Optional[int] = None,
    requested_poll_channel: Optional[discord.TextChannel] = None
) -> Tuple[Optional[discord.abc.Messageable], Optional[str]]:
    """Resolves and validates the target guild channel for poll publication."""
    if explicit_channel_id and explicit_channel_id > 0:
        ch = guild.get_channel(explicit_channel_id)
        if ch:
            return ch, None

    valid_poll_channels = [cid for cid in POLLS_CHANNEL_IDS if cid and cid > 0]

    if requested_poll_channel:
        if valid_poll_channels and requested_poll_channel.id not in valid_poll_channels:
            allowed_mentions = ", ".join(f"<#{cid}>" for cid in valid_poll_channels)
            return None, (
                f"❌ Канал {requested_poll_channel.mention} не входит в список разрешённых каналов для голосований!\n"
                f"Доступные каналы: {allowed_mentions}"
            )
        return requested_poll_channel, None

    is_guild_fallback = (
        fallback_channel is not None
        and not isinstance(fallback_channel, (discord.DMChannel, discord.GroupChannel))
        and getattr(fallback_channel, "guild", None) == guild
    )

    if valid_poll_channels:
        if is_guild_fallback and getattr(fallback_channel, "id", 0) in valid_poll_channels:
            return fallback_channel, None
        for cid in valid_poll_channels:
            ch = guild.get_channel(cid)
            if ch:
                return ch, None

    if is_guild_fallback:
        return fallback_channel, None

    for ch in guild.text_channels:
        perms = ch.permissions_for(guild.me) if guild.me else None
        if not perms or (perms.send_messages and perms.embed_links):
            return ch, None

    return None, "❌ На Площадке не найден подходящий текстовый канал для публикации голосования."


async def check_daily_command_limit(user_id: int, guild_id: int, command_name: str) -> Tuple[bool, int, int]:
    """Checks 24-hour rate limit for privileged commands. Returns (allowed, used_count, reset_ts)."""
    now = int(time.time())
    window_start = now - DAILY_LIMIT_WINDOW
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            '''SELECT timestamp FROM command_usage_logs
               WHERE user_id = ? AND guild_id = ? AND command_name = ? AND timestamp > ?
               ORDER BY timestamp ASC''',
            (user_id, guild_id, command_name, window_start)
        ) as cursor:
            rows = await cursor.fetchall()

    used_count = len(rows)
    if used_count >= DAILY_ACTION_LIMIT:
        oldest_ts = int(rows[0][0])
        return False, used_count, oldest_ts + DAILY_LIMIT_WINDOW

    return True, used_count, 0


async def record_command_usage(user_id: int, guild_id: int, command_name: str) -> None:
    """Logs execution of a rate-limited command."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO command_usage_logs (user_id, guild_id, command_name, timestamp) VALUES (?, ?, ?, ?)",
            (user_id, guild_id, command_name, int(time.time()))
        )
        await db.commit()


def parse_isolation_duration(duration_str: str) -> Optional[int]:
    """Parses duration tokens (sec/s, min, h/hr, d/day, mon/mo/m, y/yr) into seconds. Returns 0 for indefinite, None if invalid."""
    cleaned = duration_str.strip().lower()
    if not cleaned or cleaned == "0":
        return 0

    matches = DURATION_TOKEN_RE.findall(cleaned)
    if not matches:
        return None

    reconstructed = "".join(f"{val}{unit}" for val, unit in matches)
    if WHITESPACE_RE.sub("", cleaned) != reconstructed:
        return None

    return sum(int(val) * DURATION_UNITS[unit] for val, unit in matches)


def format_duration_human(seconds: int) -> str:
    """Formats seconds into a human-readable duration string."""
    if seconds <= 0:
        return "До окончания публичного разбирательства / апелляции"
    parts = []
    for unit_name, unit_sec in [("г.", 31536000), ("мес.", 2592000), ("дн.", 86400), ("ч.", 3600), ("мин.", 60), ("сек.", 1)]:
        if seconds >= unit_sec:
            count, seconds = divmod(seconds, unit_sec)
            parts.append(f"{count} {unit_name}")
    return " ".join(parts)


def has_command_permission(member: discord.Member, command_key: str) -> bool:
    """Verifies whether the member holds an authorized role for command_key."""
    allowed_roles = set(COMMAND_PERMISSIONS.get(command_key, ()))
    if not allowed_roles:
        return any(r.id in MANAGER_ROLES for r in member.roles)
    return any(role.id in allowed_roles for role in member.roles)


async def check_full_member_status(member: discord.Member) -> Tuple[bool, str]:
    """Verifies platform authorization, full member status, absence of isolation, and non-twink status."""
    if not is_authorized_platform(member.guild.id):
        return False, "⛔ Данный сервер не входит в список авторизованных Площадок АСУ (`REGISTERED_PLATFORMS`)."

    user_role_ids = {r.id for r in member.roles}
    if ISOLATION_ROLE_ID > 0 and ISOLATION_ROLE_ID in user_role_ids:
        return False, (
            "❌ Вы находитесь в статусе **Ограниченного участника (Изоляция, п. 3.5)**. "
            "Ваше право на участие в самоуправлении приостановлено (Раздел 5). "
            "Для оспаривания используйте `/rappeal` в ЛС бота."
        )

    if MEMBER_ROLE_ID > 0 and member.guild.get_role(MEMBER_ROLE_ID) is not None and MEMBER_ROLE_ID not in user_role_ids:
        return False, (
            "❌ У вас отсутствует роль **Полноценного участника** (п. 0.3 Регламента). "
            "До одобрения заявки вы не имеете права голоса."
        )

    owner_id = await is_approved_non_voting_twink(member.id, member.guild.id)
    if owner_id is not None:
        return False, (
            f"❌ Данный аккаунт зарегистрирован в реестре АСУ как **дополнительный технический аккаунт без права голоса** "
            f"(п. 2.6 Регламента, владелец: <@{owner_id}>). Голосование и запуск инициатив с него заблокированы."
        )

    return True, ""


async def get_active_users_stats(guild_id: int) -> Tuple[int, float, List[Tuple[int, int]]]:
    """Calculates 30-day activity median and active user count for the Platform (Rule 6.7)."""
    if not is_authorized_platform(guild_id):
        return 0, 0.0, []

    await flush_message_logs()
    now = time.time()

    cached = ACTIVE_STATS_CACHE.get(guild_id)
    if cached and (now - cached[0]) < ACTIVE_STATS_TTL:
        return cached[1], cached[2], cached[3]

    thirty_days_ago = now - THIRTY_DAYS_SECONDS
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                '''SELECT user_id, COUNT(*) as msg_count 
                   FROM message_logs 
                   WHERE guild_id = ? AND timestamp >= ? 
                   GROUP BY user_id 
                   ORDER BY msg_count DESC''',
                (guild_id, thirty_days_ago)
            ) as cursor:
                results = await cursor.fetchall()

        if not results:
            ACTIVE_STATS_CACHE[guild_id] = (now, 0, 0.0, [])
            return 0, 0.0, []

        counts = [row[1] for row in results if row[1] > 0]
        if not counts:
            ACTIVE_STATS_CACHE[guild_id] = (now, 0, 0.0, [])
            return 0, 0.0, []

        threshold = float(statistics.median(counts))
        active_users = [(int(row[0]), int(row[1])) for row in results if row[1] >= threshold]
        active_len = len(active_users)
        ACTIVE_STATS_CACHE[guild_id] = (now, active_len, threshold, active_users)
        return active_len, threshold, active_users
    except Exception as e:
        print(f"[DB Error in get_active_users_stats]: {e}")
        return 0, 0.0, []


async def log_moderation_action(guild: discord.Guild, embed: discord.Embed) -> None:
    """Sends a moderation audit embed to MOD_LOG_CHANNEL_ID."""
    if not is_authorized_platform(guild.id):
        return
    if not MOD_LOG_CHANNEL_ID or MOD_LOG_CHANNEL_ID <= 0:
        return
    channel = guild.get_channel(MOD_LOG_CHANNEL_ID)
    if not channel:
        try:
            channel = await guild.fetch_channel(MOD_LOG_CHANNEL_ID)
        except Exception:
            return
    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[ModLog Error]: {e}")


# =====================================================================
# 5. EMBED BUILDERS & UI SYNCHRONIZATION
# =====================================================================

def build_poll_embed(data: Dict[str, Any]) -> discord.Embed:
    """Constructs the poll status embed according to SVA V. RH and Регламент."""
    status = data.get("status", "active")
    poll_type = data.get("poll_type", "standard")
    poll_id = data.get("poll_message_id", "ID_ГОЛОСОВАНИЯ")
    voters_count = len(data.get("votes", {}))
    tier_code = data.get("tier", "Low")
    tier_label = TIER_ROMAN.get(tier_code, tier_code)

    if status == "cooldown":
        title_prefix = "🧊 [Охлаждение]"
        color = discord.Color.blue()
    elif status == "active":
        title_prefix = "🌐 [Активно]" if tier_code == "Crit" else "📊 [Активно]"
        color = discord.Color.teal() if tier_code == "Crit" else discord.Color.green()
    elif status == "rejected_frozen":
        title_prefix = "⏸️ [Заморожено]"
        color = discord.Color.orange()
    elif status == "rejected_final":
        title_prefix = "❌ [Аннулировано]"
        color = discord.Color.red()
    else:
        title_prefix = "🔒 [Завершено]"
        color = discord.Color.dark_grey()

    type_titles = {
        "standard": "Голосование",
        "impeach": "Снятие Управленца (п. 6.8)",
        "direct_election": "Прямое выдвижение (п. 7.1.1)",
        "template_election": f"Выборы: {data.get('target_role_name', 'Управленец')}",
        "appeal": "Публичная апелляция (п. 5.1)"
    }
    embed_title = f"{title_prefix} {type_titles.get(poll_type, 'Голосование')}"

    formatted_quote = "\n".join(
        f"> **{line}**" if (poll_type == "impeach" and line.strip()) else (f"> {line}" if line.strip() else ">")
        for line in data.get("quote_content", "").split("\n")
    )
    if len(formatted_quote) > 2300:
        formatted_quote = formatted_quote[:2300] + "\n> *...[текст сокращён]*"

    desc_blocks = []

    if poll_type == "impeach":
        desc_blocks.append(
            "┏ ⚠️ **Специальное шаблонное голосование (п. 6.8 Регламента)**\n"
            "┃ **Единственная функция: досрочное снятие полномочий Управленца в обход ACI.**\n"
            "┗ **Обладает полным системным иммунитетом от проверки и отмены по Стандарту ACI.**"
        )

    desc_blocks.append(
        f"### Повестка (Автор: **{data.get('target_author_name', 'Участник')}**)\n"
        f"{formatted_quote}"
    )

    embed = discord.Embed(
        title=embed_title,
        description="\n\n".join(desc_blocks),
        color=color
    )

    embed.add_field(
        name="📂 Классификация",
        value=(
            f"┏ **Категория:** `{data['category']}`\n"
            f"┗ **Тир:** `{tier_label}`"
        ),
        inline=True
    )

    embed.add_field(
        name="⚖️ Пороги SVA V. RH",
        value=(
            f"┏ **Победа:** `> {data['win_pct']}%`\n"
            f"┗ **Кворум ({data.get('quorum_pct', 0)}%):** `{data['quorum_req']} чел.`"
        ),
        inline=True
    )

    embed.add_field(
        name="👥 Явка и Шкала",
        value=(
            f"┏ **Голосов:** `{voters_count} чел.`\n"
            f"┗ **Шкала:** `⏫+2 🔼+1 ⏺️0 🔽-1 ⏬-2`"
        ),
        inline=True
    )

    if data.get("target_user_id") or data.get("target_role_id"):
        target_lines = []
        if data.get("target_user_id"):
            target_lines.append(f"👤 **Участник:** <@{data['target_user_id']}>")
        if data.get("target_role_id"):
            target_lines.append(f"🎖️ **Роль:** <@&{data['target_role_id']}>")
        embed.add_field(
            name="🎯 Цель процедуры",
            value="\n".join(target_lines),
            inline=False
        )

    aci_lines = []
    if data.get("immune_to_aci"):
        aci_lines.append("⚡ **Иммунитет от отмены (п. 6.8)**")
    elif data.get("aci_shield"):
        confirmed_by = data.get("confirmed_by_name", "Управленцем")
        aci_lines.append(f"🛡️ **Подтверждено и защищено щитом ({confirmed_by})**")
    elif data.get("aci_dispute_active"):
        dispute_votes = data.get("aci_dispute_votes", {})
        for_legit = sum(1 for v in dispute_votes.values() if v.get("vote") == 1)
        against_legit = sum(1 for v in dispute_votes.values() if v.get("vote") == -1)
        net_score = for_legit - against_legit
        deadline = data.get("aci_dispute_deadline", 0)
        aci_lines.append(
            f"⚖️ **Идёт 48-часовой спор:**\n"
            f"┏ За (`/rconfirm`): **`+{for_legit}`** | Против (`/rreject`): **`-{against_legit}`** (Баланс: **`{net_score:+d}`**)\n"
            f"┗ Завершение спора: <t:{deadline}:R> (<t:{deadline}:f>)"
        )
    if data.get("protect_from_02"):
        aci_lines.append("🛡️ **Защищено от отмены по п. 0.2 (п. 4.5)**")

    if aci_lines:
        embed.add_field(name="🛡️ Статус проверки", value="\n".join(aci_lines), inline=False)

    if status == "cooldown":
        timer_block = (
            f"┏ ⏳ **Начало голосования:** <t:{data['start_time']}:R> (<t:{data['start_time']}:f>)\n"
            f"┗ 🏁 **Завершение:** <t:{data['end_time']}:f> (<t:{data['end_time']}:R>)"
        )
    elif status == "active":
        timer_block = f"🏁 **Завершение:** <t:{data['end_time']}:f> (<t:{data['end_time']}:R>)"
    elif status == "rejected_frozen":
        rem_min = math.ceil(data.get("remaining_duration", 0) / 60)
        deadline = data.get("appeal_deadline", data.get("end_time", 0))
        timer_block = (
            f"┏ 🛑 **Отклонил:** <@{data.get('reject_by_id', 0)}>\n"
            f"┃ 📌 **Основание:** {data.get('reject_reason', 'Не указано')}\n"
            f"┃ ⏱️ **Остаток времени:** `{rem_min} мин.`\n"
            f"┗ 🔄 **Окно `/rconfirm` (п. 3.3.1) до:** <t:{deadline}:f> (<t:{deadline}:R>)"
        )
    elif status == "rejected_final":
        timer_block = f"❌ **Основание отклонения:** {data.get('reject_reason', 'Несоответствие Стандарту ACI')}"
    else:
        timer_block = f"🔒 **Завершено:** <t:{data['end_time']}:f>"

    embed.add_field(name="⏰ Таймер", value=timer_block, inline=False)

    if data.get("unexecutable_reason"):
        embed.add_field(
            name="⚠️ Статус технической реализации (п. 6.10 / 6.10.1)",
            value=(
                f"**Признано неисполнимым Управленцем <@{data.get('unexecutable_by_id', 0)}>:**\n"
                f"> {data['unexecutable_reason']}"
            ),
            inline=False
        )

    content_hash = data.get("content_sha256", "")[:10]
    embed.set_footer(
        text=f"Инициатор: {data.get('initiator_name', 'АСУ')} • ID: {poll_id} • SHA256 (п. 6.4.1): {content_hash}"
    )
    return embed


async def sync_poll_message_ui(data: Dict[str, Any]) -> None:
    """Synchronizes the Discord message embed and view with current poll state."""
    channel = bot.get_channel(data["channel_id"])
    if not channel:
        try:
            channel = await bot.fetch_channel(data["channel_id"])
        except Exception:
            return
    try:
        poll_message = await channel.fetch_message(int(data["poll_message_id"]))
        await poll_message.edit(embed=build_poll_embed(data), view=PollVotingView(data))
    except Exception:
        pass


# =====================================================================
# 6. INTERACTIVE UI VIEWS & MODALS
# =====================================================================

class ACIReportModal(discord.ui.Modal, title="Запрос на проверку Стандарта ACI"):
    """Modal for submitting an ACI compliance report (Rule 3.3, 6.2)."""
    def __init__(self, poll_id: str, poll_url: str):
        super().__init__()
        self.poll_id = poll_id
        self.poll_url = poll_url

    violation_details = discord.ui.TextInput(
        label="Причина несоответствия Стандарту ACI",
        style=discord.TextStyle.paragraph,
        placeholder="Укажите, почему инициатива не соответствует Стандарту ACI или выбранной Категории/Тиру...",
        required=True,
        max_length=800
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not is_authorized_platform(interaction.guild.id):
            return await interaction.response.send_message(
                "⛔ Доступно только на авторизованной Площадке (`REGISTERED_PLATFORMS`).",
                ephemeral=True
            )

        report_embed = discord.Embed(
            title="🚩 Запрос на валидацию голосования по Стандарту ACI (п. 3.3, 6.2)",
            description=(
                f"**Голосование:** [Перейти к сообщению]({self.poll_url}) (ID: `{self.poll_id}`)\n"
                f"**Заявитель:** {interaction.user.mention} (`{interaction.user.id}`)\n\n"
                f"**Суть несоответствия Стандарту ACI:**\n"
                f"> {self.violation_details.value}\n\n"
                f"🔧 **Действия Управленца (п. 3.3):**\n"
                f"• Подтвердить и выдать щит: `/rconfirm poll_id:{self.poll_id}`\n"
                f"• Отклонить по Стандарту ACI: `/rreject poll_id:{self.poll_id}`"
            ),
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow()
        )
        await log_moderation_action(interaction.guild, report_embed)
        await interaction.response.send_message(
            "✅ Ваш запрос на проверку соответствия Стандарту ACI передан Управленцам.",
            ephemeral=True
        )


class PollVotingView(discord.ui.View):
    """Persistent Likert-scale voting view (+2, +1, 0, -1, -2, vote check, ACI report)."""
    def __init__(self, poll_data: Optional[Dict[str, Any]] = None):
        super().__init__(timeout=None)
        if poll_data is not None:
            self._apply_state(poll_data)

    def _apply_state(self, data: Dict[str, Any]) -> None:
        status = data.get("status", "active")
        immune_to_aci = bool(data.get("immune_to_aci", False))
        aci_shield = bool(data.get("aci_shield", False))
        has_votes = bool(data.get("votes"))

        voting_disabled = (status != "active")

        for btn in (self.vote_plus_2, self.vote_plus_1, self.vote_zero, self.vote_minus_1, self.vote_minus_2):
            btn.disabled = voting_disabled
            if voting_disabled:
                btn.style = discord.ButtonStyle.secondary

        if not voting_disabled:
            self.vote_plus_2.style = discord.ButtonStyle.success
            self.vote_plus_1.style = discord.ButtonStyle.success
            self.vote_zero.style = discord.ButtonStyle.secondary
            self.vote_minus_1.style = discord.ButtonStyle.danger
            self.vote_minus_2.style = discord.ButtonStyle.danger

        check_disabled = (status == "cooldown" and not has_votes)
        self.check_my_vote.disabled = check_disabled
        self.check_my_vote.style = discord.ButtonStyle.secondary if check_disabled else discord.ButtonStyle.primary

        aci_disabled = (
            immune_to_aci
            or aci_shield
            or status in ("rejected_frozen", "finished", "rejected_final")
        )
        self.report_aci.disabled = aci_disabled
        self.report_aci.style = discord.ButtonStyle.secondary

    async def _handle_vote(self, interaction: discord.Interaction, weight: int, label_name: str) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Голосование доступно только на Площадке.", ephemeral=True)

        if not is_authorized_platform(interaction.guild.id):
            return await interaction.response.send_message(
                "⛔ Отказ доступа АСУ: данный сервер не зарегистрирован в `REGISTERED_PLATFORMS`.",
                ephemeral=True
            )

        is_allowed, err_msg = await check_full_member_status(interaction.user)
        if not is_allowed:
            return await interaction.response.send_message(err_msg, ephemeral=True)

        poll_id = str(interaction.message.id)

        async with file_lock:
            data = get_poll_data(poll_id)
            if not data:
                return await interaction.response.send_message("❌ Данные этого голосования не найдены.", ephemeral=True)

            status = data.get("status")
            if status in ("finished", "rejected_final"):
                return await interaction.response.send_message("❌ Это голосование уже окончательно закрыто.", ephemeral=True)
            if status == "rejected_frozen":
                return await interaction.response.send_message(
                    "⏸️ Голосование временно заморожено по ACI. Ожидается перекрёстный контроль (п. 3.3.1).",
                    ephemeral=True
                )
            if status == "cooldown":
                return await interaction.response.send_message(
                    f"🧊 Голосование находится на стадии охлаждения! Отдать голос можно будет <t:{data['start_time']}:R>.",
                    ephemeral=True
                )

            votes = data.setdefault("votes", {})
            user_id_str = str(interaction.user.id)
            votes[user_id_str] = {
                "weight": weight,
                "user_id": interaction.user.id,
                "username": interaction.user.name,
                "vote_name": label_name,
                "timestamp": int(time.time())
            }

            save_poll_data(poll_id, data)
            updated_embed = build_poll_embed(data)
            updated_view = PollVotingView(data)

        try:
            await interaction.message.edit(embed=updated_embed, view=updated_view)
        except Exception as e:
            print(f"[Vote Embed Update Error]: {e}")

        await interaction.response.send_message(
            f"✅ Ваш голос **{label_name}** учтён в АСУ (п. 1.2 Регламента).\n"
            f"🔒 Для защиты от конформизма (п. 6.3) выбранный вами вариант скрыт от других участников. Вы можете изменить решение до окончания таймера.",
            ephemeral=True
        )

    @discord.ui.button(label="⏫", style=discord.ButtonStyle.success, custom_id="asu_vote_plus2", row=0)
    async def vote_plus_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, 2, "⏫ Полностью согласен (+2)")

    @discord.ui.button(label="🔼", style=discord.ButtonStyle.success, custom_id="asu_vote_plus1", row=0)
    async def vote_plus_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, 1, "🔼 Согласен (+1)")

    @discord.ui.button(label="⏺️", style=discord.ButtonStyle.secondary, custom_id="asu_vote_zero", row=0)
    async def vote_zero(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, 0, "⏺️ Воздержусь / Нейтрально (+0)")

    @discord.ui.button(label="🔽", style=discord.ButtonStyle.danger, custom_id="asu_vote_minus1", row=0)
    async def vote_minus_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, -1, "🔽 Не согласен (-1)")

    @discord.ui.button(label="⏬", style=discord.ButtonStyle.danger, custom_id="asu_vote_minus2", row=0)
    async def vote_minus_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, -2, "⏬ Полностью не согласен (-2)")

    @discord.ui.button(label="🔍 Мой голос", style=discord.ButtonStyle.primary, custom_id="asu_vote_check", row=1)
    async def check_my_vote(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not is_authorized_platform(interaction.guild.id):
            return await interaction.response.send_message(
                "⛔ Отказ доступа АСУ: сервер не авторизован в `REGISTERED_PLATFORMS`.",
                ephemeral=True
            )

        poll_id = str(interaction.message.id)
        data = get_poll_data(poll_id, include_archive=True)
        if not data:
            return await interaction.response.send_message("❌ Голосование не найдено в базе данных.", ephemeral=True)

        user_id_str = str(interaction.user.id)
        votes = data.get("votes", {})
        if user_id_str in votes:
            v = votes[user_id_str]
            await interaction.response.send_message(
                f"🗳️ В голосовании `{poll_id}` ваш сохранённый голос:\n👉 **{v.get('vote_name', v['weight'])}**",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(f"ℹ️ Вы ещё не голосовали в опросе `{poll_id}`.", ephemeral=True)

    @discord.ui.button(label="🚩 Жалоба ACI", style=discord.ButtonStyle.secondary, custom_id="asu_vote_report_aci", row=1)
    async def report_aci(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not is_authorized_platform(interaction.guild.id):
            return await interaction.response.send_message(
                "⛔ Отказ доступа АСУ: сервер не авторизован в `REGISTERED_PLATFORMS`.",
                ephemeral=True
            )

        poll_id = str(interaction.message.id)
        data = get_poll_data(poll_id)
        if not data or data.get("status") in ("finished", "rejected_final"):
            return await interaction.response.send_message("❌ Голосование уже завершено.", ephemeral=True)

        if data.get("immune_to_aci"):
            return await interaction.response.send_message(
                "⚡ Это специальное голосование за снятие Управленца (п. 6.8 Регламента) — оно обладает полным системным иммунитетом от проверки и отмены по ACI.",
                ephemeral=True
            )
        if data.get("aci_shield"):
            return await interaction.response.send_message(
                "🛡️ Это голосование уже прошло проверку ACI и защищено щитом от повторного отклонения (п. 3.3.1 Регламента).",
                ephemeral=True
            )

        await interaction.response.send_modal(ACIReportModal(poll_id, interaction.message.jump_url))


# =====================================================================
# 7. MULTI-STEP ELECTION QUESTIONNAIRE MODALS
# =====================================================================

class NextQuestionnairePartView(discord.ui.View):
    """Pagination view for multi-part candidate questionnaires (Rule 7.1)."""
    def __init__(self, candidate_id: int, template_data: Dict[str, Any], collected_answers: List[Tuple[str, str]], next_index: int):
        super().__init__(timeout=900)
        self.candidate_id = candidate_id
        self.template_data = template_data
        self.collected_answers = collected_answers
        self.next_index = next_index

    @discord.ui.button(label="➡️ Перейти к следующей части анкеты", style=discord.ButtonStyle.primary)
    async def continue_questionnaire(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.candidate_id:
            return await interaction.response.send_message("❌ Эту анкету заполняет другой участник!", ephemeral=True)

        modal = ElectionQuestionnaireModal(
            candidate_id=self.candidate_id,
            template_data=self.template_data,
            collected_answers=self.collected_answers,
            start_index=self.next_index
        )
        await interaction.response.send_modal(modal)


class ElectionQuestionnaireModal(discord.ui.Modal):
    """Modal displaying up to 5 role questionnaire prompts per step."""
    def __init__(self, candidate_id: int, template_data: Dict[str, Any], collected_answers: List[Tuple[str, str]], start_index: int = 0):
        questions = template_data.get("questions", [])
        total_parts = math.ceil(len(questions) / 5)
        current_part = (start_index // 5) + 1
        role_name = template_data.get("role_name", "Управленец")

        super().__init__(title=f"Анкета: {role_name} (Часть {current_part}/{total_parts})"[:45])
        self.candidate_id = candidate_id
        self.template_data = template_data
        self.collected_answers = list(collected_answers)
        self.start_index = start_index
        self.current_batch = questions[start_index:start_index + 5]
        self.inputs: List[discord.ui.TextInput] = []

        for q_text in self.current_batch:
            short_label = q_text if len(q_text) <= 45 else q_text[:42] + "..."
            text_input = discord.ui.TextInput(
                label=short_label,
                style=discord.TextStyle.paragraph,
                placeholder=q_text[:100],
                required=True,
                max_length=600
            )
            self.inputs.append(text_input)
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.candidate_id:
            return await interaction.response.send_message("❌ Вы не являетесь автором этой анкеты.", ephemeral=True)

        for q_text, inp in zip(self.current_batch, self.inputs):
            self.collected_answers.append((q_text, inp.value.strip()))

        questions = self.template_data.get("questions", [])
        next_index = self.start_index + 5

        if next_index < len(questions):
            remaining = len(questions) - next_index
            view = NextQuestionnairePartView(
                candidate_id=self.candidate_id,
                template_data=self.template_data,
                collected_answers=self.collected_answers,
                next_index=next_index
            )
            return await interaction.response.send_message(
                f"✅ **Часть сохранена!** Осталось ответить ещё на **{remaining}** вопр.\n"
                f"Нажмите кнопку ниже, чтобы продолжить заполнение анкеты:",
                view=view,
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        qa_formatted = "\n\n".join(
            f"**{q}**\nОтвет: {a}" for q, a in self.collected_answers
        )
        header_desc = self.template_data.get("description", "Выдвижение кандидата по локальной анкете (п. 7.1, 7.2 Регламента).")
        full_content = f"{header_desc}\n\n{qa_formatted}"

        tier_name = self.template_data.get("tier", "High")
        base_tier = TIERS.get(tier_name, TIERS["High"])
        custom_win_pct = self.template_data.get("win_pct", base_tier["win_pct"])
        custom_cooldown = self.template_data.get("cooldown", base_tier["cooldown"])
        custom_quorum_pct = self.template_data.get("quorum_pct", base_tier["quorum_pct"])
        custom_duration = self.template_data.get("duration", base_tier["duration"])

        poll_msg = await launch_poll_instance(
            guild=interaction.guild,
            fallback_channel=interaction.channel,
            initiator=interaction.user,
            target_author_name=interaction.user.display_name,
            quote_content=full_content,
            category="Стафф",
            tier_name=tier_name,
            win_pct=custom_win_pct,
            cooldown_sec=custom_cooldown,
            quorum_pct=custom_quorum_pct,
            duration_sec=custom_duration,
            poll_type="template_election",
            target_user_id=interaction.user.id,
            target_role_id=int(self.template_data["role_id"]),
            target_role_name=self.template_data.get("role_name", "Управленец"),
            protect_from_02=True,
            destination_channel_id=ELECTIONS_CHANNEL_ID
        )

        if poll_msg:
            await interaction.followup.send(
                f"🎉 Ваша анкета успешно опубликована! Запущено шаблонное голосование (п. 6.6, 7.1): {poll_msg.jump_url}",
                ephemeral=True
            )
        else:
            await interaction.followup.send("❌ Не удалось опубликовать анкету. Проверьте настройки канала.", ephemeral=True)


# =====================================================================
# 8. POLL LIFECYCLE & AUTOMATED EXECUTION ENGINE
# =====================================================================

async def launch_poll_instance(
    guild: discord.Guild,
    fallback_channel: Optional[discord.abc.Messageable],
    initiator: discord.abc.User,
    target_author_name: str,
    quote_content: str,
    category: str,
    tier_name: str,
    win_pct: int,
    cooldown_sec: int,
    quorum_pct: int,
    duration_sec: int,
    poll_type: str = "standard",
    target_message_id: int = 0,
    target_user_id: Optional[int] = None,
    target_role_id: Optional[int] = None,
    target_role_name: Optional[str] = None,
    immune_to_aci: bool = False,
    protect_from_02: bool = False,
    destination_channel_id: Optional[int] = None,
    requested_poll_channel: Optional[discord.TextChannel] = None
) -> Optional[discord.Message]:
    """Creates, freezes content snapshot (Rule 6.4.1), publishes, and persists a poll on an authorized Platform."""
    if not is_authorized_platform(guild.id):
        return None

    channel, _ = resolve_poll_destination_channel(
        guild=guild,
        fallback_channel=fallback_channel,
        explicit_channel_id=destination_channel_id,
        requested_poll_channel=requested_poll_channel
    )
    if not channel:
        return None

    active_users_count, _, _ = await get_active_users_stats(guild.id)
    quorum_req = max(1, math.ceil(active_users_count * (quorum_pct / 100.0)))

    # Automatic Rule 4.5 protection from clause 0.2 for Staff/Platform/Appeal polls
    if category in ("Стафф", "Площадки и отдельные Ресурсы") or poll_type in ("impeach", "appeal", "direct_election", "template_election"):
        protect_from_02 = True

    now = int(time.time())
    start_time = now + cooldown_sec
    end_time = start_time + duration_sec
    status = "cooldown" if cooldown_sec > 0 else "active"

    # Immutable cryptographic snapshot of initial proposal composition (Rule 6.4.1)
    snapshot_payload = f"{category}|{tier_name}|{win_pct}|{quorum_pct}|{duration_sec}|{quote_content}"
    content_sha256 = hashlib.sha256(snapshot_payload.encode("utf-8")).hexdigest()

    poll_data: Dict[str, Any] = {
        "poll_message_id": 0,
        "guild_id": guild.id,
        "channel_id": channel.id,
        "poll_type": poll_type,
        "initiator_id": initiator.id,
        "initiator_name": initiator.name,
        "target_message_id": target_message_id,
        "target_author_name": target_author_name,
        "quote_content": quote_content,
        "content_sha256": content_sha256,
        "category": category,
        "tier": tier_name,
        "win_pct": win_pct,
        "quorum_pct": quorum_pct,
        "quorum_req": quorum_req,
        "created_at": now,
        "start_time": start_time,
        "end_time": end_time,
        "status": status,
        "target_user_id": target_user_id,
        "target_role_id": target_role_id,
        "target_role_name": target_role_name,
        "immune_to_aci": immune_to_aci,
        "protect_from_02": protect_from_02,
        "aci_shield": False,
        "votes": {}
    }

    initial_embed = build_poll_embed(poll_data)
    view = PollVotingView(poll_data)
    try:
        poll_msg = await channel.send(embed=initial_embed, view=view)
    except Exception as e:
        print(f"[Launch Poll Send Error]: {e}")
        return None

    poll_data["poll_message_id"] = poll_msg.id
    final_embed = build_poll_embed(poll_data)
    try:
        await poll_msg.edit(embed=final_embed, view=view)
    except Exception:
        pass

    async with file_lock:
        save_poll_data(str(poll_msg.id), poll_data)

    return poll_msg


async def execute_automated_decision(data: Dict[str, Any], guild: discord.Guild) -> str:
    """Executes automated role/isolation actions upon template poll victory (Rule 6.6)."""
    poll_type = data.get("poll_type", "standard")
    target_user_id = data.get("target_user_id")

    if poll_type == "standard" or not target_user_id:
        if data.get("tier") == "Crit":
            return "🌐 **Решение IV Тира (Crit) принято:** Вступило в высшую силу и подлежит обязательной реализации (п. 0.4.1, 3.4, 4.4)."
        return "ℹ️ **Общественное решение принято:** Вступило в законную силу и подлежит реализации (п. 3.4, 4.4)."

    member = guild.get_member(target_user_id)
    if not member:
        try:
            member = await guild.fetch_member(target_user_id)
        except Exception:
            if poll_type == "appeal":
                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute("DELETE FROM isolations WHERE user_id = ? AND guild_id = ?", (target_user_id, guild.id))
                    await db.commit()
            return "⚠️ Автоматическое исполнение не удалось: целевой пользователь покинул Площадку."

    try:
        if poll_type == "impeach":
            roles_to_remove = [r for r in member.roles if r.id in MANAGER_ROLES]
            removed_names = [r.name for r in roles_to_remove]
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason=f"АСУ: Снятие Управленца по итогам голосования #{data['poll_message_id']} (п. 6.8)")

            # Also strip any Manager roles stored in isolations.saved_roles if the user is currently isolated
            stripped_from_iso = False
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute(
                    "SELECT saved_roles FROM isolations WHERE user_id = ? AND guild_id = ?",
                    (target_user_id, guild.id)
                ) as cursor:
                    row = await cursor.fetchone()
                if row and row[0]:
                    try:
                        saved_ids = json.loads(row[0])
                        filtered_ids = [rid for rid in saved_ids if int(rid) not in MANAGER_ROLES]
                        if len(filtered_ids) != len(saved_ids):
                            stripped_from_iso = True
                            for rid in saved_ids:
                                if int(rid) in MANAGER_ROLES:
                                    r_name = MANAGER_ROLES.get(int(rid), str(rid))
                                    if r_name not in removed_names:
                                        removed_names.append(r_name)
                            await db.execute(
                                "UPDATE isolations SET saved_roles = ? WHERE user_id = ? AND guild_id = ?",
                                (json.dumps(filtered_ids), target_user_id, guild.id)
                            )
                            await db.commit()
                    except Exception as e:
                        print(f"[Impeach Isolation Roles Update Error]: {e}")

            if not roles_to_remove and not stripped_from_iso:
                return "ℹ️ На момент завершения голосования у пользователя уже отсутствовали роли Управленца."

            roles_str = ", ".join(removed_names)
            return f"⚡ **АСУ исполнено автоматически (п. 6.6, 6.8):** С пользователя {member.mention} сняты управленческие роли ({roles_str})."

        elif poll_type in ("direct_election", "template_election"):
            role_id = data.get("target_role_id")
            role = guild.get_role(int(role_id)) if role_id else None
            if not role:
                return "⚠️ Автоматическое исполнение не удалось: целевая роль не найдена на Площадке."
            await member.add_roles(role, reason=f"АСУ: Избрание по итогам голосования #{data['poll_message_id']} (Раздел 7)")
            return f"🎖️ **АСУ исполнено автоматически (п. 6.6, Раздел 7):** Пользователю {member.mention} присвоена должность {role.mention}!"

        elif poll_type == "appeal":
            await restore_user_from_isolation(
                guild,
                member,
                reason=f"АСУ: Восстановление статуса Полноценного участника по итогам Публичной апелляции #{data['poll_message_id']} (п. 5.1)"
            )
            return f"🕊️ **АСУ исполнено автоматически (п. 5.1, 6.6):** С пользователя {member.mention} снята Изоляция и возвращены все права Полноценного участника."

    except discord.Forbidden:
        return "⚠️ Ошибка прав Discord: роль бота находится ниже целевой роли в иерархии сервера (п. 6.10)."
    except Exception as e:
        return f"⚠️ Техническая ошибка при автоисполнении АСУ: `{e}`"

    return "ℹ️ Решение зафиксировано."


async def restore_user_from_isolation(guild: discord.Guild, member: discord.Member, reason: str) -> None:
    """Removes isolation status, restores saved roles, and clears timeout/voice mute."""
    iso_role = guild.get_role(ISOLATION_ROLE_ID) if ISOLATION_ROLE_ID > 0 else None
    mem_role = guild.get_role(MEMBER_ROLE_ID) if MEMBER_ROLE_ID > 0 else None

    roles_to_restore: List[discord.Role] = []
    if mem_role:
        roles_to_restore.append(mem_role)

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT saved_roles FROM isolations WHERE user_id = ? AND guild_id = ?",
            (member.id, guild.id)
        ) as cursor:
            row = await cursor.fetchone()
        if row and row[0]:
            try:
                for rid in json.loads(row[0]):
                    r_obj = guild.get_role(int(rid))
                    if r_obj and r_obj not in roles_to_restore:
                        roles_to_restore.append(r_obj)
            except Exception as e:
                print(f"[Restore Saved Roles Parse Error]: {e}")

        await db.execute("DELETE FROM isolations WHERE user_id = ? AND guild_id = ?", (member.id, guild.id))
        await db.commit()

    if iso_role and iso_role in member.roles:
        try:
            await member.remove_roles(iso_role, reason=reason)
        except Exception as e:
            print(f"[Unisolation Remove Role Error]: {e}")

    current_roles = set(member.roles)
    missing_roles = [r for r in roles_to_restore if r not in current_roles]
    if missing_roles:
        try:
            await member.add_roles(*missing_roles, reason=reason)
        except Exception as e:
            print(f"[Unisolation Restore Roles Error]: {e}")

    try:
        await member.timeout(None, reason=reason)
    except Exception:
        pass

    if member.voice and member.voice.mute:
        try:
            await member.edit(mute=False, reason=reason)
        except Exception:
            pass


# =====================================================================
# 9. BACKGROUND TASKS (POLLS, MAINTENANCE, DASHBOARD)
# =====================================================================

@tasks.loop(seconds=10)
async def poll_checker() -> None:
    """
    Processes poll state transitions (cooldown -> active -> finished/rejected).
    Enforces Rule 6.9: Tier IV (Crit) polls hold highest priority, followed by chronological end_time.
    """
    current_time = int(time.time())
    polls_to_finish: List[Tuple[int, int, str]] = []

    for poll_id, data in list(POLLS_CACHE.items()):
        try:
            if not is_authorized_platform(data.get("guild_id")):
                continue

            status = data.get("status", "active")
            if status in ("finished", "rejected_final"):
                continue

            if status == "cooldown" and current_time >= data["start_time"]:
                await activate_poll(poll_id)
                continue

            if status == "rejected_frozen":
                deadline = data.get("appeal_deadline", data.get("end_time", 0))
                if current_time >= deadline:
                    await finalize_rejected_poll(poll_id)
                continue

            # Resolve 48-hour non-blocking ACI dispute on Tier IV (Crit) polls (Rule 6.5.1)
            if data.get("aci_dispute_active"):
                dispute_deadline = data.get("aci_dispute_deadline", 0)
                if current_time >= dispute_deadline or current_time >= data["end_time"]:
                    cancelled = await resolve_crit_aci_dispute(poll_id)
                    if cancelled:
                        continue

            if status == "active" and current_time >= data["end_time"]:
                priority_rank = 0 if data.get("tier") == "Crit" else 1
                polls_to_finish.append((priority_rank, data["end_time"], poll_id))
        except Exception as e:
            print(f"[Poll Checker Error on {poll_id}]: {e}")

    # Rule 6.9: Tier IV (Crit) polls first, then strictly by chronological end_time
    polls_to_finish.sort(key=lambda x: (x[0], x[1]))
    for _, _, poll_id in polls_to_finish:
        await process_poll_results(poll_id)


async def resolve_crit_aci_dispute(poll_id: str) -> bool:
    """
    Evaluates the 48-hour dispute on a Tier IV (Crit) poll (Rule 6.5.1 & SVA V. RH):
    - If net weight < 0 (strictly more 'Reject' (-1) than 'Confirm' (+1) votes), the poll is cancelled.
    - Otherwise, the dispute closes and the poll receives permanent immunity (aci_shield = True).
    Returns True if the poll was cancelled, False if it continues.
    """
    async with file_lock:
        data = get_poll_data(poll_id)
        if not data or not data.get("aci_dispute_active"):
            return False

        dispute_votes = data.get("aci_dispute_votes", {})
        net_weight = sum(int(v.get("vote", 0)) for v in dispute_votes.values())
        data["aci_dispute_active"] = False

        if net_weight < 0:
            data["status"] = "rejected_final"
            data["reject_reason"] = (
                f"Аннулировано по итогам 48-часового спора (баланс: {net_weight:+d}). "
                f"Причина: {data.get('aci_dispute_reason', 'Не указано')}"
            )
            save_poll_data(poll_id, data)
            was_cancelled = True
        else:
            data["aci_shield"] = True
            data["confirmed_by_name"] = f"Итог 48ч спора (баланс: {net_weight:+d})"
            save_poll_data(poll_id, data)
            was_cancelled = False

    await sync_poll_message_ui(data)
    return was_cancelled


async def activate_poll(poll_id: str) -> None:
    """Transitions a poll from cooldown to active status."""
    async with file_lock:
        data = get_poll_data(poll_id)
        if not data:
            return
        data["status"] = "active"
        save_poll_data(poll_id, data)

    await sync_poll_message_ui(data)


async def finalize_rejected_poll(poll_id: str) -> None:
    """Marks a frozen rejected poll as permanently rejected after cross-control window expires."""
    async with file_lock:
        data = get_poll_data(poll_id)
        if not data:
            return
        data["status"] = "rejected_final"
        save_poll_data(poll_id, data)

    await sync_poll_message_ui(data)


async def process_poll_results(poll_id: str) -> None:
    """Calculates final weighted poll results, triggers automated execution, and posts summary."""
    data = get_poll_data(poll_id)
    if not data or not is_authorized_platform(data.get("guild_id")):
        return

    channel = bot.get_channel(data["channel_id"])
    if not channel:
        try:
            channel = await bot.fetch_channel(data["channel_id"])
        except Exception:
            return

    guild = bot.get_guild(int(data["guild_id"])) or getattr(channel, "guild", None)

    valid_votes = list(data.get("votes", {}).values())
    total_voters = len(valid_votes)

    positive_weight = sum(v["weight"] for v in valid_votes if v["weight"] > 0)
    negative_weight = sum(-v["weight"] for v in valid_votes if v["weight"] < 0)
    neutral_count = sum(1 for v in valid_votes if v["weight"] == 0)
    total_weight_pool = positive_weight + negative_weight

    win_pct = (positive_weight / total_weight_pool * 100.0) if total_weight_pool > 0 else 0.0
    req_pct = data["win_pct"]
    req_quorum = data["quorum_req"]

    is_victory = False
    execution_note = ""

    if total_voters < req_quorum:
        result_title = "🛑 НЕ СОСТОЯЛОСЬ (НЕТ КВОРУМА)"
        result_color = discord.Color.light_grey()
        verdict_text = f"**Не набрано минимальное число участников.**\nЯвка составила **`{total_voters} / {req_quorum}`**."
    elif win_pct > req_pct:
        is_victory = True
        result_title = "🏆 РЕШЕНИЕ ПРИНЯТО"
        result_color = discord.Color.green()
        verdict_text = (
            f"**Инициатива преодолела порог одобрения!**\n"
            f"Поддержка по весу: **`{win_pct:.1f}%`** (требовалось **`> {req_pct}%`**)."
        )
        if guild:
            execution_note = await execute_automated_decision(data, guild)
    else:
        result_title = "❌ РЕШЕНИЕ ОТКЛОНЕНО"
        result_color = discord.Color.red()
        verdict_text = (
            f"**Инициатива не набрала необходимой поддержки.**\n"
            f"Поддержка по весу: **`{win_pct:.1f}%`** (требовалось **`> {req_pct}%`**)."
        )

    async with file_lock:
        data["status"] = "finished"
        data["is_victory"] = is_victory
        data["final_win_pct"] = round(win_pct, 2)
        data["positive_weight"] = positive_weight
        data["negative_weight"] = negative_weight
        save_poll_data(poll_id, data)

    formatted_quote = "\n".join(
        f"> *{line}*" if line.strip() else ">"
        for line in data["quote_content"].split("\n")
    )
    if len(formatted_quote) > 1600:
        formatted_quote = formatted_quote[:1600] + "\n> *...[сокращено]*"

    tier_label = TIER_ROMAN.get(data["tier"], data["tier"])
    result_embed = discord.Embed(
        title=f"ИТОГИ ГОЛОСОВАНИЯ: {result_title}",
        description=f"### Предмет голосования\n{formatted_quote}\n",
        color=result_color,
        timestamp=discord.utils.utcnow()
    )

    result_embed.add_field(
        name="📂 Классификация",
        value=(
            f"┏ **Категория:** `{data['category']}` (**{tier_label}**)\n"
            f"┃ **Автор текста:** **{data['target_author_name']}**\n"
            f"┗ **Инициатор:** <@{data['initiator_id']}>"
        ),
        inline=True
    )

    result_embed.add_field(
        name="📊 Баланс весов (SVA V. RH)",
        value=(
            f"┏ **Явка кворума:** `{total_voters} / {req_quorum} чел.`\n"
            f"┃ **Вес «ЗА»:** `+{positive_weight}` | **«ПРОТИВ»:** `-{negative_weight}`\n"
            f"┗ **Нейтрально (`0`):** `{neutral_count} чел.` (в пуле: `{total_weight_pool}`)"
        ),
        inline=True
    )

    result_embed.add_field(name="⚖️ ВЕРДИКТ АСУ:", value=verdict_text, inline=False)

    if execution_note:
        result_embed.add_field(
            name="⚙️ Статус исполнения (п. 6.6 / 6.9)",
            value=execution_note,
            inline=False
        )

    result_embed.set_footer(text=f"ID голосования: {data['poll_message_id']} • SHA256: {data.get('content_sha256', '')[:10]}")

    try:
        poll_message = await channel.fetch_message(int(data["poll_message_id"]))
        await poll_message.edit(embed=build_poll_embed(data), view=PollVotingView(data))
        await channel.send(f"<@{data['initiator_id']}>, подведены итоги голосования!", embed=result_embed, reference=poll_message)
    except Exception:
        await channel.send(f"<@{data['initiator_id']}>, подведены итоги голосования `{data['poll_message_id']}`!", embed=result_embed)


@tasks.loop(seconds=10)
async def system_maintenance_loop() -> None:
    """Handles expired isolations, timeout renewals (>28d), log flushing, and delayed appointments on authorized Platforms."""
    await flush_message_logs()
    now = int(time.time())
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            # Release expired isolations (Rule 5.2)
            async with db.execute(
                "SELECT user_id, guild_id FROM isolations WHERE end_time > 0 AND end_time <= ?",
                (now,)
            ) as cursor:
                expired_isolations = await cursor.fetchall()

            for user_id, guild_id in expired_isolations:
                if not is_authorized_platform(guild_id):
                    continue
                guild = bot.get_guild(guild_id)
                if guild:
                    member = guild.get_member(user_id)
                    if not member:
                        try:
                            member = await guild.fetch_member(user_id)
                        except Exception:
                            member = None
                    if member:
                        await restore_user_from_isolation(guild, member, reason="АСУ: Истёк срок временной Изоляции (п. 5.2)")
                    else:
                        await db.execute("DELETE FROM isolations WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
                        await db.commit()

            # Renew Discord timeouts for indefinite or long isolations
            async with db.execute(
                "SELECT user_id, guild_id, end_time, reason FROM isolations WHERE end_time = 0 OR end_time > ?",
                (now,)
            ) as cursor:
                active_isolations = await cursor.fetchall()

            for user_id, guild_id, end_time, iso_reason in active_isolations:
                if not is_authorized_platform(guild_id):
                    continue
                guild = bot.get_guild(guild_id)
                if not guild:
                    continue
                member = guild.get_member(user_id)
                if not member:
                    continue

                rem_sec = (end_time - now) if end_time > 0 else DISCORD_MAX_TIMEOUT_SECONDS
                target_timeout_sec = min(rem_sec, DISCORD_MAX_TIMEOUT_SECONDS)
                if target_timeout_sec > 3600:
                    needs_renewal = (
                        member.timed_out_until is None
                        or (member.timed_out_until - discord.utils.utcnow()).total_seconds() < 3600
                    )
                    if needs_renewal:
                        try:
                            until_dt = discord.utils.utcnow() + datetime.timedelta(seconds=target_timeout_sec)
                            await member.timeout(until_dt, reason=f"АСУ: Поддержание мута Изоляции ({iso_reason})")
                        except Exception:
                            pass

            # Execute pending /relection role appointments
            async with db.execute(
                "SELECT id, guild_id, target_user_id, role_id, initiator_id, reason FROM pending_appointments WHERE executed = 0 AND execute_at <= ?",
                (now,)
            ) as cursor:
                due_appointments = await cursor.fetchall()

            for app_id, guild_id, target_uid, role_id, init_uid, reason in due_appointments:
                await db.execute("UPDATE pending_appointments SET executed = 1 WHERE id = ?", (app_id,))
                await db.commit()

                if not is_authorized_platform(guild_id):
                    continue
                guild = bot.get_guild(guild_id)
                if not guild:
                    continue
                member = guild.get_member(target_uid)
                role = guild.get_role(role_id)
                if member and role:
                    try:
                        await member.add_roles(role, reason=f"АСУ /relection (Инициатор: {init_uid}): {reason}")
                        log_emb = discord.Embed(
                            title="🎖️ Отложенное назначение Управленца исполнено (/relection)",
                            description=(
                                f"**Назначен:** {member.mention}\n"
                                f"**Должность:** {role.mention}\n"
                                f"**Инициатор:** <@{init_uid}>\n"
                                f"**Основание:** {reason}"
                            ),
                            color=discord.Color.green(),
                            timestamp=discord.utils.utcnow()
                        )
                        await log_moderation_action(guild, log_emb)
                    except Exception as e:
                        print(f"[Appointment Execution Error]: {e}")
    except Exception as e:
        print(f"[Maintenance Loop Error]: {e}")


@tasks.loop(seconds=5)
async def live_dashboard_loop() -> None:
    """Periodically updates the persistent /rstatistics dashboard message."""
    global DASHBOARD_CACHE
    if DASHBOARD_CACHE is None:
        if not os.path.exists(DASHBOARD_STATE_FILE):
            return
        try:
            with open(DASHBOARD_STATE_FILE, "r", encoding="utf-8") as f:
                DASHBOARD_CACHE = json.load(f)
        except Exception:
            return

    dash_guild_id = DASHBOARD_CACHE.get("guild_id", 0)
    if not is_authorized_platform(dash_guild_id):
        return

    now = int(time.time())
    interval = max(15, int(DASHBOARD_CACHE.get("interval", 60)))
    if now - DASHBOARD_CACHE.get("last_update", 0) < interval:
        return

    guild = bot.get_guild(dash_guild_id)
    if not guild:
        return
    channel = guild.get_channel(DASHBOARD_CACHE.get("channel_id", 0))
    if not channel:
        return

    try:
        msg = await channel.fetch_message(DASHBOARD_CACHE["message_id"])
        embed = await generate_statistics_embed(guild, interval)
        await msg.edit(embed=embed)
        DASHBOARD_CACHE["last_update"] = now
        write_json_atomic(DASHBOARD_STATE_FILE, DASHBOARD_CACHE)
    except discord.NotFound:
        DASHBOARD_CACHE = None
        if os.path.exists(DASHBOARD_STATE_FILE):
            os.remove(DASHBOARD_STATE_FILE)
    except Exception as e:
        print(f"[Dashboard Update Error]: {e}")


async def generate_statistics_embed(guild: discord.Guild, interval: int) -> discord.Embed:
    """Builds the live SVA V. RH statistics dashboard embed."""
    active_count, median_threshold, _ = await get_active_users_stats(guild.id)

    all_polls = [p for p in POLLS_CACHE.values() if p.get("guild_id") == guild.id]

    active_polls_count = 0
    cooldown_polls_count = 0
    frozen_polls_count = 0
    finished_polls_count = 0
    rejected_polls_count = 0
    victories = 0

    for p in all_polls:
        st = p.get("status")
        if st == "active":
            active_polls_count += 1
        elif st == "cooldown":
            cooldown_polls_count += 1
        elif st == "rejected_frozen":
            frozen_polls_count += 1
        elif st == "finished":
            finished_polls_count += 1
            if p.get("is_victory"):
                victories += 1
        elif st == "rejected_final":
            rejected_polls_count += 1

    defeats = finished_polls_count - victories
    win_rate = (victories / finished_polls_count * 100.0) if finished_polls_count else 0.0

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT COUNT(*) FROM isolations WHERE guild_id = ?", (guild.id,)) as cur:
            iso_row = await cur.fetchone()
            isolated_count = iso_row[0] if iso_row else 0

    recent_polls = sorted(all_polls, key=lambda x: x.get("created_at", 0), reverse=True)[:6]
    recent_lines = []
    for p in recent_polls:
        st = p.get("status")
        ico = {
            "active": "🟢 **[АКТИВНО]**",
            "cooldown": "🧊 **[ОХЛАЖДЕНИЕ]**",
            "rejected_frozen": "⏸️ **[ЗАМОРОЖЕНО ACI]**",
            "rejected_final": "❌ **[ОТКЛОНЕНО ACI]**",
            "finished": "🏆 **[ПРИНЯТО]**" if p.get("is_victory") else "🛑 **[НЕ ПРОШЛО]**"
        }.get(st, "📊")
        link = f"https://discord.com/channels/{guild.id}/{p['channel_id']}/{p['poll_message_id']}"
        short_q = p.get("quote_content", "").replace("\n", " ")[:48]
        voters_n = len(p.get("votes", {}))
        t_lbl = TIER_ROMAN.get(p["tier"], p["tier"])
        recent_lines.append(
            f"┏ {ico} [`#{p['poll_message_id']}`]({link}) — **`{p['category']} ({t_lbl})`** (Явка: **`{voters_n}/{p['quorum_req']}`**)\n"
            f"┗ *{short_q}...*"
        )

    recent_block = "\n\n".join(recent_lines) if recent_lines else "*Голосования ещё не проводились.*"

    q_low = TIERS["Low"]["quorum_pct"]
    q_mid = TIERS["Mid"]["quorum_pct"]
    q_high = TIERS["High"]["quorum_pct"]
    q_crit = TIERS["Crit"]["quorum_pct"]

    embed = discord.Embed(
        title="📈 Мониторинг АСУ «Горизонт» (SVA V. RH)",
        description=f"🔄 **Интервал обновления:** `{interval} сек.` (Следующее: <t:{int(time.time()) + interval}:R>)",
        color=discord.Color.teal(),
        timestamp=discord.utils.utcnow()
    )

    embed.add_field(
        name="👥 База активности (30 дней, п. 6.7)",
        value=(
            f"┏ **Активных участников:** `{active_count} чел.`\n"
            f"┃ **Порог медианы:** `{median_threshold:g} сообщ.`\n"
            f"┗ **В Изоляции (п. 3.5):** `{isolated_count} чел.`"
        ),
        inline=True
    )

    embed.add_field(
        name="🎯 Кворум по Тирам SVA V. RH",
        value=(
            f"┏ **I. Low ({q_low}% / >50%):** `{max(1, math.ceil(active_count * (q_low / 100.0)))} чел.`\n"
            f"┃ **II. Mid ({q_mid}% / >50%):** `{max(1, math.ceil(active_count * (q_mid / 100.0)))} чел.`\n"
            f"┃ **III. High ({q_high}% / >60%):** `{max(1, math.ceil(active_count * (q_high / 100.0)))} чел.`\n"
            f"┗ **IV. Crit ({q_crit}% / >70%):** `{max(1, math.ceil(active_count * (q_crit / 100.0)))} чел.`"
        ),
        inline=True
    )

    embed.add_field(
        name="📊 Сводка голосований",
        value=(
            f"┏ 🟢 **Активных:** `{active_polls_count}` | 🧊 **Охлаждение:** `{cooldown_polls_count}` | ⏸️ **Заморожено ACI:** `{frozen_polls_count}`\n"
            f"┗ 🔒 **Завершено:** `{finished_polls_count}` "
            f"(🏆 **Принято:** `{victories}` [`{win_rate:.1f}%`] • 🛑 **Отклонено:** `{defeats}` • ❌ **Снято по ACI:** `{rejected_polls_count}`)"
        ),
        inline=False
    )

    embed.add_field(
        name="🗂️ Последние инициативы",
        value=recent_block[:1024],
        inline=False
    )

    embed.set_footer(text="Автоматизированная Система Управления • SVA V. RH • RedAdapter")
    return embed


# =====================================================================
# 10. GLOBAL PLATFORM GUARD, BOT EVENTS & SLASH COMMANDS
# =====================================================================

async def global_platform_interaction_check(interaction: discord.Interaction) -> bool:
    """
    Global guard for all Slash Commands:
    - If invoked inside a guild, the guild ID MUST be in REGISTERED_PLATFORMS / REGISTERED_PLATFORM_ID.
    - If invoked in Direct Messages (guild is None), only /rappeal and /rhelp are permitted.
    """
    if interaction.guild is not None:
        if not is_authorized_platform(interaction.guild.id):
            await interaction.response.send_message(
                "⛔ **Отказ доступа АСУ:** данный сервер не зарегистрирован в списке авторизованных Площадок (`REGISTERED_PLATFORMS`). Работа бота здесь заблокирована.",
                ephemeral=True
            )
            return False
        return True

    # In Direct Messages (DMs): allow /rappeal and /rhelp
    cmd_name = interaction.command.name if interaction.command else ""
    if cmd_name not in ("rappeal", "rhelp"):
        await interaction.response.send_message(
            "❌ В личных сообщениях с ботом доступна только подача Публичной апелляции (`/rappeal`) и справка (`/rhelp`).",
            ephemeral=True
        )
        return False
    return True


bot.tree.interaction_check = global_platform_interaction_check


async def setup_hook() -> None:
    ensure_sample_election_template()
    load_polls_into_cache()
    await setup_db()
    bot.add_view(PollVotingView())
    await bot.tree.sync()

bot.setup_hook = setup_hook


@bot.event
async def on_ready() -> None:
    allowed = get_registered_platform_ids()
    print(f"[АСУ RedAdapter | SVA V. RH] Запущен как: {bot.user} (ID: {bot.user.id})")
    print(f"[АСУ RedAdapter] Авторизованные Площадки (REGISTERED_PLATFORMS): {allowed or 'НЕ ЗАДАНЫ!'}")
    for task_loop in (poll_checker, system_maintenance_loop, live_dashboard_loop, db_cleanup):
        if not task_loop.is_running():
            task_loop.start()


@bot.event
async def on_message(message: discord.Message) -> None:
    """Buffers message timestamps per user on authorized Platforms only without storing content (Rule 6.7)."""
    if message.author.bot or not message.guild:
        return

    # Strict platform guard: ignore activity on any unauthorized guild
    if not is_authorized_platform(message.guild.id):
        return

    should_flush = False
    async with buffer_lock:
        MESSAGE_LOG_BUFFER.append((message.author.id, message.guild.id, time.time()))
        if len(MESSAGE_LOG_BUFFER) >= 25:
            should_flush = True

    if should_flush:
        await flush_message_logs()


@tasks.loop(hours=1)
async def db_cleanup() -> None:
    """Prunes expired message activity (>30d) and command usage logs (>24h)."""
    await flush_message_logs()
    now = time.time()
    thirty_days_ago = now - THIRTY_DAYS_SECONDS
    day_ago = int(now - DAILY_LIMIT_WINDOW)
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("DELETE FROM message_logs WHERE timestamp < ?", (thirty_days_ago,))
            await db.execute("DELETE FROM command_usage_logs WHERE timestamp < ?", (day_ago,))
            await db.commit()
    except Exception:
        pass


async def fetch_message_across_guild(
    guild: discord.Guild,
    msg_id: int,
    preferred_channel: Optional[discord.abc.Messageable] = None
) -> Optional[discord.Message]:
    """Resolves a message by ID from cache, preferred channel, or accessible guild channels."""
    cached = discord.utils.get(bot.cached_messages, id=msg_id)
    if cached and getattr(cached.guild, "id", None) == guild.id:
        return cached

    if preferred_channel and hasattr(preferred_channel, "fetch_message"):
        try:
            return await preferred_channel.fetch_message(msg_id)
        except Exception:
            pass

    me = guild.me
    channels = list(guild.text_channels) + list(guild.threads) + list(guild.voice_channels)
    for ch in channels:
        if preferred_channel and getattr(ch, "id", None) == getattr(preferred_channel, "id", None):
            continue
        if me:
            perms = ch.permissions_for(me)
            if not (perms.view_channel and perms.read_message_history):
                continue
        try:
            return await ch.fetch_message(msg_id)
        except (discord.NotFound, discord.Forbidden):
            continue
        except Exception:
            continue
    return None


# =====================================================================
# /rpoll COMMAND GROUP (STANDARD, IMPEACH, GLOBAL ELECTION)
# =====================================================================

rpoll_group = app_commands.Group(name="rpoll", description="Инициация голосований по архитектуре SVA V. RH")


@rpoll_group.command(name="standard", description="Создать голосование по сообщению и Категории SVA V. RH")
@app_commands.describe(
    message_id="ID сообщения с текстом инициативы (соответствующим Стандарту ACI)",
    category="Категория вопроса (автоматически определяет Тир I–IV, порог, охлаждение и кворум)",
    poll_channel="Канал для публикации голосования (из списка POLLS_CHANNEL_IDS)"
)
@app_commands.choices(
    category=[
        app_commands.Choice(name=f"[{c}] — {TIER_ROMAN[t]}", value=c)
        for c, t in CATEGORIES.items()
    ]
)
async def rpoll_standard(
    interaction: discord.Interaction,
    message_id: str,
    category: app_commands.Choice[str],
    poll_channel: Optional[discord.TextChannel] = None
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Команда доступна только на Площадке.", ephemeral=True)

    is_allowed, err_msg = await check_full_member_status(interaction.user)
    if not is_allowed:
        return await interaction.response.send_message(err_msg, ephemeral=True)

    _, chan_err = resolve_poll_destination_channel(interaction.guild, interaction.channel, requested_poll_channel=poll_channel)
    if chan_err:
        return await interaction.response.send_message(chan_err, ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    id_matches = SNOWFLAKE_RE.findall(message_id.strip())
    if not id_matches:
        return await interaction.followup.send("⚠️ Укажите корректный цифровой ID сообщения.")

    target_message = await fetch_message_across_guild(interaction.guild, int(id_matches[-1]), preferred_channel=interaction.channel)
    if not target_message:
        return await interaction.followup.send("⚠️ Сообщение с указанным ID не найдено на Площадке.")

    cat_name = category.value
    tier_name = CATEGORIES[cat_name]
    t_cfg = TIERS[tier_name]

    poll_msg = await launch_poll_instance(
        guild=interaction.guild,
        fallback_channel=interaction.channel,
        initiator=interaction.user,
        target_author_name=target_message.author.display_name,
        quote_content=target_message.content or "[Вложение / Медиафайл]",
        category=cat_name,
        tier_name=tier_name,
        win_pct=t_cfg["win_pct"],
        cooldown_sec=t_cfg["cooldown"],
        quorum_pct=t_cfg["quorum_pct"],
        duration_sec=t_cfg["duration"],
        poll_type="standard",
        target_message_id=target_message.id,
        requested_poll_channel=poll_channel
    )

    if poll_msg:
        await interaction.followup.send(f"✅ Голосование успешно запущено: {poll_msg.jump_url}", ephemeral=True)
    else:
        await interaction.followup.send("❌ Ошибка при публикации голосования.", ephemeral=True)


@rpoll_group.command(name="impeach", description="Специальное голосование за снятие Управленца в обход ACI (п. 6.8 Регламента)")
@app_commands.describe(
    user="Управленец, чьи полномочия ставятся на голосование о снятии",
    poll_channel="Канал для публикации голосования (из списка POLLS_CHANNEL_IDS)"
)
async def rpoll_impeach(
    interaction: discord.Interaction,
    user: discord.Member,
    poll_channel: Optional[discord.TextChannel] = None
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Команда доступна только на Площадке.", ephemeral=True)

    is_allowed, err_msg = await check_full_member_status(interaction.user)
    if not is_allowed:
        return await interaction.response.send_message(err_msg, ephemeral=True)

    _, chan_err = resolve_poll_destination_channel(interaction.guild, interaction.channel, requested_poll_channel=poll_channel)
    if chan_err:
        return await interaction.response.send_message(chan_err, ephemeral=True)

    user_manager_roles = [r for r in user.roles if r.id in MANAGER_ROLES]
    suspended_mgr_names: List[str] = []

    # Check if user has suspended Manager roles inside the isolation table (Rule 7.3)
    if not user_manager_roles:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT saved_roles FROM isolations WHERE user_id = ? AND guild_id = ?",
                (user.id, interaction.guild.id)
            ) as cursor:
                row = await cursor.fetchone()
            if row and row[0]:
                try:
                    for rid in json.loads(row[0]):
                        if int(rid) in MANAGER_ROLES:
                            suspended_mgr_names.append(MANAGER_ROLES[int(rid)])
                except Exception:
                    pass

    if not user_manager_roles and not suspended_mgr_names:
        return await interaction.response.send_message(
            f"❌ **Техническая реализация невозможна:** у пользователя {user.mention} отсутствуют роли Управленца из реестра АСУ (`MANAGER_ROLES`).",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)
    role_names = ", ".join(r.name for r in user_manager_roles) if user_manager_roles else ", ".join(suspended_mgr_names)
    t_cfg = TIERS["High"]

    # Strictly standardized text without free-form user input (Rule 6.8)
    impeach_template_text = (
        f"Инициирована штатная процедура досрочного прекращения полномочий Управленца {user.mention} (`{user.id}`).\n"
        f"Текущие управленческие должности: **{role_names}**.\n"
        f"При достижении порога победы III Тира АСУ автоматически снимет все указанные управленческие роли (п. 6.6, 6.8)."
    )

    poll_msg = await launch_poll_instance(
        guild=interaction.guild,
        fallback_channel=interaction.channel,
        initiator=interaction.user,
        target_author_name="Системный шаблон АСУ (п. 6.8)",
        quote_content=impeach_template_text,
        category="Стафф",
        tier_name="High",
        win_pct=t_cfg["win_pct"],
        cooldown_sec=t_cfg["cooldown"],
        quorum_pct=t_cfg["quorum_pct"],
        duration_sec=t_cfg["duration"],
        poll_type="impeach",
        target_user_id=user.id,
        immune_to_aci=True,
        protect_from_02=True,
        requested_poll_channel=poll_channel
    )

    if poll_msg:
        await interaction.followup.send(f"⚡ Специальное голосование за снятие Управленца запущено: {poll_msg.jump_url}", ephemeral=True)
    else:
        await interaction.followup.send("❌ Ошибка при запуске голосования.", ephemeral=True)


rpoll_global_group = app_commands.Group(
    name="global",
    description="Прямые выдвижения в обход локальных анкет и процедур (п. 7.1.1 Регламента)"
)


@rpoll_global_group.command(name="election", description="Прямое выдвижение кандидата на пост Управленца в обход анкет (п. 7.1.1)")
@app_commands.describe(
    user="Кандидат на должность Управленца",
    role="Управленческая роль (из реестра АСУ)",
    message_id="ID сообщения с программой/обоснованием выдвижения (проверяется по Стандарту ACI)",
    poll_channel="Канал для публикации голосования (из списка POLLS_CHANNEL_IDS)"
)
async def rpoll_global_election(
    interaction: discord.Interaction,
    user: discord.Member,
    role: discord.Role,
    message_id: str,
    poll_channel: Optional[discord.TextChannel] = None
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Команда доступна только на Площадке.", ephemeral=True)

    is_allowed, err_msg = await check_full_member_status(interaction.user)
    if not is_allowed:
        return await interaction.response.send_message(err_msg, ephemeral=True)

    _, chan_err = resolve_poll_destination_channel(interaction.guild, interaction.channel, requested_poll_channel=poll_channel)
    if chan_err:
        return await interaction.response.send_message(chan_err, ephemeral=True)

    if role.id not in MANAGER_ROLES:
        allowed_str = ", ".join(f"`{name}`" for name in MANAGER_ROLES.values()) or "реестр пуст"
        return await interaction.response.send_message(
            f"❌ **Техническая реализация невозможна:** роль {role.mention} не входит в реестр должностей Управленцев АСУ (`MANAGER_ROLES`: {allowed_str}).",
            ephemeral=True
        )

    id_matches = SNOWFLAKE_RE.findall(message_id.strip())
    if not id_matches:
        return await interaction.response.send_message("⚠️ Укажите корректный цифровой ID сообщения.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    target_message = await fetch_message_across_guild(interaction.guild, int(id_matches[-1]), preferred_channel=interaction.channel)
    if not target_message:
        return await interaction.followup.send("⚠️ Сообщение с текстом выдвижения не найдено.")

    t_cfg = TIERS["High"]
    poll_msg = await launch_poll_instance(
        guild=interaction.guild,
        fallback_channel=interaction.channel,
        initiator=interaction.user,
        target_author_name=target_message.author.display_name,
        quote_content=target_message.content or "[Текст программы отсутствует]",
        category="Стафф",
        tier_name="High",
        win_pct=t_cfg["win_pct"],
        cooldown_sec=t_cfg["cooldown"],
        quorum_pct=t_cfg["quorum_pct"],
        duration_sec=t_cfg["duration"],
        poll_type="direct_election",
        target_message_id=target_message.id,
        target_user_id=user.id,
        target_role_id=role.id,
        target_role_name=MANAGER_ROLES[role.id],
        immune_to_aci=False,
        protect_from_02=True,
        requested_poll_channel=poll_channel
    )

    if poll_msg:
        await interaction.followup.send(f"🗳️ Прямое голосование за избрание {user.mention} (п. 7.1.1) запущено: {poll_msg.jump_url}", ephemeral=True)
    else:
        await interaction.followup.send("❌ Ошибка при создании голосования.", ephemeral=True)


rpoll_group.add_command(rpoll_global_group)
bot.tree.add_command(rpoll_group)


# =====================================================================
# TEMPLATE ELECTION COMMAND (/rpollelection)
# =====================================================================

@bot.tree.command(name="rpollelection", description="Подать заявку и пройти анкетирование на пост Управленца (п. 7.1, 7.2)")
@app_commands.describe(role="Роль должности, на которую вы выдвигаетесь")
async def rpollelection(interaction: discord.Interaction, role: discord.Role):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Команда доступна только на Площадке.", ephemeral=True)

    is_allowed, err_msg = await check_full_member_status(interaction.user)
    if not is_allowed:
        return await interaction.response.send_message(err_msg, ephemeral=True)

    matched_template = None
    role_name_lower = role.name.lower()
    for fname in os.listdir(ELECTIONS_DIR):
        if not fname.endswith(".json") or fname.lower() == "example.json":
            continue
        fpath = os.path.join(ELECTIONS_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                tpl = json.load(f)
            if int(tpl.get("role_id", 0)) == role.id or str(tpl.get("role_name", "")).lower() == role_name_lower:
                matched_template = tpl
                matched_template["role_id"] = role.id
                matched_template["role_name"] = role.name
                break
        except Exception:
            continue

    if not matched_template:
        return await interaction.response.send_message(
            f"❌ В папке `elections_jsons` не найден активный JSON-шаблон анкеты для роли {role.mention} (ID: `{role.id}`).\n"
            f"*(Вы также можете использовать прямое выдвижение `/rpoll global election` по п. 7.1.1 Регламента)*",
            ephemeral=True
        )

    questions = matched_template.get("questions", [])
    if not questions:
        return await interaction.response.send_message("⚠️ Шаблон анкеты для этой роли не содержит вопросов.", ephemeral=True)

    modal = ElectionQuestionnaireModal(
        candidate_id=interaction.user.id,
        template_data=matched_template,
        collected_answers=[],
        start_index=0
    )
    await interaction.response.send_modal(modal)


# =====================================================================
# ACI VALIDATION COMMANDS (/rreject, /rconfirm, /runexecutable)
# =====================================================================

@bot.tree.command(name="rreject", description="Отклонить голосование по Стандарту ACI или п. 0.2 (п. 3.3, 6.5, 6.5.1)")
@app_commands.describe(
    poll_id="ID сообщения голосования",
    reason="Основание по Регламенту и Стандарту ACI",
    comment="Подробное публичное обоснование причины (обязательно по п. 4.3 Регламента)"
)
@app_commands.choices(reason=[
    app_commands.Choice(name="Несоответствие Стандарту ACI (п. 6.2)", value="Несоответствие Стандарту ACI (п. 6.2)"),
    app_commands.Choice(name="Деструктивная инициатива / Саботаж (п. 0.2)", value="Деструктивная инициатива / Саботаж (п. 0.2)"),
    app_commands.Choice(name="Утрата исходного внешнего источника (п. 6.4.1)", value="Утрата исходного предмета голосования (п. 6.4.1)")
])
async def rreject(interaction: discord.Interaction, poll_id: str, reason: app_commands.Choice[str], comment: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Доступно только на Площадке.", ephemeral=True)

    if not has_command_permission(interaction.user, "rreject"):
        return await interaction.response.send_message("❌ У вашей должности нет прав на выполнение `/rreject`.", ephemeral=True)

    now = int(time.time())
    is_crit_dispute = False

    async with file_lock:
        data = get_poll_data(poll_id)
        if not data or data.get("guild_id") != interaction.guild.id:
            return await interaction.response.send_message("❌ Голосование с таким ID не найдено на Площадке.", ephemeral=True)

        if data.get("immune_to_aci"):
            return await interaction.response.send_message(
                "⚡ **Отказ АСУ (п. 4.5, 6.8):** Специальное голосование за снятие Управленца обладает полным системным иммунитетом и не может быть отклонено!",
                ephemeral=True
            )

        # Conflict of interest protection: target manager cannot block a poll about themselves (Rule 4.5)
        if data.get("target_user_id") and int(data["target_user_id"]) == interaction.user.id:
            return await interaction.response.send_message(
                "❌ **Конфликт интересов (п. 4.1, 4.5):** Вы являетесь затрагиваемым лицом в этом голосовании и не вправе блокировать или отклонять его.",
                ephemeral=True
            )

        if data.get("protect_from_02") and "0.2" in reason.value:
            return await interaction.response.send_message(
                "🛡️ **Отказ АСУ (п. 4.5):** Пресечение деструктивной инициативы (п. 0.2) не может применяться для закрытия "
                "Публичных апелляций (п. 5.1), голосований о статусе Площадок (п. 0.4.1) или кадровых вопросов Управленцев!",
                ephemeral=True
            )

        if data.get("aci_shield"):
            return await interaction.response.send_message(
                "🛡️ **Отказ АСУ (п. 3.3.1 / 6.5.1):** Это голосование уже прошло проверку и защищено иммунитетом от повторного отклонения.",
                ephemeral=True
            )

        if data.get("status") in ("finished", "rejected_final", "rejected_frozen"):
            return await interaction.response.send_message(
                "⚠️ **Отказ АСУ (п. 6.5):** Голосование уже завершено или уже находится в заморозке.",
                ephemeral=True
            )

        # Tier IV (Crit) Non-Blocking 48-Hour Dispute Protocol (Rule 6.5.1 & SVA V. RH)
        if data.get("tier") == "Crit":
            is_crit_dispute = True
            if not data.get("aci_dispute_active"):
                # Dispute can only be initiated no later than 48 hours before the end of the poll
                if (data["end_time"] - now) < ACI_CRIT_DISPUTE_WINDOW:
                    data["aci_shield"] = True
                    data["confirmed_by_name"] = "Авто-защита (<48ч до конца)"
                    save_poll_data(poll_id, data)
                    await sync_poll_message_ui(data)
                    return await interaction.response.send_message(
                        "🛡️ **Отказ АСУ:** До окончания голосования осталось менее 48 часов. "
                        "Инициировать спор уже нельзя — голосование получило иммунитет от отмены.",
                        ephemeral=True
                    )
                data["aci_dispute_active"] = True
                data["aci_dispute_started_at"] = now
                data["aci_dispute_deadline"] = now + ACI_CRIT_DISPUTE_WINDOW
                data["aci_dispute_reason"] = f"{reason.value} — {comment}"

            dispute_votes = data.setdefault("aci_dispute_votes", {})
            dispute_votes[str(interaction.user.id)] = {
                "vote": -1,
                "name": interaction.user.display_name,
                "reason": f"{reason.value} — {comment}",
                "timestamp": now
            }
            save_poll_data(poll_id, data)
        else:
            # Local Tiers I–III: Freeze poll and await cross-control (Rule 3.3, 3.3.1)
            if data["status"] == "cooldown":
                rem_cooldown = max(0, data["start_time"] - now)
                rem_duration = max(60, data["end_time"] - data["start_time"])
            else:
                rem_cooldown = 0
                rem_duration = max(60, data["end_time"] - now)

            data["status"] = "rejected_frozen"
            data["rejected_at"] = now
            data["remaining_cooldown"] = rem_cooldown
            data["remaining_duration"] = rem_duration
            data["appeal_deadline"] = max(data["end_time"], now + ACI_CROSS_CONTROL_WINDOW)
            data["reject_by_id"] = interaction.user.id
            data["reject_by_name"] = interaction.user.display_name
            data["reject_reason"] = f"{reason.value} — {comment}"

            save_poll_data(poll_id, data)

    await sync_poll_message_ui(data)

    if is_crit_dispute:
        dispute_votes = data.get("aci_dispute_votes", {})
        net_score = sum(int(v.get("vote", 0)) for v in dispute_votes.values())
        log_emb = discord.Embed(
            title="⚖️ Спор по голосованию (/rreject — Голос «Отклонить» -1)",
            description=(
                f"**ID голосования:** `{poll_id}`\n"
                f"**Управленец:** {interaction.user.mention}\n"
                f"**Основание:** {reason.value}\n"
                f"**Комментарий:** {comment}\n"
                f"**Баланс спора:** `{net_score:+d}`\n"
                f"**Окончание спора:** <t:{data['aci_dispute_deadline']}:f> (<t:{data['aci_dispute_deadline']}:R>)"
            ),
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
        await log_moderation_action(interaction.guild, log_emb)
        return await interaction.response.send_message(
            f"⚖️ Зафиксирован ваш голос **«Отклонить» (`-1`)** в 48-часовом споре по голосованию `{poll_id}`.\n"
            f"Текущий баланс: **`{net_score:+d}`**. Итоги спора: <t:{data['aci_dispute_deadline']}:R>.",
            ephemeral=True
        )

    log_emb = discord.Embed(
        title="⏸️ Голосование отклонено и заморожено (/rreject)",
        description=(
            f"**ID голосования:** `{poll_id}`\n"
            f"**Управленец:** {interaction.user.mention}\n"
            f"**Основание:** {reason.value}\n"
            f"**Комментарий (п. 4.3):** {comment}\n"
            f"**Сохранённый остаток таймера:** `{math.ceil(data['remaining_duration'] / 60)} мин.`\n"
            f"**Окно перекрёстного контроля (`/rconfirm` по п. 3.3.1) до:** <t:{data['appeal_deadline']}:f>"
        ),
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    await log_moderation_action(interaction.guild, log_emb)
    await interaction.response.send_message(
        f"✅ Голосование `{poll_id}` отклонено по ACI и переведено в ожидание перекрёстного контроля (п. 3.3.1).",
        ephemeral=True
    )


@bot.tree.command(name="rconfirm", description="Подтвердить легитимность ACI (наложить щит, разморозить или отдать +1 в споре IV Тира)")
@app_commands.describe(
    poll_id="ID сообщения голосования",
    comment="Обоснование соответствия Стандарту ACI (п. 4.3, 6.2)"
)
async def rconfirm(interaction: discord.Interaction, poll_id: str, comment: str = "Соответствует Стандарту ACI (п. 6.2)"):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Доступно только на Площадке.", ephemeral=True)

    if not has_command_permission(interaction.user, "rconfirm"):
        return await interaction.response.send_message("❌ У вашей должности нет прав на выполнение `/rconfirm`.", ephemeral=True)

    now = int(time.time())
    is_crit_dispute_vote = False

    async with file_lock:
        data = get_poll_data(poll_id)
        if not data or data.get("guild_id") != interaction.guild.id:
            return await interaction.response.send_message("❌ Голосование не найдено на Площадке.", ephemeral=True)

        status = data.get("status")
        if status in ("finished", "rejected_final"):
            return await interaction.response.send_message(
                "❌ Это голосование уже достигло окончательного завершения.",
                ephemeral=True
            )

        # Conflict of interest: initiator cannot self-confirm (Rule 4.1)
        if interaction.user.id == data.get("initiator_id"):
            return await interaction.response.send_message(
                "❌ **Конфликт интересов (п. 4.1):** Вы не можете подтверждать легитимность собственного голосования. Проверку должен провести другой Управленец.",
                ephemeral=True
            )

        # If a 48-hour Tier IV (Crit) dispute is active, record +1 ("Легитимно")
        if data.get("tier") == "Crit" and data.get("aci_dispute_active"):
            is_crit_dispute_vote = True
            dispute_votes = data.setdefault("aci_dispute_votes", {})
            dispute_votes[str(interaction.user.id)] = {
                "vote": 1,
                "name": interaction.user.display_name,
                "reason": comment,
                "timestamp": now
            }
            save_poll_data(poll_id, data)
        else:
            was_frozen = (status == "rejected_frozen")
            if was_frozen:
                # Rule 3.3.1: Cross-control requires another Manager
                if interaction.user.id == data.get("reject_by_id"):
                    return await interaction.response.send_message(
                        "❌ **Нарушение процедуры перекрёстного контроля (п. 3.3.1):** Вы не можете самостоятельно разморозить голосование, отклонённое вами же. Перепроверку должен выполнить **другой Управленец**.",
                        ephemeral=True
                    )

                if now >= data.get("appeal_deadline", 0):
                    data["status"] = "rejected_final"
                    save_poll_data(poll_id, data)
                    return await interaction.response.send_message("❌ Срок восстановления этого голосования истёк.", ephemeral=True)

                rem_cd = data.get("remaining_cooldown", 0)
                rem_dur = data.get("remaining_duration", 3600)
                data["start_time"] = now + rem_cd
                data["end_time"] = data["start_time"] + rem_dur
                data["status"] = "cooldown" if rem_cd > 0 else "active"

            data["aci_shield"] = True
            data["confirmed_by_id"] = interaction.user.id
            data["confirmed_by_name"] = interaction.user.display_name
            data["confirm_comment"] = comment

            save_poll_data(poll_id, data)

    await sync_poll_message_ui(data)

    if is_crit_dispute_vote:
        dispute_votes = data.get("aci_dispute_votes", {})
        net_score = sum(int(v.get("vote", 0)) for v in dispute_votes.values())
        log_emb = discord.Embed(
            title="⚖️ Спор по голосованию (/rconfirm — Голос «Легитимно» +1)",
            description=(
                f"**ID голосования:** `{poll_id}`\n"
                f"**Управленец:** {interaction.user.mention}\n"
                f"**Комментарий:** {comment}\n"
                f"**Баланс спора:** `{net_score:+d}`\n"
                f"**Окончание спора:** <t:{data['aci_dispute_deadline']}:f>"
            ),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        await log_moderation_action(interaction.guild, log_emb)
        return await interaction.response.send_message(
            f"⚖️ Ваш голос **«Легитимно» (`+1`)** учтён в 48-часовом споре по голосованию `{poll_id}`.\n"
            f"Текущий баланс: **`{net_score:+d}`**.",
            ephemeral=True
        )

    action_str = "Восстановлено из заморозки и защищено щитом (п. 3.3.1)" if was_frozen else "Подтверждено и защищено щитом (п. 3.3)"
    log_emb = discord.Embed(
        title="🛡️ Валидация ACI подтверждена (/rconfirm)",
        description=(
            f"**ID голосования:** `{poll_id}`\n"
            f"**Управленец:** {interaction.user.mention}\n"
            f"**Действие:** {action_str}\n"
            f"**Комментарий:** {comment}"
        ),
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    await log_moderation_action(interaction.guild, log_emb)
    await interaction.response.send_message(f"🛡️ {action_str}.", ephemeral=True)


@bot.tree.command(name="runexecutable", description="Опубликовать официальный отказ от реализации по технической неисполнимости (п. 6.10 / 6.10.1)")
@app_commands.describe(
    poll_id="ID завершившегося победой голосования",
    clause="Пункт Регламента (6.10 — физическая/техническая невозможность, 6.10.1 — отсутствие исполнителя с нуля)",
    proof="Доказательство объективной невозможности, двусмысленности или отсутствия готовой реализации (п. 4.3)"
)
@app_commands.choices(clause=[
    app_commands.Choice(name="п. 6.10 — Техническая невозможность / Неустранимая двусмысленность", value="п. 6.10 Регламента"),
    app_commands.Choice(name="п. 6.10.1 — Отсутствие исполнителя и готовой реализации с нуля", value="п. 6.10.1 Регламента")
])
async def runexecutable(interaction: discord.Interaction, poll_id: str, clause: app_commands.Choice[str], proof: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Доступно только на Площадке.", ephemeral=True)

    if not has_command_permission(interaction.user, "runexecutable"):
        return await interaction.response.send_message("❌ У вашей должности нет прав на выполнение `/runexecutable`.", ephemeral=True)

    async with file_lock:
        data = get_poll_data(poll_id)
        if not data or data.get("guild_id") != interaction.guild.id:
            return await interaction.response.send_message("❌ Голосование не найдено на Площадке.", ephemeral=True)

        if data.get("status") != "finished" or not data.get("is_victory"):
            return await interaction.response.send_message(
                "⚠️ Пункты 6.10 / 6.10.1 применяются только к завершившимися победой голосованиям.",
                ephemeral=True
            )

        data["unexecutable_by_id"] = interaction.user.id
        data["unexecutable_reason"] = f"[{clause.value}] {proof}"
        save_poll_data(poll_id, data)

    await sync_poll_message_ui(data)

    log_emb = discord.Embed(
        title=f"⚠️ Зафиксирована неисполнимость решения ({clause.value})",
        description=(
            f"**ID голосования:** `{poll_id}`\n"
            f"**Управленец:** {interaction.user.mention}\n"
            f"**Основание и доказательство (п. 4.3, 6.10):**\n> {proof}"
        ),
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    await log_moderation_action(interaction.guild, log_emb)
    await interaction.response.send_message(
        f"✅ Официальный мотивированный отказ от реализации по **{clause.value}** опубликован в карточке голосования `{poll_id}` и в логе модерации.",
        ephemeral=True
    )


# =====================================================================
# ISOLATION & APPEAL COMMANDS (/risolation, /runisolation, /rappeal)
# =====================================================================

@bot.tree.command(name="risolation", description="Поместить участника в Изоляцию с мутом (п. 3.5 Регламента, лимит: 3 в 24ч)")
@app_commands.describe(
    user="Участник, переводимый в статус Ограниченного участника",
    reason="Публичное обоснование причины (конкретный пункт Регламента или описание ситуации, п. 4.3)",
    duration="Срок: sec/s, min, h/hr, d, mon/m, y (пример: 30sec, 15min, 12h, 7d, 1mon) или 0 — до апелляции",
    is_twink="Доказанный твинк-аккаунт? (Да = аннулировать его голоса по п. 1.2 и 2.6)"
)
async def risolation(
    interaction: discord.Interaction,
    user: discord.Member,
    reason: str,
    duration: str = "0",
    is_twink: bool = False
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Доступно только на Площадке.", ephemeral=True)

    if not has_command_permission(interaction.user, "risolation"):
        return await interaction.response.send_message("❌ У вашей должности нет прав на выполнение `/risolation`.", ephemeral=True)

    if user.id == interaction.user.id or user.bot:
        return await interaction.response.send_message("❌ Невозможно применить Изоляцию к себе или боту.", ephemeral=True)

    can_use, used_count, next_avail = await check_daily_command_limit(interaction.user.id, interaction.guild.id, "risolation")
    if not can_use:
        return await interaction.response.send_message(
            f"⏳ **Достигнут суточный лимит:** Команду `/risolation` можно использовать не более **{DAILY_ACTION_LIMIT} раз за 24 часа** "
            f"(использовано: `{used_count}/{DAILY_ACTION_LIMIT}`).\n"
            f"Следующее использование станет доступно: <t:{next_avail}:R> (<t:{next_avail}:f>).",
            ephemeral=True
        )

    duration_seconds = parse_isolation_duration(duration)
    if duration_seconds is None:
        return await interaction.response.send_message(
            "⚠️ **Неверный формат времени!** Используйте единицы:\n"
            "• `sec` / `s` — секунды (например `30sec`)\n"
            "• `min` — минуты (например `30min` или `60min`)\n"
            "• `h` / `hr` — часы (например `12h`)\n"
            "• `d` / `day` — дни (например `7d`)\n"
            "• `mon` / `mo` / `m` — месяцы (например `1mon`)\n"
            "• `y` / `yr` — годы (например `1y`)\n"
            "• Можно комбинировать (`1h 30min`) или указать `0` (до апелляции).",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    iso_role = interaction.guild.get_role(ISOLATION_ROLE_ID) if ISOLATION_ROLE_ID > 0 else None
    if not iso_role:
        return await interaction.followup.send(f"❌ На Площадке не настроена роль Изоляции (`ISOLATION_ROLE_ID = {ISOLATION_ROLE_ID}`).")

    # Strip and save ALL user roles (except @everyone, managed/booster roles, and the isolation role itself)
    roles_to_remove = [
        r for r in user.roles
        if not r.is_default() and not r.managed and r.id != ISOLATION_ROLE_ID
    ]
    all_removed_role_ids = [r.id for r in roles_to_remove]

    # Merge with any previously saved roles if the user was already in isolation
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT saved_roles FROM isolations WHERE user_id = ? AND guild_id = ?",
            (user.id, interaction.guild.id)
        ) as cursor:
            existing_iso_row = await cursor.fetchone()
        if existing_iso_row and existing_iso_row[0]:
            try:
                for prev_rid in json.loads(existing_iso_row[0]):
                    if int(prev_rid) not in all_removed_role_ids and int(prev_rid) != ISOLATION_ROLE_ID:
                        all_removed_role_ids.append(int(prev_rid))
            except Exception:
                pass

    try:
        if roles_to_remove:
            await user.remove_roles(*roles_to_remove, reason=f"АСУ Изоляция (п. 3.5): {reason}")
        if iso_role not in user.roles:
            await user.add_roles(iso_role, reason=f"АСУ Изоляция (п. 3.5): {reason}")
    except discord.Forbidden:
        return await interaction.followup.send("❌ Ошибка иерархии Discord: бот не может изменить роли этого пользователя.")

    timeout_sec = min(duration_seconds, DISCORD_MAX_TIMEOUT_SECONDS) if duration_seconds > 0 else DISCORD_MAX_TIMEOUT_SECONDS
    mute_status_note = "Системный мут (текст + голос) наложен"
    try:
        until_dt = discord.utils.utcnow() + datetime.timedelta(seconds=timeout_sec)
        await user.timeout(until_dt, reason=f"АСУ Изоляция (п. 3.5): {reason}")
    except discord.Forbidden:
        mute_status_note = "Роль выдана (тайм-аут не применён: у цели права Администратора сервера)"
    except Exception as e:
        mute_status_note = f"Роль выдана (ошибка тайм-аута: {e})"

    if user.voice:
        try:
            await user.edit(mute=True, reason=f"АСУ Изоляция (п. 3.5): {reason}")
        except Exception:
            pass

    now = int(time.time())
    end_time = (now + duration_seconds) if duration_seconds > 0 else 0

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            '''INSERT OR REPLACE INTO isolations 
               (user_id, guild_id, moderator_id, reason, start_time, end_time, saved_roles, is_twink)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (user.id, interaction.guild.id, interaction.user.id, reason, now, end_time, json.dumps(all_removed_role_ids), int(is_twink))
        )
        await db.commit()

    await record_command_usage(interaction.user.id, interaction.guild.id, "risolation")

    # Rule 1.2 & 2.6: Preserve prior votes unless proven twink account
    purged_polls = 0
    if is_twink:
        uid_str = str(user.id)
        async with file_lock:
            for poll_id, pdata in list(POLLS_CACHE.items()):
                if pdata.get("status") in ("active", "cooldown", "rejected_frozen") and uid_str in pdata.get("votes", {}):
                    del pdata["votes"][uid_str]
                    purged_polls += 1
                    save_poll_data(poll_id, pdata)

    human_dur = format_duration_human(duration_seconds)
    duration_str = f"До <t:{end_time}:f> (<t:{end_time}:R>)" if end_time > 0 else human_dur
    twink_str = f"Да (аннулировано голосов: {purged_polls})" if is_twink else "Нет"

    log_emb = discord.Embed(
        title="🔒 Применена мера Изоляции (п. 3.5)",
        description=(
            f"**Ограниченный участник:** {user.mention} (`{user.id}`)\n"
            f"**Управленец:** {interaction.user.mention} (Лимит: `{used_count + 1}/{DAILY_ACTION_LIMIT}`)\n"
            f"**Причина:** {reason}\n"
            f"**Срок:** {duration_str}\n"
            f"**Статус мута:** {mute_status_note}\n"
            f"**Изъято и сохранено ролей:** `{len(all_removed_role_ids)}`\n"
            f"**Твинк:** {twink_str}"
        ),
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )
    await log_moderation_action(interaction.guild, log_emb)

    try:
        dm_embed = discord.Embed(
            title="⚠️ Вы переведены в статус Ограниченного участника (Изоляция, п. 3.5)",
            description=(
                f"**Площадка:** {interaction.guild.name}\n"
                f"**Применил Управленец:** {interaction.user.display_name}\n"
                f"**Обоснование причины (п. 4.3):** {reason}\n"
                f"**Срок и условия выхода (п. 5.2):** {duration_str}\n\n"
                f"⚖️ **Ваше гарантированное право на Публичную апелляцию (п. 5.1 Регламента):**\n"
                f"1. Напишите в этот личный чат с ботом сообщение с аргументированным текстом вашей апелляции.\n"
                f"2. Скопируйте ID вашего сообщения (или ссылку на него) и вызовите команду `/rappeal message_id:<ID>`.\n"
                f"3. АСУ запустит голосование в категории `[Правосудие]` (III Тир, защищено от отмены по п. 0.2 согласно п. 4.5) "
                f"и при победе автоматически восстановит ваш статус Полноценного участника!"
            ),
            color=discord.Color.orange()
        )
        await user.send(embed=dm_embed)
    except Exception:
        pass

    await interaction.followup.send(
        f"✅ Пользователь {user.mention} переведён в статус Ограниченного участника (`{human_dur}`).\n"
        f"📊 Использовано лимитов `/risolation` за 24ч: **{used_count + 1}/{DAILY_ACTION_LIMIT}**.",
        ephemeral=True
    )


@bot.tree.command(name="runisolation", description="Досрочно снять Изоляцию с участника и восстановить его права")
@app_commands.describe(user="Ограниченный участник", reason="Причина снятия ограничений (п. 4.3)")
async def runisolation(interaction: discord.Interaction, user: discord.Member, reason: str = "Завершение разбирательства / Оправдание"):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Доступно только на Площадке.", ephemeral=True)

    if not has_command_permission(interaction.user, "runisolation"):
        return await interaction.response.send_message("❌ У вашей должности нет прав на выполнение `/runisolation`.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    await restore_user_from_isolation(interaction.guild, user, reason=f"Снято Управленцем {interaction.user.name}: {reason}")

    log_emb = discord.Embed(
        title="🕊️ Снятие режима Изоляции (/runisolation)",
        description=(
            f"**Участник:** {user.mention}\n"
            f"**Управленец:** {interaction.user.mention}\n"
            f"**Основание:** {reason}"
        ),
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    await log_moderation_action(interaction.guild, log_emb)
    await interaction.followup.send(f"✅ С пользователя {user.mention} снята Изоляция, права Полноценного участника восстановлены.", ephemeral=True)


@bot.tree.command(name="rappeal", description="Подать Публичную апелляцию из ЛС бота для автоматического снятия Изоляции (п. 5.1)")
@app_commands.describe(message_id="ID вашего сообщения (или ссылка на него) с текстом апелляции в ЛС боту")
async def rappeal(interaction: discord.Interaction, message_id: str):
    await interaction.response.defer(ephemeral=True)

    id_matches = SNOWFLAKE_RE.findall(message_id.strip())
    if not id_matches:
        return await interaction.followup.send("⚠️ Укажите корректный числовой ID вашего сообщения (или ссылку на него), отправленного в ЛС боту.")
    parsed_msg_id = int(id_matches[-1])

    # Strictly check ONLY authorized platforms from REGISTERED_PLATFORMS / REGISTERED_PLATFORM_ID
    authorized_guilds = get_authorized_guilds()
    if not authorized_guilds:
        return await interaction.followup.send("⛔ Бот не подключён ни к одной авторизованной Площадке (`REGISTERED_PLATFORMS`).")

    iso_guild_ids: Set[int] = set()
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT guild_id FROM isolations WHERE user_id = ?", (interaction.user.id,)) as cursor:
            for row in await cursor.fetchall():
                if is_authorized_platform(int(row[0])):
                    iso_guild_ids.add(int(row[0]))

    target_guild: Optional[discord.Guild] = None
    target_member: Optional[discord.Member] = None

    for g in authorized_guilds:
        m = g.get_member(interaction.user.id)
        if not m:
            try:
                m = await g.fetch_member(interaction.user.id)
            except Exception:
                continue

        has_iso_role = (ISOLATION_ROLE_ID > 0 and any(r.id == ISOLATION_ROLE_ID for r in m.roles))
        in_iso_db = (g.id in iso_guild_ids)

        if has_iso_role or in_iso_db:
            target_guild = g
            target_member = m
            break

    if not target_guild or not target_member:
        return await interaction.followup.send(
            "⛔ **Отказ АСУ:** Вы не найдены на авторизованной Площадке (`REGISTERED_PLATFORMS`) или у вас отсутствует роль/статус Изоляции (`ISOLATION_ROLE_ID`)."
        )

    for pdata in POLLS_CACHE.values():
        if (
            pdata.get("poll_type") == "appeal"
            and pdata.get("guild_id") == target_guild.id
            and pdata.get("target_user_id") == interaction.user.id
            and pdata.get("status") in ("cooldown", "active", "rejected_frozen")
        ):
            existing_link = f"https://discord.com/channels/{pdata['guild_id']}/{pdata['channel_id']}/{pdata['poll_message_id']}"
            return await interaction.followup.send(
                f"⚠️ Ваша Публичная апелляция уже активна и находится на рассмотрении: {existing_link}"
            )

    target_msg: Optional[discord.Message] = None
    if interaction.channel:
        try:
            target_msg = await interaction.channel.fetch_message(parsed_msg_id)
        except Exception:
            pass

    if not target_msg:
        try:
            dm_channel = interaction.user.dm_channel or await interaction.user.create_dm()
            target_msg = await dm_channel.fetch_message(parsed_msg_id)
        except Exception:
            pass

    if not target_msg:
        target_msg = discord.utils.get(bot.cached_messages, id=parsed_msg_id)

    if not target_msg:
        return await interaction.followup.send(
            "⚠️ Сообщение с таким ID не найдено в ЛС с ботом.\n"
            "Отправьте текст апелляции в личные сообщения боту, скопируйте его ID и вызовите `/rappeal message_id:<ID>`."
        )

    if target_msg.author.id != interaction.user.id:
        return await interaction.followup.send("⚠️ Для подачи апелляции можно указать только собственное сообщение.")

    if not (target_msg.content and target_msg.content.strip()):
        return await interaction.followup.send("⚠️ Сообщение с апелляцией должно содержать текстовое обоснование.")

    t_cfg = TIERS["High"]

    poll_msg = await launch_poll_instance(
        guild=target_guild,
        fallback_channel=None,
        initiator=interaction.user,
        target_author_name=f"{target_member.display_name} (Ограниченный участник)",
        quote_content=target_msg.content.strip(),
        category="Правосудие",
        tier_name="High",
        win_pct=t_cfg["win_pct"],
        cooldown_sec=t_cfg["cooldown"],
        quorum_pct=t_cfg["quorum_pct"],
        duration_sec=t_cfg["duration"],
        poll_type="appeal",
        target_message_id=target_msg.id,
        target_user_id=interaction.user.id,
        protect_from_02=True,
        destination_channel_id=APPEALS_CHANNEL_ID
    )

    if poll_msg:
        await interaction.followup.send(
            f"✅ Ваша Публичная апелляция опубликована на Площадке **{target_guild.name}**: {poll_msg.jump_url}\n"
            f"⚙️ При победе голосования АСУ **автоматически снимет с вас Изоляцию** и вернёт статус Полноценного участника (п. 5.1, 6.6)."
        )
    else:
        await interaction.followup.send("❌ Не удалось опубликовать апелляцию. Проверьте настройки каналов Площадки.")


if hasattr(app_commands, "allowed_contexts"):
    rappeal = app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)(rappeal)
if hasattr(app_commands, "allowed_installs"):
    rappeal = app_commands.allowed_installs(guilds=True, users=True)(rappeal)


# =====================================================================
# ROLE & TWINK REGISTRY COMMANDS (/relection, /rselfimpeach, /rapprove_twink)
# =====================================================================

@bot.tree.command(name="relection", description="Техническая выдача роли Управленца с задержкой 30 мин. (лимит: 3 в 24ч)")
@app_commands.describe(
    user="Участник, которому назначается роль",
    role="Роль Управленца (не выше вашей собственной управленческой роли по иерархии Discord)",
    reason="Публичное основание выдачи (ссылка на решение или стажировку, п. 4.3, 7.1.3)"
)
async def relection(interaction: discord.Interaction, user: discord.Member, role: discord.Role, reason: str = "Исполнение кадрового решения"):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Доступно только на Площадке.", ephemeral=True)

    if not has_command_permission(interaction.user, "relection"):
        return await interaction.response.send_message("❌ У вашей должности нет прав на использование `/relection`.", ephemeral=True)

    if role.id not in MANAGER_ROLES:
        allowed_names = ", ".join(f"`{n}`" for n in MANAGER_ROLES.values()) or "список пуст"
        return await interaction.response.send_message(
            f"❌ Роль {role.mention} не числится в реестре ролей Управленцев (`MANAGER_ROLES`: {allowed_names}).",
            ephemeral=True
        )

    caller_manager_roles = [r for r in interaction.user.roles if r.id in MANAGER_ROLES]
    if not caller_manager_roles and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ У вас нет активной роли Управленца.", ephemeral=True)

    highest_caller_mgr_pos = max((r.position for r in caller_manager_roles), default=interaction.user.top_role.position)

    if role.position > highest_caller_mgr_pos:
        return await interaction.response.send_message(
            f"❌ **Нарушение иерархии:** Роль {role.mention} находится выше вашей должности.",
            ephemeral=True
        )

    can_use, used_count, next_avail = await check_daily_command_limit(interaction.user.id, interaction.guild.id, "relection")
    if not can_use:
        return await interaction.response.send_message(
            f"⏳ **Достигнут суточный лимит:** Команду `/relection` можно использовать не более **{DAILY_ACTION_LIMIT} раз за 24 часа**.\n"
            f"Следующее использование станет доступно: <t:{next_avail}:R>.",
            ephemeral=True
        )

    execute_at = int(time.time()) + RELECTION_DELAY_SECONDS
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            '''INSERT INTO pending_appointments (guild_id, target_user_id, role_id, initiator_id, reason, execute_at, executed)
               VALUES (?, ?, ?, ?, ?, ?, 0)''',
            (interaction.guild.id, user.id, role.id, interaction.user.id, reason, execute_at)
        )
        await db.commit()

    await record_command_usage(interaction.user.id, interaction.guild.id, "relection")

    log_emb = discord.Embed(
        title="⏳ Запланирована выдача роли Управленца (/relection)",
        description=(
            f"**Кандидат:** {user.mention}\n"
            f"**Назначаемая должность:** {role.mention}\n"
            f"**Инициатор:** {interaction.user.mention} (Лимит за 24ч: `{used_count + 1}/{DAILY_ACTION_LIMIT}`)\n"
            f"**Основание (п. 4.3):** {reason}\n"
            f"**Автоматическое исполнение (задержка 30 мин.):** <t:{execute_at}:R> (<t:{execute_at}:T>)"
        ),
        color=discord.Color.blue(),
        timestamp=discord.utils.utcnow()
    )
    await log_moderation_action(interaction.guild, log_emb)

    await interaction.response.send_message(
        f"✅ Назначение роли {role.mention} пользователю {user.mention} поставлено в очередь АСУ.\n"
        f"⏳ Роль будет выдана автоматически через **30 минут** (<t:{execute_at}:R>).",
        ephemeral=True
    )


@bot.tree.command(name="rselfimpeach", description="Добровольно сложить с себя полномочия Управленца (п. 7.3 Регламента)")
@app_commands.describe(
    role="Управленческая роль (@роль), которую вы хотите снять с себя",
    role_id="ID управленческой роли (можно указать вместо @роли)"
)
async def rselfimpeach(
    interaction: discord.Interaction,
    role: Optional[discord.Role] = None,
    role_id: Optional[str] = None
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Команда доступна только на Площадке.", ephemeral=True)

    target_role: Optional[discord.Role] = role

    if role_id:
        id_matches = SNOWFLAKE_RE.findall(role_id.strip())
        if not id_matches:
            return await interaction.response.send_message("⚠️ В параметре `role_id` указан некорректный ID роли.", ephemeral=True)
        parsed_rid = int(id_matches[0])
        if target_role and target_role.id != parsed_rid:
            return await interaction.response.send_message("⚠️ Указанная роль не совпадает с введённым `role_id`.", ephemeral=True)
        if not target_role:
            target_role = interaction.guild.get_role(parsed_rid)

    if not target_role:
        return await interaction.response.send_message("⚠️ Укажите управленческую роль через `@role` или `role_id`.", ephemeral=True)

    if target_role.id not in MANAGER_ROLES:
        return await interaction.response.send_message(
            f"❌ Роль {target_role.mention} не входит в реестр управленческих должностей АСУ (`MANAGER_ROLES`).",
            ephemeral=True
        )

    if target_role not in interaction.user.roles:
        return await interaction.response.send_message(f"ℹ️ У вас нет роли {target_role.mention}.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    try:
        await interaction.user.remove_roles(
            target_role,
            reason="АСУ: Добровольная отставка Управленца (/rselfimpeach, п. 7.3 Регламента)"
        )
    except discord.Forbidden:
        return await interaction.followup.send("❌ Ошибка иерархии Discord: бот не может снять эту роль.", ephemeral=True)

    log_emb = discord.Embed(
        title="🏳️ Добровольная отставка Управленца (/rselfimpeach, п. 7.3)",
        description=(
            f"**Участник:** {interaction.user.mention} (`{interaction.user.id}`)\n"
            f"**Снятая должность:** {target_role.mention} (`{target_role.id}`)"
        ),
        color=discord.Color.gold(),
        timestamp=discord.utils.utcnow()
    )
    await log_moderation_action(interaction.guild, log_emb)

    await interaction.followup.send(
        f"✅ Вы добровольно сложили с себя полномочия: должность {target_role.mention} снята (п. 7.3 Регламента).",
        ephemeral=True
    )


@bot.tree.command(name="rapprove_twink", description="Внести одобренный голосованием технический аккаунт в реестр без права голоса (п. 2.6)")
@app_commands.describe(
    twink_user="Технический/проектный дополнительный аккаунт",
    owner_user="Основной аккаунт владельца",
    poll_id="ID успешного голосования Сообщества, одобрившего нахождение аккаунта (п. 2.6)"
)
async def rapprove_twink(
    interaction: discord.Interaction,
    twink_user: discord.Member,
    owner_user: discord.Member,
    poll_id: str
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Доступно только на Площадке.", ephemeral=True)

    if not has_command_permission(interaction.user, "rapprove_twink"):
        return await interaction.response.send_message("❌ У вашей должности нет прав на выполнение `/rapprove_twink`.", ephemeral=True)

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR REPLACE INTO approved_twinks (twink_user_id, guild_id, owner_user_id, poll_id, approved_at) VALUES (?, ?, ?, ?, ?)",
            (twink_user.id, interaction.guild.id, owner_user.id, poll_id.strip(), int(time.time()))
        )
        await db.commit()

    log_emb = discord.Embed(
        title="🤖 Зарегистрирован технический аккаунт без права голоса (п. 2.6)",
        description=(
            f"**Технический аккаунт:** {twink_user.mention} (`{twink_user.id}`)\n"
            f"**Владелец:** {owner_user.mention} (`{owner_user.id}`)\n"
            f"**Одобрено голосованием:** `{poll_id}`\n"
            f"**Внёс в реестр:** {interaction.user.mention}"
        ),
        color=discord.Color.teal(),
        timestamp=discord.utils.utcnow()
    )
    await log_moderation_action(interaction.guild, log_emb)
    await interaction.response.send_message(
        f"✅ Аккаунт {twink_user.mention} зарегистрирован как технический аккаунт без права голоса (владелец: {owner_user.mention}, п. 2.6).",
        ephemeral=True
    )


# =====================================================================
# STATISTICS & INFORMATION COMMANDS (/rstatistics, /rlist, /ractivelist, /rhelp)
# =====================================================================

@bot.tree.command(name="rstatistics", description="Создать самообновляющийся виджет статистики АСУ SVA V. RH")
@app_commands.describe(seconds="Интервал автообновления виджета в секундах (от 15 сек.)")
async def rstatistics(interaction: discord.Interaction, seconds: int = 60):
    global DASHBOARD_CACHE
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Доступно только на Площадке.", ephemeral=True)

    if not has_command_permission(interaction.user, "rstatistics"):
        return await interaction.response.send_message("❌ У вашей должности нет прав на запуск `/rstatistics`.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    safe_interval = max(15, seconds)

    embed = await generate_statistics_embed(interaction.guild, safe_interval)
    widget_msg = await interaction.channel.send(embed=embed)

    state_data = {
        "guild_id": interaction.guild.id,
        "channel_id": interaction.channel.id,
        "message_id": widget_msg.id,
        "interval": safe_interval,
        "last_update": int(time.time())
    }
    DASHBOARD_CACHE = state_data
    write_json_atomic(DASHBOARD_STATE_FILE, state_data)

    await interaction.followup.send(
        f"✅ Живой виджет мониторинга АСУ создан и обновляется каждые **{safe_interval} сек.**!",
        ephemeral=True
    )


@bot.tree.command(name="rlist", description="Показать список всех текущих голосований Площадки")
async def rlist(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("❌ Доступно только на Площадке.", ephemeral=True)

    description_lines = []

    for data in POLLS_CACHE.values():
        try:
            if data.get("guild_id") != interaction.guild.id:
                continue
            if data.get("status") not in ("active", "cooldown", "rejected_frozen"):
                continue

            msg_link = f"https://discord.com/channels/{interaction.guild.id}/{data['channel_id']}/{data['poll_message_id']}"
            quote = data["quote_content"].replace("\n", " ")
            if len(quote) > 45:
                quote = quote[:45] + "..."

            status_ico = {"cooldown": "🧊", "active": "📊", "rejected_frozen": "⏸️"}.get(data.get("status"), "📊")
            if data.get("tier") == "Crit" and data.get("status") == "active":
                status_ico = "🌐"
            shield_ico = "🛡️" if data.get("aci_shield") else ("⚡" if data.get("immune_to_aci") else "")
            t_lbl = TIER_ROMAN.get(data["tier"], data["tier"])
            line = (
                f"**{len(description_lines)+1}.** {status_ico}{shield_ico} "
                f"[{data['category']} / {t_lbl}]({msg_link}) — ID: `{data['poll_message_id']}`\n"
                f"> *{quote}*\n"
                f"Итоги: <t:{data['end_time']}:R> | Явка: **{len(data.get('votes', {}))}/{data['quorum_req']}**"
            )
            description_lines.append(line)
        except Exception:
            pass

    if not description_lines:
        return await interaction.response.send_message("📭 Активных голосований в данный момент нет.", ephemeral=True)

    embed = discord.Embed(
        title="📋 Текущие голосования Сообщества «Горизонт»",
        description="\n\n".join(description_lines)[:4096],
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="ractivelist", description="Показать статистику активности за 30 дней (база кворума, п. 6.7)")
async def ractivelist(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("❌ Доступно только на Площадке.", ephemeral=True)

    await interaction.response.defer()
    active_count, threshold, active_users = await get_active_users_stats(interaction.guild.id)

    if not active_users:
        return await interaction.followup.send("📊 За последние 30 дней нет данных об активности.")

    top_display = [f"**{idx}.** <@{uid}> — `{count}` сообщ." for idx, (uid, count) in enumerate(active_users[:15], 1)]
    if len(active_users) > 15:
        top_display.append(f"\n*...и ещё {len(active_users) - 15} активных участников на Площадке.*")

    embed = discord.Embed(
        title="👥 База расчёта динамического кворума (за 30 дней, п. 6.7)",
        description=(
            f"**Порог активности (медиана):** `{threshold:g} сообщ.`\n"
            f"**Активных участников Площадки (100% базы кворума):** `{active_count} чел.`\n\n"
            f"**Топ активности:**\n" + "\n".join(top_display)
        ),
        color=discord.Color.purple()
    )
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="rhelp", description="Справка по командам АСУ RedAdapter и архитектуре SVA V. RH")
async def rhelp(interaction: discord.Interaction):
    help_text = (
        "**Автоматизированная Система Управления (АСУ) — SVA V. RH**\n"
        "### 🗳️ Шкала Лайкерта и защита от конформизма (п. 6.3):\n"
        "- `⏫ (+2)` / `🔼 (+1)` / `⏺️ (0)` / `🔽 (-1)` / `⏬ (-2)` — веса голосов для расчёта поддержки.\n"
        "- `🔍 Мой голос` — проверить свой сохранённый вариант ответа.\n"
        "- `🚩 Жалоба ACI` — отправить запрос Управленцам на проверку `Legitimacy` и `Theme` (Тира).\n\n"
        "### 📊 Запуск голосований (Разделы 1, 6, 7):\n"
        "- `/rpoll standard <ID> <Категория> [Канал]` — создать голосование по тексту и Категории (I–IV Тир).\n"
        "- `/rpoll impeach <@Управленец> [Канал]` — шаблонное голосование III Тира за снятие Управленца в обход ACI (п. 6.8).\n"
        "- `/rpoll global election <@Кандидат> <@Роль> <ID> [Канал]` — прямое выдвижение на должность в обход анкет (п. 7.1.1).\n"
        "- `/rpollelection <@Роль>` — пройти локальное квалификационное анкетирование (п. 7.1, 7.2).\n\n"
        "### 🕊️ Защита прав Ограниченных участников (Раздел 5):\n"
        "- `/rappeal <ID сообщения из ЛС>` — подать Публичную апелляцию напрямую из ЛС с ботом (п. 5.1).\n\n"
        "### 🛡️ Инструменты Управленцев (Разделы 3, 4, 6, 7):\n"
        "- `/rreject <ID> <Основание> <Комментарий>` — отклонить и заморозить голосование по ACI или п. 0.2 (п. 3.3).\n"
        "- `/rconfirm <ID> [Комментарий]` — выдать щит ACI или разморозить опрос в порядке перекрёстного контроля (п. 3.3.1).\n"
        "- `/runexecutable <ID> <Пункт 6.10 / 6.10.1> <Доказательство>` — опубликовать обоснованный отказ по технической неисполнимости.\n"
        "- `/risolation <@Юзер> <Причина> [Срок] [Твинк]` — перевести в Изоляцию с мутом (п. 3.5, лимит: 3 в 24ч).\n"
        "- `/runisolation <@Юзер> [Причина]` — досрочно снять Изоляцию.\n"
        "- `/relection <@Юзер> <@Роль> [Причина]` — назначить роль Управленца с задержкой 30 мин. (лимит: 3 в 24ч).\n"
        "- `/rselfimpeach [@Роль] [role_id]` — добровольно сложить полномочия Управленца (п. 7.3).\n"
        "- `/rapprove_twink <@Твинк> <@Владелец> <ID опроса>` — внести одобренный технический аккаунт в реестр без права голоса (п. 2.6).\n"
        "- `/rstatistics`, `/rlist`, `/ractivelist` — мониторинг, список голосований и база кворума."
    )
    embed = discord.Embed(title="ℹ️ Документация АСУ RedAdapter (SVA V. RH)", description=help_text, color=discord.Color.teal())
    await interaction.response.send_message(embed=embed, ephemeral=True)


# =====================================================================
# ENTRYPOINT
# =====================================================================
if __name__ == "__main__":
    if PROXY_URL:
        bot.run(BOT_TOKEN, proxy=PROXY_URL)
    else:
        bot.run(BOT_TOKEN)
