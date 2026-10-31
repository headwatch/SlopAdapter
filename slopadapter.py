import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import io
import time
import asyncio
import aiosqlite
import statistics
import math
import re
import datetime
from typing import Optional, Dict, Any, List, Tuple

# =====================================================================
# 1. CONFIGURATION & PERMISSIONS
# =====================================================================

BOT_TOKEN = "ВАШ_ТОКЕН_ЗДЕСЬ"
PROXY_URL = None

GUILD_ID = 0

POLLS_CHANNEL_IDS: List[int] = []

ELECTIONS_CHANNEL_ID = 0
APPEALS_CHANNEL_ID = 0
MOD_LOG_CHANNEL_ID = 0

MEMBER_ROLE_ID = 0
ISOLATION_ROLE_ID = 0

# Map of role_id -> role_title for authorized staff roles
MANAGER_ROLES: Dict[int, str] = {}

# Role IDs permitted to execute specific privileged commands
COMMAND_PERMISSIONS: Dict[str, List[int]] = {
    "rreject":      [],
    "rconfirm":     [],
    "risolation":   [],
    "runisolation": [],
    "relection":    [],
    "rstatistics":  []
}

ACI_CROSS_CONTROL_WINDOW = 86400
RELECTION_DELAY_SECONDS = 1800
THIRTY_DAYS_SECONDS = 2592000

DAILY_ACTION_LIMIT = 3
DAILY_LIMIT_WINDOW = 86400

# =====================================================================
# 2. SVA V. RH TIERS & CATEGORIES
# =====================================================================

TIERS: Dict[str, Dict[str, int]] = {
    "Low":  {"win_pct": 50, "cooldown": 0,     "quorum_pct": 15, "duration": 86400},
    "Mid":  {"win_pct": 55, "cooldown": 10800, "quorum_pct": 30, "duration": 172800},
    "High": {"win_pct": 60, "cooldown": 43200, "quorum_pct": 50, "duration": 259200},
    "Crit": {"win_pct": 80, "cooldown": 86400, "quorum_pct": 75, "duration": 604800}
}

CATEGORIES: Dict[str, str] = {
    "Косметика": "Low",
    "Ивенты": "Low",
    "Опросы": "Low",
    "Каналы": "Mid",
    "Роли": "Mid",
    "Формат": "Mid",
    "Правосудие": "High",
    "Правила": "High",
    "Стафф": "High",
    "Интеграции": "High",
    "Лого": "High",
    "Название": "Crit",
    "Система голосования": "Crit",
    "Фундамент": "Crit"
}

DURATION_UNITS: Dict[str, int] = {
    "s": 1,
    "h": 3600,
    "d": 86400,
    "m": 2592000,
    "y": 31536000
}
DURATION_TOKEN_RE = re.compile(r"(\d+)\s*([shdmy])")
WHITESPACE_RE = re.compile(r"\s+")
SNOWFLAKE_RE = re.compile(r"\d{15,22}")
DISCORD_MAX_TIMEOUT_SECONDS = 2419200

# =====================================================================
# 3. STATE, CACHES & PERSISTENCE
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
ARCHIVE_CACHE: Dict[str, Dict[str, Any]] = {}
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


def write_json_atomic(filepath: str, data: Dict[str, Any]) -> None:
    """Writes JSON via a temporary file to prevent corruption on crash."""
    tmp_path = f"{filepath}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp_path, filepath)


def archive_poll_data(poll_id: str, data: Dict[str, Any]) -> None:
    """Moves completed or permanently rejected poll from active storage to archive."""
    poll_id_str = str(poll_id)
    active_path = os.path.join(POLLS_DIR, f"{poll_id_str}.json")
    archive_path = os.path.join(ARCHIVE_DIR, f"{poll_id_str}.json")
    write_json_atomic(archive_path, data)
    if os.path.exists(active_path):
        try:
            os.remove(active_path)
        except Exception:
            pass
    POLLS_CACHE.pop(poll_id_str, None)
    ARCHIVE_CACHE[poll_id_str] = data


def load_polls_into_cache() -> None:
    """Loads active and archived polls from disk; auto-archives terminal polls."""
    POLLS_CACHE.clear()
    ARCHIVE_CACHE.clear()

    for fname in os.listdir(ARCHIVE_DIR):
        if not fname.endswith(".json"):
            continue
        poll_id = fname[:-5]
        fpath = os.path.join(ARCHIVE_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                ARCHIVE_CACHE[poll_id] = json.load(f)
        except Exception as e:
            print(f"[Archive Load Error on {fname}]: {e}")

    for fname in os.listdir(POLLS_DIR):
        if not fname.endswith(".json"):
            continue
        poll_id = fname[:-5]
        fpath = os.path.join(POLLS_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("status") in ("finished", "rejected_final"):
                archive_poll_data(poll_id, data)
            else:
                POLLS_CACHE[poll_id] = data
        except Exception as e:
            print(f"[Cache Load Error on {fname}]: {e}")


def get_poll_data(poll_id: str, include_archive: bool = False) -> Optional[Dict[str, Any]]:
    """Fetches poll state from active cache/disk, optionally falling back to archive."""
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
        if poll_id in ARCHIVE_CACHE:
            return ARCHIVE_CACHE[poll_id]
        archive_path = os.path.join(ARCHIVE_DIR, f"{poll_id}.json")
        if os.path.exists(archive_path):
            try:
                with open(archive_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                ARCHIVE_CACHE[poll_id] = data
                return data
            except Exception:
                return None
    return None


def save_poll_data(poll_id: str, data: Dict[str, Any]) -> None:
    """Saves active poll or routes terminal poll directly to archive."""
    poll_id_str = str(poll_id)
    if data.get("status") in ("finished", "rejected_final"):
        archive_poll_data(poll_id_str, data)
    else:
        POLLS_CACHE[poll_id_str] = data
        filepath = os.path.join(POLLS_DIR, f"{poll_id_str}.json")
        write_json_atomic(filepath, data)


def ensure_sample_election_template() -> None:
    """Creates a reference questionnaire JSON template if none exists."""
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
            '    "role_id": 0, // Замените на цифровой ID роли в Discord\n'
            '    "role_name": "Модератор", // Точное название роли\n'
            '    "tier": "High", // Уровень значимости: Low, Mid, High или Crit\n'
            '    "win_pct": 60, // Процент одобрения для победы\n'
            '    "cooldown": 43200, // Время охлаждения в секундах (43200 = 12 часов)\n'
            '    "quorum_pct": 50, // Кворум от активных пользователей в %\n'
            '    "duration": 259200, // Длительность голосования в секундах (259200 = 3 дня)\n'
            '    "description": "Квалификационная анкета кандидата на должность Модератора сообщества «Горизонт» (п. 7.1, 7.2 Регламента).",\n'
            '    "questions": [\n'
            '        "1. Ваш часовой пояс и среднее время онлайна в сутки?",\n'
            '        "2. Был ли у вас опыт модерации и работы со Стандартом ACI?",\n'
            '        "3. Как вы поступите, если другой Управленец нарушит п. 4.3 (скрытая модерация)?",\n'
            '        "4. В каких случаях допустимо применение Изоляции (п. 3.5)?",\n'
            '        "5. Почему ваша инициатива не приведёт к «админюзу» (Раздел 4)?",\n'
            '        "6. Опишите, как работает перекрёстный контроль валидации (п. 3.3.1).",\n'
            '        "7. Ваша главная цель на посту Управленца в ближайший месяц?"\n'
            '    ]\n'
            "}\n"
            "*/\n"
        )
        with open(sample_path, "w", encoding="utf-8") as f:
            f.write(commented_template)


async def setup_db() -> None:
    """Initializes SQLite tables in WAL mode for activity, isolations, and rate limits."""
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
            user_id INTEGER PRIMARY KEY,
            guild_id INTEGER,
            moderator_id INTEGER,
            reason TEXT,
            start_time INTEGER,
            end_time INTEGER,
            saved_roles TEXT,
            is_twink INTEGER
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
        await db.commit()


async def flush_message_logs() -> None:
    """Persists in-memory message timestamp buffer to SQLite."""
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
# 4. VALIDATION, PERMISSIONS & QUORUM HELPERS
# =====================================================================

def resolve_poll_destination_channel(
    guild: discord.Guild,
    fallback_channel: Optional[discord.abc.Messageable],
    explicit_channel_id: Optional[int] = None,
    requested_poll_channel: Optional[discord.TextChannel] = None
) -> Tuple[Optional[discord.abc.Messageable], Optional[str]]:
    """Determines and validates the target channel for publishing a poll."""
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

    return None, "❌ На сервере не найден подходящий текстовый канал для публикации голосования."


def is_manager_related_poll(
    guild: discord.Guild,
    category: str,
    content: str,
    target_user_id: Optional[int] = None,
    target_role_id: Optional[int] = None
) -> bool:
    """Checks if a poll evaluates or restricts Managers to grant Rule 0.2 protection (Rule 4.5)."""
    if target_role_id and target_role_id in MANAGER_ROLES:
        return True

    if target_user_id:
        member = guild.get_member(target_user_id)
        if member and any(r.id in MANAGER_ROLES for r in member.roles):
            return True

    if category not in ("Правосудие", "Стафф"):
        return False

    extracted_ids = {int(x) for x in SNOWFLAKE_RE.findall(content)}
    for sid in extracted_ids:
        if sid in MANAGER_ROLES:
            return True
        member = guild.get_member(sid)
        if member and any(r.id in MANAGER_ROLES for r in member.roles):
            return True

    lowered = content.lower()
    manager_keywords = ["управлен", "модератор", "админ", "п. 4.", "п. 3.5", "импичмент", "сняти", "должност"]
    for role_title in MANAGER_ROLES.values():
        if role_title.lower() in lowered:
            return True

    return any(kw in lowered for kw in manager_keywords)


async def check_daily_command_limit(user_id: int, guild_id: int, command_name: str) -> Tuple[bool, int, int]:
    """Enforces 24h rolling window rate limit for sensitive staff commands."""
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
    """Records execution timestamp for rate-limited command."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO command_usage_logs (user_id, guild_id, command_name, timestamp) VALUES (?, ?, ?, ?)",
            (user_id, guild_id, command_name, int(time.time()))
        )
        await db.commit()


def parse_isolation_duration(duration_str: str) -> Optional[int]:
    """Converts duration string (e.g., '1d 12h', '0') into seconds; None if malformed."""
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
    """Formats seconds into a localized readable duration string."""
    if seconds <= 0:
        return "До окончания публичного разбирательства / апелляции"
    parts = []
    for unit_name, unit_sec in [("г.", 31536000), ("мес.", 2592000), ("дн.", 86400), ("ч.", 3600), ("мин.", 60), ("сек.", 1)]:
        if seconds >= unit_sec:
            count, seconds = divmod(seconds, unit_sec)
            parts.append(f"{count} {unit_name}")
    return " ".join(parts)


def has_command_permission(member: discord.Member, command_key: str) -> bool:
    """Checks if member has any role listed in COMMAND_PERMISSIONS[command_key]."""
    allowed_roles = set(COMMAND_PERMISSIONS.get(command_key, ()))
    if not allowed_roles:
        return False
    return any(role.id in allowed_roles for role in member.roles)


def round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def get_rconfirm_quorum_stats(guild: Optional[discord.Guild]) -> Tuple[int, int]:
    if not guild:
        return 1, 1
    allowed_roles = set(COMMAND_PERMISSIONS.get("rconfirm", ()))
    if not allowed_roles:
        return 1, 0
    eligible_count = sum(
        1 for m in guild.members
        if not m.bot and any(r.id in allowed_roles for r in m.roles)
    )
    required_count = max(1, round_half_up(eligible_count / 5.0))
    return required_count, eligible_count


def resolve_poll_confirm_thresholds(data: Dict[str, Any]) -> Tuple[int, int]:
    guild = bot.get_guild(data.get("guild_id", GUILD_ID))
    if guild:
        req_count, total_eligible = get_rconfirm_quorum_stats(guild)
        data["confirm_req"] = req_count
        data["confirm_eligible_total"] = total_eligible
        return req_count, total_eligible
    return max(1, int(data.get("confirm_req", 1))), int(data.get("confirm_eligible_total", 1))


def check_full_member_status(member: discord.Member) -> Tuple[bool, str]:
    """Verifies member holds Full Member role and is not under Isolation (Rules 0.3, 3.5)."""
    user_role_ids = {r.id for r in member.roles}
    if ISOLATION_ROLE_ID > 0 and ISOLATION_ROLE_ID in user_role_ids:
        return False, "❌ Вы находитесь в статусе **Ограниченного участника (Изоляция, п. 3.5)**. Ваше право на участие в голосованиях приостановлено (Раздел 5). Для оспаривания используйте `/rappeal` в ЛС бота."

    if MEMBER_ROLE_ID > 0 and member.guild.get_role(MEMBER_ROLE_ID) is not None and MEMBER_ROLE_ID not in user_role_ids:
        return False, "❌ У вас отсутствует роль **Полноценного участника** (п. 0.3 Регламента). До одобрения заявки вы не можете голосовать или запускать опросы."

    return True, ""


async def get_active_users_stats(guild_id: int) -> Tuple[int, float, List[Tuple[int, int]]]:
    """Computes 30-day message median and active user count for dynamic quorum (Rule 6.7)."""
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
        active_users = [row for row in results if row[1] >= threshold]
        active_len = len(active_users)
        ACTIVE_STATS_CACHE[guild_id] = (now, active_len, threshold, active_users)
        return active_len, threshold, active_users
    except Exception as e:
        print(f"[DB Error in get_active_users_stats]: {e}")
        return 0, 0.0, []


async def log_moderation_action(guild: discord.Guild, embed: discord.Embed) -> None:
    """Sends audit embed to MOD_LOG_CHANNEL_ID if configured."""
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
# 5. POLL EMBED & INTERACTIVE UI COMPONENTS
# =====================================================================

def build_poll_embed(data: Dict[str, Any]) -> discord.Embed:
    """Renders poll embed including status, thresholds, ACI shields, and complaint counter."""
    status = data.get("status", "active")
    poll_type = data.get("poll_type", "standard")
    poll_id = data.get("poll_message_id", "ID_ГОЛОСОВАНИЯ")
    voters_count = len(data.get("votes", {}))
    reports_count = len(data.get("aci_reports", []))
    confirmations = data.get("aci_confirmations", [])
    confirms_count = len(confirmations)
    req_confirms, eligible_total = resolve_poll_confirm_thresholds(data)

    if status == "cooldown":
        title_prefix = "🧊 [Охлаждение]"
        color = discord.Color.blue()
    elif status == "active":
        title_prefix = "📊 [Активно]"
        color = discord.Color.green()
    elif status == "rejected_frozen":
        title_prefix = "⏸️ [Заморожено ACI]"
        color = discord.Color.orange()
    elif status == "rejected_final":
        title_prefix = "❌ [Отклонено]"
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
    if len(formatted_quote) > 2400:
        formatted_quote = formatted_quote[:2400] + "\n> *...[полный неизменяемый текст прикреплён в файле ниже (п. 6.4.1)]*"

    desc_blocks = []

    if poll_type == "impeach":
        desc_blocks.append(
            "┏ ⚠️ **Экстренный протокол (п. 6.8 Регламента)**\n"
            "┃ **Снятие Управленца в обход ACI — крайняя мера защиты от саботажа.**\n"
            "┗ **Рекомендуется воздержаться (`⏺️`) или голосовать против (`🔽`/`⏬`), если Управленец не нарушал Стандарт ACI (п. 4.5, 6.2) или п. 0.2 Регламента.**"
        )

    source_link_str = ""
    if data.get("target_message_id") and data.get("source_channel_id") and data.get("guild_id"):
        src_url = f"https://discord.com/channels/{data['guild_id']}/{data['source_channel_id']}/{data['target_message_id']}"
        source_link_str = f" • [Оригинал]({src_url})"

    desc_blocks.append(
        f"### Повестка (Автор: **{data.get('target_author_name', 'Участник')}**{source_link_str})\n"
        f"{formatted_quote}"
    )

    embed = discord.Embed(
        title=embed_title,
        description="\n\n".join(desc_blocks),
        color=color
    )

    embed.add_field(
        name="📂 Данные",
        value=(
            f"┏ **Категория:** `{data['category']}`\n"
            f"┗ **Тир:** `{data['tier']}`"
        ),
        inline=True
    )

    embed.add_field(
        name="⚖️ Пороги",
        value=(
            f"┏ **Одобрение:** `>{data['win_pct']}%`\n"
            f"┗ **Кворум:** `{data['quorum_req']} чел.`"
        ),
        inline=True
    )

    embed.add_field(
        name="👥 Явка",
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

    if data.get("immune_to_aci"):
        aci_field_value = (
            f"┏ ⚡ Иммунитет от отмены по ACI (п. 6.8)\n"
            f"┗ ⚠️Жалобы: `{reports_count} шт.`"
        )
    else:
        aci_lines = []
        if data.get("aci_shield"):
            confirmed_by = data.get("confirmed_by_name", "Управленцем")
            aci_lines.append(f"┏ 🛡️ Подтверждено и защищено щитом ACI ({confirmed_by}, п. 3.3.1)")
        elif data.get("protect_from_02"):
            aci_lines.append("┏ 🛡️ Защищено от отмены по п. 0.2 (п. 4.5)")

        line_prefix = "┃" if aci_lines else "┏"
        aci_lines.append(
            f"{line_prefix} ⚖️ **Предохранитель ACI:** `/rreject` — `1 чел.` | `/rconfirm` — `{confirms_count}/{req_confirms} чел.` (1/5 от `{eligible_total}` с доступом)"
        )
        if confirms_count > 0 and not data.get("aci_shield"):
            confirmed_mentions = ", ".join(f"<@{c['user_id']}>" for c in confirmations)
            aci_lines.append(f"┃ ✅ **Подтвердили `/rconfirm`:** {confirmed_mentions}")
        aci_lines.append(f"┗ ⚠️Жалобы: `{reports_count} шт.`")
        aci_field_value = "\n".join(aci_lines)

    embed.add_field(name="🛡️ Статус ACI", value=aci_field_value, inline=False)

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
            f"┃ 📌 **Причина:** {data.get('reject_reason', 'Не указано')}\n"
            f"┃ 🛡️ **Для разморозки `/rconfirm`:** `{confirms_count}/{req_confirms} чел.` (1/5 от `{eligible_total}` с доступом)\n"
            f"┃ ⏱️ **Остаток времени:** `{rem_min} мин.`\n"
            f"┗ 🔄 **Окно `/rconfirm` (п. 3.3.1) до:** <t:{deadline}:f> (<t:{deadline}:R>)"
        )
    elif status == "rejected_final":
        timer_block = f"❌ **Причина отклонения:** {data.get('reject_reason', 'Несоответствие ACI')}"
    else:
        timer_block = f"🔒 **Завершено:** <t:{data['end_time']}:f>"

    embed.add_field(
        name="⏰ Таймер",
        value=timer_block,
        inline=False
    )

    embed.set_footer(text=f"Инициатор: {data.get('initiator_name', 'АСУ')} • ID: {poll_id}")
    return embed


async def sync_poll_message_ui(data: Dict[str, Any]) -> None:
    """Refreshes the Discord message embed and button states for a poll."""
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


class ACIReportModal(discord.ui.Modal, title="Запрос на проверку Стандарта ACI"):
    """Modal for submitting a single ACI violation report per user per poll."""
    def __init__(self, poll_id: str, poll_url: str):
        super().__init__()
        self.poll_id = poll_id
        self.poll_url = poll_url

    violation_details = discord.ui.TextInput(
        label="Какой пункт ACI или Тир нарушен?",
        style=discord.TextStyle.paragraph,
        placeholder="Укажите, почему текст не соответствует Section I-III ACI (например: выбран Тир Low вместо High)...",
        required=True,
        max_length=800
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("❌ Доступно только на сервере.", ephemeral=True)

        async with file_lock:
            data = get_poll_data(self.poll_id)
            if not data or data.get("status") in ("finished", "rejected_final", "rejected_frozen"):
                return await interaction.response.send_message("❌ Голосование уже завершено или заморожено.", ephemeral=True)

            if data.get("immune_to_aci") or data.get("aci_shield"):
                return await interaction.response.send_message("🛡️ Это голосование защищено от жалоб ACI.", ephemeral=True)

            reports = data.setdefault("aci_reports", [])
            already_reported = any(
                (r.get("user_id") == interaction.user.id if isinstance(r, dict) else r == interaction.user.id)
                for r in reports
            )
            if already_reported:
                return await interaction.response.send_message(
                    "⚠️ Вы уже отправляли жалобу ACI на это голосование. Повторная отправка не допускается.",
                    ephemeral=True
                )

            reports.append({
                "user_id": interaction.user.id,
                "username": interaction.user.name,
                "details": self.violation_details.value,
                "timestamp": int(time.time())
            })
            reports_count = len(reports)
            req_confirms, eligible_total = resolve_poll_confirm_thresholds(data)
            confirms_count = len(data.get("aci_confirmations", []))
            save_poll_data(self.poll_id, data)

        await sync_poll_message_ui(data)

        report_embed = discord.Embed(
            title="🚩 Запрос на валидацию голосования (Стандарт ACI)",
            description=(
                f"**Голосование:** [Перейти к сообщению]({self.poll_url}) (ID: `{self.poll_id}`)\n"
                f"**Заявитель:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                f"**Всего жалоб на опросе:** `{reports_count} шт.`\n\n"
                f"**Суть несоответствия ACI:**\n> {self.violation_details.value}\n\n"
                f"🔧 **Действия Управленца (п. 3.3):**\n"
                f"• Подтвердить и выдать щит (`{confirms_count}/{req_confirms}`, 1/5 от `{eligible_total}`): `/rconfirm poll_id:{self.poll_id}`\n"
                f"• Отклонить и заморозить (`1 чел.`): `/rreject poll_id:{self.poll_id}`"
            ),
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow()
        )
        await log_moderation_action(interaction.guild, report_embed)
        await interaction.response.send_message(
            f"✅ Ваш запрос на проверку соответствия Стандарту ACI передан Управленцам (всего жалоб: `{reports_count} шт.`).",
            ephemeral=True
        )


class PollVotingView(discord.ui.View):
    """Persistent Likert scale voting buttons (+2 to -2), vote lookup, and ACI report trigger."""
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
            return await interaction.response.send_message("❌ Голосование доступно только на сервере.", ephemeral=True)

        is_allowed, err_msg = check_full_member_status(interaction.user)
        if not is_allowed:
            return await interaction.response.send_message(err_msg, ephemeral=True)

        poll_id = str(interaction.message.id)

        async with file_lock:
            data = get_poll_data(poll_id)
            if not data:
                return await interaction.response.send_message("❌ Данные этого голосования не найдены или оно в архиве.", ephemeral=True)

            status = data.get("status")
            if status in ("finished", "rejected_final"):
                return await interaction.response.send_message("❌ Это голосование уже окончательно закрыто.", ephemeral=True)
            if status == "rejected_frozen":
                return await interaction.response.send_message("⏸️ Голосование временно заморожено по ACI. Ожидается перекрёстный контроль.", ephemeral=True)
            if status == "cooldown":
                return await interaction.response.send_message(
                    f"🧊 Голосование находится на стадии охлаждения! Отдать голос можно будет <t:{data['start_time']}:R>.",
                    ephemeral=True
                )

            votes = data.setdefault("votes", {})
            user_id_str = str(interaction.user.id)
            votes[user_id_str] = {
                "weight": weight,
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
            f"🔒 Для защиты от конформизма другие участники не видят выбранный вами вариант. Вы можете изменить решение до окончания таймера.",
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
        await self._handle_vote(interaction, 0, "⏺️ Воздержусь/Нейтрально (0)")

    @discord.ui.button(label="🔽", style=discord.ButtonStyle.danger, custom_id="asu_vote_minus1", row=0)
    async def vote_minus_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, -1, "🔽 Не согласен (-1)")

    @discord.ui.button(label="⏬", style=discord.ButtonStyle.danger, custom_id="asu_vote_minus2", row=0)
    async def vote_minus_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, -2, "⏬ Полностью не согласен (-2)")

    @discord.ui.button(label="🔍 Мой голос", style=discord.ButtonStyle.primary, custom_id="asu_vote_check", row=1)
    async def check_my_vote(self, interaction: discord.Interaction, button: discord.ui.Button):
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

        reports = data.get("aci_reports", [])
        if any((r.get("user_id") == interaction.user.id if isinstance(r, dict) else r == interaction.user.id) for r in reports):
            return await interaction.response.send_message(
                "⚠️ Вы уже отправляли жалобу ACI на это голосование. Повторная отправка не допускается.",
                ephemeral=True
            )

        await interaction.response.send_modal(ACIReportModal(poll_id, interaction.message.jump_url))


# =====================================================================
# 6. CANDIDATE QUESTIONNAIRE MODALS (/rpollelection)
# =====================================================================

class NextQuestionnairePartView(discord.ui.View):
    """Chains multi-step modals when an election template has >5 questions."""
    def __init__(self, candidate_id: int, template_data: Dict[str, Any], collected_answers: List[Tuple[str, str]], next_index: int):
        super().__init__(timeout=900)
        self.candidate_id = candidate_id
        self.template_data = template_data
        self.collected_answers = collected_answers
        self.next_index = next_index

    @discord.ui.button(label="➡️ Перейти ко 2-й части анкеты", style=discord.ButtonStyle.primary)
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
    """Collects up to 5 questionnaire answers per modal and launches template election poll."""
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
                f"✅ **Часть 1 сохранена!** Осталось ответить ещё на **{remaining}** вопр.\n"
                f"Нажмите кнопку ниже, чтобы открыть следующее окно анкеты:",
                view=view,
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        qa_formatted = "\n\n".join(
            f"**{q}**\nОтвет: {a}" for q, a in self.collected_answers
        )
        header_desc = self.template_data.get("description", "Выдвижение кандидата по локальной анкете (п. 7.1 Регламента).")
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
            destination_channel_id=ELECTIONS_CHANNEL_ID
        )

        if poll_msg:
            await interaction.followup.send(
                f"🎉 Ваша анкета успешно опубликована! Запущено голосование по шкале Лайкерта: {poll_msg.jump_url}",
                ephemeral=True
            )
        else:
            await interaction.followup.send("❌ Не удалось опубликовать анкету. Проверьте настройки канала.", ephemeral=True)


# =====================================================================
# 7. POLL CREATION & AUTOMATED EXECUTION ENGINE
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
    source_channel_id: int = 0,
    target_user_id: Optional[int] = None,
    target_role_id: Optional[int] = None,
    target_role_name: Optional[str] = None,
    immune_to_aci: bool = False,
    protect_from_02: bool = False,
    destination_channel_id: Optional[int] = None,
    requested_poll_channel: Optional[discord.TextChannel] = None
) -> Optional[discord.Message]:
    """Creates, sends, and persists a new poll; attaches frozen .txt if proposal exceeds embed limits."""
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
    confirm_req, confirm_eligible_total = get_rconfirm_quorum_stats(guild)

    now = int(time.time())
    start_time = now + cooldown_sec
    end_time = start_time + duration_sec
    status = "cooldown" if cooldown_sec > 0 else "active"

    if not protect_from_02:
        protect_from_02 = is_manager_related_poll(
            guild=guild,
            category=category,
            content=quote_content,
            target_user_id=target_user_id,
            target_role_id=target_role_id
        )

    poll_data: Dict[str, Any] = {
        "poll_message_id": 0,
        "guild_id": guild.id,
        "channel_id": channel.id,
        "poll_type": poll_type,
        "initiator_id": initiator.id,
        "initiator_name": initiator.name,
        "target_message_id": target_message_id,
        "source_channel_id": source_channel_id,
        "target_author_name": target_author_name,
        "quote_content": quote_content,
        "category": category,
        "tier": tier_name,
        "win_pct": win_pct,
        "quorum_req": quorum_req,
        "confirm_req": confirm_req,
        "confirm_eligible_total": confirm_eligible_total,
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
        "aci_confirmations": [],
        "aci_reports": [],
        "votes": {}
    }

    initial_embed = build_poll_embed(poll_data)
    view = PollVotingView(poll_data)

    # Attach immutable text snapshot if proposal exceeds embed display threshold (Rule 6.4.1)
    files_to_attach = []
    if len(quote_content) > 2200:
        frozen_bytes = quote_content.encode("utf-8")
        files_to_attach.append(discord.File(io.BytesIO(frozen_bytes), filename=f"frozen_proposal_{now}.txt"))

    try:
        if files_to_attach:
            poll_msg = await channel.send(embed=initial_embed, view=view, files=files_to_attach)
        else:
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
    """Executes automated role/isolation changes upon poll victory and logs to MOD_LOG_CHANNEL_ID."""
    poll_type = data.get("poll_type", "standard")
    target_user_id = data.get("target_user_id")

    if poll_type == "standard" or not target_user_id:
        return "ℹ️ Общественное решение принято и вступило в законную силу (подлежит исполнению по п. 3.4 / 4.4)."

    member = guild.get_member(target_user_id)
    if not member:
        try:
            member = await guild.fetch_member(target_user_id)
        except Exception:
            if poll_type == "appeal":
                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute("DELETE FROM isolations WHERE user_id = ?", (target_user_id,))
                    await db.commit()
            return "⚠️ Автоматическое исполнение не удалось: целевой пользователь покинул сервер."

    try:
        if poll_type == "impeach":
            roles_to_remove = [r for r in member.roles if r.id in MANAGER_ROLES]
            if not roles_to_remove:
                return "ℹ️ На момент завершения голосования у пользователя уже отсутствовали роли Управленца."
            await member.remove_roles(*roles_to_remove, reason=f"АСУ: Импичмент по итогам голосования #{data['poll_message_id']} (п. 6.8)")
            removed_names = ", ".join(r.name for r in roles_to_remove)
            note = f"⚡ **АСУ исполнено автоматически (п. 6.8):** С пользователя {member.mention} сняты все управленческие роли ({removed_names})."
            log_emb = discord.Embed(
                title="⚡ Автоисполнение АСУ: Снятие Управленца (п. 6.8)",
                description=f"**Голосование:** `{data['poll_message_id']}`\n**Пользователь:** {member.mention}\n**Снятые роли:** {removed_names}",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow()
            )
            await log_moderation_action(guild, log_emb)
            return note

        elif poll_type in ("direct_election", "template_election"):
            role_id = data.get("target_role_id")
            role = guild.get_role(int(role_id)) if role_id else None
            if not role:
                return "⚠️ Автоматическое исполнение не удалось: целевая роль не найдена на сервере."
            await member.add_roles(role, reason=f"АСУ: Избрание по итогам голосования #{data['poll_message_id']} (Раздел 7)")
            note = f"🎖️ **АСУ исполнено автоматически (п. 6.6, Раздел 7):** Пользователю {member.mention} присвоена должность {role.mention}!"
            log_emb = discord.Embed(
                title="🎖️ Автоисполнение АСУ: Избрание Управленца (Раздел 7)",
                description=f"**Голосование:** `{data['poll_message_id']}`\n**Избран:** {member.mention}\n**Должность:** {role.mention}",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            await log_moderation_action(guild, log_emb)
            return note

        elif poll_type == "appeal":
            await restore_user_from_isolation(
                guild,
                member,
                reason=f"АСУ: Автоматическое снятие Изоляции по итогам Публичной апелляции #{data['poll_message_id']} (п. 5.1)"
            )
            note = f"🕊️ **АСУ исполнено автоматически (п. 5.1):** С пользователя {member.mention} снята Изоляция и возвращены все изъятые при изоляции роли и права."
            log_emb = discord.Embed(
                title="🕊️ Автоисполнение АСУ: Удовлетворена Публичная апелляция (п. 5.1)",
                description=f"**Голосование:** `{data['poll_message_id']}`\n**Восстановлен участник:** {member.mention}",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            await log_moderation_action(guild, log_emb)
            return note

    except discord.Forbidden:
        return "⚠️ Ошибка прав Discord: роль бота находится ниже целевой роли в иерархии сервера."
    except Exception as e:
        return f"⚠️ Техническая ошибка при автоисполнении АСУ: `{e}`"

    return "ℹ️ Решение зафиксировано."


async def restore_user_from_isolation(guild: discord.Guild, member: discord.Member, reason: str) -> None:
    """Removes Isolation role, restores saved roles from SQLite, and clears timeouts/mutes."""
    iso_role = guild.get_role(ISOLATION_ROLE_ID) if ISOLATION_ROLE_ID > 0 else None
    mem_role = guild.get_role(MEMBER_ROLE_ID) if MEMBER_ROLE_ID > 0 else None

    roles_to_restore: List[discord.Role] = []
    if mem_role:
        roles_to_restore.append(mem_role)

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT saved_roles FROM isolations WHERE user_id = ?", (member.id,)) as cursor:
            row = await cursor.fetchone()
        if row and row[0]:
            try:
                for rid in json.loads(row[0]):
                    r_obj = guild.get_role(int(rid))
                    if r_obj and r_obj not in roles_to_restore:
                        roles_to_restore.append(r_obj)
            except Exception as e:
                print(f"[Restore Saved Roles Parse Error]: {e}")

        await db.execute("DELETE FROM isolations WHERE user_id = ?", (member.id,))
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
# 8. BACKGROUND LOOPS (POLL LIFECYCLE, ISOLATIONS, DASHBOARD)
# =====================================================================

@tasks.loop(seconds=10)
async def poll_checker() -> None:
    """Processes poll state transitions in chronological order (Rule 6.9.1)."""
    current_time = int(time.time())
    polls_to_finish: List[Tuple[int, str]] = []

    for poll_id, data in list(POLLS_CACHE.items()):
        try:
            status = data.get("status", "active")
            if status in ("finished", "rejected_final"):
                async with file_lock:
                    archive_poll_data(poll_id, data)
                continue

            if status == "cooldown" and current_time >= data["start_time"]:
                await activate_poll(poll_id)
                continue

            if status == "rejected_frozen":
                deadline = data.get("appeal_deadline", data.get("end_time", 0))
                if current_time >= deadline:
                    await finalize_rejected_poll(poll_id)
                continue

            if status == "active" and current_time >= data["end_time"]:
                polls_to_finish.append((data["end_time"], poll_id))
        except Exception as e:
            print(f"[Poll Checker Error on {poll_id}]: {e}")

    polls_to_finish.sort(key=lambda x: x[0])
    for _, poll_id in polls_to_finish:
        await process_poll_results(poll_id)


async def activate_poll(poll_id: str) -> None:
    """Transitions poll from cooldown to active voting."""
    async with file_lock:
        data = get_poll_data(poll_id)
        if not data:
            return
        data["status"] = "active"
        save_poll_data(poll_id, data)

    await sync_poll_message_ui(data)


async def finalize_rejected_poll(poll_id: str) -> None:
    """Permanently rejects and archives a frozen poll once the cross-control window expires."""
    async with file_lock:
        data = get_poll_data(poll_id)
        if not data:
            return
        data["status"] = "rejected_final"
        save_poll_data(poll_id, data)

    await sync_poll_message_ui(data)


async def process_poll_results(poll_id: str) -> None:
    """Tallies Likert weights, executes automated actions on victory, and archives the poll."""
    data = get_poll_data(poll_id)
    if not data:
        return

    channel = bot.get_channel(data["channel_id"])
    if not channel:
        try:
            channel = await bot.fetch_channel(data["channel_id"])
        except Exception:
            return

    guild = bot.get_guild(data.get("guild_id", GUILD_ID)) or getattr(channel, "guild", None)

    valid_votes = list(data.get("votes", {}).values())
    total_voters = len(valid_votes)
    reports_count = len(data.get("aci_reports", []))

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
        verdict_text = f"**Инициатива преодолела порог одобрения!**\nПоддержка по весу: **`{win_pct:.1f}%`** (требовалось **`>{req_pct}%`**)."
        if guild:
            execution_note = await execute_automated_decision(data, guild)
    else:
        result_title = "❌ РЕШЕНИЕ ОТКЛОНЕНО"
        result_color = discord.Color.red()
        verdict_text = f"**Инициатива не набрала необходимой поддержки.**\nПоддержка по весу: **`{win_pct:.1f}%`** (требовалось **`>{req_pct}%`**)."

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

    result_desc = (
        f"### Конец голосования\n"
        f"{formatted_quote}\n"
    )

    result_embed = discord.Embed(
        title=f"ИТОГИ ГОЛОСОВАНИЯ: {result_title}",
        description=result_desc,
        color=result_color,
        timestamp=discord.utils.utcnow()
    )

    result_embed.add_field(
        name="📂 Данные",
        value=(
            f"┏ **Категория:** `{data['category']}` (Тир: **{data['tier']}**)\n"
            f"┃ **Автор текста:** **{data['target_author_name']}**\n"
            f"┃ **Инициатор:** <@{data['initiator_id']}>\n"
            f"┗ ⚠️Жалобы: `{reports_count} шт.`"
        ),
        inline=True
    )

    result_embed.add_field(
        name="📊 Статистика весов",
        value=(
            f"┏ **Явка:** `{total_voters} / {req_quorum} чел.`\n"
            f"┃ **Вес «ЗА»:** `+{positive_weight}` | **«ПРОТИВ»:** `-{negative_weight}`\n"
            f"┗ **Воздержалось (`0`):** `{neutral_count} чел.`"
        ),
        inline=True
    )

    result_embed.add_field(
        name="⚖️ ВЕРДИКТ:",
        value=verdict_text,
        inline=False
    )

    if execution_note:
        result_embed.add_field(
            name="⚙️ Автоматическое исполнение (п. 6.6)",
            value=execution_note,
            inline=False
        )

    result_embed.set_footer(text=f"ID голосования: {data['poll_message_id']}")

    try:
        poll_message = await channel.fetch_message(int(data["poll_message_id"]))
        await poll_message.edit(embed=build_poll_embed(data), view=PollVotingView(data))
        await channel.send(f"<@{data['initiator_id']}>, подведены итоги голосования!", embed=result_embed, reference=poll_message)
    except Exception:
        await channel.send(f"<@{data['initiator_id']}>, подведены итоги голосования `{data['poll_message_id']}`!", embed=result_embed)


@tasks.loop(seconds=10)
async def system_maintenance_loop() -> None:
    """Releases expired isolations, renews 28d Discord timeouts, and executes delayed /relection roles."""
    await flush_message_logs()
    now = int(time.time())
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT user_id, guild_id FROM isolations WHERE end_time > 0 AND end_time <= ?", (now,)) as cursor:
                expired_isolations = await cursor.fetchall()

            for user_id, guild_id in expired_isolations:
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
                        log_emb = discord.Embed(
                            title="🕊️ Автоматическое окончание срока Изоляции (п. 5.2)",
                            description=f"**Участник:** {member.mention} (`{member.id}`)\n**Основание:** Истёк установленный таймер Изоляции.",
                            color=discord.Color.green(),
                            timestamp=discord.utils.utcnow()
                        )
                        await log_moderation_action(guild, log_emb)
                    else:
                        await db.execute("DELETE FROM isolations WHERE user_id = ?", (user_id,))
                        await db.commit()

            async with db.execute("SELECT user_id, guild_id, end_time, reason FROM isolations WHERE end_time = 0 OR end_time > ?", (now,)) as cursor:
                active_isolations = await cursor.fetchall()

            for user_id, guild_id, end_time, iso_reason in active_isolations:
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
                            await member.timeout(until_dt, reason=f"АСУ: Продление мута Изоляции ({iso_reason})")
                        except Exception:
                            pass

            async with db.execute(
                "SELECT id, guild_id, target_user_id, role_id, initiator_id, reason FROM pending_appointments WHERE executed = 0 AND execute_at <= ?",
                (now,)
            ) as cursor:
                due_appointments = await cursor.fetchall()

            for app_id, guild_id, target_uid, role_id, init_uid, reason in due_appointments:
                await db.execute("UPDATE pending_appointments SET executed = 1 WHERE id = ?", (app_id,))
                await db.commit()

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
    """Updates the persistent /rstatistics message at the configured interval."""
    global DASHBOARD_CACHE
    if DASHBOARD_CACHE is None:
        if not os.path.exists(DASHBOARD_STATE_FILE):
            return
        try:
            with open(DASHBOARD_STATE_FILE, "r", encoding="utf-8") as f:
                DASHBOARD_CACHE = json.load(f)
        except Exception:
            return

    now = int(time.time())
    interval = max(15, int(DASHBOARD_CACHE.get("interval", 60)))
    if now - DASHBOARD_CACHE.get("last_update", 0) < interval:
        return

    guild = bot.get_guild(DASHBOARD_CACHE.get("guild_id", GUILD_ID))
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
    """Aggregates active and archived polls, quorum thresholds, and isolation counts for dashboard."""
    active_count, median_threshold, _ = await get_active_users_stats(guild.id)

    combined_map: Dict[str, Dict[str, Any]] = {}
    combined_map.update(ARCHIVE_CACHE)
    combined_map.update(POLLS_CACHE)
    all_polls = list(combined_map.values())

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
            "active": "🟢 **[В ПРОЦЕССЕ]**",
            "cooldown": "🧊 **[ОХЛАЖДЕНИЕ]**",
            "rejected_frozen": "⏸️ **[ЗАМОРОЖЕНО ACI]**",
            "rejected_final": "❌ **[ОТКЛОНЕНО ACI]**",
            "finished": "🏆 **[ПРИНЯТО]**" if p.get("is_victory") else "🛑 **[НЕ ПРОШЛО]**"
        }.get(st, "📊")
        link = f"https://discord.com/channels/{guild.id}/{p['channel_id']}/{p['poll_message_id']}"
        short_q = p.get("quote_content", "").replace("\n", " ")[:48]
        voters_n = len(p.get("votes", {}))
        reports_n = len(p.get("aci_reports", []))
        recent_lines.append(
            f"┏ {ico} [`#{p['poll_message_id']}`]({link}) — **`{p['category']} / Тир {p['tier']}`** (Явка: **`{voters_n}/{p['quorum_req']}`** | ⚠️Жалобы: `{reports_n} шт.`)\n"
            f"┗ *{short_q}...*"
        )

    recent_block = "\n\n".join(recent_lines) if recent_lines else "*Голосования ещё не проводились.*"

    header_desc = (
        f"🔄 **Цикл обновления каждые:** `{interval} сек.` (Следующее: <t:{int(time.time()) + interval}:R>)"
    )

    embed = discord.Embed(
        title="📈 Мониторинг",
        description=header_desc,
        color=discord.Color.teal(),
        timestamp=discord.utils.utcnow()
    )

    embed.add_field(
        name="👥 База активности за 30 дней",
        value=(
            f"┏ **Активных участников:** `{active_count} чел.`\n"
            f"┃ **Порог медианы:** `{median_threshold:g} сообщ.`\n"
            f"┗ **В Изоляции (п. 3.5):** `{isolated_count} чел.`"
        ),
        inline=True
    )

    embed.add_field(
        name="🎯 Кворум по тирам",
        value=(
            f"┏ **I. Low (15%):** `{max(1, math.ceil(active_count * 0.15))} чел.`\n"
            f"┃ **II. Mid (30%):** `{max(1, math.ceil(active_count * 0.30))} чел.`\n"
            f"┃ **III. High (50%):** `{max(1, math.ceil(active_count * 0.50))} чел.`\n"
            f"┗ **IV. Crit (75%):** `{max(1, math.ceil(active_count * 0.75))} чел.`"
        ),
        inline=True
    )

    embed.add_field(
        name="📊 Сводка голосований",
        value=(
            f"┏ 🟢 **Активных сейчас:** `{active_polls_count}` | 🧊 **На охлаждении:** `{cooldown_polls_count}` | ⏸️ **На проверке ACI:** `{frozen_polls_count}`\n"
            f"┗ 🔒 **Завершено всего:** `{finished_polls_count}` "
            f"(🏆 **Принято:** `{victories}` [`{win_rate:.1f}%`] • 🛑 **Не прошло:** `{defeats}` • ❌ **Снято по ACI:** `{rejected_polls_count}`)"
        ),
        inline=False
    )

    embed.add_field(
        name="🗂️ Последние голосования",
        value=recent_block[:1024],
        inline=False
    )

    embed.set_footer(text="Автоматизированная Система Управления • RedAdapter")
    return embed


# =====================================================================
# 9. LIFECYCLE EVENTS & ACTIVITY LOGGING
# =====================================================================

async def setup_hook() -> None:
    ensure_sample_election_template()
    load_polls_into_cache()
    await setup_db()
    bot.add_view(PollVotingView())
    await bot.tree.sync()

bot.setup_hook = setup_hook


@bot.event
async def on_ready() -> None:
    print(f"[АСУ RedAdapter] Запущен как: {bot.user} (ID: {bot.user.id})")
    for task_loop in (poll_checker, system_maintenance_loop, live_dashboard_loop, db_cleanup):
        if not task_loop.is_running():
            task_loop.start()


@bot.event
async def on_member_join(member: discord.Member) -> None:
    """Re-applies Isolation role and timeout if an isolated user rejoins the guild."""
    if member.bot:
        return
    now = int(time.time())
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT end_time, reason FROM isolations WHERE user_id = ? AND guild_id = ?",
            (member.id, member.guild.id)
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        return

    end_time, iso_reason = int(row[0]), str(row[1])
    if end_time > 0 and end_time <= now:
        return

    iso_role = member.guild.get_role(ISOLATION_ROLE_ID) if ISOLATION_ROLE_ID > 0 else None
    mem_role = member.guild.get_role(MEMBER_ROLE_ID) if MEMBER_ROLE_ID > 0 else None

    try:
        if mem_role and mem_role in member.roles:
            await member.remove_roles(mem_role, reason="АСУ: Возврат режима Изоляции при перезаходе (п. 3.5)")
        if iso_role:
            await member.add_roles(iso_role, reason="АСУ: Возврат режима Изоляции при перезаходе (п. 3.5)")
    except Exception:
        pass

    rem_sec = (end_time - now) if end_time > 0 else DISCORD_MAX_TIMEOUT_SECONDS
    target_timeout_sec = min(max(60, rem_sec), DISCORD_MAX_TIMEOUT_SECONDS)
    try:
        until_dt = discord.utils.utcnow() + datetime.timedelta(seconds=target_timeout_sec)
        await member.timeout(until_dt, reason=f"АСУ: Восстановление мута Изоляции при перезаходе ({iso_reason})")
    except Exception:
        pass

    log_emb = discord.Embed(
        title="🔒 Восстановлена Изоляция при перезаходе на сервер",
        description=(
            f"**Ограниченный участник:** {member.mention} (`{member.id}`)\n"
            f"**Причина активной Изоляции:** {iso_reason}"
        ),
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    await log_moderation_action(member.guild, log_emb)


@bot.event
async def on_message(message: discord.Message) -> None:
    """Logs message timestamps (without content) for 30-day quorum calculation (Rule 6.7)."""
    if message.author.bot or not message.guild:
        await bot.process_commands(message)
        return

    should_flush = False
    async with buffer_lock:
        MESSAGE_LOG_BUFFER.append((message.author.id, message.guild.id, time.time()))
        if len(MESSAGE_LOG_BUFFER) >= 25:
            should_flush = True

    if should_flush:
        await flush_message_logs()

    await bot.process_commands(message)


@tasks.loop(hours=1)
async def db_cleanup() -> None:
    """Purges message logs older than 30 days and command logs older than 24 hours."""
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


# =====================================================================
# 10. SLASH COMMANDS: POLLS & ELECTIONS
# =====================================================================

rpoll_group = app_commands.Group(name="rpoll", description="Инициация голосований по архитектуре SVA V. RH")


@rpoll_group.command(name="standard", description="Создать стандартное голосование по сообщению и категории")
@app_commands.describe(
    message_id="ID сообщения с текстом инициативы (соответствующим Стандарту ACI)",
    category="Категория вопроса (автоматически определяет Тир, кворум и таймеры)",
    poll_channel="Канал для публикации голосования (из списка POLLS_CHANNEL_IDS)"
)
@app_commands.choices(category=[app_commands.Choice(name=f"{c} (Тир {t})", value=c) for c, t in CATEGORIES.items()])
async def rpoll_standard(
    interaction: discord.Interaction,
    message_id: str,
    category: app_commands.Choice[str],
    poll_channel: Optional[discord.TextChannel] = None
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Команда доступна только на сервере.", ephemeral=True)

    is_allowed, err_msg = check_full_member_status(interaction.user)
    if not is_allowed:
        return await interaction.response.send_message(err_msg, ephemeral=True)

    _, chan_err = resolve_poll_destination_channel(interaction.guild, interaction.channel, requested_poll_channel=poll_channel)
    if chan_err:
        return await interaction.response.send_message(chan_err, ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    if not message_id.isdigit():
        return await interaction.followup.send("⚠️ ID сообщения должен состоять только из цифр.")

    target_message = await fetch_message_across_guild(interaction.guild, int(message_id), preferred_channel=interaction.channel)
    if not target_message:
        return await interaction.followup.send("⚠️ Сообщение с указанным ID не найдено на сервере.")

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
        source_channel_id=target_message.channel.id,
        requested_poll_channel=poll_channel
    )

    if poll_msg:
        await interaction.followup.send(f"✅ Голосование успешно запущено: {poll_msg.jump_url}", ephemeral=True)
    else:
        await interaction.followup.send("❌ Ошибка при публикации голосования.", ephemeral=True)


@rpoll_group.command(name="impeach", description="Спец-голосование за снятие Управленца в обход ACI (п. 6.8 Регламента)")
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
        return await interaction.response.send_message("❌ Команда доступна только на сервере.", ephemeral=True)

    is_allowed, err_msg = check_full_member_status(interaction.user)
    if not is_allowed:
        return await interaction.response.send_message(err_msg, ephemeral=True)

    _, chan_err = resolve_poll_destination_channel(interaction.guild, interaction.channel, requested_poll_channel=poll_channel)
    if chan_err:
        return await interaction.response.send_message(chan_err, ephemeral=True)

    user_manager_roles = [r for r in user.roles if r.id in MANAGER_ROLES]
    if not user_manager_roles:
        return await interaction.response.send_message(
            f"❌ **Техническая реализация невозможна:** у пользователя {user.mention} отсутствуют роли Управленца из системного реестра АСУ (`MANAGER_ROLES`).",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)
    role_names = ", ".join(r.name for r in user_manager_roles)
    t_cfg = TIERS["High"]

    impeach_template_text = (
        f"Инициирована штатная процедура досрочного прекращения полномочий Управленца {user.mention} (`{user.id}`).\n"
        f"Текущие управленческие роли: **{role_names}**.\n"
        f"При достижении порога победы III Тира АСУ автоматически снимет все указанные управленческие роли."
    )

    poll_msg = await launch_poll_instance(
        guild=interaction.guild,
        fallback_channel=interaction.channel,
        initiator=interaction.user,
        target_author_name="Системный протокол АСУ (п. 6.8)",
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
    description="Прямые общественные голосования в обход локальных процедур (п. 7.1.1)"
)


@rpoll_global_group.command(name="election", description="Прямое выдвижение кандидата на пост Управленца по тексту (п. 7.1.1)")
@app_commands.describe(
    user="Кандидат на должность Управленца",
    role="Управленческая роль (из реестра АСУ)",
    message_id="ID сообщения с программой/обоснованием выдвижения (проверяется по ACI)",
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
        return await interaction.response.send_message("❌ Команда доступна только на сервере.", ephemeral=True)

    is_allowed, err_msg = check_full_member_status(interaction.user)
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

    if not message_id.isdigit():
        return await interaction.response.send_message("⚠️ ID сообщения должен состоять только из цифр.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    target_message = await fetch_message_across_guild(interaction.guild, int(message_id), preferred_channel=interaction.channel)
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
        source_channel_id=target_message.channel.id,
        target_user_id=user.id,
        target_role_id=role.id,
        target_role_name=MANAGER_ROLES[role.id],
        immune_to_aci=False,
        requested_poll_channel=poll_channel
    )

    if poll_msg:
        await interaction.followup.send(f"🗳️ Прямое голосование за избрание {user.mention} запущено: {poll_msg.jump_url}", ephemeral=True)
    else:
        await interaction.followup.send("❌ Ошибка при создании голосования.", ephemeral=True)


rpoll_group.add_command(rpoll_global_group)
bot.tree.add_command(rpoll_group)


async def fetch_message_across_guild(
    guild: discord.Guild,
    msg_id: int,
    preferred_channel: Optional[discord.abc.Messageable] = None
) -> Optional[discord.Message]:
    """Searches cache, current channel, and all readable guild channels for a message ID."""
    cached = discord.utils.get(bot.cached_messages, id=msg_id)
    if cached:
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


@bot.tree.command(name="rpollelection", description="Подать заявку и пройти анкетирование на пост Управленца (п. 7.1)")
@app_commands.describe(role="Роль должности, на которую вы выдвигаетесь")
async def rpollelection(interaction: discord.Interaction, role: discord.Role):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Команда доступна только на сервере.", ephemeral=True)

    is_allowed, err_msg = check_full_member_status(interaction.user)
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
            f"*(Подсказка для Администратора: раскомментируйте и настройте шаблон на базе `elections_jsons/example.json`)*",
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
# 11. SLASH COMMANDS: ACI VALIDATION (/rreject, /rconfirm)
# =====================================================================

@bot.tree.command(name="rreject", description="Отклонить и заморозить голосование по Стандарту ACI или п. 0.2 (Управленец)")
@app_commands.describe(
    poll_id="ID сообщения голосования",
    reason="Основание по Регламенту",
    comment="Подробное публичное обоснование причины (обязательно по п. 4.3 Регламента)"
)
@app_commands.choices(reason=[
    app_commands.Choice(name="Несоответствие Стандарту ACI (п. 6.2)", value="Несоответствие Стандарту ACI (п. 6.2)"),
    app_commands.Choice(name="Деструктивная инициатива / Саботаж (п. 0.2)", value="Деструктивная инициатива / Саботаж (п. 0.2)")
])
async def rreject(interaction: discord.Interaction, poll_id: str, reason: app_commands.Choice[str], comment: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Доступно только на сервере.", ephemeral=True)

    if not has_command_permission(interaction.user, "rreject"):
        return await interaction.response.send_message("❌ У вашей должности нет прав на выполнение `/rreject`.", ephemeral=True)

    now = int(time.time())
    async with file_lock:
        data = get_poll_data(poll_id)
        if not data:
            return await interaction.response.send_message("❌ Голосование с таким ID не найдено.", ephemeral=True)

        if data.get("immune_to_aci"):
            return await interaction.response.send_message(
                "⚡ **Отказ АСУ (п. 4.5, 6.8):** Специальное голосование за снятие Управленца обладает полным системным иммунитетом и не может быть отклонено!",
                ephemeral=True
            )

        if data.get("protect_from_02") and "0.2" in reason.value:
            return await interaction.response.send_message(
                "🛡️ **Отказ АСУ (п. 4.5):** Пресечение деструктивной инициативы (п. 0.2) не может применяться для закрытия Публичных апелляций Ограниченных участников и голосований, касающихся оценки действий, ограничения прав или снятия Управленцев!",
                ephemeral=True
            )

        if data.get("aci_shield"):
            return await interaction.response.send_message(
                "🛡️ **Отказ АСУ (п. 3.3.1):** Это голосование уже прошло проверку `/rconfirm` и имеет неснижаемый щит от отклонения.",
                ephemeral=True
            )

        if data.get("status") in ("finished", "rejected_final", "rejected_frozen"):
            return await interaction.response.send_message("⚠️ Голосование уже завершено или уже находится в заморозке.", ephemeral=True)

        if data["status"] == "cooldown":
            rem_cooldown = max(0, data["start_time"] - now)
            rem_duration = max(60, data["end_time"] - data["start_time"])
        else:
            rem_cooldown = 0
            rem_duration = max(60, data["end_time"] - now)

        req_confirms, eligible_total = get_rconfirm_quorum_stats(interaction.guild)
        data["status"] = "rejected_frozen"
        data["rejected_at"] = now
        data["remaining_cooldown"] = rem_cooldown
        data["remaining_duration"] = rem_duration
        data["appeal_deadline"] = max(data["end_time"], now + ACI_CROSS_CONTROL_WINDOW)
        data["reject_by_id"] = interaction.user.id
        data["reject_by_name"] = interaction.user.display_name
        data["reject_reason"] = f"{reason.value} — {comment}"
        data["aci_confirmations"] = []
        data["confirm_req"] = req_confirms
        data["confirm_eligible_total"] = eligible_total

        save_poll_data(poll_id, data)

    await sync_poll_message_ui(data)

    log_emb = discord.Embed(
        title="⏸️ Голосование отклонено и заморожено (/rreject)",
        description=(
            f"**ID голосования:** `{poll_id}`\n"
            f"**Управленец:** {interaction.user.mention}\n"
            f"**Основание:** {reason.value}\n"
            f"**Комментарий (п. 4.3):** {comment}\n"
            f"**Сохранённый остаток таймера:** `{math.ceil(rem_duration / 60)} мин.`\n"
            f"**Требуется для разморозки (`/rconfirm`):** `{req_confirms} чел.` (1/5 от `{eligible_total}` с доступом)\n"
            f"**Окно восстановления (`/rconfirm` по п. 3.3.1) до:** <t:{data['appeal_deadline']}:f>"
        ),
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    await log_moderation_action(interaction.guild, log_emb)
    await interaction.response.send_message(
        f"✅ Голосование `{poll_id}` отклонено по ACI и переведено в режим ожидания перекрёстного контроля "
        f"(для разморозки требуется `/rconfirm` от `{req_confirms}` из `{eligible_total}` уполномоченных Управленцев).",
        ephemeral=True
    )


@bot.tree.command(name="rconfirm", description="Подтвердить легитимность ACI (наложить щит или разморозить по п. 3.3.1)")
@app_commands.describe(
    poll_id="ID сообщения голосования",
    comment="Комментарий Управленца к проверке ACI (п. 4.3)"
)
async def rconfirm(interaction: discord.Interaction, poll_id: str, comment: str = "Соответствует Стандарту ACI (Legitimacy: 1)"):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Доступно только на сервере.", ephemeral=True)

    if not has_command_permission(interaction.user, "rconfirm"):
        return await interaction.response.send_message("❌ У вашей должности нет прав на выполнение `/rconfirm`.", ephemeral=True)

    now = int(time.time())
    async with file_lock:
        data = get_poll_data(poll_id)
        if not data:
            return await interaction.response.send_message("❌ Голосование не найдено.", ephemeral=True)

        status = data.get("status")
        if status in ("finished", "rejected_final"):
            return await interaction.response.send_message(
                "❌ Это голосование уже достигло окончательного завершения и не подлежит перезапуску.",
                ephemeral=True
            )

        if data.get("aci_shield"):
            return await interaction.response.send_message(
                "🛡️ Это голосование уже набрало необходимое число подтверждений `/rconfirm` и защищено щитом ACI.",
                ephemeral=True
            )

        # Disallow self-confirmation by poll initiator (Rule 4.1)
        if interaction.user.id == data.get("initiator_id"):
            return await interaction.response.send_message(
                "❌ **Конфликт интересов (п. 4.1):** Вы не можете самостоятельно подтверждать легитимность (`ACI`) собственного голосования. Проверку должен провести другой Управленец.",
                ephemeral=True
            )

        was_frozen = (status == "rejected_frozen")
        if was_frozen:
            # Require a different Manager to overturn a rejection (Rule 3.3.1)
            if interaction.user.id == data.get("reject_by_id"):
                return await interaction.response.send_message(
                    "❌ **Нарушение процедуры перекрёстного контроля (п. 3.3.1):** Вы не можете самостоятельно разморозить и защитить щитом голосование, отклонённое вами же через `/rreject`. Перепроверку должен выполнить **другой Управленец**.",
                    ephemeral=True
                )

            if now >= data.get("appeal_deadline", 0):
                data["status"] = "rejected_final"
                save_poll_data(poll_id, data)
                return await interaction.response.send_message("❌ Срок восстановления этого голосования истёк.", ephemeral=True)

        confirmations = data.setdefault("aci_confirmations", [])
        if any(c.get("user_id") == interaction.user.id for c in confirmations):
            return await interaction.response.send_message(
                "⚠️ Вы уже подтверждали легитимность этого голосования через `/rconfirm`.",
                ephemeral=True
            )

        confirmations.append({
            "user_id": interaction.user.id,
            "username": interaction.user.display_name,
            "comment": comment,
            "timestamp": now
        })

        req_confirms, eligible_total = get_rconfirm_quorum_stats(interaction.guild)
        data["confirm_req"] = req_confirms
        data["confirm_eligible_total"] = eligible_total
        confirms_count = len(confirmations)
        shield_activated = (confirms_count >= req_confirms)

        if shield_activated:
            if was_frozen:
                rem_cd = data.get("remaining_cooldown", 0)
                rem_dur = data.get("remaining_duration", 3600)
                data["start_time"] = now + rem_cd
                data["end_time"] = data["start_time"] + rem_dur
                data["status"] = "cooldown" if rem_cd > 0 else "active"

            data["aci_shield"] = True
            data["confirmed_by_id"] = interaction.user.id
            data["confirmed_by_name"] = ", ".join(c["username"] for c in confirmations)
            data["confirm_comment"] = comment

        save_poll_data(poll_id, data)

    await sync_poll_message_ui(data)

    if shield_activated:
        action_str = (
            f"Восстановлено из заморозки и защищено щитом ACI (`{confirms_count}/{req_confirms}`, 1/5 от `{eligible_total}`, п. 3.3.1)"
            if was_frozen else
            f"Подтверждено и защищено щитом ACI (`{confirms_count}/{req_confirms}`, 1/5 от `{eligible_total}`, п. 3.3)"
        )
        embed_color = discord.Color.green()
    else:
        remaining_needed = req_confirms - confirms_count
        action_str = (
            f"Голос `/rconfirm` учтён (`{confirms_count}/{req_confirms}`, 1/5 от `{eligible_total}` с доступом). "
            f"Для {'разморозки и ' if was_frozen else ''}активации щита ACI требуется ещё `{remaining_needed} чел.`"
        )
        embed_color = discord.Color.gold()

    log_emb = discord.Embed(
        title="🛡️ Валидация ACI подтверждена (/rconfirm)" if shield_activated else "⏳ Промежуточное подтверждение ACI (/rconfirm)",
        description=(
            f"**ID голосования:** `{poll_id}`\n"
            f"**Управленец:** {interaction.user.mention}\n"
            f"**Прогресс предохранителя (1/5):** `{confirms_count}/{req_confirms} чел.` (всего с доступом: `{eligible_total}`)\n"
            f"**Действие:** {action_str}\n"
            f"**Комментарий:** {comment}"
        ),
        color=embed_color,
        timestamp=discord.utils.utcnow()
    )
    await log_moderation_action(interaction.guild, log_emb)
    await interaction.response.send_message(f"🛡️ {action_str}.", ephemeral=True)


# =====================================================================
# 12. SLASH COMMANDS: ISOLATION & APPEALS (/risolation, /runisolation, /rappeal)
# =====================================================================

@bot.tree.command(name="risolation", description="Поместить участника в Изоляцию с мутом (п. 3.5 Регламента, лимит: 3 в 24ч)")
@app_commands.describe(
    user="Участник, переводимый в статус Ограниченного участника",
    reason="Публичное обоснование причины (конкретный пункт Регламента или описание ситуации, п. 4.3)",
    duration="Срок в формате Ns, Nh, Nd, Nm, Ny (пример: 30s, 12h, 7d, 1m, 1y) или 0 — до апелляции",
    is_twink="Доказанный твинк-аккаунт? (Да = аннулировать его голоса в активных опросах по п. 1.2 и 2.6)"
)
async def risolation(
    interaction: discord.Interaction,
    user: discord.Member,
    reason: str,
    duration: str = "0",
    is_twink: bool = False
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Доступно только на сервере.", ephemeral=True)

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
            "⚠️ **Неверный формат времени!** Используйте:\n"
            "• `Ns` — секунды (например `45s`)\n"
            "• `Nh` — часы (например `12h`)\n"
            "• `Nd` — дни (например `7d`)\n"
            "• `Nm` — месяцы (например `1m`)\n"
            "• `Ny` — годы (например `1y`)\n"
            "• Можно комбинировать (`1d 12h`) или указать `0` для бессрочной изоляции до апелляции.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    iso_role = interaction.guild.get_role(ISOLATION_ROLE_ID) if ISOLATION_ROLE_ID > 0 else None
    mem_role = interaction.guild.get_role(MEMBER_ROLE_ID) if MEMBER_ROLE_ID > 0 else None
    if not iso_role:
        return await interaction.followup.send(f"❌ На сервере не настроена или не найдена роль Изоляции (`ISOLATION_ROLE_ID = {ISOLATION_ROLE_ID}`).")

    # Suspend Manager and Full Member roles during Isolation (Rules 3.5, 7.3)
    saved_manager_roles = [r.id for r in user.roles if r.id in MANAGER_ROLES]
    roles_to_remove = [r for r in user.roles if r.id in MANAGER_ROLES]
    if mem_role and mem_role in user.roles and mem_role not in roles_to_remove:
        roles_to_remove.append(mem_role)

    all_removed_role_ids = [r.id for r in roles_to_remove]

    try:
        if roles_to_remove:
            await user.remove_roles(*roles_to_remove, reason=f"АСУ Изоляция (п. 3.5): {reason}")
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

    # Purge votes from active polls and sync UI if isolated account is a confirmed twink (Rules 1.2, 2.6)
    purged_polls = 0
    affected_polls: List[Dict[str, Any]] = []
    if is_twink:
        uid_str = str(user.id)
        async with file_lock:
            for poll_id, pdata in list(POLLS_CACHE.items()):
                if pdata.get("status") in ("active", "cooldown", "rejected_frozen") and uid_str in pdata.get("votes", {}):
                    del pdata["votes"][uid_str]
                    purged_polls += 1
                    save_poll_data(poll_id, pdata)
                    affected_polls.append(pdata)

    for pdata in affected_polls:
        await sync_poll_message_ui(pdata)

    human_dur = format_duration_human(duration_seconds)
    duration_str = f"До <t:{end_time}:f> (<t:{end_time}:R>, `{human_dur}`)" if end_time > 0 else human_dur
    twink_str = f"Да (аннулировано голосов: {purged_polls})" if is_twink else "Нет (голоса сохранены по п. 1.2)"

    log_emb = discord.Embed(
        title="🔒 Применена мера Изоляции и Мут (п. 3.5 Регламента)",
        description=(
            f"**Ограниченный участник:** {user.mention} (`{user.id}`)\n"
            f"**Управленец:** {interaction.user.mention} (Использований за 24ч: `{used_count + 1}/{DAILY_ACTION_LIMIT}`)\n"
            f"**Причина (п. 4.3):** {reason}\n"
            f"**Срок / Условие выхода (п. 5.2):** {duration_str}\n"
            f"**Статус мута:** {mute_status_note}\n"
            f"**Приостановлены роли Управленца (п. 7.3):** `{len(saved_manager_roles)}`\n"
            f"**Статус твинка (п. 2.6):** {twink_str}"
        ),
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )
    await log_moderation_action(interaction.guild, log_emb)

    try:
        dm_embed = discord.Embed(
            title="⚠️ Вы переведены в статус Ограниченного участника (Изоляция + Мут)",
            description=(
                f"**Сервер:** {interaction.guild.name}\n"
                f"**Применил Управленец:** {interaction.user.display_name}\n"
                f"**Причина (п. 4.3):** {reason}\n"
                f"**Условия выхода (п. 5.2):** {duration_str}\n\n"
                f"⚖️ **Ваше право на Публичную апелляцию (п. 5.1 Регламента):**\n"
                f"1. Напишите в этот личный чат с ботом сообщение с аргументированным текстом вашей апелляции.\n"
                f"2. Скопируйте ID вашего сообщения (или ссылку на него) и введите здесь команду `/rappeal message_id:<ID>`.\n"
                f"3. АСУ автоматически опубликует вашу апелляцию и при победе сама снимет с вас Изоляцию!"
            ),
            color=discord.Color.orange()
        )
        await user.send(embed=dm_embed)
    except Exception:
        pass

    await interaction.followup.send(
        f"✅ Пользователь {user.mention} переведён в режим Изоляции и получил мут (`{human_dur}`).\n"
        f"📊 Использовано лимитов `/risolation` за 24ч: **{used_count + 1}/{DAILY_ACTION_LIMIT}**.",
        ephemeral=True
    )


@bot.tree.command(name="runisolation", description="Снять Изоляцию с участника и восстановить его права")
@app_commands.describe(user="Ограниченный участник", reason="Причина снятия ограничений")
async def runisolation(interaction: discord.Interaction, user: discord.Member, reason: str = "Завершение разбирательства"):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Доступно только на сервере.", ephemeral=True)

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
        return await interaction.followup.send("⚠️ Укажите корректный числовой ID вашего сообщения (или ссылку на сообщение), отправленного в ЛС боту.")
    parsed_msg_id = int(id_matches[-1])

    iso_guild_id = None
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT guild_id FROM isolations WHERE user_id = ?", (interaction.user.id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                iso_guild_id = int(row[0])

    candidate_guilds: List[discord.Guild] = []
    if iso_guild_id:
        g = bot.get_guild(iso_guild_id)
        if g:
            candidate_guilds.append(g)
    if interaction.guild and interaction.guild not in candidate_guilds:
        candidate_guilds.append(interaction.guild)
    if GUILD_ID > 0:
        g = bot.get_guild(GUILD_ID)
        if g and g not in candidate_guilds:
            candidate_guilds.append(g)
    for g in bot.guilds:
        if g not in candidate_guilds:
            candidate_guilds.append(g)

    if not candidate_guilds:
        return await interaction.followup.send("❌ Бот не состоит ни на одном сервере Сообщества.")

    target_guild: Optional[discord.Guild] = None
    target_member: Optional[discord.Member] = None
    is_isolated_user = False

    for g in candidate_guilds:
        m = g.get_member(interaction.user.id)
        if not m:
            try:
                m = await g.fetch_member(interaction.user.id)
            except Exception:
                continue

        has_iso_role = (ISOLATION_ROLE_ID > 0 and any(r.id == ISOLATION_ROLE_ID for r in m.roles))
        in_iso_db = (iso_guild_id == g.id)
        is_timed_out = (m.timed_out_until is not None and m.timed_out_until > discord.utils.utcnow())

        if in_iso_db or has_iso_role or is_timed_out:
            target_guild = g
            target_member = m
            is_isolated_user = True
            break

    if not is_isolated_user or not target_guild or not target_member:
        return await interaction.followup.send(
            "ℹ️ Вы не числитесь в режиме Изоляции ни в базе АСУ, ни по ролям сервера — ваши права Полноценного участника не ограничены."
        )

    for pdata in POLLS_CACHE.values():
        if (
            pdata.get("poll_type") == "appeal"
            and pdata.get("target_user_id") == interaction.user.id
            and pdata.get("status") in ("cooldown", "active", "rejected_frozen")
        ):
            existing_link = f"https://discord.com/channels/{pdata['guild_id']}/{pdata['channel_id']}/{pdata['poll_message_id']}"
            return await interaction.followup.send(
                f"⚠️ Ваша Публичная апелляция уже запущена и находится на рассмотрении: {existing_link}"
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
            "**Как подать апелляцию:**\n"
            "1. Отправьте обычный текст вашей апелляции прямо сюда, в личные сообщения боту.\n"
            "2. Нажмите правой кнопкой мыши (или удерживайте на телефоне) на отправленное сообщение → **Копировать ID сообщения** (или **Копировать ссылку на сообщение**).\n"
            "3. Введите `/rappeal message_id:<скопированный ID>`."
        )

    if target_msg.author.id != interaction.user.id:
        return await interaction.followup.send("⚠️ Для подачи апелляции вы можете указать только собственное сообщение.")

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
            f"✅ Ваша Публичная апелляция успешно опубликована на сервере **{target_guild.name}**: {poll_msg.jump_url}\n"
            f"⚙️ Это автоматическая процедура (п. 5.1, 6.6): при победе голосования АСУ **автоматически снимет с вас Изоляцию**."
        )
    else:
        await interaction.followup.send(
            "❌ Не удалось опубликовать апелляцию на сервере. Убедитесь, что у бота есть права на отправку сообщений в канале апелляций (`APPEALS_CHANNEL_ID` / `POLLS_CHANNEL_IDS`)."
        )


if hasattr(app_commands, "allowed_contexts"):
    rappeal = app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)(rappeal)
if hasattr(app_commands, "allowed_installs"):
    rappeal = app_commands.allowed_installs(guilds=True, users=True)(rappeal)


# =====================================================================
# 13. SLASH COMMANDS: STAFF ROLES & DASHBOARD (/relection, /rselfimpeach, /rstatistics)
# =====================================================================

@bot.tree.command(name="relection", description="Техническая выдача роли Управленца с задержкой 30 мин. (лимит: 3 в 24ч)")
@app_commands.describe(
    user="Участник, которому назначается роль",
    role="Роль Управленца (не выше вашей собственной управленческой роли по иерархии Discord)",
    reason="Основание выдачи (ссылка на решение или стажировку, п. 4.3)"
)
async def relection(interaction: discord.Interaction, user: discord.Member, role: discord.Role, reason: str = "Исполнение кадрового решения"):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Доступно только на сервере.", ephemeral=True)

    if not has_command_permission(interaction.user, "relection"):
        return await interaction.response.send_message("❌ У вашей должности нет прав на использование `/relection`.", ephemeral=True)

    if role.id not in MANAGER_ROLES:
        allowed_names = ", ".join(f"`{n}`" for n in MANAGER_ROLES.values()) or "список пуст"
        return await interaction.response.send_message(
            f"❌ Роль {role.mention} не числится в системном массиве ролей Управленцев ({allowed_names}).",
            ephemeral=True
        )

    caller_manager_roles = [r for r in interaction.user.roles if r.id in MANAGER_ROLES]
    if not caller_manager_roles and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ У вас нет активной роли Управленца из системного списка.", ephemeral=True)

    highest_caller_mgr_pos = max((r.position for r in caller_manager_roles), default=interaction.user.top_role.position)

    if role.position > highest_caller_mgr_pos:
        return await interaction.response.send_message(
            f"❌ **Нарушение иерархии:** Роль {role.mention} находится выше вашей должности в иерархии Discord. "
            f"Вы можете назначать только свою роль Управленца или роли ниже неё.",
            ephemeral=True
        )

    can_use, used_count, next_avail = await check_daily_command_limit(interaction.user.id, interaction.guild.id, "relection")
    if not can_use:
        return await interaction.response.send_message(
            f"⏳ **Достигнут суточный лимит:** Команду `/relection` можно использовать не более **{DAILY_ACTION_LIMIT} раз за 24 часа** "
            f"(использовано: `{used_count}/{DAILY_ACTION_LIMIT}`).\n"
            f"Следующее использование станет доступно: <t:{next_avail}:R> (<t:{next_avail}:f>).",
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
            f"**Назначаемая роль:** {role.mention}\n"
            f"**Инициатор:** {interaction.user.mention} (Использований за 24ч: `{used_count + 1}/{DAILY_ACTION_LIMIT}`)\n"
            f"**Основание:** {reason}\n"
            f"**Автоматическое исполнение (задержка 30 мин.):** <t:{execute_at}:R> (<t:{execute_at}:T>)"
        ),
        color=discord.Color.blue(),
        timestamp=discord.utils.utcnow()
    )
    await log_moderation_action(interaction.guild, log_emb)

    await interaction.response.send_message(
        f"✅ Назначение роли {role.mention} пользователю {user.mention} поставлено в очередь АСУ.\n"
        f"⏳ Роль будет выдана автоматически через **30 минут** (<t:{execute_at}:R>).\n"
        f"📊 Использовано лимитов `/relection` за 24ч: **{used_count + 1}/{DAILY_ACTION_LIMIT}**.",
        ephemeral=True
    )


@bot.tree.command(name="rselfimpeach", description="Добровольно снять с себя роль Управленца (самоотвод / отставка по п. 7.3 Регламента)")
@app_commands.describe(
    role="Управленческая роль (@роль), которую вы хотите снять с себя",
    role_id="ID управленческой роли (можно указать вместе с @ролью или вместо неё)"
)
async def rselfimpeach(
    interaction: discord.Interaction,
    role: Optional[discord.Role] = None,
    role_id: Optional[str] = None
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Команда доступна только на сервере.", ephemeral=True)

    target_role: Optional[discord.Role] = role

    if role_id:
        id_matches = SNOWFLAKE_RE.findall(role_id.strip())
        if not id_matches:
            return await interaction.response.send_message("⚠️ В параметре `role_id` указан некорректный цифровой ID роли.", ephemeral=True)
        parsed_rid = int(id_matches[0])
        if target_role and target_role.id != parsed_rid:
            return await interaction.response.send_message(
                f"⚠️ Указанная роль {target_role.mention} (`{target_role.id}`) не совпадает с введённым `role_id` (`{parsed_rid}`).",
                ephemeral=True
            )
        if not target_role:
            target_role = interaction.guild.get_role(parsed_rid)

    if not target_role:
        return await interaction.response.send_message(
            "⚠️ Укажите управленческую роль через `@role` или введите её цифровой `role_id`.",
            ephemeral=True
        )

    if target_role.id not in MANAGER_ROLES:
        allowed_names = ", ".join(f"`{n}`" for n in MANAGER_ROLES.values()) or "реестр пуст"
        return await interaction.response.send_message(
            f"❌ Роль {target_role.mention} (ID: `{target_role.id}`) не входит в массив управленческих ролей АСУ (`MANAGER_ROLES`: {allowed_names}).",
            ephemeral=True
        )

    if target_role not in interaction.user.roles:
        return await interaction.response.send_message(
            f"ℹ️ У вас нет роли {target_role.mention}, поэтому снять её невозможно.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)
    try:
        await interaction.user.remove_roles(
            target_role,
            reason="АСУ: Добровольное сложение полномочий Управленца (/rselfimpeach, п. 7.3 Регламента)"
        )
    except discord.Forbidden:
        return await interaction.followup.send(
            "❌ Ошибка иерархии Discord: роль бота находится ниже снимаемой роли Управленца.",
            ephemeral=True
        )
    except Exception as e:
        return await interaction.followup.send(f"❌ Ошибка при снятии роли: `{e}`", ephemeral=True)

    log_emb = discord.Embed(
        title="🏳️ Добровольная отставка Управленца (/rselfimpeach, п. 7.3)",
        description=(
            f"**Участник:** {interaction.user.mention} (`{interaction.user.id}`)\n"
            f"**Снятая должность:** {target_role.mention} (`{target_role.id}` — {MANAGER_ROLES.get(target_role.id, 'Управленец')})\n"
            f"**Основание:** Самостоятельный отказ от прав через АСУ (п. 7.3 Регламента)"
        ),
        color=discord.Color.gold(),
        timestamp=discord.utils.utcnow()
    )
    await log_moderation_action(interaction.guild, log_emb)

    await interaction.followup.send(
        f"✅ Вы добровольно сложили с себя полномочия: управленческая роль {target_role.mention} (`{target_role.id}`) успешно снята (п. 7.3 Регламента).",
        ephemeral=True
    )


@bot.tree.command(name="rstatistics", description="Создать вечный самообновляющийся виджет статистики АСУ (Пункт 7 ТЗ)")
@app_commands.describe(seconds="Интервал автообновления виджета в секундах (рекомендуется от 30 сек.)")
async def rstatistics(interaction: discord.Interaction, seconds: int = 60):
    global DASHBOARD_CACHE
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Доступно только на сервере.", ephemeral=True)

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
        f"✅ Вечный виджет статистики АСУ создан и будет обновляться каждые **{safe_interval} сек.**!",
        ephemeral=True
    )


@bot.tree.command(name="rlist", description="Показать список всех текущих голосований")
async def rlist(interaction: discord.Interaction):
    description_lines = []

    for data in POLLS_CACHE.values():
        try:
            if data.get("status") not in ("active", "cooldown", "rejected_frozen"):
                continue

            guild_id = interaction.guild_id or data.get("guild_id", "@me")
            msg_link = f"https://discord.com/channels/{guild_id}/{data['channel_id']}/{data['poll_message_id']}"
            quote = data["quote_content"].replace("\n", " ")
            if len(quote) > 45:
                quote = quote[:45] + "..."

            status_ico = {"cooldown": "🧊", "active": "📊", "rejected_frozen": "⏸️"}.get(data.get("status"), "📊")
            shield_ico = "🛡️" if data.get("aci_shield") else ("⚡" if data.get("immune_to_aci") else "")
            reports_n = len(data.get("aci_reports", []))
            line = (
                f"**{len(description_lines)+1}.** {status_ico}{shield_ico} "
                f"[{data['category']} / {data['tier']}]({msg_link}) — ID: `{data['poll_message_id']}`\n"
                f"> *{quote}*\n"
                f"Итоги: <t:{data['end_time']}:R> | Голосов: **{len(data.get('votes', {}))}/{data['quorum_req']}** | ⚠️Жалобы: `{reports_n} шт.`"
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
        return await interaction.response.send_message("❌ Доступно только на сервере.", ephemeral=True)

    await interaction.response.defer()
    active_count, threshold, active_users = await get_active_users_stats(interaction.guild.id)

    if not active_users:
        return await interaction.followup.send("📊 За последние 30 дней нет данных об активности.")

    top_display = [f"**{idx}.** <@{uid}> — `{count}` сообщ." for idx, (uid, count) in enumerate(active_users[:15], 1)]
    if len(active_users) > 15:
        top_display.append(f"\n*...и ещё {len(active_users) - 15} активных участников.*")

    embed = discord.Embed(
        title="👥 База расчёта динамического кворума (за 30 дней)",
        description=(
            f"Порог активности (медиана): **{threshold:g} сообщ.**\n"
            f"Активных пользователей (учитываются в 100% базы кворума): **{active_count}**\n\n"
            f"**Топ активности:**\n" + "\n".join(top_display)
        ),
        color=discord.Color.purple()
    )
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="rhelp", description="Справка")
async def rhelp(interaction: discord.Interaction):
    help_text = (
        "**Автоматизированная Система Управления (АСУ) — RedAdapter**\n"
        "### Обозначение кнопок под голосованиями (шкала Лайкерта):\n"
        "- `⏫` — **Полностью согласен (+2)**\n"
        "- `🔼` — **Согласен (+1)**\n"
        "- `⏺️` — **Воздержусь / Нейтрально (0)**\n"
        "- `🔽` — **Не согласен (-1)**\n"
        "- `⏬` — **Полностью не согласен (-2)**\n"
        "- `🔍 Мой голос` — **проверить свой текущий сохранённый выбор**\n"
        "- `🚩 Жалоба ACI` — **отправить запрос Управленцам на проверку соответствия Стандарту ACI**\n"
        "*(Если голосование на охлаждении или кнопка недоступна — она становится серой и некликабельной)*\n\n"
        "### 🗳️ Голосования и Самоуправление (Разделы 1, 6, 7):\n"
        "- `/rpoll standard <ID> <Категория> [Канал]` — запустить голосование по тексту любого участника.\n"
        "- `/rpoll impeach <@Управленец> [Канал]` — спец-голосование III Тира за снятие Управленца в обход ACI (п. 6.8).\n"
        "- `/rpoll global election <@Кандидат> <@Роль> <ID> [Канал]` — прямое выдвижение на пост по тексту (п. 7.1.1).\n"
        "- `/rpollelection <@Роль>` — заполнить интерактивную анкету кандидата и запустить выборы (п. 7.1).\n\n"
        "### 🕊️ Защита прав Ограниченных участников (Раздел 5):\n"
        "- `/rappeal <ID сообщения или ссылка из ЛС>` — подать Публичную апелляцию напрямую из ЛС с ботом (п. 5.1). При победе АСУ автоматически снимает Изоляцию и возвращает все изъятые роли.\n\n"
        "### 🛡️ Инструменты Управленцев (Разделы 3, 4, 7):\n"
        "- `/rreject <ID> <Основание> <Комментарий>` — отклонить и заморозить опрос по ACI или п. 0.2 (**требуется 1 чел.**).\n"
        "- `/rconfirm <ID> [Комментарий]` — подтвердить ACI (наложить щит или разморозить по п. 3.3.1, **требуется 1/5 от имеющих доступ к `/rconfirm` с округлением по половине**).\n"
        "- `/risolation <@Юзер> <Причина> [Срок: Ns/Nh/Nd/Nm/Ny] [Твинк]` — перевести в Изоляцию с мутом (п. 3.5, **лимит: до 3 раз в 24 часа**).\n"
        "- `/runisolation <@Юзер> [Причина]` — досрочно снять Изоляцию и вернуть все изъятые роли.\n"
        "- `/relection <@Юзер> <@Роль> [Причина]` — выдать роль по иерархии с задержкой 30 мин. (**лимит: до 3 раз в 24 часа**).\n"
        "- `/rselfimpeach [@Роль] [role_id]` — добровольно снять с самого себя роль Управленца (п. 7.3).\n"
        "- `/rstatistics <Секунды>` — запустить вечный живой дашборд статистики АСУ.\n"
        "- `/rlist` и `/ractivelist` — список текущих опросов и расчёт медианы кворума."
    )
    embed = discord.Embed(title="ℹ️ Документация АСУ RedAdapter", description=help_text, color=discord.Color.orange())
    await interaction.response.send_message(embed=embed, ephemeral=True)


# =====================================================================
# 14. ENTRYPOINT
# =====================================================================

if __name__ == "__main__":
    if PROXY_URL:
        bot.run(BOT_TOKEN, proxy=PROXY_URL)
    else:
        bot.run(BOT_TOKEN)
