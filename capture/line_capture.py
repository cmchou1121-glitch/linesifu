"""L1 被動落地：把 LINE 案場群組訊息原樣存進獨立 SQLite，供 L2 每日萃取。

紀律（2026-08-15 三鏡頭抗辯後定稿，改動前先讀完）：

1. **只落地，不寫任何 SoT**。這個模組不碰 Vault、不碰 Excel 流水帳、不碰
   yuesheng.db 的任何既有 table。L2 才做萃取，L3 才由人工核可寫回。
2. **白名單預設空 = 不擷取任何東西**。capture 掛在授權檢查之前，沒有白名單
   就會連你自己的 1:1 私訊與財務群組（含廠商銀行帳號、業主姓名地址）一起錄。
   把 group id 加進白名單這個動作，就是你親自進群並告知成員的時機。
3. **任何失敗都不得影響 finance / 出工**。呼叫端自帶 try/except；這裡的
   DB timeout 刻意設得很短（預設 2 秒），因為 capture 跑在 finance 的
   `_line_event_claim` 之前，久等會燒掉 LINE 約 1 分鐘的 replyToken。
   **競爭時 capture 掉資料，不是 finance 變慢。**
4. **unsend 寫墓碑，絕不 DELETE**。capture 在去重 claim 之前，刪掉列之後
   LINE 重送原訊息會讓 `INSERT OR IGNORE` 找不到列，已撤回的內容會永久復活。
   墓碑佔住同一個 dedupe_key，重送就再也插不進來。墓碑也可能**先於**原訊息
   建立（webhook 每個 POST 一條 thread，無序），那是預期情況不是錯誤。
5. **擷取的資料視為可拋棄**。不進備份輪替：一來它是 L2 的原料不是 SoT，
   二來備份是附加式的，unsend 的刪除不會傳播到副本，備份反而讓合規失效。
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")

logger = logging.getLogger(__name__)

# LINE message id 會被拿去組 URL 與檔名，先驗格式再用（避免路徑穿越／注入）
_MESSAGE_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")

_MEDIA_TYPES = {"image", "audio", "video", "file"}
_MEDIA_EXT = {"image": ".jpg", "audio": ".m4a", "video": ".mp4", "file": ".bin"}

# Windows 檔名非法字元；案場名要當資料夾名，先洗過
_UNSAFE_SEGMENT_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SAFE_EXT_RE = re.compile(r"^\.[0-9A-Za-z]{1,10}$")

# Windows 保留裝置名：拿來當資料夾名會讓 mkdir 直接失敗，
# 而且判定看的是第一個句點之前那段，所以 "CON.txt" 一樣中。
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_CONTENT_URL = "https://api-data.line.me/v2/bot/message/{message_id}/content"

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}


# ── 設定（一律 call time 讀，不在 import 時凍結） ────────────────────────────

def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def capture_enabled() -> bool:
    return _truthy(os.getenv("LINE_CAPTURE_ENABLED", ""))


def media_enabled() -> bool:
    """媒體下載可單獨關閉，方便先只跑文字觀察一天再開圖片／語音。"""
    return _truthy(os.getenv("LINE_CAPTURE_MEDIA_ENABLED", "true"))


def allowed_groups() -> set[str]:
    """空集合＝不擷取任何對話。這是預設值，且是刻意的。"""
    raw = os.getenv("LINE_CAPTURE_GROUP_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def groups_csv_path(override: Path | str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.getenv("LINE_CAPTURE_GROUPS_CSV", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1] / "line_poc" / "line_capture_groups.csv"


def group_map(csv_override: Path | str | None = None) -> dict[str, str]:
    """群組 → 案場名。這張表同時就是擷取白名單：沒列在裡面的群組不擷取。

    CSV 是 SoT（欄位 line_group_id,project,note），因為案場名是中文、
    而且要一個個加專案，用檔案比塞 .env 好編輯也好 review。
    `LINE_CAPTURE_GROUP_IDS` 仍然有效（相容既有設定），只是沒有案場名。
    """
    mapping: dict[str, str] = {}
    path = groups_csv_path(csv_override)
    if path.exists():
        try:
            # utf-8-sig：Excel 存出來的 CSV 常帶 BOM
            with path.open("r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    gid = str(row.get("line_group_id") or "").strip()
                    if gid and not gid.startswith("#"):
                        mapping[gid] = str(row.get("project") or "").strip()
        except (OSError, csv.Error):
            logger.exception("line_capture groups csv unreadable: %s", path)
    for gid in allowed_groups():
        mapping.setdefault(gid, "")
    return mapping


def _safe_segment(value: str, fallback: str) -> str:
    cleaned = _UNSAFE_SEGMENT_RE.sub("_", str(value or "").strip()).strip(". ")
    if not cleaned:
        return fallback
    if cleaned.split(".")[0].upper() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:120]


def _lease_cutoff() -> str:
    """媒體下載租約：認領後超過這個時間仍沒結果，視為該執行緒已死，可重撿。"""
    seconds = _int_env("LINE_CAPTURE_MEDIA_LEASE_SECONDS", 300)
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _taipei_month(line_ts: Any) -> str:
    """媒體按台北時間分月：用 UTC 分的話，台北月初早上的檔案會掉到上個月。"""
    try:
        ms = int(line_ts or 0)
    except (TypeError, ValueError):
        ms = 0
    if ms <= 0:
        return datetime.now(TAIPEI).strftime("%Y-%m")
    return datetime.fromtimestamp(ms / 1000, tz=TAIPEI).strftime("%Y-%m")


def _media_ext(message_type: str, file_name: str) -> str:
    """檔案訊息用原始副檔名，否則 PDF 會被存成 .bin。"""
    if message_type == "file" and file_name:
        suffix = Path(str(file_name)).suffix.lower()
        if _SAFE_EXT_RE.match(suffix):
            return suffix
    return _MEDIA_EXT.get(message_type, ".bin")


def discover_mode() -> bool:
    """探索模式：把「不在白名單」的 group_id 記進 log，但**不存任何內容**。

    LINE 不會主動告訴你 groupId，唯一的取得方式就是從實際進來的事件裡看。

    **預設開啟**（2026-08-16 定）：只記一行 id、不碰內容，成本幾乎是零；
    關著的代價卻是「群組加了、訊息也確實收到了，卻因為旗標沒開而撈不到 id，
    得再麻煩對方發一次」——這在範例燈飾群組實際發生過一次。
    """
    return _truthy(os.getenv("LINE_CAPTURE_DISCOVER", "true"))


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _default_data_dir() -> Path:
    # 走 config.BOT_DATA_DIR 才能繼承測試沙箱的重導向（tests/_bootstrap）
    try:
        import config  # type: ignore

        return Path(config.BOT_DATA_DIR)
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parents[1] / "data"


def db_path(override: Path | str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.getenv("LINE_CAPTURE_DB_PATH", "").strip()
    if env:
        return Path(env)
    return _default_data_dir() / "line_capture.db"


def media_dir(override: Path | str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.getenv("LINE_CAPTURE_MEDIA_DIR", "").strip()
    if env:
        return Path(env)
    return _default_data_dir() / "line_capture_media"


# ── SQLite ────────────────────────────────────────────────────────────────────

_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS line_capture_events (
    id INTEGER PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    message_id TEXT NOT NULL DEFAULT '',
    group_id TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT '',
    message_type TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    line_ts INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '',
    media_state TEXT NOT NULL DEFAULT 'none',
    media_path TEXT NOT NULL DEFAULT '',
    media_bytes INTEGER NOT NULL DEFAULT 0,
    media_error TEXT NOT NULL DEFAULT '',
    media_attempts INTEGER NOT NULL DEFAULT 0,
    media_claimed_at TEXT NOT NULL DEFAULT '',
    revoked_at TEXT NOT NULL DEFAULT '',
    captured_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# 索引一定要等 _ensure_columns 補完欄位才建：既有 DB 沒有 project 欄，
# CREATE TABLE IF NOT EXISTS 不會動它，索引卻會 no such column 直接炸。
_SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_line_capture_group_ts
    ON line_capture_events(group_id, line_ts);
CREATE INDEX IF NOT EXISTS ix_line_capture_project_ts
    ON line_capture_events(project, line_ts);
CREATE INDEX IF NOT EXISTS ix_line_capture_message
    ON line_capture_events(message_id);
CREATE INDEX IF NOT EXISTS ix_line_capture_media
    ON line_capture_events(media_state);
"""


def _connect(target: Path) -> sqlite3.Connection:
    target.parent.mkdir(parents=True, exist_ok=True)
    # 刻意用短 timeout：capture 排在 finance claim 前面，寧可掉自己的資料，
    # 也不能讓 finance 的 replyToken 等到過期。
    conn = sqlite3.connect(str(target), timeout=_float_env("LINE_CAPTURE_DB_TIMEOUT", 2.0))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = %d" % _int_env("LINE_CAPTURE_DB_BUSY_MS", 2000))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA_TABLE)
    _ensure_columns(conn)
    conn.executescript(_SCHEMA_INDEXES)
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """house pattern：沒有 migration framework，用 PRAGMA 檢查後補 ALTER。

    SQLite 不能 ALTER 加 NOT NULL 而無 DEFAULT，所以新欄位一律帶 DEFAULT。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(line_capture_events)")}
    for name, ddl in (
        ("project", "TEXT NOT NULL DEFAULT ''"),
        ("file_name", "TEXT NOT NULL DEFAULT ''"),
        ("media_claimed_at", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in existing:
            try:
                conn.execute(f"ALTER TABLE line_capture_events ADD COLUMN {name} {ddl}")
            except sqlite3.OperationalError as exc:
                # 升級後第一批事件可能有兩條 webhook thread 同時看到欄位不存在，
                # 輸的那條收到 duplicate column——目標已經達成，不是錯誤。
                if "duplicate column" not in str(exc).lower():
                    raise


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── dedupe key ────────────────────────────────────────────────────────────────

def _canonical_hash(event: dict[str, Any]) -> str:
    """對事件做去重雜湊。

    必須先剔除 deliveryContext 與 webhookEventId：LINE 重送同一事件時會把
    isRedelivery 由 false 翻成 true，不剔除的話同一則訊息會算出兩個 key。
    """
    trimmed = {k: v for k, v in event.items() if k not in ("deliveryContext", "webhookEventId")}
    blob = json.dumps(trimmed, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _dedupe_key(event: dict[str, Any]) -> str:
    """依「欄位是否為空」逐級退讓，不是依事件型別。

    若照型別分支，一則 message 事件遇到空的 message.id 會退化成字面 'msg:'，
    之後每一則同樣情況的訊息都會被 INSERT OR IGNORE 靜默丟掉。
    """
    event_type = str(event.get("type") or "")
    event_id = str(event.get("webhookEventId") or "").strip()

    # 編輯事件帶的是「原訊息」的 id，用 msg: 會撞掉原始那列而被 IGNORE。
    # 改用自己的 webhookEventId 另存一列，原文保留，由 L2 讀取時取最新版。
    if event_type == "messageEdited":
        if event_id:
            return f"evt:{event_id}"
        return f"hash:{_canonical_hash(event)}"

    message_id = str((event.get("message") or {}).get("id") or "").strip()
    if message_id:
        return f"msg:{message_id}"
    if event_id:
        return f"evt:{event_id}"
    return f"hash:{_canonical_hash(event)}"


# ── 主入口 ────────────────────────────────────────────────────────────────────

def capture_event(
    event: dict[str, Any],
    *,
    db_path_override: Path | str | None = None,
) -> dict[str, Any] | None:
    """落地一則 LINE 事件。回傳 None 代表刻意略過（未啟用／不在白名單／非群組）。

    呼叫端必須自帶 try/except——這裡雖然盡量不拋，但 SQLite 仍可能因鎖或磁碟
    問題拋出，而呼叫點在 finance 的例外保護之外。
    """
    if not capture_enabled():
        return None

    source = event.get("source") or {}  # source 在 LINE 規格上是 optional
    source_type = str(source.get("type") or "")
    if source_type not in ("group", "room"):
        return None

    group_id = str(source.get("groupId") or source.get("roomId") or "").strip()
    if not group_id:
        return None

    event_type = str(event.get("type") or "")

    groups = group_map()
    if group_id not in groups:
        if discover_mode():
            # 只記 group_id 與事件型別，不碰訊息內容
            logger.info(
                "line_capture discover group_id=%s event=%s (not in allowlist)",
                group_id,
                event_type or "-",
            )
        return None

    # 落地當下就把案場名寫死（snapshot）：群組日後可能改用途或案場結案，
    # L2 事後再查對應表會查到「現在的」歸屬，不是訊息發生當時的。
    project = groups[group_id]
    target = db_path(db_path_override)

    if event_type == "unsend":
        return _apply_unsend(event, group_id=group_id, project=project, target=target)

    message = event.get("message") or {}
    message_type = str(message.get("type") or "")
    key = _dedupe_key(event)

    want_media = (
        event_type == "message"
        and message_type in _MEDIA_TYPES
        and media_enabled()
        and _MESSAGE_ID_RE.match(str(message.get("id") or ""))
    )

    row = {
        "dedupe_key": key,
        "message_id": str(message.get("id") or ""),
        "group_id": group_id,
        "project": project,
        "user_id": str(source.get("userId") or ""),
        # fileName 只在這個事件裡出現一次，之後再也拿不到
        "file_name": str(message.get("fileName") or ""),
        "event_type": event_type,
        "message_type": message_type,
        "text": str(message.get("text") or ""),
        "line_ts": int(event.get("timestamp") or 0),
        "mode": str(event.get("mode") or ""),
        "raw_json": json.dumps(event, ensure_ascii=False)[:65536],
        "media_state": "pending" if want_media else "none",
    }

    conn = _connect(target)
    try:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO line_capture_events (
                dedupe_key, message_id, group_id, project, user_id, file_name,
                event_type, message_type, text, line_ts, mode, raw_json, media_state
            ) VALUES (
                :dedupe_key, :message_id, :group_id, :project, :user_id, :file_name,
                :event_type, :message_type, :text, :line_ts, :mode, :raw_json, :media_state
            )
            """,
            row,
        )
        conn.commit()
        inserted = cursor.rowcount == 1
    finally:
        conn.close()

    # 內容不進 log：案場群組訊息含姓名、電話、報價
    logger.info(
        "line_capture stored key=%s type=%s inserted=%s",
        key, event_type or "-", inserted,
    )
    return {"dedupe_key": key, "inserted": inserted, "media_state": row["media_state"]}


def _apply_unsend(
    event: dict[str, Any],
    *,
    group_id: str,
    project: str,
    target: Path,
) -> dict[str, Any]:
    """收回訊息＝立墓碑並抹掉內容，同時刪掉已落地的媒體檔。

    LINE 明文要求把被收回的訊息從資料庫刪除，所以 text 與 raw_json 一起清空
    （raw_json 裡也有原文，只清 text 等於沒清）。這確實犧牲了「原始事件留底」
    的保險，但兩者互斥，這裡選擇遵守收回。
    """
    message_id = str((event.get("unsend") or {}).get("messageId") or "").strip()
    if not message_id:
        return {"dedupe_key": "", "revoked": False, "reason": "no_message_id"}

    key = f"msg:{message_id}"
    conn = _connect(target)
    stale_paths: list[str] = []
    try:
        # 不能只清 dedupe_key 那一列：訊息被編輯過的話，編輯內容另存成
        # evt:<webhookEventId> 一列（message_id 相同）。只清前者的話，
        # 被收回的文字仍完整留在編輯列裡，等於沒有遵守收回。
        stale_paths = [
            str(r["media_path"] or "")
            for r in conn.execute(
                """
                SELECT media_path FROM line_capture_events
                 WHERE dedupe_key = ? OR message_id = ?
                """,
                (key, message_id),
            ).fetchall()
        ]

        cursor = conn.execute(
            """
            UPDATE line_capture_events
               SET text = '', raw_json = '', media_state = 'revoked',
                   media_path = '', media_error = '', media_claimed_at = '',
                   revoked_at = ?
             WHERE dedupe_key = ? OR message_id = ?
            """,
            (_now(), key, message_id),
        )
        cursor_rowcount = cursor.rowcount
        created_ahead = False
        if cursor_rowcount == 0:
            # unsend 比原訊息先抵達（每個 webhook POST 各跑一條 thread，無序）。
            # 先立墓碑佔住 key，原訊息之後來會被 INSERT OR IGNORE 擋掉。
            conn.execute(
                """
                INSERT OR IGNORE INTO line_capture_events (
                    dedupe_key, message_id, group_id, project, event_type,
                    media_state, revoked_at
                ) VALUES (?, ?, ?, ?, 'unsend', 'revoked', ?)
                """,
                (key, message_id, group_id, project, _now()),
            )
            created_ahead = True
        conn.commit()
    finally:
        conn.close()

    unlinked = sum(1 for path in stale_paths if _unlink_media(path))
    logger.info(
        "line_capture revoked key=%s rows=%s ahead_of_message=%s media_unlinked=%s",
        key, cursor_rowcount, created_ahead, unlinked,
    )
    return {
        "dedupe_key": key,
        "revoked": True,
        "created_ahead": created_ahead,
        "media_unlinked": unlinked,
    }


def _unlink_media(path_value: str) -> bool:
    """刪除媒體檔，但只允許刪 media root 底下的東西。"""
    if not path_value:
        return False
    try:
        root = media_dir().resolve()
        target = Path(path_value).resolve()
        target.relative_to(root)  # 不在 root 底下就丟 ValueError
    except (ValueError, OSError):
        logger.warning("line_capture refused to unlink outside media root")
        return False
    try:
        target.unlink(missing_ok=True)
        return True
    except OSError:
        logger.exception("line_capture unlink failed")
        return False


# ── 媒體下載 ──────────────────────────────────────────────────────────────────

def _fetch_content(message_id: str, access_token: str, *, max_bytes: int, timeout: float) -> bytes:
    """串流下載並邊讀邊計量。

    不能用 resp.content：那會先把整個 body 讀進記憶體才輪到我們檢查大小，
    上限就形同虛設。
    """
    import requests  # 延遲載入，讓沒有網路的單元測試不必付這個成本

    url = _CONTENT_URL.format(message_id=message_id)
    headers = {"Authorization": f"Bearer {access_token}"}
    with requests.get(url, headers=headers, timeout=timeout, stream=True) as resp:
        resp.raise_for_status()
        buf = bytearray()
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise ValueError(f"media exceeds cap {max_bytes} bytes")
        return bytes(buf)


def drain_media(
    *,
    access_token: str,
    db_path_override: Path | str | None = None,
    media_dir_override: Path | str | None = None,
    fetch_fn: Callable[..., bytes] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """把 media_state='pending' 的列補下載。

    刻意不開常駐 thread：Flask 早就回過 200 了，沒有 timeout 壓力，跟在
    webhook 迴圈後面跑就夠；下一則訊息進來時也會順手把上次沒抓完的補掉。

    注意 fetch_fn 是這個模組自己的注入點，**絕不可以**接 line_finance_poc
    的那個 fetch_fn——既有測試在數那個 seam 的呼叫次數，共用會讓斷言失效，
    production 也會把每張財務照片重複下載一次。
    """
    if not capture_enabled() or not media_enabled():
        return {"drained": 0, "saved": 0, "failed": 0}

    target = db_path(db_path_override)
    if not target.exists():
        return {"drained": 0, "saved": 0, "failed": 0}

    root = media_dir(media_dir_override)
    max_bytes = _int_env("LINE_CAPTURE_MEDIA_MAX_BYTES", 20 * 1024 * 1024)
    timeout = _float_env("LINE_CAPTURE_FETCH_TIMEOUT", 20.0)
    max_attempts = _int_env("LINE_CAPTURE_MEDIA_MAX_ATTEMPTS", 3)
    fetcher = fetch_fn or _fetch_content

    conn = _connect(target)
    try:
        rows = conn.execute(
            """
            SELECT id, message_id, message_type, line_ts,
                   project, group_id, file_name
              FROM line_capture_events
             WHERE media_attempts < ?
               AND (
                   media_state = 'pending'
                   OR (media_state = 'downloading' AND media_claimed_at <= ?)
               )
             ORDER BY id
             LIMIT ?
            """,
            (max_attempts, _lease_cutoff(), int(limit)),
        ).fetchall()
    finally:
        conn.close()

    saved = failed = 0
    for row in rows:
        message_id = str(row["message_id"] or "")
        if not _MESSAGE_ID_RE.match(message_id):
            _release_media(target, row["id"], "invalid_message_id", terminal=True)
            failed += 1
            continue

        # 先原子認領再下載。少了這一步，兩條 webhook thread 會同時抓同一則訊息、
        # 寫同一個路徑，然後晚到的那條發現自己 commit 不到，就把先到的那條剛存好的
        # 檔案刪掉——DB 標 saved，磁碟上卻沒有檔案。
        if not _claim_media(target, row["id"]):
            continue

        try:
            blob = fetcher(message_id, access_token, max_bytes=max_bytes, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            _release_media(target, row["id"], str(exc)[:300])
            failed += 1
            continue

        # 按案場分資料夾，讓人找得到；沒對應案場名就退回用 group_id
        bucket = _safe_segment(str(row["project"] or ""), str(row["group_id"] or "unknown"))
        ext = _media_ext(str(row["message_type"] or ""), str(row["file_name"] or ""))
        out_path = root / bucket / _taipei_month(row["line_ts"]) / f"{message_id}{ext}"
        try:
            # mkdir 一定要在 try 裡：保留裝置名、權限、路徑過長都會讓它拋錯，
            # 拋出去會直接中斷整個 drain 迴圈，該列永遠停在原狀態一再重試。
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(blob)
        except OSError as exc:
            _release_media(target, row["id"], f"write_failed: {exc}"[:300])
            failed += 1
            continue

        outcome = _commit_media(target, row["id"], out_path, len(blob))
        if outcome == "saved":
            saved += 1
        elif outcome == "lost:revoked":
            # 下載途中被收回：把剛寫下去的檔刪掉
            _unlink_media(str(out_path))
            failed += 1
        else:
            # 既沒認到帳也不是撤回（例如 commit 當下 DB 鎖住）。**不刪檔**——
            # 同名檔可能是別人剛存好的有效內容。
            logger.warning(
                "line_capture media commit inconclusive id=%s state=%s", row["id"], outcome
            )
            failed += 1

    return {"drained": len(rows), "saved": saved, "failed": failed}


def _claim_media(target: Path, row_id: int) -> bool:
    """把單列從 pending（或租約過期的 downloading）原子搶成 downloading。"""
    conn = _connect(target)
    try:
        cursor = conn.execute(
            """
            UPDATE line_capture_events
               SET media_state = 'downloading',
                   media_claimed_at = ?,
                   media_attempts = media_attempts + 1
             WHERE id = ?
               AND (
                   media_state = 'pending'
                   OR (media_state = 'downloading' AND media_claimed_at <= ?)
               )
            """,
            (_now(), int(row_id), _lease_cutoff()),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def _commit_media(target: Path, row_id: int, out_path: Path, size: int) -> str:
    """認帳。回傳 'saved'、或該列當下的實際狀態（'revoked' 等）。"""
    conn = _connect(target)
    try:
        cursor = conn.execute(
            """
            UPDATE line_capture_events
               SET media_state = 'saved', media_path = ?, media_bytes = ?,
                   media_error = '', media_claimed_at = ''
             WHERE id = ? AND media_state = 'downloading'
            """,
            (str(out_path), int(size), int(row_id)),
        )
        conn.commit()
        if cursor.rowcount == 1:
            return "saved"
        # 沒認到帳：回報「輸給了什麼狀態」，呼叫端才分得出該不該刪檔。
        # 別人剛存好的 saved，跟訊息被收回的 revoked，處置完全相反。
        row = conn.execute(
            "SELECT media_state FROM line_capture_events WHERE id = ?", (int(row_id),)
        ).fetchone()
        return f"lost:{row['media_state']}" if row is not None else "lost:missing"
    finally:
        conn.close()


def _release_media(target: Path, row_id: int, error: str, *, terminal: bool = False) -> None:
    """下載失敗後把列放回去：還有重試額度就回 pending，否則標 failed。

    已經是 revoked／saved 的列不動——那是別人的終局狀態。
    """
    conn = _connect(target)
    try:
        conn.execute(
            """
            UPDATE line_capture_events
               SET media_error = ?,
                   media_claimed_at = '',
                   media_state = CASE
                       WHEN media_state NOT IN ('pending', 'downloading') THEN media_state
                       WHEN ? = 1 OR media_attempts >= ? THEN 'failed'
                       ELSE 'pending' END
             WHERE id = ?
            """,
            (
                error,
                1 if terminal else 0,
                _int_env("LINE_CAPTURE_MEDIA_MAX_ATTEMPTS", 3),
                int(row_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ── 觀測（給啟動 log 與人工排查用） ───────────────────────────────────────────

def capture_stats(db_path_override: Path | str | None = None) -> dict[str, Any]:
    target = db_path(db_path_override)
    if not target.exists():
        return {"rows": 0, "groups": 0, "media_pending": 0, "media_failed": 0, "revoked": 0}
    conn = _connect(target)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS rows_total,
                   COUNT(DISTINCT group_id) AS groups_total,
                   SUM(media_state = 'pending') AS media_pending,
                   SUM(media_state = 'failed') AS media_failed,
                   SUM(revoked_at <> '') AS revoked
              FROM line_capture_events
            """
        ).fetchone()
    finally:
        conn.close()
    return {
        "rows": int(row["rows_total"] or 0),
        "groups": int(row["groups_total"] or 0),
        "media_pending": int(row["media_pending"] or 0),
        "media_failed": int(row["media_failed"] or 0),
        "revoked": int(row["revoked"] or 0),
    }
