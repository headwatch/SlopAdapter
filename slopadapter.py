import datetime
import hashlib
import io
import json
import logging
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

# =============================================================================
# PLATFORM CONFIGURATION
# =============================================================================

BOT_TOKEN: str = os.getenv("DISCORD_BOT_TOKEN", "YOUR_DISCORD_BOT_TOKEN_HERE")

# Strict single-guild lock: bot automatically leaves any other guild
ALLOWED_GUILD_ID: int = int(os.getenv("ALLOWED_GUILD_ID", "123456789012345678"))

# Core role IDs
FULL_MEMBER_ROLE_ID: int = int(os.getenv("FULL_MEMBER_ROLE_ID", "111111111111111111"))
RESTRICTED_ROLE_ID: int = int(os.getenv("RESTRICTED_ROLE_ID", "222222222222222222"))
TWINK_ROLE_ID: int = int(os.getenv("TWINK_ROLE_ID", "000000000000000000"))

# Manager role registry: add any number (N) of roles here as {ROLE_ID: "Role Title"}
MANAGER_ROLES: Dict[int, str] = {
    222222222222222222: "Mod",
    333333333333333333: "Admin",
}

# Leave list empty [] to allow all MANAGER_ROLES, or specify any combination of Role IDs per action
COMMAND_PERMISSIONS: Dict[str, List[int]] = {
    "aci_confirm":     [222222222222222222, 333333333333333333],
    "aci_reject":      [222222222222222222, 333333333333333333],
    "isolate":         [222222222222222222, 333333333333333333],
    "unisolate":       [222222222222222222, 333333333333333333],
    "ban_twink":       [222222222222222222, 333333333333333333],
    "approve_twink":   [222222222222222222, 333333333333333333],
    "unapprove_twink": [222222222222222222, 333333333333333333],
    "appoint":         [222222222222222222, 333333333333333333],
    "unexecutable":    [222222222222222222, 333333333333333333],
}

# Core channel IDs
VOTING_CHANNEL_ID: int = int(os.getenv("VOTING_CHANNEL_ID", "444444444444444444"))
AUDIT_LOG_CHANNEL_ID: int = int(os.getenv("AUDIT_LOG_CHANNEL_ID", "555555555555555555"))

BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
DB_PATH: str = os.path.join(BASE_DIR, "horizon_asu_local.db")
ELECTIONS_DIR: str = os.path.join(BASE_DIR, "elections_jsons")

# Security limits & constants
DAILY_ISOLATE_LIMIT: int = 3
DAILY_APPOINT_LIMIT: int = 3
DAILY_BAN_TWINK_LIMIT: int = 1
APPOINT_DELAY_SECONDS: int = 1800
DISCORD_MAX_TIMEOUT_SECONDS: int = 2419200
MAX_ATTACHMENT_BYTES: int = 8 * 1024 * 1024

# Timer multiplier (1.0 = production SVA V. RH timers; e.g. 0.01 = 100x faster for testing)
TIME_SCALE: float = 1.0

DURATION_UNITS: Dict[str, int] = {
    "sec": 1, "s": 1,
    "min": 60,
    "hr": 3600, "h": 3600,
    "day": 86400, "d": 86400,
    "mon": 2592000, "mo": 2592000, "m": 2592000,
    "yr": 31536000, "y": 31536000,
}
DURATION_TOKEN_RE = re.compile(r"(\d+)\s*(sec|min|mon|day|hr|mo|yr|s|h|d|m|y)\b")
WHITESPACE_RE = re.compile(r"\s+")
SNOWFLAKE_RE = re.compile(r"\d{15,22}")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ASU_Horizon")


# =============================================================================
# SVA V. RH TIERS & CATEGORIES
# =============================================================================

@dataclass(frozen=True)
class TierSpec:
    tier_id: int
    code: str
    name: str
    win_percentage: float
    cooldown_seconds: int
    quorum_percentage: float
    duration_seconds: int
    color: int


TIERS: Dict[int, TierSpec] = {
    1: TierSpec(1, "I. Low", "Tier I (Low)", 50.0, 0, 20.0, 24 * 3600, 0x2ECC71),
    2: TierSpec(2, "II. Mid", "Tier II (Mid)", 50.0, 3 * 3600, 30.0, 48 * 3600, 0x3498DB),
    3: TierSpec(3, "III. High", "Tier III (High)", 60.0, 12 * 3600, 40.0, 72 * 3600, 0xE67E22),
    4: TierSpec(4, "IV. Crit", "Tier IV (Crit — Локальный контур)", 70.0, 24 * 3600, 50.0, 168 * 3600, 0xE74C3C),
}

CATEGORIES: Dict[str, Tuple[int, str]] = {
    "Косметика": (1, "Внешнее и звуковое оформление Площадки без изменения символики"),
    "Ивенты": (1, "Организация разовых и временных активностей и временных комнат"),
    "Опросы": (1, "Консультативные голосования и сбор мнений без технических последствий"),
    "Каналы": (2, "Изменение структуры/навигации разделов, отмена быстрых правок Управленцев"),
    "Роли": (2, "Создание, изменение и удаление пользовательских ролей без прав Управленца"),
    "Формат": (2, "Утверждение расписания, рубрик, справочников и пользовательских проектов"),
    "Правосудие": (3, "Санкции (бан, кик), Изоляция, Публичные апелляции, одобрение тех. твинков"),
    "Локальные правила": (3, "Принятие и изменение локальных подправил и параметров шаблонов АСУ"),
    "Стафф": (3, "Избрание, назначение и снятие (импичмент) Управленцев, правила отбора"),
    "Подконтрольные Ресурсы": (3, "Создание, реорганизация или закрытие зависимых ресурсов"),
    "Интеграции": (3, "Добавление, настройка и удаление ботов, скриптов и модулей с правами"),
    "Локальное лого": (3, "Смена локального аватара или отображаемого названия Площадки"),
    "Название": (4, "Смена официального общего названия всего сообщества"),
    "Лого": (4, "Смена основного логотипа, эмблемы и главной визуальной символики"),
    "Система голосования": (4, "Изменение архитектуры SVA V. RH, формул кворума, весов и ACI"),
    "Площадки и отдельные Ресурсы": (4, "Принудительное принятие/отмена решений I-III тира, пресечение саботажа"),
    "Фундамент": (4, "Изменение Манифеста, Регламента, вектора развития или роспуск сообщества"),
}


def ensure_sample_election_template() -> None:
    os.makedirs(ELECTIONS_DIR, exist_ok=True)
    sample_path = os.path.join(ELECTIONS_DIR, "example.json")
    default_role_id = next(iter(MANAGER_ROLES.keys()), 0)
    if not os.path.exists(sample_path):
        sample_content = (
            "/*\n"
            "ПРИМЕР ШАБЛОНА АНКЕТЫ ДЛЯ ВЫБОРОВ (/pollelection) — п. 7.1, 7.2 Регламента\n"
            "Скопируйте этот файл, назовите его по ID роли (например: 222222222222222222.json),\n"
            "уберите комментарии /* */ и укажите реальный role_id:\n\n"
            "{\n"
            f'    "role_id": {default_role_id},\n'
            '    "role_name": "Управленец",\n'
            '    "description": "Квалификационная анкета кандидата на должность Управленца сообщества «Горизонт» (п. 7.1, 7.2).",\n'
            '    "questions": [\n'
            '        "1. Ваш часовой пояс и среднее время активности в сутки?",\n'
            '        "2. Опишите ваш опыт модерации и знание Стандарта ACI.",\n'
            '        "3. В каких случаях допустимо применение экстренной Изоляции (п. 3.5)?",\n'
            '        "4. Как вы поступите при обнаружении скрытой модерации (нарушение п. 4.3)?",\n'
            '        "5. Ваши главные цели на должности Управленца?"\n'
            "    ]\n"
            "}\n"
            "*/\n"
        )
        with open(sample_path, "w", encoding="utf-8") as f:
            f.write(sample_content)


def parse_duration_string(duration_str: str) -> Optional[int]:
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
    if seconds <= 0:
        return "До окончания публичного разбирательства / апелляции"
    parts = []
    for unit_name, unit_sec in [
        ("г.", 31536000),
        ("мес.", 2592000),
        ("дн.", 86400),
        ("ч.", 3600),
        ("мин.", 60),
        ("сек.", 1),
    ]:
        if seconds >= unit_sec:
            count, seconds = divmod(seconds, unit_sec)
            parts.append(f"{count} {unit_name}")
    return " ".join(parts)


# =============================================================================
# DATABASE & SVA V. RH MATHEMATICAL ENGINE
# =============================================================================

class HorizonDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS activity_impulses (
                    user_id INTEGER NOT NULL,
                    interval_bucket INTEGER NOT NULL,
                    recorded_at REAL NOT NULL,
                    PRIMARY KEY (user_id, interval_bucket)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS polls (
                    poll_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    initiator_id INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    tier INTEGER NOT NULL,
                    proposal_text TEXT NOT NULL,
                    proposal_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    voting_starts_at REAL NOT NULL,
                    ends_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'COOLDOWN',
                    aci_immune INTEGER NOT NULL DEFAULT 0,
                    target_user_id INTEGER,
                    action_type TEXT NOT NULL DEFAULT 'NONE',
                    action_payload TEXT NOT NULL DEFAULT '{}',
                    channel_id INTEGER,
                    message_id INTEGER,
                    final_quorum_active_users INTEGER,
                    final_quorum_required INTEGER,
                    final_voters_count INTEGER,
                    final_support_pct REAL,
                    final_aci_balance REAL,
                    execution_log TEXT,
                    burger_links_json TEXT NOT NULL DEFAULT '[]',
                    attachments_meta_json TEXT NOT NULL DEFAULT '[]',
                    unexecutable_by_id INTEGER,
                    unexecutable_reason TEXT
                )
            """)

            for col_def in [
                "burger_links_json TEXT NOT NULL DEFAULT '[]'",
                "attachments_meta_json TEXT NOT NULL DEFAULT '[]'",
                "unexecutable_by_id INTEGER",
                "unexecutable_reason TEXT",
            ]:
                try:
                    cursor.execute(f"ALTER TABLE polls ADD COLUMN {col_def}")
                except sqlite3.OperationalError:
                    pass

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS votes (
                    poll_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    weight INTEGER NOT NULL,
                    voted_at REAL NOT NULL,
                    PRIMARY KEY (poll_id, user_id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS aci_validations (
                    poll_id INTEGER NOT NULL,
                    manager_id INTEGER NOT NULL,
                    verdict INTEGER NOT NULL,
                    effective_weight REAL NOT NULL,
                    reason TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (poll_id, manager_id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS isolations (
                    user_id INTEGER PRIMARY KEY,
                    manager_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    isolated_at REAL NOT NULL,
                    until_ts REAL NOT NULL DEFAULT 0,
                    saved_roles TEXT NOT NULL DEFAULT '[]'
                )
            """)

            for iso_col in [
                "until_ts REAL NOT NULL DEFAULT 0",
                "saved_roles TEXT NOT NULL DEFAULT '[]'",
            ]:
                try:
                    cursor.execute(f"ALTER TABLE isolations ADD COLUMN {iso_col}")
                except sqlite3.OperationalError:
                    pass

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS command_usage_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    command_name TEXT NOT NULL,
                    used_at REAL NOT NULL
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pending_appointments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_user_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    initiator_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    execute_at REAL NOT NULL,
                    executed INTEGER NOT NULL DEFAULT 0
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS approved_twinks (
                    twink_user_id INTEGER PRIMARY KEY,
                    owner_user_id INTEGER NOT NULL,
                    poll_id INTEGER NOT NULL,
                    approved_at REAL NOT NULL
                )
            """)
            conn.commit()

    def check_command_rate_limit(
        self, user_id: int, command_name: str, limit_count: int, window_sec: int = 86400
    ) -> Tuple[bool, int, int]:
        now = time.time()
        cutoff = now - window_sec
        with self._get_conn() as conn:
            conn.execute("DELETE FROM command_usage_logs WHERE used_at < ?", (now - 86400 * 7,))
            rows = conn.execute(
                "SELECT used_at FROM command_usage_logs WHERE user_id = ? AND command_name = ? AND used_at > ? ORDER BY used_at ASC",
                (user_id, command_name, cutoff),
            ).fetchall()

        used = len(rows)
        if used >= limit_count:
            reset_ts = int(rows[0]["used_at"] + window_sec)
            return False, used, reset_ts
        return True, used, 0

    def record_command_usage(self, user_id: int, command_name: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO command_usage_logs (user_id, command_name, used_at) VALUES (?, ?, ?)",
                (user_id, command_name, time.time()),
            )
            conn.commit()

    def register_approved_twink(self, twink_id: int, owner_id: int, poll_id: Optional[int] = None) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO approved_twinks (twink_user_id, owner_user_id, poll_id, approved_at) VALUES (?, ?, ?, ?)",
                (twink_id, owner_id, poll_id or 0, time.time()),
            )
            conn.commit()

    def remove_approved_twink(self, twink_id: int) -> bool:
        with self._get_conn() as conn:
            cur = conn.execute(
                "DELETE FROM approved_twinks WHERE twink_user_id = ?",
                (twink_id,),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_approved_twink_owner(self, user_id: int) -> Optional[int]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT owner_user_id FROM approved_twinks WHERE twink_user_id = ?",
                (user_id,),
            ).fetchone()
            return int(row["owner_user_id"]) if row else None

    def get_all_excluded_quorum_user_ids(self) -> Set[int]:
        excluded: Set[int] = set()
        with self._get_conn() as conn:
            for r in conn.execute("SELECT user_id FROM isolations").fetchall():
                excluded.add(int(r["user_id"]))
            for r in conn.execute("SELECT twink_user_id FROM approved_twinks").fetchall():
                excluded.add(int(r["twink_user_id"]))
        return excluded

    def purge_user_votes_from_open_polls(self, user_id: int) -> List[int]:
        affected_poll_ids: List[int] = []
        with self._get_conn() as conn:
            open_rows = conn.execute(
                "SELECT poll_id FROM polls WHERE status IN ('COOLDOWN', 'ACTIVE')"
            ).fetchall()
            for r in open_rows:
                pid = int(r["poll_id"])
                cur = conn.execute(
                    "DELETE FROM votes WHERE poll_id = ? AND user_id = ?",
                    (pid, user_id),
                )
                if cur.rowcount > 0:
                    affected_poll_ids.append(pid)
            conn.commit()
        return affected_poll_ids

    # 15-minute quantized impulse logging (900s buckets, 720h retention, no message content stored)
    def record_message_impulse(self, user_id: int, now_ts: Optional[float] = None) -> None:
        now = now_ts or time.time()
        interval_bucket = int(now // 900)
        cutoff = now - (720 * 3600)

        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO activity_impulses (user_id, interval_bucket, recorded_at) VALUES (?, ?, ?)",
                (user_id, interval_bucket, now),
            )
            conn.execute("DELETE FROM activity_impulses WHERE recorded_at < ?", (cutoff,))
            conn.commit()

    # SVA V. RH 30-day quorum calculation:
    # Score_d = ln(1 + impulses_d) * (0.5 + 0.5 * d / 30); requires >= 2 active days.
    # Threshold = 50% of the weighted median of sorted user indices.
    def calculate_active_users_stats(
        self, extra_excluded_ids: Optional[Set[int]] = None, now_ts: Optional[float] = None
    ) -> Dict[str, Any]:
        now = now_ts or time.time()
        window_start = now - (720 * 3600)
        excluded_ids = self.get_all_excluded_quorum_user_ids()
        if extra_excluded_ids:
            excluded_ids |= extra_excluded_ids

        with self._get_conn() as conn:
            conn.execute("DELETE FROM activity_impulses WHERE recorded_at < ?", (window_start,))
            conn.commit()
            rows = conn.execute(
                "SELECT user_id, recorded_at FROM activity_impulses WHERE recorded_at >= ?",
                (window_start,),
            ).fetchall()

        user_daily_impulses: Dict[int, Dict[int, int]] = {}
        total_raw_impulses = 0
        for row in rows:
            uid = int(row["user_id"])
            if uid in excluded_ids:
                continue
            rec_at = float(row["recorded_at"])
            day_index = int((rec_at - window_start) // 86400) + 1
            day_index = max(1, min(30, day_index))

            if uid not in user_daily_impulses:
                user_daily_impulses[uid] = {}
            if user_daily_impulses[uid].get(day_index, 0) < 96:
                user_daily_impulses[uid][day_index] = user_daily_impulses[uid].get(day_index, 0) + 1
                total_raw_impulses += 1

        qualified_indices: List[Tuple[int, float]] = []
        user_details: Dict[int, Dict[str, Any]] = {}
        discarded_single_day_count = 0

        for uid, days_map in user_daily_impulses.items():
            active_days_count = sum(1 for imp in days_map.values() if imp > 0)
            total_user_impulses = sum(days_map.values())
            last_24h_impulses = days_map.get(30, 0)

            total_index = 0.0
            for d in range(1, 31):
                imp = days_map.get(d, 0)
                if imp > 0:
                    freshness_weight = 0.5 + 0.5 * (d / 30.0)
                    total_index += math.log(1 + imp) * freshness_weight

            user_details[uid] = {
                "active_days": active_days_count,
                "total_impulses": total_user_impulses,
                "last_24h_impulses": last_24h_impulses,
                "index": round(total_index, 4),
                "qualified": active_days_count >= 2,
            }

            if active_days_count < 2:
                discarded_single_day_count += 1
                continue

            qualified_indices.append((uid, total_index))

        if not qualified_indices:
            return {
                "active_count": 0,
                "qualified_count": 0,
                "discarded_single_day": discarded_single_day_count,
                "total_raw_impulses": total_raw_impulses,
                "total_index_sum": 0.0,
                "weighted_median": 0.0,
                "threshold": 0.0,
                "active_user_ids": set(),
                "user_indices": {},
                "user_details": user_details,
            }

        qualified_indices.sort(key=lambda item: item[1])
        total_index_sum = sum(idx for _, idx in qualified_indices)
        half_sum_target = 0.5 * total_index_sum

        cumulative_sum = 0.0
        weighted_median = qualified_indices[-1][1]
        for _, idx in qualified_indices:
            cumulative_sum += idx
            if cumulative_sum >= half_sum_target - 1e-12:
                weighted_median = idx
                break

        activity_threshold = 0.5 * weighted_median
        active_user_ids = {
            uid for uid, idx in qualified_indices if idx >= activity_threshold - 1e-12
        }

        return {
            "active_count": len(active_user_ids),
            "qualified_count": len(qualified_indices),
            "discarded_single_day": discarded_single_day_count,
            "total_raw_impulses": total_raw_impulses,
            "total_index_sum": round(total_index_sum, 4),
            "weighted_median": round(weighted_median, 4),
            "threshold": round(activity_threshold, 4),
            "active_user_ids": active_user_ids,
            "user_indices": {uid: round(idx, 4) for uid, idx in qualified_indices},
            "user_details": user_details,
        }

    def create_poll(
        self,
        initiator_id: int,
        category: str,
        proposal_text: str,
        aci_immune: bool = False,
        target_user_id: Optional[int] = None,
        action_type: str = "NONE",
        action_payload: Optional[dict] = None,
        burger_links: Optional[List[str]] = None,
        attachments_meta: Optional[List[Dict[str, str]]] = None,
        custom_hash: Optional[str] = None,
    ) -> int:
        tier_id, _ = CATEGORIES[category]
        spec = TIERS[tier_id]

        now = time.time()
        cooldown_sec = spec.cooldown_seconds * TIME_SCALE
        duration_sec = spec.duration_seconds * TIME_SCALE

        voting_starts_at = now + cooldown_sec
        ends_at = voting_starts_at + duration_sec
        initial_status = "COOLDOWN" if cooldown_sec > 0 else "ACTIVE"
        proposal_hash = custom_hash or hashlib.sha256(proposal_text.strip().encode("utf-8")).hexdigest()[:16]

        with self._get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO polls (
                    initiator_id, category, tier, proposal_text, proposal_hash,
                    created_at, voting_starts_at, ends_at, status, aci_immune,
                    target_user_id, action_type, action_payload,
                    burger_links_json, attachments_meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    initiator_id,
                    category,
                    tier_id,
                    proposal_text.strip(),
                    proposal_hash,
                    now,
                    voting_starts_at,
                    ends_at,
                    initial_status,
                    1 if aci_immune else 0,
                    target_user_id,
                    action_type,
                    json.dumps(action_payload or {}, ensure_ascii=False),
                    json.dumps(burger_links or [], ensure_ascii=False),
                    json.dumps(attachments_meta or [], ensure_ascii=False),
                ),
            )
            conn.commit()
            return cur.lastrowid

    def bind_poll_message(
        self, poll_id: int, channel_id: int, message_id: int, burger_links: Optional[List[str]] = None
    ) -> None:
        with self._get_conn() as conn:
            if burger_links is not None:
                conn.execute(
                    "UPDATE polls SET channel_id = ?, message_id = ?, burger_links_json = ? WHERE poll_id = ?",
                    (channel_id, message_id, json.dumps(burger_links, ensure_ascii=False), poll_id),
                )
            else:
                conn.execute(
                    "UPDATE polls SET channel_id = ?, message_id = ? WHERE poll_id = ?",
                    (channel_id, message_id, poll_id),
                )
            conn.commit()

    def mark_poll_unexecutable(self, poll_id: int, manager_id: int, reason_text: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE polls SET unexecutable_by_id = ?, unexecutable_reason = ? WHERE poll_id = ?",
                (manager_id, reason_text.strip(), poll_id),
            )
            conn.commit()

    def get_poll(self, poll_id: int) -> Optional[sqlite3.Row]:
        with self._get_conn() as conn:
            return conn.execute("SELECT * FROM polls WHERE poll_id = ?", (poll_id,)).fetchone()

    def get_open_polls(self) -> List[sqlite3.Row]:
        with self._get_conn() as conn:
            return conn.execute(
                "SELECT * FROM polls WHERE status IN ('COOLDOWN', 'ACTIVE') ORDER BY poll_id ASC"
            ).fetchall()

    def update_poll_status(self, poll_id: int, status: str) -> None:
        with self._get_conn() as conn:
            conn.execute("UPDATE polls SET status = ? WHERE poll_id = ?", (status, poll_id))
            conn.commit()

    def cast_vote(self, poll_id: int, user_id: int, weight: int) -> None:
        now = time.time()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO votes (poll_id, user_id, weight, voted_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(poll_id, user_id) DO UPDATE SET
                    weight = excluded.weight,
                    voted_at = excluded.voted_at
                """,
                (poll_id, user_id, weight, now),
            )
            conn.commit()

    def get_user_vote(self, poll_id: int, user_id: int) -> Optional[sqlite3.Row]:
        with self._get_conn() as conn:
            return conn.execute(
                "SELECT * FROM votes WHERE poll_id = ? AND user_id = ?",
                (poll_id, user_id),
            ).fetchone()

    # Likert weight calculation: Support % = pos_weight / (pos_weight + |neg_weight|) * 100.
    # Neutral votes (0) count toward physical quorum only.
    def calculate_poll_tallies(self, poll_id: int) -> Dict[str, Any]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT weight FROM votes WHERE poll_id = ?", (poll_id,)).fetchall()

        total_voters = len(rows)
        pos_weight = 0
        neg_weight = 0
        neutral_count = 0

        for r in rows:
            w = r["weight"]
            if w > 0:
                pos_weight += w
            elif w < 0:
                neg_weight += abs(w)
            else:
                neutral_count += 1

        weight_pool = pos_weight + neg_weight
        support_pct = (pos_weight / weight_pool * 100.0) if weight_pool > 0 else 0.0

        return {
            "total_voters": total_voters,
            "pos_weight": pos_weight,
            "neg_weight": neg_weight,
            "neutral_count": neutral_count,
            "weight_pool": weight_pool,
            "support_pct": round(support_pct, 2),
        }

    # Anti-sniping ACI weight decay: full weight (1.0) during cooldown and first 50% of active duration,
    # then decays linearly to 0.0 as (remaining_time_ratio * 2).
    @staticmethod
    def compute_aci_manager_weight(
        voting_starts_at: float, ends_at: float, now_ts: Optional[float] = None
    ) -> float:
        now = now_ts or time.time()
        if now <= voting_starts_at:
            return 1.0
        if now >= ends_at:
            return 0.0

        voting_duration = ends_at - voting_starts_at
        if voting_duration <= 0:
            return 1.0

        remaining_ratio = (ends_at - now) / voting_duration
        if remaining_ratio >= 0.5:
            return 1.0
        return round(max(0.0, min(1.0, remaining_ratio * 2.0)), 4)

    def record_aci_validation(
        self, poll_id: int, manager_id: int, verdict: int, reason: str, now_ts: Optional[float] = None
    ) -> Tuple[float, float]:
        poll = self.get_poll(poll_id)
        if not poll:
            raise ValueError("Голосование не найдено.")

        now = now_ts or time.time()
        abs_weight = self.compute_aci_manager_weight(poll["voting_starts_at"], poll["ends_at"], now)
        signed_weight = round(verdict * abs_weight, 4)

        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO aci_validations (poll_id, manager_id, verdict, effective_weight, reason, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(poll_id, manager_id) DO UPDATE SET
                    verdict = excluded.verdict,
                    effective_weight = excluded.effective_weight,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (poll_id, manager_id, verdict, signed_weight, reason.strip(), now),
            )
            conn.commit()

        summary = self.get_aci_summary(poll_id)
        return signed_weight, summary["balance"]

    def get_aci_summary(self, poll_id: int) -> Dict[str, Any]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM aci_validations WHERE poll_id = ? ORDER BY updated_at ASC",
                (poll_id,),
            ).fetchall()

        balance = round(sum(r["effective_weight"] for r in rows), 4)
        return {
            "balance": balance,
            "count": len(rows),
            "records": rows,
        }


# =============================================================================
# PERMISSION HELPERS & EMBED BUILDER
# =============================================================================

def verify_guild(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and interaction.guild.id == ALLOWED_GUILD_ID


def get_all_manager_role_ids() -> Set[int]:
    return {rid for rid in MANAGER_ROLES.keys() if rid > 0}


def is_twink_member(db: HorizonDatabase, member: discord.Member) -> bool:
    if TWINK_ROLE_ID > 0 and any(r.id == TWINK_ROLE_ID for r in member.roles):
        return True
    return db.get_approved_twink_owner(member.id) is not None


def is_full_member(member: discord.Member, db: Optional[HorizonDatabase] = None) -> bool:
    role_ids = {r.id for r in member.roles}
    if RESTRICTED_ROLE_ID in role_ids:
        return False
    if TWINK_ROLE_ID > 0 and TWINK_ROLE_ID in role_ids:
        return False
    if db is not None and db.get_approved_twink_owner(member.id) is not None:
        return False
    mgr_ids = get_all_manager_role_ids()
    return (
        FULL_MEMBER_ROLE_ID in role_ids
        or bool(role_ids & mgr_ids)
        or member.guild_permissions.administrator
    )


def is_manager(member: discord.Member) -> bool:
    role_ids = {r.id for r in member.roles}
    if RESTRICTED_ROLE_ID in role_ids:
        return False
    mgr_ids = get_all_manager_role_ids()
    return bool(role_ids & mgr_ids) or member.guild_permissions.administrator


def has_command_permission(member: discord.Member, command_key: str) -> bool:
    role_ids = {r.id for r in member.roles}
    if RESTRICTED_ROLE_ID in role_ids:
        return False
    allowed_roles = {rid for rid in COMMAND_PERMISSIONS.get(command_key, []) if rid > 0}
    if not allowed_roles:
        return is_manager(member)
    return bool(role_ids & allowed_roles) or member.guild_permissions.administrator


def is_restricted_member(member: discord.Member) -> bool:
    return any(r.id == RESTRICTED_ROLE_ID for r in member.roles)


def split_text_into_chunks(text: str, max_chars: int = 1850) -> List[str]:
    lines = text.split("\n")
    chunks: List[str] = []
    current_lines: List[str] = []
    current_len = 0

    for line in lines:
        if len(line) > max_chars:
            if current_lines:
                chunks.append("\n".join(current_lines))
                current_lines = []
                current_len = 0
            for i in range(0, len(line), max_chars):
                chunks.append(line[i : i + max_chars])
            continue

        if current_len + len(line) + 1 > max_chars and current_lines:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
            current_len = len(line)
        else:
            current_lines.append(line)
            current_len += len(line) + 1

    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks


def build_poll_embed(db: HorizonDatabase, poll: sqlite3.Row, guild: Optional[discord.Guild] = None) -> discord.Embed:
    tier_spec = TIERS[poll["tier"]]
    tallies = db.calculate_poll_tallies(poll["poll_id"])
    aci = db.get_aci_summary(poll["poll_id"])

    extra_excl: Set[int] = set()
    if guild and TWINK_ROLE_ID > 0:
        twink_role = guild.get_role(TWINK_ROLE_ID)
        if twink_role:
            extra_excl = {m.id for m in twink_role.members}

    activity = db.calculate_active_users_stats(extra_excluded_ids=extra_excl)
    active_users_count = activity["active_count"]
    required_quorum_votes = max(1, math.ceil(active_users_count * (tier_spec.quorum_percentage / 100.0)))

    status_map = {
        "COOLDOWN": "🧊 ВРЕМЯ НА ОХЛАЖДЕНИЕ (Чтение и обсуждение)",
        "ACTIVE": "🗳️ АКТИВНЫЙ СБОР ГОЛОСОВ",
        "PASSED": "✅ ПРИНЯТО И ИСПОЛНЕНО",
        "FAILED_QUORUM": "❌ НЕ СОСТОЯЛОСЬ (Не набран кворум)",
        "FAILED_SUPPORT": "❌ ОТКЛОНЕНО УЧАСТНИКАМИ (Не набран % поддержки)",
        "INVALIDATED_ACI": "🚫 АННУЛИРОВАНО ПО СТАНДАРТУ ACI (Баланс <= -0.5)",
    }
    status_label = status_map.get(poll["status"], poll["status"])

    if poll["aci_immune"]:
        aci_status_str = "🛡️ **ИММУНИТЕТ К ACI (п. 6.8 Регламента)**"
    else:
        bal = aci["balance"]
        if bal <= -0.5:
            aci_status_str = f"🚨 **Оспаривается — На грани отмены ({bal:+.2f} <= -0.5)**"
        elif bal < 0:
            aci_status_str = f"⚠️ **Оспаривается ({bal:+.2f})**"
        elif bal > 0:
            aci_status_str = f"☑️ **Подтверждено ({bal:+.2f})**"
        else:
            aci_status_str = "⚖️ **Презумпция легитимности (0.00)**"

    raw_text = poll["proposal_text"]
    if len(raw_text) > 1900:
        display_text = raw_text[:1850] + "\n... [Полный текст в блоках выше 👆]"
    else:
        display_text = raw_text

    embed = discord.Embed(
        title=f"Голосование #{poll['poll_id']} │ [{poll['category']}]",
        description=f"```\n{display_text}\n```",
        color=tier_spec.color if poll["status"] in ("COOLDOWN", "ACTIVE") else (
            0x2ECC71 if poll["status"] == "PASSED" else 0x7F8C3C
        ),
    )

    embed.add_field(
        name="📊 Параметры Тира SVA V. RH",
        value=(
            f"• **Тир:** `{tier_spec.code}`\n"
            f"• **Порог победы:** `> {tier_spec.win_percentage:.0f}%`\n"
            f"• **Требуемый кворум:** `{tier_spec.quorum_percentage:.0f}%` от активных\n"
            f"• **Шкала:** `⏫+2 🔼+1 ⏺️0 🔽-1 ⏬-2`"
        ),
        inline=True,
    )

    created_int = int(poll["created_at"])
    vote_start_int = int(poll["voting_starts_at"])
    ends_int = int(poll["ends_at"])

    embed.add_field(
        name="⏱️ Тайминги (п. 6.4.1)",
        value=(
            f"• **Создано:** <t:{created_int}:R>\n"
            f"• **Старт приёма голосов:** <t:{vote_start_int}:R>\n"
            f"• **Завершение:** <t:{ends_int}:F> (<t:{ends_int}:R>)"
        ),
        inline=True,
    )

    try:
        burger_links = json.loads(poll["burger_links_json"] or "[]")
    except Exception:
        burger_links = []
    try:
        att_meta = json.loads(poll["attachments_meta_json"] or "[]")
    except Exception:
        att_meta = []

    if burger_links or att_meta:
        mat_lines = []
        if burger_links:
            links_str = " • ".join(f"[Блок {i}]({url})" for i, url in enumerate(burger_links, 1))
            mat_lines.append(f"🍔 **Сообщения инициативы:** {links_str}")
        for att in att_meta:
            mat_lines.append(f"📎 `{att['filename']}` (SHA-256: `{att['sha256']}`)")
        embed.add_field(
            name="📦 Материалы и слепки (п. 6.4.1)",
            value="\n".join(mat_lines)[:1024],
            inline=False,
        )

    # Hide For/Against breakdown while poll is open to prevent conformity (Rule 6.3)
    if poll["status"] in ("COOLDOWN", "ACTIVE"):
        quorum_progress = (
            f"`{tallies['total_voters']} / {required_quorum_votes}` "
            f"(Активных на Площадке: `{active_users_count}`)"
        )
        voting_progress_text = (
            f"• **Статус:** {status_label}\n"
            f"• **Явка (Кворум):** {quorum_progress}\n"
            f"• **Баланс весов За/Против:** 🔒 *Скрыто до конца таймера (Защита от конформизма, п. 6.3)*"
        )
    else:
        q_used = poll["final_quorum_required"] if poll["final_quorum_required"] is not None else required_quorum_votes
        act_used = poll["final_quorum_active_users"] if poll["final_quorum_active_users"] is not None else active_users_count
        voting_progress_text = (
            f"• **Итоговый вердикт:** {status_label}\n"
            f"• **Финальная явка:** `{tallies['total_voters']} / {q_used}` (при `{act_used}` активных)\n"
            f"• **Вес «ЗА»:** `+{tallies['pos_weight']}` │ **Вес «ПРОТИВ»:** `-{tallies['neg_weight']}` │ **Воздержались:** `{tallies['neutral_count']}`\n"
            f"• **Итоговая поддержка:** **`{tallies['support_pct']:.2f}%`** (требовалось `> {tier_spec.win_percentage:.0f}%`)"
        )

    embed.add_field(name="📈 Ход и итоги голосования", value=voting_progress_text, inline=False)

    embed.add_field(
        name="🔍 Валидация по Стандарту ACI (п. 3.3, 6.2, 6.5)",
        value=(
            f"• **Состояние:** {aci_status_str}\n"
            f"• **Оценок Управленцев:** `{aci['count']}` (порог отмены: `<= -0.50`)"
        ),
        inline=False,
    )

    if poll["action_type"] != "NONE":
        embed.add_field(
            name="⚙️ Автоматическое исполнение АСУ (п. 6.6)",
            value=f"Сценарий: `{poll['action_type']}` │ Лог: `{poll['execution_log'] or 'Ожидает завершения таймера'}`",
            inline=False,
        )

    if poll["unexecutable_reason"]:
        embed.add_field(
            name="⚠️ Отказ от технической реализации (п. 6.10 / 6.10.1)",
            value=(
                f"**Зафиксировал Управленец <@{poll['unexecutable_by_id']}>:**\n"
                f"> {poll['unexecutable_reason']}"
            ),
            inline=False,
        )

    embed.set_footer(
        text=f"SHA-256 слепок (п. 6.4.1): {poll['proposal_hash']} │ Инициатор ID: {poll['initiator_id']}"
    )
    return embed


# =============================================================================
# DISCORD UI MODALS & VIEWS
# =============================================================================

class ACIReportModal(discord.ui.Modal, title="🚩 Жалоба на проверку по Стандарту ACI"):
    violation_details = discord.ui.TextInput(
        label="Причина несоответствия Стандарту ACI",
        style=discord.TextStyle.paragraph,
        placeholder="Укажите, почему текст нарушает Legitimacy (разделы I-II) или не соответствует выбранному Тиру...",
        required=True,
        min_length=10,
        max_length=700,
    )

    def __init__(self, bot: "HorizonBot", poll_id: int, jump_url: str):
        super().__init__()
        self.bot = bot
        self.poll_id = poll_id
        self.jump_url = jump_url

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not verify_guild(interaction):
            return

        await self.bot.log_audit_event(
            title=f"🚩 Запрос на проверку ACI │ Голосование #{self.poll_id}",
            description=(
                f"**Голосование:** [Перейти к карточке #{self.poll_id}]({self.jump_url})\n"
                f"**Заявитель:** {interaction.user.mention} (`{interaction.user.id}`)\n\n"
                f"**Суть жалобы по Стандарту ACI (п. 6.2):**\n"
                f"> {self.violation_details.value.strip()}\n\n"
                f"*Управленцы могут вынести вердикт кнопками `☑️ ACI (+)` / `🚫 ACI (-)` под голосованием.*"
            ),
            color=0xF1C40F,
        )
        await interaction.response.send_message(
            "✅ Ваш запрос на проверку по Стандарту ACI передан в канал аудита Управленцев.",
            ephemeral=True,
        )


class ACIValidationModal(discord.ui.Modal):
    reason = discord.ui.TextInput(
        label="Обоснование по Стандарту ACI (п. 4.3)",
        style=discord.TextStyle.paragraph,
        placeholder="Укажите конкретный раздел/пункт ACI или Регламента, подтверждающий или отклоняющий легитимность...",
        required=True,
        min_length=10,
        max_length=600,
    )

    def __init__(self, bot: "HorizonBot", poll_id: int, verdict: int):
        title_str = "Подтверждение ACI (+)" if verdict > 0 else "Отклонение по ACI (-)"
        super().__init__(title=f"{title_str} │ Голосование #{poll_id}")
        self.bot = bot
        self.poll_id = poll_id
        self.verdict = verdict

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not verify_guild(interaction) or not isinstance(interaction.user, discord.Member):
            return

        perm_key = "aci_confirm" if self.verdict > 0 else "aci_reject"
        if not has_command_permission(interaction.user, perm_key):
            await interaction.response.send_message(
                "❌ У вашей должности нет прав на выполнение данного действия валидации по ACI (п. 3.1, 3.3).",
                ephemeral=True,
            )
            return

        poll = self.bot.db.get_poll(self.poll_id)
        if not poll:
            await interaction.response.send_message("❌ Голосование не найдено.", ephemeral=True)
            return

        if poll["status"] not in ("COOLDOWN", "ACTIVE"):
            await interaction.response.send_message(
                "❌ Таймер голосования уже истёк. Валидация задним числом запрещена (п. 6.5).",
                ephemeral=True,
            )
            return

        if poll["aci_immune"]:
            await interaction.response.send_message(
                "🛡️ Данное шаблонное голосование обладает **полным иммунитетом от проверки и отклонения по ACI** (п. 6.8 Регламента).",
                ephemeral=True,
            )
            return

        if poll["target_user_id"] == interaction.user.id:
            await interaction.response.send_message(
                "🚫 **Конфликт интересов (п. 4.5 Регламента):** Вы затрагиваетесь данным голосованием и не вправе участвовать в его валидации по ACI.",
                ephemeral=True,
            )
            return

        if self.verdict > 0 and poll["initiator_id"] == interaction.user.id:
            await interaction.response.send_message(
                "🚫 **Конфликт интересов (п. 4.1 Регламента):** Инициатор не вправе самостоятельно подтверждать легитимность собственного голосования.",
                ephemeral=True,
            )
            return

        signed_weight, new_balance = self.bot.db.record_aci_validation(
            poll_id=self.poll_id,
            manager_id=interaction.user.id,
            verdict=self.verdict,
            reason=self.reason.value,
        )

        updated_poll = self.bot.db.get_poll(self.poll_id)
        await self.bot.refresh_poll_message(updated_poll)

        action_word = "ПОДТВЕРДИЛ легитимность" if self.verdict > 0 else "ОТКЛОНИЛ легитимность"
        await self.bot.log_audit_event(
            title=f"⚖️ Валидация ACI │ Голосование #{self.poll_id}",
            description=(
                f"**Управленец:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                f"**Решение:** {action_word}\n"
                f"**Вес оценки (с учётом анти-снайпинга):** `{signed_weight:+.4f}`\n"
                f"**Новый баланс ACI:** `{new_balance:+.4f}`\n"
                f"**Обоснование (п. 4.3):** {self.reason.value}"
            ),
            color=0x2ECC71 if self.verdict > 0 else 0xE74C3C,
        )

        await interaction.response.send_message(
            f"✅ Ваша оценка ACI записана с весом **`{signed_weight:+.4f}`**. Текущий баланс ACI: **`{new_balance:+.4f}`**.",
            ephemeral=True,
        )


class PersonalACIView(discord.ui.View):
    def __init__(self, bot: "HorizonBot", poll: sqlite3.Row, member: discord.Member):
        super().__init__(timeout=300)
        self.bot = bot
        self.poll_id = int(poll["poll_id"])

        poll_open = poll["status"] in ("COOLDOWN", "ACTIVE") and not bool(poll["aci_immune"])
        is_target = poll["target_user_id"] == member.id
        is_initiator = poll["initiator_id"] == member.id

        can_confirm = (
            poll_open
            and has_command_permission(member, "aci_confirm")
            and not is_target
            and not is_initiator
        )
        can_reject = (
            poll_open
            and has_command_permission(member, "aci_reject")
            and not is_target
        )

        self.aci_confirm_btn.disabled = not can_confirm
        self.aci_confirm_btn.style = (
            discord.ButtonStyle.success if can_confirm else discord.ButtonStyle.secondary
        )

        self.aci_reject_btn.disabled = not can_reject
        self.aci_reject_btn.style = (
            discord.ButtonStyle.danger if can_reject else discord.ButtonStyle.secondary
        )

    @discord.ui.button(label="☑️ ACI (+)", style=discord.ButtonStyle.success, row=0)
    async def aci_confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not isinstance(interaction.user, discord.Member) or not has_command_permission(interaction.user, "aci_confirm"):
            await interaction.response.send_message(
                "❌ У вашей должности нет прав на подтверждение голосований по ACI (`aci_confirm`).",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(ACIValidationModal(self.bot, self.poll_id, +1))

    @discord.ui.button(label="🚫 ACI (-)", style=discord.ButtonStyle.danger, row=0)
    async def aci_reject_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not isinstance(interaction.user, discord.Member) or not has_command_permission(interaction.user, "aci_reject"):
            await interaction.response.send_message(
                "❌ У вашей должности нет прав на отклонение голосований по ACI (`aci_reject`).",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(ACIValidationModal(self.bot, self.poll_id, -1))


class PersistentPollView(discord.ui.View):
    def __init__(self, bot: "HorizonBot", poll: Optional[sqlite3.Row] = None):
        super().__init__(timeout=None)
        self.bot = bot
        if poll is not None:
            self._apply_poll_state(poll)

    def _apply_poll_state(self, poll: sqlite3.Row) -> None:
        status = poll["status"]
        aci_immune = bool(poll["aci_immune"])
        vote_buttons = (
            self.vote_plus_2,
            self.vote_plus_1,
            self.vote_zero,
            self.vote_minus_1,
            self.vote_minus_2,
        )

        if status == "COOLDOWN":
            for btn in vote_buttons:
                btn.disabled = True
                btn.style = discord.ButtonStyle.secondary
            self.check_my_vote_and_aci.disabled = True
            self.check_my_vote_and_aci.style = discord.ButtonStyle.secondary
        elif status == "ACTIVE":
            for btn in vote_buttons:
                btn.disabled = False
            self.vote_plus_2.style = discord.ButtonStyle.success
            self.vote_plus_1.style = discord.ButtonStyle.success
            self.vote_zero.style = discord.ButtonStyle.secondary
            self.vote_minus_1.style = discord.ButtonStyle.danger
            self.vote_minus_2.style = discord.ButtonStyle.danger
            self.check_my_vote_and_aci.disabled = False
            self.check_my_vote_and_aci.style = discord.ButtonStyle.primary
        else:
            for btn in vote_buttons:
                btn.disabled = True
                btn.style = discord.ButtonStyle.secondary
            self.check_my_vote_and_aci.disabled = False
            self.check_my_vote_and_aci.style = discord.ButtonStyle.primary

        aci_disabled = aci_immune or status not in ("COOLDOWN", "ACTIVE")
        self.aci_report_btn.disabled = aci_disabled
        self.aci_report_btn.style = discord.ButtonStyle.secondary
        self.aci_panel_btn.disabled = aci_immune
        self.aci_panel_btn.style = discord.ButtonStyle.secondary

    async def _handle_likert_vote(self, interaction: discord.Interaction, weight: int, label: str) -> None:
        if not verify_guild(interaction) or not isinstance(interaction.user, discord.Member):
            return

        poll = self.bot.get_poll_by_message(interaction.message.id)
        if not poll:
            await interaction.response.send_message("❌ Голосование не найдено в базе АСУ.", ephemeral=True)
            return

        if is_twink_member(self.bot.db, interaction.user):
            await interaction.response.send_message(
                "🚫 Данный аккаунт имеет статус **технического дополнительного аккаунта без права голоса** (п. 2.6 Регламента).",
                ephemeral=True,
            )
            return

        if not is_full_member(interaction.user, self.bot.db):
            await interaction.response.send_message(
                "🚫 Голосовать могут только **Полноценные участники** (п. 1.1). "
                "Если вы находитесь в Изоляции, вы можете подать Публичную апелляцию через `/appeal` (п. 5.1).",
                ephemeral=True,
            )
            return

        now = time.time()
        if poll["status"] == "COOLDOWN" and now >= poll["voting_starts_at"]:
            self.bot.db.update_poll_status(poll["poll_id"], "ACTIVE")
            poll = self.bot.db.get_poll(poll["poll_id"])

        if poll["status"] == "COOLDOWN":
            unlock_ts = int(poll["voting_starts_at"])
            await interaction.response.send_message(
                f"🧊 **Идёт время на охлаждение!** Приём голосов заморожен для защиты от импульсивных решений "
                f"и откроется <t:{unlock_ts}:R> (<t:{unlock_ts}:T>).",
                ephemeral=True,
            )
            return

        if poll["status"] != "ACTIVE" or now >= poll["ends_at"]:
            await interaction.response.send_message(
                "⏹️ Приём голосов по данной инициативе уже завершён.",
                ephemeral=True,
            )
            return

        self.bot.db.cast_vote(poll["poll_id"], interaction.user.id, weight)
        updated_poll = self.bot.db.get_poll(poll["poll_id"])
        await self.bot.refresh_poll_message(updated_poll)

        await interaction.response.send_message(
            f"🗳️ Ваш голос **«{label}»** (вес `{weight:+d}`) учтён в АСУ и защищён п. 1.2 Регламента.\n"
            f"🔒 *Выбор скрыт от других участников для защиты от конформизма (п. 6.3).*",
            ephemeral=True,
        )

    @discord.ui.button(label="⏫", style=discord.ButtonStyle.success, custom_id="sva_rh:vote:plus2", row=0)
    async def vote_plus_2(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_likert_vote(interaction, 2, "⏫ Полностью согласен (+2)")

    @discord.ui.button(label="🔼", style=discord.ButtonStyle.success, custom_id="sva_rh:vote:plus1", row=0)
    async def vote_plus_1(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_likert_vote(interaction, 1, "🔼 Согласен (+1)")

    @discord.ui.button(label="⏺️", style=discord.ButtonStyle.secondary, custom_id="sva_rh:vote:zero", row=0)
    async def vote_zero(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_likert_vote(interaction, 0, "⏺️ Воздержусь / Нейтрально (+0)")

    @discord.ui.button(label="🔽", style=discord.ButtonStyle.danger, custom_id="sva_rh:vote:minus1", row=0)
    async def vote_minus_1(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_likert_vote(interaction, -1, "🔽 Не согласен (-1)")

    @discord.ui.button(label="⏬", style=discord.ButtonStyle.danger, custom_id="sva_rh:vote:minus2", row=0)
    async def vote_minus_2(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_likert_vote(interaction, -2, "⏬ Полностью не согласен (-2)")

    @discord.ui.button(label="🔍 Мой голос", style=discord.ButtonStyle.primary, custom_id="sva_rh:info", row=1)
    async def check_my_vote_and_aci(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        poll = self.bot.get_poll_by_message(interaction.message.id)
        if not poll:
            await interaction.response.send_message("❌ Голосование не найдено.", ephemeral=True)
            return

        user_vote = self.bot.db.get_user_vote(poll["poll_id"], interaction.user.id)
        vote_str = (
            f"Ваш текущий записанный вес голоса: **`{user_vote['weight']:+d}`**"
            if user_vote
            else "Вы ещё не голосовали в этом опросе."
        )

        aci = self.bot.db.get_aci_summary(poll["poll_id"])
        if not aci["records"]:
            aci_lines = "Оценок Управленцев по ACI пока нет (баланс `0.00`)."
        else:
            lines = []
            for r in aci["records"]:
                sign_icon = "✅" if r["verdict"] > 0 else "❌"
                lines.append(
                    f"{sign_icon} <@{r['manager_id']}> (`{r['effective_weight']:+.3f}`): *{r['reason']}*"
                )
            aci_lines = "\n".join(lines)

        await interaction.response.send_message(
            f"### 🔍 Личная справка по голосованию #{poll['poll_id']}\n"
            f"{vote_str}\n\n"
            f"**Журнал проверок по Стандарту ACI (п. 4.3, 6.2):**\n{aci_lines}",
            ephemeral=True,
        )

    @discord.ui.button(label="🚩 Жалоба ACI", style=discord.ButtonStyle.secondary, custom_id="sva_rh:aci:report", row=1)
    async def aci_report_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        poll = self.bot.get_poll_by_message(interaction.message.id)
        if not poll or poll["status"] not in ("COOLDOWN", "ACTIVE"):
            await interaction.response.send_message("❌ Голосование уже завершено.", ephemeral=True)
            return
        if poll["aci_immune"]:
            await interaction.response.send_message(
                "🛡️ Это специальное голосование по п. 6.8 Регламента — оно обладает полным иммунитетом к ACI.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(ACIReportModal(self.bot, poll["poll_id"], interaction.message.jump_url))

    @discord.ui.button(label="⚖️ Панель ACI", style=discord.ButtonStyle.secondary, custom_id="sva_rh:aci:panel", row=1)
    async def aci_panel_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not verify_guild(interaction) or not isinstance(interaction.user, discord.Member):
            return

        poll = self.bot.get_poll_by_message(interaction.message.id)
        if not poll:
            await interaction.response.send_message("❌ Голосование не найдено.", ephemeral=True)
            return

        aci = self.bot.db.get_aci_summary(poll["poll_id"])
        cur_weight = self.bot.db.compute_aci_manager_weight(poll["voting_starts_at"], poll["ends_at"])

        if not aci["records"]:
            aci_lines = "Оценок Управленцев по ACI пока нет (`0.00`)."
        else:
            lines = []
            for r in aci["records"]:
                sign_icon = "✅" if r["verdict"] > 0 else "❌"
                lines.append(
                    f"{sign_icon} <@{r['manager_id']}> (`{r['effective_weight']:+.3f}`): *{r['reason']}*"
                )
            aci_lines = "\n".join(lines)

        personal_view = PersonalACIView(self.bot, poll, interaction.user)
        await interaction.response.send_message(
            f"### ⚖️ Панель валидации ACI │ Голосование #{poll['poll_id']}\n"
            f"• **Текущий баланс ACI:** `{aci['balance']:+.4f}` (порог отмены: `<= -0.50`)\n"
            f"• **Вес новой оценки в данный момент:** `±{cur_weight:.4f}`\n\n"
            f"**Журнал оценок Управленцев (п. 4.3, 6.2):**\n{aci_lines}",
            view=personal_view,
            ephemeral=True,
        )


class NextQuestionnairePartView(discord.ui.View):
    def __init__(
        self,
        bot: "HorizonBot",
        candidate_id: int,
        template_data: Dict[str, Any],
        collected_answers: List[Tuple[str, str]],
        next_index: int,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.candidate_id = candidate_id
        self.template_data = template_data
        self.collected_answers = collected_answers
        self.next_index = next_index

    @discord.ui.button(label="➡️ Перейти к следующей части анкеты", style=discord.ButtonStyle.primary)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.candidate_id:
            await interaction.response.send_message("❌ Эту анкету заполняет другой участник.", ephemeral=True)
            return
        modal = ElectionQuestionnaireModal(
            bot=self.bot,
            candidate_id=self.candidate_id,
            template_data=self.template_data,
            collected_answers=self.collected_answers,
            start_index=self.next_index,
        )
        await interaction.response.send_modal(modal)


class ElectionQuestionnaireModal(discord.ui.Modal):
    def __init__(
        self,
        bot: "HorizonBot",
        candidate_id: int,
        template_data: Dict[str, Any],
        collected_answers: List[Tuple[str, str]],
        start_index: int = 0,
    ):
        questions = template_data.get("questions", [])
        total_parts = max(1, math.ceil(len(questions) / 5))
        current_part = (start_index // 5) + 1
        role_name = template_data.get("role_name", "Управленец")

        super().__init__(title=f"Анкета: {role_name} ({current_part}/{total_parts})"[:45])
        self.bot = bot
        self.candidate_id = candidate_id
        self.template_data = template_data
        self.collected_answers = list(collected_answers)
        self.start_index = start_index
        self.current_batch = questions[start_index : start_index + 5]
        self.inputs: List[discord.ui.TextInput] = []

        for q_text in self.current_batch:
            short_label = q_text if len(q_text) <= 45 else q_text[:42] + "..."
            inp = discord.ui.TextInput(
                label=short_label,
                style=discord.TextStyle.paragraph,
                placeholder=q_text[:100],
                required=True,
                max_length=500,
            )
            self.inputs.append(inp)
            self.add_item(inp)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.candidate_id or not interaction.guild:
            return

        for q_text, inp in zip(self.current_batch, self.inputs):
            self.collected_answers.append((q_text, inp.value.strip()))

        questions = self.template_data.get("questions", [])
        next_index = self.start_index + 5

        if next_index < len(questions):
            rem = len(questions) - next_index
            view = NextQuestionnairePartView(
                bot=self.bot,
                candidate_id=self.candidate_id,
                template_data=self.template_data,
                collected_answers=self.collected_answers,
                next_index=next_index,
            )
            await interaction.response.send_message(
                f"✅ Часть сохранена! Осталось ответить ещё на **{rem}** вопр. Нажмите кнопку ниже:",
                view=view,
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        role_id = int(self.template_data["role_id"])
        role_name = self.template_data.get("role_name", "Управленец")
        header_desc = self.template_data.get(
            "description", f"Квалификационная анкета на должность {role_name} (п. 7.1, 7.2 Регламента)"
        )
        qa_text = "\n\n".join(f"• {q}\nОтвет: {a}" for q, a in self.collected_answers)
        full_text = f"[{header_desc}]\nКандидат: {interaction.user.display_name} (ID: {interaction.user.id})\n\n{qa_text}"

        poll_id = self.bot.db.create_poll(
            initiator_id=interaction.user.id,
            category="Стафф",
            proposal_text=full_text,
            target_user_id=interaction.user.id,
            action_type="ELECT_MANAGER",
            action_payload={"user_id": interaction.user.id, "role_id": role_id},
        )
        msg = await self.bot.publish_poll_to_channel(
            interaction,
            poll_id,
            burger_blocks=[(f"Анкета кандидата {interaction.user.display_name}", full_text, [])]
            if len(full_text) > 1800
            else None,
        )
        await interaction.followup.send(
            f"🎉 Ваша анкета опубликована и запущено голосование **#{poll_id}** (`[Стафф]`): {msg.jump_url if msg else ''}",
            ephemeral=True,
        )


# =============================================================================
# BOT CONTROLLER
# =============================================================================

class HorizonBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.messages = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.db = HorizonDatabase(DB_PATH)

    async def setup_hook(self) -> None:
        ensure_sample_election_template()
        self.add_view(PersistentPollView(self))

        guild_obj = discord.Object(id=ALLOWED_GUILD_ID)
        self.tree.copy_global_to(guild=guild_obj)
        self.tree.clear_commands(guild=None)
        try:
            await self.tree.sync()
            await self.tree.sync(guild=guild_obj)
            logger.info(f"Global commands wiped; clean slash commands synced for Guild ID={ALLOWED_GUILD_ID}")
        except Exception as exc:
            logger.warning(f"Failed to sync commands for {ALLOWED_GUILD_ID}: {exc}")

        self.poll_lifecycle_task.start()

    async def on_ready(self) -> None:
        logger.info(f"ASU Horizon running as {self.user} (ID: {self.user.id})")
        for guild in self.guilds:
            if guild.id != ALLOWED_GUILD_ID:
                logger.warning(f"Leaving unauthorized guild '{guild.name}' ({guild.id}).")
                await guild.leave()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if guild.id != ALLOWED_GUILD_ID:
            logger.warning(f"Joined unauthorized guild {guild.id}. Leaving...")
            await guild.leave()

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild or message.guild.id != ALLOWED_GUILD_ID:
            return
        if message.content.strip() == "!sync" and isinstance(message.author, discord.Member):
            guild_obj = discord.Object(id=ALLOWED_GUILD_ID)
            self.tree.copy_global_to(guild=guild_obj)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            synced = await self.tree.sync(guild=guild_obj)
            await message.reply(
                f"✅ Синхронизировано `{len(synced)}` актуальных слэш-команд для этой Площадки. "
                f"Нажмите **`Ctrl + R`** в Discord, чтобы сбросить старый кэш (`text_id`)."
            )
            return
        if isinstance(message.author, discord.Member) and is_full_member(message.author, self.db):
            self.db.record_message_impulse(message.author.id)

    def get_poll_by_message(self, message_id: int) -> Optional[sqlite3.Row]:
        with self.db._get_conn() as conn:
            return conn.execute("SELECT * FROM polls WHERE message_id = ?", (message_id,)).fetchone()

    async def fetch_message_across_guild(
        self, guild: discord.Guild, msg_id: int, preferred_channel: Optional[discord.abc.Messageable] = None
    ) -> Optional[discord.Message]:
        if preferred_channel and hasattr(preferred_channel, "fetch_message"):
            try:
                return await preferred_channel.fetch_message(msg_id)
            except Exception:
                pass

        me = guild.me
        channels = list(guild.text_channels) + list(guild.threads)
        for ch in channels:
            if preferred_channel and getattr(ch, "id", None) == getattr(preferred_channel, "id", None):
                continue
            if me:
                perms = ch.permissions_for(me)
                if not (perms.view_channel and perms.read_message_history):
                    continue
            try:
                return await ch.fetch_message(msg_id)
            except Exception:
                continue
        return None

    async def extract_message_payload(
        self, target_message: discord.Message
    ) -> Tuple[str, List[Dict[str, str]], List[Tuple[str, bytes]]]:
        base_text = (target_message.content or "").strip()
        attachments_meta: List[Dict[str, str]] = []
        raw_files: List[Tuple[str, bytes]] = []
        extracted_txt_blocks: List[str] = []

        for att in target_message.attachments:
            if att.size > MAX_ATTACHMENT_BYTES:
                attachments_meta.append({"filename": att.filename, "sha256": "превышен_8МБ"})
                continue
            try:
                data_bytes = await att.read()
                f_hash = hashlib.sha256(data_bytes).hexdigest()[:12]
                attachments_meta.append({"filename": att.filename, "sha256": f_hash})
                raw_files.append((att.filename, data_bytes))

                if att.filename.lower().endswith((".txt", ".md")):
                    try:
                        decoded = data_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        decoded = data_bytes.decode("cp1251", errors="replace")
                    if decoded.strip():
                        extracted_txt_blocks.append(f"[Файл {att.filename}]:\n{decoded.strip()}")
            except Exception as exc:
                logger.warning(f"Attachment read error ({att.filename}): {exc}")

        if extracted_txt_blocks:
            combined_txt = "\n\n".join(extracted_txt_blocks)
            final_text = f"{base_text}\n\n{combined_txt}".strip() if base_text else combined_txt
        else:
            final_text = base_text or "[Медиафайл / Вложение]"

        return final_text, attachments_meta, raw_files

    async def refresh_poll_message(self, poll: sqlite3.Row, disable_buttons: bool = False) -> None:
        if not poll["channel_id"] or not poll["message_id"]:
            return
        guild = self.get_guild(ALLOWED_GUILD_ID)
        if not guild:
            return
        channel = guild.get_channel(poll["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            msg = await channel.fetch_message(poll["message_id"])
            embed = build_poll_embed(self.db, poll, guild)
            view = PersistentPollView(self, poll)
            if disable_buttons:
                for item in view.children:
                    if isinstance(item, discord.ui.Button) and item.custom_id != "sva_rh:info":
                        item.disabled = True
                        item.style = discord.ButtonStyle.secondary
            await msg.edit(embed=embed, view=view)
        except Exception as exc:
            logger.debug(f"Could not update poll message #{poll['poll_id']}: {exc}")

    async def log_audit_event(self, title: str, description: str, color: int = 0x3498DB) -> None:
        guild = self.get_guild(ALLOWED_GUILD_ID)
        if not guild:
            return
        channel = guild.get_channel(AUDIT_LOG_CHANNEL_ID) or guild.get_channel(VOTING_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            embed = discord.Embed(
                title=title,
                description=description,
                color=color,
                timestamp=discord.utils.utcnow(),
            )
            embed.set_footer(text="АСУ «Горизонт» • Журнал прозрачности (п. 4.3 Регламента)")
            try:
                await channel.send(embed=embed)
            except Exception as exc:
                logger.warning(f"Audit log send failed: {exc}")

    # Publishes optional multi-message "burger" blocks first, followed by the interactive voting card
    async def publish_poll_to_channel(
        self,
        interaction: discord.Interaction,
        poll_id: int,
        burger_blocks: Optional[List[Tuple[str, str, List[Tuple[str, bytes]]]]] = None,
    ) -> Optional[discord.Message]:
        poll = self.db.get_poll(poll_id)
        guild = self.get_guild(ALLOWED_GUILD_ID) or interaction.guild
        if not poll or not guild:
            return None

        target_channel = guild.get_channel(VOTING_CHANNEL_ID)
        if not isinstance(target_channel, discord.TextChannel):
            target_channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None

        if not target_channel:
            return None

        burger_jump_urls: List[str] = []
        last_sent_msg: Optional[discord.Message] = None

        if burger_blocks:
            total_blocks = len(burger_blocks)
            for idx, (block_title, block_text, raw_files) in enumerate(burger_blocks, 1):
                chunks = split_text_into_chunks(block_text, max_chars=1850) or ["[Вложение]"]
                for c_idx, chunk in enumerate(chunks, 1):
                    part_suffix = f" (ч. {c_idx}/{len(chunks)})" if len(chunks) > 1 else ""
                    b_embed = discord.Embed(
                        title=f"📄 Голосование #{poll_id} │ Блок {idx}/{total_blocks}: {block_title}{part_suffix}",
                        description=f"```\n{chunk}\n```",
                        color=0x34495E,
                    )
                    b_embed.set_footer(text=f"SHA-256 слепок (п. 6.4.1): {poll['proposal_hash']}")
                    files_to_send = (
                        [discord.File(io.BytesIO(b_data), filename=fname) for fname, b_data in raw_files]
                        if c_idx == 1 and raw_files
                        else None
                    )
                    if files_to_send:
                        sent_b = await target_channel.send(embed=b_embed, files=files_to_send)
                    else:
                        sent_b = await target_channel.send(embed=b_embed)
                    burger_jump_urls.append(sent_b.jump_url)
                    last_sent_msg = sent_b

        self.db.bind_poll_message(poll_id, target_channel.id, 0, burger_links=burger_jump_urls)
        poll = self.db.get_poll(poll_id)

        embed = build_poll_embed(self.db, poll, guild)
        view = PersistentPollView(self, poll)
        if last_sent_msg:
            msg = await target_channel.send(embed=embed, view=view, reference=last_sent_msg)
        else:
            msg = await target_channel.send(embed=embed, view=view)

        self.db.bind_poll_message(poll_id, target_channel.id, msg.id, burger_links=burger_jump_urls)
        return msg

    async def restore_member_from_isolation(
        self, guild: discord.Guild, member: discord.Member, reason: str
    ) -> None:
        restricted_role = guild.get_role(RESTRICTED_ROLE_ID)
        full_role = guild.get_role(FULL_MEMBER_ROLE_ID)

        roles_to_restore: List[discord.Role] = []
        if full_role:
            roles_to_restore.append(full_role)

        with self.db._get_conn() as conn:
            row = conn.execute(
                "SELECT saved_roles FROM isolations WHERE user_id = ?", (member.id,)
            ).fetchone()
            if row and row["saved_roles"]:
                try:
                    for rid in json.loads(row["saved_roles"]):
                        r_obj = guild.get_role(int(rid))
                        if r_obj and r_obj not in roles_to_restore:
                            roles_to_restore.append(r_obj)
                except Exception:
                    pass
            conn.execute("DELETE FROM isolations WHERE user_id = ?", (member.id,))
            conn.commit()

        if restricted_role and restricted_role in member.roles:
            try:
                await member.remove_roles(restricted_role, reason=reason)
            except Exception:
                pass

        missing_roles = [r for r in roles_to_restore if r not in member.roles]
        if missing_roles:
            try:
                await member.add_roles(*missing_roles, reason=reason)
            except Exception:
                pass

        try:
            await member.timeout(None, reason=reason)
        except Exception:
            pass

        if member.voice and member.voice.mute:
            try:
                await member.edit(mute=False, reason=reason)
            except Exception:
                pass

    async def execute_passed_poll_action(self, poll: sqlite3.Row) -> str:
        action_type = poll["action_type"]
        if action_type == "NONE":
            return "Текстовая инициатива принята (подлежит исполнению по п. 3.4 / 4.4)"

        guild = self.get_guild(ALLOWED_GUILD_ID)
        if not guild:
            return "Ошибка: Сервер Площадки недоступен"

        try:
            payload = json.loads(poll["action_payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}

        try:
            if action_type == "IMPEACH_MANAGER":
                target_id = poll["target_user_id"] or payload.get("user_id")
                member = guild.get_member(target_id)
                mgr_ids = get_all_manager_role_ids()
                removed_names: List[str] = []

                if member:
                    roles_to_strip = [r for r in member.roles if r.id in mgr_ids]
                    if roles_to_strip:
                        await member.remove_roles(
                            *roles_to_strip,
                            reason=f"АСУ SVA V. RH: Импичмент по голосованию #{poll['poll_id']} (п. 6.8 / 7.3)",
                        )
                        removed_names.extend(r.name for r in roles_to_strip)

                with self.db._get_conn() as conn:
                    iso_row = conn.execute(
                        "SELECT saved_roles FROM isolations WHERE user_id = ?", (target_id,)
                    ).fetchone()
                    if iso_row and iso_row["saved_roles"]:
                        saved_list = json.loads(iso_row["saved_roles"])
                        filtered_list = [rid for rid in saved_list if int(rid) not in mgr_ids]
                        if len(filtered_list) != len(saved_list):
                            conn.execute(
                                "UPDATE isolations SET saved_roles = ? WHERE user_id = ?",
                                (json.dumps(filtered_list), target_id),
                            )
                            conn.commit()
                            removed_names.append("сохранённые в Изоляции роли")

                if removed_names:
                    return f"С участника ID {target_id} автоматически сняты полномочия Управленца ({', '.join(removed_names)})."
                return "На момент завершения у пользователя уже отсутствовали роли Управленца."

            if action_type == "ELECT_MANAGER":
                target_id = poll["target_user_id"] or payload.get("user_id")
                default_mgr_role = next(iter(MANAGER_ROLES.keys()), 0)
                role_id = int(payload.get("role_id") or default_mgr_role)
                member = guild.get_member(target_id)
                manager_role = guild.get_role(role_id)
                if member and manager_role:
                    await member.add_roles(
                        manager_role,
                        reason=f"АСУ SVA V. RH: Избрание по итогам голосования #{poll['poll_id']} (Раздел 7)",
                    )
                    return f"Участник {member.display_name} ({member.id}) наделён должностью @{manager_role.name}."
                return "Кандидат или целевая роль Управленца не найдены (п. 6.10)."

            if action_type == "LIFT_ISOLATION":
                target_id = poll["target_user_id"] or payload.get("user_id")
                member = guild.get_member(target_id)
                if member:
                    await self.restore_member_from_isolation(
                        guild,
                        member,
                        reason=f"АСУ SVA V. RH: Удовлетворена Публичная апелляция #{poll['poll_id']} (п. 5.1)",
                    )
                    return f"С участника {member.display_name} снята Изоляция и возвращены все сохранённые роли."
                return "Участник не найден на сервере."

            if action_type == "APPLY_ISOLATION":
                target_id = poll["target_user_id"] or payload.get("user_id")
                member = guild.get_member(target_id)
                restricted_role = guild.get_role(RESTRICTED_ROLE_ID)
                if member and restricted_role:
                    roles_to_remove = [
                        r for r in member.roles if not r.is_default() and not r.managed and r.id != RESTRICTED_ROLE_ID
                    ]
                    saved_ids = [r.id for r in roles_to_remove]
                    if roles_to_remove:
                        await member.remove_roles(*roles_to_remove, reason=f"АСУ SVA V. RH: Голосование #{poll['poll_id']}")
                    await member.add_roles(restricted_role, reason=f"АСУ SVA V. RH: Голосование #{poll['poll_id']}")
                    with self.db._get_conn() as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO isolations (user_id, manager_id, reason, isolated_at, until_ts, saved_roles) VALUES (?, ?, ?, ?, 0, ?)",
                            (member.id, self.user.id, f"Решение голосования #{poll['poll_id']}", time.time(), json.dumps(saved_ids)),
                        )
                        conn.commit()
                    return f"Участник {member.display_name} переведён в статус Ограниченного участника."
                return "Участник или роль Изоляции не найдены."

            if action_type == "CREATE_ROLE":
                raw_names = payload.get("role_name", "Новая роль")
                role_names = [n.strip() for n in raw_names.split(",") if n.strip()] or ["Новая роль"]
                color_hex = payload.get("color_hex", "#95A5A6").lstrip("#")
                try:
                    color_val = int(color_hex, 16)
                except ValueError:
                    color_val = 0x95A5A6
                created_mentions: List[str] = []
                for r_name in role_names:
                    new_role = await guild.create_role(
                        name=r_name,
                        permissions=discord.Permissions.none(),
                        colour=discord.Colour(color_val),
                        mentionable=True,
                        reason=f"АСУ SVA V. RH: Голосование #{poll['poll_id']} [Роли]",
                    )
                    created_mentions.append(f"@{new_role.name} ({new_role.id})")
                return f"Создано ролей ({len(created_mentions)}): {', '.join(created_mentions)}."

            if action_type == "CREATE_CHANNEL":
                ch_name = payload.get("channel_name", "новый-канал")
                ch_kind = payload.get("channel_kind", "text")
                if ch_kind == "voice":
                    ch = await guild.create_voice_channel(
                        name=ch_name, reason=f"АСУ SVA V. RH: Голосование #{poll['poll_id']} [Каналы]"
                    )
                else:
                    ch = await guild.create_text_channel(
                        name=ch_name, reason=f"АСУ SVA V. RH: Голосование #{poll['poll_id']} [Каналы]"
                    )
                return f"Создан канал #{ch.name} (ID: {ch.id})."

            if action_type == "RENAME_GUILD":
                new_name = payload.get("new_name")
                if new_name:
                    await guild.edit(
                        name=new_name,
                        reason=f"АСУ SVA V. RH: Голосование #{poll['poll_id']}",
                    )
                    return f"Отображаемое название Площадки изменено на «{new_name}»."

        except discord.Forbidden:
            return "Неисполнимо (п. 6.10): у бота недостаточно технических прав Discord для операции."
        except Exception as exc:
            return f"Техническая ошибка исполнения (п. 6.10): {exc}"

        return "Исполнено."

    @tasks.loop(seconds=15)
    async def poll_lifecycle_task(self) -> None:
        now = time.time()
        open_polls = self.db.get_open_polls()

        for poll in open_polls:
            poll_id = poll["poll_id"]

            if poll["status"] == "COOLDOWN" and now >= poll["voting_starts_at"]:
                self.db.update_poll_status(poll_id, "ACTIVE")
                updated = self.db.get_poll(poll_id)
                await self.refresh_poll_message(updated)
                poll = updated

            if poll["status"] in ("COOLDOWN", "ACTIVE") and now >= poll["ends_at"]:
                await self.finalize_poll(poll)

        guild = self.get_guild(ALLOWED_GUILD_ID)
        if not guild:
            return

        with self.db._get_conn() as conn:
            expired_isos = conn.execute(
                "SELECT user_id FROM isolations WHERE until_ts > 0 AND until_ts <= ?", (now,)
            ).fetchall()
            active_isos = conn.execute(
                "SELECT user_id, until_ts, reason FROM isolations WHERE until_ts = 0 OR until_ts > ?", (now,)
            ).fetchall()
            due_appointments = conn.execute(
                "SELECT * FROM pending_appointments WHERE executed = 0 AND execute_at <= ?", (now,)
            ).fetchall()

        for r in expired_isos:
            uid = int(r["user_id"])
            member = guild.get_member(uid)
            if member:
                await self.restore_member_from_isolation(
                    guild, member, reason="АСУ: Истёк срок временной Изоляции (п. 5.2 Регламента)"
                )
                await self.log_audit_event(
                    title="🕊️ Автоматическое окончание Изоляции (п. 5.2)",
                    description=f"С участника {member.mention} (`{member.id}`) снята Изоляция по истечении установленного срока.",
                    color=0x2ECC71,
                )
            else:
                with self.db._get_conn() as conn:
                    conn.execute("DELETE FROM isolations WHERE user_id = ?", (uid,))
                    conn.commit()

        for r in active_isos:
            uid = int(r["user_id"])
            until_ts = float(r["until_ts"])
            member = guild.get_member(uid)
            if not member:
                continue
            rem_sec = int(until_ts - now) if until_ts > 0 else DISCORD_MAX_TIMEOUT_SECONDS
            target_timeout = min(rem_sec, DISCORD_MAX_TIMEOUT_SECONDS)
            if target_timeout > 3600:
                needs_renewal = (
                    member.timed_out_until is None
                    or (member.timed_out_until - discord.utils.utcnow()).total_seconds() < 3600
                )
                if needs_renewal:
                    try:
                        until_dt = discord.utils.utcnow() + datetime.timedelta(seconds=target_timeout)
                        await member.timeout(until_dt, reason=f"АСУ: Поддержание мута Изоляции ({r['reason']})")
                    except Exception:
                        pass

        for app in due_appointments:
            with self.db._get_conn() as conn:
                conn.execute("UPDATE pending_appointments SET executed = 1 WHERE id = ?", (app["id"],))
                conn.commit()

            initiator = guild.get_member(int(app["initiator_id"]))
            target_member = guild.get_member(int(app["target_user_id"]))
            role = guild.get_role(int(app["role_id"]))

            if not initiator or not has_command_permission(initiator, "appoint") or not target_member or not role:
                continue
            if is_restricted_member(target_member):
                continue

            try:
                await target_member.add_roles(
                    role, reason=f"АСУ /appoint (Инициатор {initiator.id}): {app['reason']}"
                )
                await self.log_audit_event(
                    title="🎖️ Отложенное назначение Управленца исполнено (/appoint)",
                    description=(
                        f"**Назначен:** {target_member.mention} (`{target_member.id}`)\n"
                        f"**Должность:** {role.mention}\n"
                        f"**Инициатор:** {initiator.mention}\n"
                        f"**Основание (п. 4.3):** {app['reason']}"
                    ),
                    color=0x2ECC71,
                )
            except Exception as exc:
                logger.warning(f"Failed to execute /appoint #{app['id']}: {exc}")

    @poll_lifecycle_task.before_loop
    async def before_poll_lifecycle(self) -> None:
        await self.wait_until_ready()

    async def finalize_poll(self, poll: sqlite3.Row) -> None:
        poll_id = poll["poll_id"]
        tier_spec = TIERS[poll["tier"]]
        tallies = self.db.calculate_poll_tallies(poll_id)
        aci = self.db.get_aci_summary(poll_id)
        activity = self.db.calculate_active_users_stats()

        active_users_count = activity["active_count"]
        required_quorum = math.ceil(active_users_count * (tier_spec.quorum_percentage / 100.0))

        exec_log = ""
        if not poll["aci_immune"] and aci["balance"] <= -0.5:
            final_status = "INVALIDATED_ACI"
            exec_log = f"Аннулировано по ACI (баланс {aci['balance']:+.2f} <= -0.50)"
        elif tallies["total_voters"] < required_quorum or tallies["total_voters"] == 0:
            final_status = "FAILED_QUORUM"
            exec_log = f"Кворум не набран ({tallies['total_voters']} из {required_quorum})"
        elif tallies["support_pct"] <= tier_spec.win_percentage:
            final_status = "FAILED_SUPPORT"
            exec_log = (
                f"Поддержка {tallies['support_pct']:.2f}% не превысила порог > {tier_spec.win_percentage:.0f}%"
            )
        else:
            final_status = "PASSED"
            exec_log = await self.execute_passed_poll_action(poll)

        with self.db._get_conn() as conn:
            conn.execute(
                """
                UPDATE polls SET
                    status = ?,
                    final_quorum_active_users = ?,
                    final_quorum_required = ?,
                    final_voters_count = ?,
                    final_support_pct = ?,
                    final_aci_balance = ?,
                    execution_log = ?
                WHERE poll_id = ?
                """,
                (
                    final_status,
                    active_users_count,
                    required_quorum,
                    tallies["total_voters"],
                    tallies["support_pct"],
                    aci["balance"],
                    exec_log,
                    poll_id,
                ),
            )
            conn.commit()

        final_poll = self.db.get_poll(poll_id)
        await self.refresh_poll_message(final_poll, disable_buttons=True)

        await self.log_audit_event(
            title=f"🏁 Итоги голосования #{poll_id} │ [{poll['category']}]",
            description=(
                f"**Вердикт АСУ:** `{final_status}`\n"
                f"**Явка:** `{tallies['total_voters']} / {required_quorum}` (Активных: `{active_users_count}`)\n"
                f"**Поддержка:** `{tallies['support_pct']:.2f}%` (За: `+{tallies['pos_weight']}` / Против: `-{tallies['neg_weight']}`)\n"
                f"**Баланс ACI:** `{aci['balance']:+.2f}`\n"
                f"**Исполнение:** {exec_log}"
            ),
            color=0x2ECC71 if final_status == "PASSED" else 0xE74C3C,
        )


# =============================================================================
# SLASH COMMANDS
# =============================================================================

bot = HorizonBot()

CATEGORY_CHOICES = [
    app_commands.Choice(name=f"[Тир {tier}] {cat}", value=cat)
    for cat, (tier, _) in CATEGORIES.items()
]


@bot.tree.command(
    name="poll",
    description="Запустить голосование SVA V. RH по 1 или нескольким ID/ссылкам сообщений Discord («бургер»)",
)
@app_commands.describe(
    category="Категория инициативы (АСУ автоматически определит Тир и параметры)",
    message_ids="Одно или несколько ID/ссылок на сообщения Discord через пробел (соберёт «бургер» из сообщений)",
)
@app_commands.choices(category=CATEGORY_CHOICES)
async def cmd_poll_by_id(
    interaction: discord.Interaction,
    category: app_commands.Choice[str],
    message_ids: str,
) -> None:
    if not verify_guild(interaction) or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ Команда доступна только на основной Площадке.", ephemeral=True)
        return

    if not is_full_member(interaction.user, bot.db):
        await interaction.response.send_message(
            "🚫 Запускать общие голосования могут только **Полноценные участники** (п. 1.1).",
            ephemeral=True,
        )
        return

    snowflakes = SNOWFLAKE_RE.findall(message_ids)
    if not snowflakes:
        await interaction.response.send_message(
            "❌ Не найдено корректных ID или ссылок на сообщения в параметре `message_ids`.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    burger_blocks: List[Tuple[str, str, List[Tuple[str, bytes]]]] = []
    combined_texts: List[str] = []
    all_att_meta: List[Dict[str, str]] = []

    for sf in snowflakes:
        msg_obj = await bot.fetch_message_across_guild(
            interaction.guild, int(sf), preferred_channel=interaction.channel
        )
        if not msg_obj:
            await interaction.followup.send(f"❌ Сообщение с ID `{sf}` не найдено на Площадке.")
            return
        m_text, m_att_meta, m_raw_files = await bot.extract_message_payload(msg_obj)
        burger_blocks.append((f"От {msg_obj.author.display_name}", m_text, m_raw_files))
        combined_texts.append(m_text)
        all_att_meta.extend(m_att_meta)

    full_proposal_text = "\n\n---\n\n".join(combined_texts)
    att_hashes = ",".join(f"{a['filename']}:{a['sha256']}" for a in all_att_meta)
    combined_hash = hashlib.sha256(f"{full_proposal_text}|{att_hashes}".encode("utf-8")).hexdigest()[:16]

    use_burger = len(burger_blocks) > 1 or bool(all_att_meta) or len(full_proposal_text) > 1800

    poll_id = bot.db.create_poll(
        initiator_id=interaction.user.id,
        category=category.value,
        proposal_text=full_proposal_text,
        attachments_meta=all_att_meta,
        custom_hash=combined_hash,
    )
    msg = await bot.publish_poll_to_channel(
        interaction, poll_id, burger_blocks=burger_blocks if use_burger else None
    )
    tier_id, _ = CATEGORIES[category.value]
    spec = TIERS[tier_id]

    await interaction.followup.send(
        f"🍔 **Голосование #{poll_id}** (сообщений в связке: `{len(burger_blocks)}`) запущено!\n"
        f"• **Категория:** `[{category.value}]` ➔ **{spec.name}**\n"
        f"• **Ссылка:** {msg.jump_url if msg else 'опубликовано'}"
    )


@bot.tree.command(
    name="poll_impeach",
    description="Пункт 6.8: Специальное шаблонное голосование за снятие Управленца в обход ACI (полный иммунитет)",
)
@app_commands.describe(manager="Действующий Управленец, вопрос о снятии прав с которого выносится на голосование")
async def cmd_poll_impeach(interaction: discord.Interaction, manager: discord.Member) -> None:
    if not verify_guild(interaction) or not isinstance(interaction.user, discord.Member):
        return

    if not is_full_member(interaction.user, bot.db):
        await interaction.response.send_message(
            "🚫 Инициировать процедуру п. 6.8 могут только **Полноценные участники**.",
            ephemeral=True,
        )
        return

    mgr_ids = get_all_manager_role_ids()
    has_active_mgr = any(r.id in mgr_ids for r in manager.roles)
    has_saved_mgr = False
    with bot.db._get_conn() as conn:
        iso_row = conn.execute("SELECT saved_roles FROM isolations WHERE user_id = ?", (manager.id,)).fetchone()
        if iso_row and iso_row["saved_roles"]:
            has_saved_mgr = any(int(rid) in mgr_ids for rid in json.loads(iso_row["saved_roles"]))

    if not has_active_mgr and not has_saved_mgr:
        await interaction.response.send_message(
            f"❌ Пользователь {manager.mention} не обладает управленческими ролями из `MANAGER_ROLES`.",
            ephemeral=True,
        )
        return

    fixed_template_text = (
        f"[ШАБЛОННОЕ ГОЛОСОВАНИЕ АСУ ПО П. 6.8 РЕГЛАМЕНТА]\n"
        f"Предмет голосования: Досрочное прекращение полномочий и снятие всех управленческих ролей "
        f"с участника {manager.display_name} (Discord ID: {manager.id}).\n"
        f"Статус защиты: Полный системный иммунитет от отклонения по Стандарту ACI."
    )

    poll_id = bot.db.create_poll(
        initiator_id=interaction.user.id,
        category="Стафф",
        proposal_text=fixed_template_text,
        aci_immune=True,
        target_user_id=manager.id,
        action_type="IMPEACH_MANAGER",
        action_payload={"user_id": manager.id},
    )
    msg = await bot.publish_poll_to_channel(interaction, poll_id)

    await interaction.response.send_message(
        f"🛡️ **Специальное голосование #{poll_id} (п. 6.8)** за снятие Управленца {manager.mention} запущено!\n"
        f"Голосование обладает полным системным иммунитетом к ACI: {msg.jump_url if msg else ''}",
        ephemeral=False,
    )


@bot.tree.command(
    name="pollelection",
    description="Пункт 7.1 и 7.2: Подать заявку и пройти анкетирование на должность Управленца",
)
@app_commands.describe(role="Управленческая роль, на которую вы выдвигаетесь")
async def cmd_pollelection(interaction: discord.Interaction, role: discord.Role) -> None:
    if not verify_guild(interaction) or not isinstance(interaction.user, discord.Member):
        return

    if not is_full_member(interaction.user, bot.db):
        await interaction.response.send_message(
            "🚫 Выдвигаться на должности могут только **Полноценные участники** (п. 1.3).",
            ephemeral=True,
        )
        return

    ensure_sample_election_template()
    matched_template = None
    for fname in os.listdir(ELECTIONS_DIR):
        if not fname.endswith(".json") or fname.lower() == "example.json":
            continue
        try:
            with open(os.path.join(ELECTIONS_DIR, fname), "r", encoding="utf-8") as f:
                tpl = json.load(f)
            if int(tpl.get("role_id", 0)) == role.id or str(tpl.get("role_name", "")).lower() == role.name.lower():
                matched_template = tpl
                matched_template["role_id"] = role.id
                matched_template["role_name"] = role.name
                break
        except Exception:
            continue

    if not matched_template or not matched_template.get("questions"):
        await interaction.response.send_message(
            f"ℹ️ В папке `{ELECTIONS_DIR}` не найден активный JSON-шаблон анкеты для роли {role.mention}.\n"
            f"Вы можете выдвинуть кандидата напрямую в обход анкет через `/poll_action` (`Избрать кандидата в Управленцы`, п. 7.1.1).",
            ephemeral=True,
        )
        return

    modal = ElectionQuestionnaireModal(
        bot=bot,
        candidate_id=interaction.user.id,
        template_data=matched_template,
        collected_answers=[],
        start_index=0,
    )
    await interaction.response.send_modal(modal)


@bot.tree.command(
    name="appeal",
    description="Пункт 5.1: Подать Публичную апелляцию для снятия Изоляции и восстановления статуса",
)
@app_commands.describe(justification="Ваше объяснение ситуации и аргументы в пользу восстановления статуса")
async def cmd_public_appeal(interaction: discord.Interaction, justification: str) -> None:
    guild = bot.get_guild(ALLOWED_GUILD_ID)
    if not guild:
        await interaction.response.send_message("❌ Площадка недоступна.", ephemeral=True)
        return

    member = guild.get_member(interaction.user.id)
    if not member:
        await interaction.response.send_message("❌ Вы не найдены на Площадке.", ephemeral=True)
        return

    if not is_restricted_member(member):
        await interaction.response.send_message(
            "ℹ️ У вас нет статуса Ограниченного / Изолированного участника — ваши права не ограничены.",
            ephemeral=True,
        )
        return

    appeal_text = (
        f"[ПУБЛИЧНАЯ АПЕЛЛЯЦИЯ ПО П. 5.1 РЕГЛАМЕНТА]\n"
        f"Апеллянт: {member.display_name} (ID: {member.id})\n"
        f"Требуемое действие АСУ: Снятие статуса Ограниченного участника (Изоляции) и восстановление "
        f"всех прав Полноценного участника.\n\n"
        f"Аргументация апеллянта:\n{justification.strip()}"
    )

    poll_id = bot.db.create_poll(
        initiator_id=member.id,
        category="Правосудие",
        proposal_text=appeal_text,
        aci_immune=False,
        target_user_id=member.id,
        action_type="LIFT_ISOLATION",
        action_payload={"user_id": member.id},
    )
    msg = await bot.publish_poll_to_channel(interaction, poll_id)

    await interaction.response.send_message(
        f"⚖️ Ваша **Публичная апелляция #{poll_id}** (Тир III `[Правосудие]`) опубликована: "
        f"{msg.jump_url if msg else 'в канале голосований'}.",
        ephemeral=True,
    )


@bot.tree.command(
    name="poll_action",
    description="Пункт 6.6 и 7.1.1: Запустить шаблонное голосование с автоисполнением действия ботом АСУ",
)
@app_commands.describe(
    action="Тип автоматического действия, которое бот выполнит сам при победе голосования",
    rationale="Обоснование инициативы согласно стандарту ACI",
    target_user="Целевой участник (для избрания в Стафф или применения Изоляции)",
    target_role="Целевая роль Управленца из MANAGER_ROLES (для прямого избрания по п. 7.1.1)",
    object_name="Название создаваемой роли, канала или нового имени Площадки",
)
@app_commands.choices(
    action=[
        app_commands.Choice(name="[Тир II • Роли] Создать пользовательскую роль", value="CREATE_ROLE"),
        app_commands.Choice(name="[Тир II • Каналы] Создать текстовый канал", value="CREATE_TEXT_CHANNEL"),
        app_commands.Choice(name="[Тир II • Каналы] Создать голосовой канал", value="CREATE_VOICE_CHANNEL"),
        app_commands.Choice(name="[Тир III • Стафф] Избрать кандидата в Управленцы (п. 7.1.1)", value="ELECT_MANAGER"),
        app_commands.Choice(name="[Тир III • Правосудие] Перевести участника в Изоляцию", value="APPLY_ISOLATION"),
        app_commands.Choice(name="[Тир III • Локальное лого] Переименовать сервер Площадки", value="RENAME_GUILD"),
    ]
)
async def cmd_poll_action(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    rationale: str,
    target_user: Optional[discord.Member] = None,
    target_role: Optional[discord.Role] = None,
    object_name: Optional[str] = None,
) -> None:
    if not verify_guild(interaction) or not isinstance(interaction.user, discord.Member):
        return

    if not is_full_member(interaction.user, bot.db):
        await interaction.response.send_message(
            "🚫 Запускать шаблонные голосования могут только **Полноценные участники**.",
            ephemeral=True,
        )
        return

    act_val = action.value
    payload: Dict[str, Any] = {}
    target_uid = target_user.id if target_user else None

    if act_val == "CREATE_ROLE":
        if not object_name:
            await interaction.response.send_message("❌ Укажите `object_name` (название роли).", ephemeral=True)
            return
        category = "Роли"
        db_action = "CREATE_ROLE"
        payload = {"role_name": object_name}
        header = f"[АВТО-ШАБЛОН п. 6.6]: Создать пользовательскую роль «{object_name}» без прав Управленца."

    elif act_val in ("CREATE_TEXT_CHANNEL", "CREATE_VOICE_CHANNEL"):
        if not object_name:
            await interaction.response.send_message("❌ Укажите `object_name` (название канала).", ephemeral=True)
            return
        category = "Каналы"
        db_action = "CREATE_CHANNEL"
        ch_kind = "voice" if act_val == "CREATE_VOICE_CHANNEL" else "text"
        payload = {"channel_name": object_name, "channel_kind": ch_kind}
        header = f"[АВТО-ШАБЛОН п. 6.6]: Создать {'голосовой' if ch_kind == 'voice' else 'текстовый'} канал «{object_name}»."

    elif act_val == "ELECT_MANAGER":
        if not target_user:
            await interaction.response.send_message("❌ Укажите `target_user` (кандидата).", ephemeral=True)
            return
        mgr_ids = get_all_manager_role_ids()
        default_mgr_role = next(iter(MANAGER_ROLES.keys()), 0)
        chosen_role_id = target_role.id if target_role else default_mgr_role
        if chosen_role_id not in mgr_ids:
            await interaction.response.send_message(
                "❌ Выбранная роль не входит в реестр должностей `MANAGER_ROLES`.", ephemeral=True
            )
            return
        role_label = target_role.name if target_role else MANAGER_ROLES.get(chosen_role_id, "Управленец")
        category = "Стафф"
        db_action = "ELECT_MANAGER"
        payload = {"user_id": target_user.id, "role_id": chosen_role_id}
        header = f"[АВТО-ШАБЛОН п. 6.6 / п. 7.1.1]: Избрать {target_user.display_name} ({target_user.id}) на должность «{role_label}»."

    elif act_val == "APPLY_ISOLATION":
        if not target_user:
            await interaction.response.send_message("❌ Укажите `target_user`.", ephemeral=True)
            return
        category = "Правосудие"
        db_action = "APPLY_ISOLATION"
        payload = {"user_id": target_user.id}
        header = f"[АВТО-ШАБЛОН п. 6.6]: Перевести {target_user.display_name} ({target_user.id}) в статус Ограниченного участника."

    elif act_val == "RENAME_GUILD":
        if not object_name:
            await interaction.response.send_message("❌ Укажите `object_name` (новое название).", ephemeral=True)
            return
        category = "Локальное лого"
        db_action = "RENAME_GUILD"
        payload = {"new_name": object_name}
        header = f"[АВТО-ШАБЛОН п. 6.6]: Изменить локальное название Площадки на «{object_name}»."
    else:
        return

    full_text = f"{header}\n\nОбоснование инициативы:\n{rationale.strip()}"
    poll_id = bot.db.create_poll(
        initiator_id=interaction.user.id,
        category=category,
        proposal_text=full_text,
        target_user_id=target_uid,
        action_type=db_action,
        action_payload=payload,
    )
    msg = await bot.publish_poll_to_channel(interaction, poll_id)
    await interaction.response.send_message(
        f"⚙️ **Автоматизированное голосование #{poll_id}** (`[{category}]`) запущено!\n"
        f"В случае победы бот исполнит действие автоматически: {msg.jump_url if msg else ''}",
        ephemeral=True,
    )


@bot.tree.command(
    name="isolate",
    description="Пункт 3.5 и 4.3: Поместить участника в Изоляцию с мутом и сохранением ролей (лимит: 3 в 24ч)",
)
@app_commands.describe(
    member="Участник, помещаемый в Изоляцию",
    reason="Обязательное публичное обоснование (пункт Регламента или описание ситуации, п. 4.3)",
    duration="Срок: 30sec, 15min, 12h, 7d, 1mon или 0 — до апелляции (п. 5.2)",
)
async def cmd_isolate_member(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str,
    duration: str = "0",
) -> None:
    if not verify_guild(interaction) or not isinstance(interaction.user, discord.Member):
        return

    if not has_command_permission(interaction.user, "isolate"):
        await interaction.response.send_message(
            "❌ У вашей должности нет прав на применение `/isolate` (п. 3.1, 3.5).",
            ephemeral=True,
        )
        return

    if member.id == interaction.user.id or member.bot:
        await interaction.response.send_message("❌ Нельзя изолировать самого себя или бота.", ephemeral=True)
        return

    if len(reason.strip()) < 5:
        await interaction.response.send_message(
            "❌ Согласно п. 4.3 Регламента скрытая модерация без фиксации причины запрещена.",
            ephemeral=True,
        )
        return

    allowed, used_cnt, reset_ts = bot.db.check_command_rate_limit(
        interaction.user.id, "isolate", DAILY_ISOLATE_LIMIT
    )
    if not allowed:
        await interaction.response.send_message(
            f"⏳ **Достигнут суточный лимит:** `/isolate` можно использовать не более `{DAILY_ISOLATE_LIMIT}` раз за 24 часа.\n"
            f"Следующее использование доступно <t:{reset_ts}:R>.",
            ephemeral=True,
        )
        return

    duration_sec = parse_duration_string(duration)
    if duration_sec is None:
        await interaction.response.send_message(
            "⚠️ Неверный формат срока! Примеры: `30min`, `12h`, `7d`, `1mon` или `0` (до апелляции).",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    restricted_role = guild.get_role(RESTRICTED_ROLE_ID)
    if not restricted_role:
        await interaction.response.send_message(
            "❌ На сервере не настроена роль `RESTRICTED_ROLE_ID`.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    roles_to_remove = [
        r for r in member.roles if not r.is_default() and not r.managed and r.id != RESTRICTED_ROLE_ID
    ]
    saved_role_ids = [r.id for r in roles_to_remove]

    try:
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason=f"Изоляция п. 3.5 ({interaction.user}): {reason}")
        if restricted_role not in member.roles:
            await member.add_roles(restricted_role, reason=f"Изоляция п. 3.5 ({interaction.user}): {reason}")
    except discord.Forbidden:
        await interaction.followup.send("❌ У бота недостаточно прав в иерархии для изменения ролей этого участника.")
        return

    timeout_sec = min(duration_sec, DISCORD_MAX_TIMEOUT_SECONDS) if duration_sec > 0 else DISCORD_MAX_TIMEOUT_SECONDS
    try:
        until_dt = discord.utils.utcnow() + datetime.timedelta(seconds=timeout_sec)
        await member.timeout(until_dt, reason=f"АСУ Изоляция (п. 3.5): {reason}")
    except Exception:
        pass

    if member.voice:
        try:
            await member.edit(mute=True, reason=f"АСУ Изоляция (п. 3.5): {reason}")
        except Exception:
            pass

    now = time.time()
    until_ts = (now + duration_sec) if duration_sec > 0 else 0.0
    with bot.db._get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO isolations (user_id, manager_id, reason, isolated_at, until_ts, saved_roles)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (member.id, interaction.user.id, reason.strip(), now, until_ts, json.dumps(saved_role_ids)),
        )
        conn.commit()

    bot.db.record_command_usage(interaction.user.id, "isolate")
    human_dur = format_duration_human(duration_sec)
    dur_display = f"До <t:{int(until_ts)}:F> (<t:{int(until_ts)}:R>)" if until_ts > 0 else human_dur

    await bot.log_audit_event(
        title="🔒 Применение Изоляции (п. 3.5, 4.3 Регламента)",
        description=(
            f"**Изолированный участник:** {member.mention} (`{member.id}`)\n"
            f"**Управленец:** {interaction.user.mention} (Лимит 24ч: `{used_cnt + 1}/{DAILY_ISOLATE_LIMIT}`)\n"
            f"**Причина (п. 4.3):** {reason.strip()}\n"
            f"**Срок (п. 5.2):** {dur_display}\n"
            f"**Сохранено ролей:** `{len(saved_role_ids)}`\n\n"
            f"*Участник сохраняет право подать Публичную апелляцию командой `/appeal` (п. 5.1). "
            f"Голоса, отданные им до Изоляции, остаются неприкосновенными (п. 1.2).*"
        ),
        color=0xE67E22,
    )

    try:
        dm_embed = discord.Embed(
            title="⚠️ Вы переведены в статус Ограниченного участника (Изоляция, п. 3.5)",
            description=(
                f"**Площадка:** {guild.name}\n"
                f"**Управленец:** {interaction.user.display_name}\n"
                f"**Причина (п. 4.3):** {reason.strip()}\n"
                f"**Срок и условия выхода (п. 5.2):** {dur_display}\n\n"
                f"⚖️ **Право на Публичную апелляцию (п. 5.1):**\n"
                f"Вы можете оспорить ограничение, вызвав команду `/appeal` и указав ваши аргументы."
            ),
            color=0xE67E22,
        )
        await member.send(embed=dm_embed)
    except Exception:
        pass

    await interaction.followup.send(
        f"🔒 Участник {member.mention} переведён в **Изоляцию** (`{human_dur}`). "
        f"Использовано лимитов за 24ч: `{used_cnt + 1}/{DAILY_ISOLATE_LIMIT}`."
    )


@bot.tree.command(
    name="unisolate",
    description="Досрочно снять Изоляцию с участника и вернуть все сохранённые роли",
)
@app_commands.describe(member="Изолированный участник", reason="Публичная причина снятия ограничений (п. 4.3)")
async def cmd_unisolate_member(
    interaction: discord.Interaction, member: discord.Member, reason: str = "Завершение разбирательства"
) -> None:
    if not verify_guild(interaction) or not isinstance(interaction.user, discord.Member):
        return

    if not has_command_permission(interaction.user, "unisolate"):
        await interaction.response.send_message("❌ У вашей должности нет прав на выполнение `/unisolate`.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    await bot.restore_member_from_isolation(
        interaction.guild, member, reason=f"Снято Управленцем {interaction.user}: {reason}"
    )

    await bot.log_audit_event(
        title="🕊️ Досрочное снятие Изоляции (/unisolate)",
        description=(
            f"**Участник:** {member.mention} (`{member.id}`)\n"
            f"**Управленец:** {interaction.user.mention}\n"
            f"**Основание (п. 4.3):** {reason.strip()}"
        ),
        color=0x2ECC71,
    )
    await interaction.followup.send(
        f"✅ С участника {member.mention} снята Изоляция, все сохранённые роли возвращены."
    )


@bot.tree.command(
    name="ban_twink",
    description="Пункт 1.2 и 2.6: Аннулировать голоса и кикнуть изолированного твинка (лимит: 1 в 24ч)",
)
@app_commands.describe(
    member="Находящийся в Изоляции рядовой участник (доказанный твинк)",
    reason="Доказательство принадлежности дополнительного аккаунта (п. 2.6, 4.3)",
)
async def cmd_ban_twink(interaction: discord.Interaction, member: discord.Member, reason: str) -> None:
    if not verify_guild(interaction) or not isinstance(interaction.user, discord.Member):
        return

    if not has_command_permission(interaction.user, "ban_twink"):
        await interaction.response.send_message("❌ У вашей должности нет прав на выполнение `/ban_twink`.", ephemeral=True)
        return

    allowed, _, reset_ts = bot.db.check_command_rate_limit(
        interaction.user.id, "ban_twink", DAILY_BAN_TWINK_LIMIT
    )
    if not allowed:
        await interaction.response.send_message(
            f"⏳ **Перезарядка команды:** `/ban_twink` можно использовать не чаще **1 раза в 24 часа**.\n"
            f"Доступно повторно: <t:{reset_ts}:R>.",
            ephemeral=True,
        )
        return

    with bot.db._get_conn() as conn:
        iso_row = conn.execute("SELECT saved_roles FROM isolations WHERE user_id = ?", (member.id,)).fetchone()

    if not is_restricted_member(member) and not iso_row:
        await interaction.response.send_message(
            "⚠️ Команда `/ban_twink` может применяться **только к участникам, предварительно помещённым в Изоляцию** (`/isolate`).",
            ephemeral=True,
        )
        return

    mgr_ids = get_all_manager_role_ids()
    has_saved_mgr = False
    if iso_row and iso_row["saved_roles"]:
        try:
            has_saved_mgr = any(int(rid) in mgr_ids for rid in json.loads(iso_row["saved_roles"]))
        except Exception:
            pass

    if is_manager(member) or has_saved_mgr:
        await interaction.response.send_message(
            "🚫 Запрещено применять `/ban_twink` к действующим или изолированным Управленцам!",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    affected_polls = bot.db.purge_user_votes_from_open_polls(member.id)
    for pid in affected_polls:
        p_row = bot.db.get_poll(pid)
        if p_row:
            await bot.refresh_poll_message(p_row)

    with bot.db._get_conn() as conn:
        conn.execute("DELETE FROM isolations WHERE user_id = ?", (member.id,))
        conn.commit()

    try:
        await member.kick(reason=f"АСУ п. 2.6 (Твинк в голосовании, Управленец {interaction.user}): {reason}")
    except discord.Forbidden:
        await interaction.followup.send("❌ У бота недостаточно прав Discord для кика этого пользователя.")
        return

    bot.db.record_command_usage(interaction.user.id, "ban_twink")

    await bot.log_audit_event(
        title="🚫 Пресечение твинк-аккаунта (/ban_twink │ п. 1.2, 2.6)",
        description=(
            f"**Исключённый аккаунт:** {member.mention} (`{member.id}`)\n"
            f"**Управленец:** {interaction.user.mention}\n"
            f"**Аннулировано голосов в активных опросах:** `{len(affected_polls)}`\n"
            f"**Основание и доказательства (п. 4.3):** {reason.strip()}"
        ),
        color=0xE74C3C,
    )

    await interaction.followup.send(
        f"✅ Твинк-аккаунт `{member.display_name}` кикнут с Площадки, его голоса аннулированы в `{len(affected_polls)}` активных опросах."
    )


@bot.tree.command(
    name="appoint",
    description="Техническая выдача роли Управленца с задержкой безопасности 30 мин. (лимит: 3 в 24ч)",
)
@app_commands.describe(
    user="Назначаемый участник",
    role="Управленческая роль из реестра MANAGER_ROLES (не выше вашей собственной роли)",
    reason="Публичное основание назначения (ссылка на решение или стажировку, п. 4.3, 7.1.3)",
)
async def cmd_appoint_manager(
    interaction: discord.Interaction, user: discord.Member, role: discord.Role, reason: str
) -> None:
    if not verify_guild(interaction) or not isinstance(interaction.user, discord.Member):
        return

    if not has_command_permission(interaction.user, "appoint"):
        await interaction.response.send_message("❌ У вашей должности нет прав на выполнение `/appoint`.", ephemeral=True)
        return

    mgr_ids = get_all_manager_role_ids()
    if role.id not in mgr_ids:
        await interaction.response.send_message(
            f"❌ Роль {role.mention} не входит в реестр управленческих должностей `MANAGER_ROLES`.",
            ephemeral=True,
        )
        return

    caller_mgr_roles = [r for r in interaction.user.roles if r.id in mgr_ids]
    highest_pos = max((r.position for r in caller_mgr_roles), default=interaction.user.top_role.position)
    if role.position > highest_pos and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            f"❌ Вы не можете выдать роль {role.mention}, так как она выше вашей собственной должности.",
            ephemeral=True,
        )
        return

    allowed, used_cnt, reset_ts = bot.db.check_command_rate_limit(
        interaction.user.id, "appoint", DAILY_APPOINT_LIMIT
    )
    if not allowed:
        await interaction.response.send_message(
            f"⏳ Достигнут лимит `/appoint` (`{DAILY_APPOINT_LIMIT}` за 24 часа). Доступно: <t:{reset_ts}:R>.",
            ephemeral=True,
        )
        return

    execute_at = time.time() + APPOINT_DELAY_SECONDS
    with bot.db._get_conn() as conn:
        conn.execute(
            """
            INSERT INTO pending_appointments (target_user_id, role_id, initiator_id, reason, execute_at, executed)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (user.id, role.id, interaction.user.id, reason.strip(), execute_at),
        )
        conn.commit()

    bot.db.record_command_usage(interaction.user.id, "appoint")
    exec_int = int(execute_at)

    await bot.log_audit_event(
        title="⏳ Запланировано назначение Управленца (/appoint)",
        description=(
            f"**Кандидат:** {user.mention} (`{user.id}`)\n"
            f"**Должность:** {role.mention}\n"
            f"**Инициатор:** {interaction.user.mention} (Лимит: `{used_cnt + 1}/{DAILY_APPOINT_LIMIT}`)\n"
            f"**Основание (п. 4.3):** {reason.strip()}\n"
            f"**Автоматическая выдача через 30 мин.:** <t:{exec_int}:R> (<t:{exec_int}:T>)"
        ),
        color=0x3498DB,
    )
    await interaction.response.send_message(
        f"✅ Назначение роли {role.mention} участнику {user.mention} поставлено в очередь АСУ и исполнится <t:{exec_int}:R>.",
        ephemeral=True,
    )


@bot.tree.command(
    name="selfimpeach",
    description="Пункт 7.3: Добровольно сложить с себя полномочия Управленца",
)
@app_commands.describe(role="Управленческая роль, которую вы хотите снять с себя")
async def cmd_selfimpeach(interaction: discord.Interaction, role: discord.Role) -> None:
    if not verify_guild(interaction) or not isinstance(interaction.user, discord.Member):
        return

    mgr_ids = get_all_manager_role_ids()
    if role.id not in mgr_ids:
        await interaction.response.send_message(
            f"❌ Роль {role.mention} не входит в реестр `MANAGER_ROLES`.", ephemeral=True
        )
        return

    if role not in interaction.user.roles:
        await interaction.response.send_message(f"ℹ️ У вас нет роли {role.mention}.", ephemeral=True)
        return

    try:
        await interaction.user.remove_roles(role, reason="АСУ п. 7.3: Добровольная отставка Управленца (/selfimpeach)")
    except discord.Forbidden:
        await interaction.response.send_message("❌ Бот не может снять эту роль из-за иерархии ролей.", ephemeral=True)
        return

    await bot.log_audit_event(
        title="🏳️ Добровольная отставка Управленца (/selfimpeach │ п. 7.3)",
        description=f"Участник {interaction.user.mention} (`{interaction.user.id}`) добровольно сложил полномочия {role.mention}.",
        color=0xF1C40F,
    )
    await interaction.response.send_message(
        f"✅ Вы добровольно сложили с себя полномочия {role.mention} (п. 7.3 Регламента).", ephemeral=True
    )


@bot.tree.command(
    name="unexecutable",
    description="Пункт 6.10 и 6.10.1: Опубликовать мотивированный отказ от реализации по неисполнимости",
)
@app_commands.describe(
    poll_id="Номер завершившегося победой голосования",
    clause="Пункт Регламента (6.10 — тех. невозможность, 6.10.1 — отсутствие исполнителя)",
    proof="Доказательство объективной невозможности или отсутствия готового результата (п. 4.3)",
)
@app_commands.choices(
    clause=[
        app_commands.Choice(name="п. 6.10 — Физическая/техническая невозможность или двусмысленность", value="п. 6.10"),
        app_commands.Choice(name="п. 6.10.1 — Отсутствие исполнителя и готовой реализации с нуля", value="п. 6.10.1"),
    ]
)
async def cmd_unexecutable(
    interaction: discord.Interaction, poll_id: int, clause: app_commands.Choice[str], proof: str
) -> None:
    if not verify_guild(interaction) or not isinstance(interaction.user, discord.Member):
        return

    if not has_command_permission(interaction.user, "unexecutable"):
        await interaction.response.send_message("❌ У вашей должности нет прав на выполнение `/unexecutable`.", ephemeral=True)
        return

    poll = bot.db.get_poll(poll_id)
    if not poll or poll["status"] != "PASSED":
        await interaction.response.send_message(
            "⚠️ Пункты 6.10 / 6.10.1 применяются только к завершившимися победой (`PASSED`) голосованиям.",
            ephemeral=True,
        )
        return

    formatted_reason = f"[{clause.value} Регламента] {proof.strip()}"
    bot.db.mark_poll_unexecutable(poll_id, interaction.user.id, formatted_reason)
    updated_poll = bot.db.get_poll(poll_id)
    await bot.refresh_poll_message(updated_poll, disable_buttons=True)

    await bot.log_audit_event(
        title=f"⚠️ Зафиксирована неисполнимость решения #{poll_id} ({clause.value})",
        description=(
            f"**Голосование:** `#{poll_id}` (`[{poll['category']}]`)\n"
            f"**Управленец:** {interaction.user.mention}\n"
            f"**Обоснование и доказательство (п. 4.3, 6.10):**\n> {proof.strip()}"
        ),
        color=0xE67E22,
    )
    await interaction.response.send_message(
        f"✅ Мотивированный отказ по **{clause.value}** прикреплён к карточке голосования `#{poll_id}` и опубликован в аудит-логе.",
        ephemeral=True,
    )


@bot.tree.command(
    name="approve_twink",
    description="Пункт 2.6: Внести технический аккаунт в реестр без права голоса",
)
@app_commands.describe(
    twink_user="Технический/проектный дополнительный аккаунт",
    owner_user="Основной аккаунт владельца",
    poll_id="Номер голосования, одобрившего аккаунт (необязательно)",
)
async def cmd_approve_twink(
    interaction: discord.Interaction,
    twink_user: discord.Member,
    owner_user: discord.Member,
    poll_id: Optional[int] = None,
) -> None:
    if not verify_guild(interaction) or not isinstance(interaction.user, discord.Member):
        return

    if not has_command_permission(interaction.user, "approve_twink"):
        await interaction.response.send_message("❌ У вашей должности нет прав на выполнение `/approve_twink`.", ephemeral=True)
        return

    poll_info_str = "Без привязки к голосованию (прямое решение Управленца)"
    if poll_id is not None:
        poll = bot.db.get_poll(poll_id)
        if poll:
            poll_info_str = f"`#{poll_id}` (`[{poll['category']}]`, статус: `{poll['status']}`)"
        else:
            poll_info_str = f"`#{poll_id}` (указан вручную)"

    bot.db.register_approved_twink(twink_user.id, owner_user.id, poll_id)
    if TWINK_ROLE_ID > 0 and interaction.guild:
        twink_role = interaction.guild.get_role(TWINK_ROLE_ID)
        if twink_role and twink_role not in twink_user.roles:
            try:
                reason_suffix = f" (голосование #{poll_id})" if poll_id is not None else ""
                await twink_user.add_roles(twink_role, reason=f"АСУ п. 2.6: Технический аккаунт{reason_suffix}")
            except Exception:
                pass

    affected = bot.db.purge_user_votes_from_open_polls(twink_user.id)
    for pid in affected:
        p_row = bot.db.get_poll(pid)
        if p_row:
            await bot.refresh_poll_message(p_row)

    await bot.log_audit_event(
        title="🤖 Зарегистрирован технический аккаунт без права голоса (п. 2.6)",
        description=(
            f"**Технический аккаунт:** {twink_user.mention} (`{twink_user.id}`)\n"
            f"**Владелец:** {owner_user.mention} (`{owner_user.id}`)\n"
            f"**Голосование-основание:** {poll_info_str}\n"
            f"**Внёс в реестр:** {interaction.user.mention}"
        ),
        color=0x1ABC9C,
    )
    await interaction.response.send_message(
        f"✅ Аккаунт {twink_user.mention} внесён в реестр технических аккаунтов без права голоса "
        f"(владелец: {owner_user.mention}, основание: {poll_info_str}).",
        ephemeral=True,
    )


@bot.tree.command(
    name="unapprove_twink",
    description="Пункт 2.6: Снять с пользователя статус технического аккаунта (твинка) и вернуть право голоса",
)
@app_commands.describe(
    twink_user="Аккаунт, с которого снимается статус технического твинка",
    reason="Причина снятия статуса технического аккаунта (п. 4.3)",
)
async def cmd_unapprove_twink(
    interaction: discord.Interaction,
    twink_user: discord.Member,
    reason: str = "Снятие статуса технического аккаунта",
) -> None:
    if not verify_guild(interaction) or not isinstance(interaction.user, discord.Member):
        return

    if not (
        has_command_permission(interaction.user, "unapprove_twink")
        or has_command_permission(interaction.user, "approve_twink")
    ):
        await interaction.response.send_message(
            "❌ У вашей должности нет прав на выполнение `/unapprove_twink`.", ephemeral=True
        )
        return

    was_in_db = bot.db.remove_approved_twink(twink_user.id)
    role_removed = False

    if TWINK_ROLE_ID > 0 and interaction.guild:
        twink_role = interaction.guild.get_role(TWINK_ROLE_ID)
        if twink_role and twink_role in twink_user.roles:
            try:
                await twink_user.remove_roles(
                    twink_role, reason=f"АСУ п. 2.6: Снятие статуса тех. аккаунта ({interaction.user}): {reason}"
                )
                role_removed = True
            except Exception:
                pass

    if not was_in_db and not role_removed:
        await interaction.response.send_message(
            f"ℹ️ У пользователя {twink_user.mention} не было статуса или роли технического аккаунта.",
            ephemeral=True,
        )
        return

    await bot.log_audit_event(
        title="🔓 Снят статус технического аккаунта (/unapprove_twink │ п. 2.6)",
        description=(
            f"**Аккаунт:** {twink_user.mention} (`{twink_user.id}`)\n"
            f"**Управленец:** {interaction.user.mention}\n"
            f"**Причина (п. 4.3):** {reason.strip()}"
        ),
        color=0x2ECC71,
    )
    await interaction.response.send_message(
        f"✅ С аккаунта {twink_user.mention} снят статус технического твинка.",
        ephemeral=True,
    )


@bot.tree.command(
    name="list",
    description="Показать список всех текущих активных и охлаждающихся голосований Площадки",
)
async def cmd_poll_list(interaction: discord.Interaction) -> None:
    if not verify_guild(interaction) or not interaction.guild:
        return

    open_polls = bot.db.get_open_polls()
    if not open_polls:
        await interaction.response.send_message("📭 Активных голосований в данный момент нет.", ephemeral=True)
        return

    activity = bot.db.calculate_active_users_stats()
    active_cnt = activity["active_count"]
    lines = []

    for idx, p in enumerate(open_polls, 1):
        spec = TIERS[p["tier"]]
        req_q = max(1, math.ceil(active_cnt * (spec.quorum_percentage / 100.0)))
        tallies = bot.db.calculate_poll_tallies(p["poll_id"])
        icon = "🧊" if p["status"] == "COOLDOWN" else "🗳️"
        shield = " 🛡️" if p["aci_immune"] else ""
        jump_url = (
            f"https://discord.com/channels/{interaction.guild.id}/{p['channel_id']}/{p['message_id']}"
            if p["channel_id"] and p["message_id"]
            else ""
        )
        short_text = p["proposal_text"].replace("\n", " ")[:55] + "..."
        title_link = f"[Голосование #{p['poll_id']} │ {p['category']} ({spec.code})]({jump_url})" if jump_url else f"Голосование #{p['poll_id']}"
        lines.append(
            f"**{idx}.** {icon}{shield} {title_link}\n"
            f"> *{short_text}*\n"
            f"Явка: **`{tallies['total_voters']}/{req_q}`** │ Завершение: <t:{int(p['ends_at'])}:R>"
        )

    embed = discord.Embed(
        title="📋 Текущие голосования Площадки «Горизонт»",
        description="\n\n".join(lines)[:4096],
        color=0x2ECC71,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="activelist",
    description="Подробная сводка 30-дневной модели кворума, импульсов и индексов участников (п. 6.7)",
)
async def cmd_activelist(interaction: discord.Interaction) -> None:
    if not verify_guild(interaction) or not interaction.guild:
        return

    extra_excl: Set[int] = set()
    if TWINK_ROLE_ID > 0:
        twink_role = interaction.guild.get_role(TWINK_ROLE_ID)
        if twink_role:
            extra_excl = {m.id for m in twink_role.members}

    stats = bot.db.calculate_active_users_stats(extra_excluded_ids=extra_excl)
    active_cnt = stats["active_count"]
    details_map = stats["user_details"]

    embed = discord.Embed(
        title="📊 Детальная аналитика кворума и импульсов активности (720ч / 30 суток)",
        description=(
            "Математическая модель **SVA V. RH** (п. 6.7): каждые сутки делятся на `96` интервалов по 15 минут (`1 импульс`). "
            "Балл за сутки `d` (от `1` до `30`): `ln(1 + imp_d) * (0.5 + 0.5 * d / 30)`."
        ),
        color=0x3498DB,
    )

    embed.add_field(
        name="🧮 Глобальные метрики Площадки за 30 суток",
        value=(
            f"• **Всего зафиксировано 15-мин импульсов:** `{stats['total_raw_impulses']}`\n"
            f"• **Квалифицировано (>= 2 разных суток):** `{stats['qualified_count']} чел.` "
            f"*(отсеяно с 1 сутками: `{stats['discarded_single_day']}`)*\n"
            f"• **Сумма всех индексов массива:** `{stats['total_index_sum']:.4f}`\n"
            f"• **Взвешенная медиана (рубеж 50% суммы):** `{stats['weighted_median']:.4f}`\n"
            f"• **Порог активности (50% взвешенной медианы):** **`{stats['threshold']:.4f}`**\n"
            f"• **Активных пользователей (100% база кворума):** **`{active_cnt} чел.`**"
        ),
        inline=False,
    )

    tier_lines = []
    for _, spec in TIERS.items():
        req = max(1, math.ceil(active_cnt * (spec.quorum_percentage / 100.0)))
        tier_lines.append(
            f"• **{spec.code}** (Кворум `{spec.quorum_percentage:.0f}%`, Победа `> {spec.win_percentage:.0f}%`): "
            f"**`{req} чел.`**"
        )
    embed.add_field(name="🎯 Требуемая явка по Тирам", value="\n".join(tier_lines), inline=False)

    sorted_users = sorted(
        [(uid, info) for uid, info in details_map.items() if info["qualified"]],
        key=lambda x: x[1]["index"],
        reverse=True,
    )
    if sorted_users:
        top_lines = []
        for rank, (uid, info) in enumerate(sorted_users[:15], 1):
            status_mark = "🟢" if uid in stats["active_user_ids"] else "⚪"
            top_lines.append(
                f"`{rank:02d}.` {status_mark} <@{uid}> — Индекс: **`{info['index']:.3f}`** "
                f"│ Дней: `{info['active_days']}/30` │ Импульсов: `{info['total_impulses']}` *(за 24ч: `{info['last_24h_impulses']}`)*"
            )
        if len(sorted_users) > 15:
            top_lines.append(f"*...и ещё {len(sorted_users) - 15} квалифицированных участников.*")
        embed.add_field(
            name="🏆 Рейтинг участников по взвешенному индексу (Топ-15)",
            value="\n".join(top_lines)[:1024],
            inline=False,
        )

    caller_info = details_map.get(interaction.user.id)
    if caller_info:
        in_core = (
            "Да ✅ (входите в базу кворума)"
            if interaction.user.id in stats["active_user_ids"]
            else "Нет ⚪ (ниже порога или < 2 суток, но право голоса по п. 1.1 сохранено)"
        )
        caller_text = (
            f"• **Ваш индекс:** `{caller_info['index']:.4f}` │ **Активных суток:** `{caller_info['active_days']}/30` │ "
            f"**Всего импульсов:** `{caller_info['total_impulses']}` *(за последние 24ч: `{caller_info['last_24h_impulses']}`)*\n"
            f"• **Статус в кворуме:** {in_core}"
        )
    else:
        caller_text = "У вас пока нет зафиксированных импульсов за последние 30 суток (право голоса по п. 1.1 сохранено)."

    embed.add_field(name="👤 Ваша личная карточка активности", value=caller_text, inline=False)
    embed.set_footer(text="АСУ «Горизонт» • Содержание сообщений не сохраняется (п. 6.7 Регламента)")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="help",
    description="Справка по всем командам АСУ «Горизонт» и архитектуре SVA V. RH",
)
async def cmd_help(interaction: discord.Interaction) -> None:
    help_text = (
        "### 🗳️ Кнопки голосования\n"
        "• `⏫ (+2)` / `🔼 (+1)` / `⏺️ (0)` / `🔽 (-1)` / `⏬ (-2)` — веса голосов.\n"
        "• `🔍 Мой голос` — проверить свой сохранённый голос и лог оценок Управленцев по ACI.\n"
        "• `🚩 Жалоба ACI` — отправить наводку Управленцам на нарушение стандарта ACI.\n"
        "• `⚖️ Панель ACI` — открыть окно валидации ACI.\n\n"
        "### 📊 Запуск голосований (Разделы 1, 6, 7)\n"
        "• `/poll <category> <message_ids>` — создать голосование по 1 или нескольким ID/ссылкам сообщений («бургер»).\n"
        "• `/poll_impeach <@manager>` — голосование за снятие Управленца в обход ACI с полным иммунитетом (п. 6.8).\n"
        "• `/pollelection <@role>` — заполнить анкету кандидата на должность Управленца (п. 7.1, 7.2).\n"
        "• `/poll_action` — шаблонное голосование с автоисполнением (создание ролей, каналов, прямое избрание по п. 7.1.1).\n"
        "• `/appeal <justification>` — подать Публичную апелляцию для выхода из Изоляции (п. 5.1).\n"
        "• `/list` и `/activelist` — список текущих голосований и детальная математика 30-дневного кворума (п. 6.7).\n\n"
        "### 🛡️ Команды Управленцев (по ролям из `COMMAND_PERMISSIONS`)\n"
        "• `/isolate <@member> <reason> [duration]` — перевести в Изоляцию с сохранением ролей и мутом (п. 3.5, лимит 3/24ч).\n"
        "• `/unisolate <@member> [reason]` — досрочно снять Изоляцию и вернуть роли.\n"
        "• `/ban_twink <@member> <reason>` — очистить голоса и кикнуть изолированного твинка (п. 1.2, 2.6, лимит 1/24ч).\n"
        "• `/appoint <@user> <@role> <reason>` — выдать роль Управленца с задержкой 30 минут (лимит 3/24ч).\n"
        "• `/selfimpeach <@role>` — добровольно сложить полномочия Управленца (п. 7.3).\n"
        "• `/unexecutable <poll_id> <clause> <proof>` — мотивированный отказ по неисполнимости (п. 6.10 / 6.10.1).\n"
        "• `/approve_twink <@twink> <@owner> [poll_id]` — внести тех. аккаунт в реестр без права голоса (п. 2.6).\n"
        "• `/unapprove_twink <@twink> [reason]` — снять статус технического аккаунта (п. 2.6)."
    )
    embed = discord.Embed(
        title="ℹ️ Документация АСУ «Горизонт» (SVA V. RH)",
        description=help_text,
        color=0x3498DB,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


if __name__ == "__main__":
    if BOT_TOKEN == "YOUR_DISCORD_BOT_TOKEN_HERE" or ALLOWED_GUILD_ID == 123456789012345678:
        logger.error("Please configure BOT_TOKEN and ALLOWED_GUILD_ID before starting the bot.")
    else:
        bot.run(BOT_TOKEN)
