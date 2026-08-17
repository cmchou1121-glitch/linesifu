# -*- coding: utf-8 -*-
"""L3 審核表的資料源：把待歸檔文件與待確認事項組成一份 JSON 給頁面讀。

兩條資料合併，它們的性質不一樣：

1. **待歸檔文件**——直接從 L1 的落地表撈 `media_state='saved'` 的列。
   這條**不需要 LLM**：檔案是什麼、誰傳的、原始檔名都是事實，唯一要決定的
   是「歸到哪個案場」。廠商群組一個群組橫跨多個案場，那格只能人來選。
   附上同群組前後幾則訊息當上下文，省得回 LINE 翻。

2. **待確認事項**——L2 用訂閱制 Claude 萃取出來的五類候選（缺失／報價／進度／
   出工／待確認）。這條是判斷，會錯，所以標了信心值並附原文。

輸出寫到 `PIPELINE_DATA_DIR/line_review.json`，審核頁向 review/app.py 要這份資料。

已經**成功歸檔過**的項目一律濾掉——判斷依據是審核佇列裡那筆的 status 與
`line_candidate_id`。只認成功，不認「送出過」。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from extract.daily_extract import (  # noqa: E402
    TAIPEI,
    already_handled_ids,
    data_dir,
    taipei_day_bounds,
)

logger = logging.getLogger(__name__)

# L3 自己的產出（審核頁讀這份）
DEFAULT_OUT = data_dir() / "line_review.json"
# L2 的產出（這裡讀它）。兩者**必須不同檔**，有 regression test 釘住。
DEFAULT_CANDIDATES = data_dir() / "line_candidates.json"

# 專案樹的位置。這兩個預設值是原公司的慣例，換一家公司就要改：
#   PROJECT_FILES_ROOT — 每個案子一個資料夾的根目錄
#   PROJECT_INDEX_ROOT — 另一棵樹，用來交叉確認「什麼才算一個案子」
# 交叉比對的用意見 active_projects()。
DEFAULT_FILES_ROOT = Path(os.getenv("PROJECT_FILES_ROOT", "") or (REPO_ROOT / "example_projects"))
DEFAULT_INDEX_ROOT = Path(os.getenv("PROJECT_INDEX_ROOT", "") or (REPO_ROOT / "example_index"))

# 上下文取前後各幾則訊息
CONTEXT_BEFORE = 3
CONTEXT_AFTER = 2
# 上下文的時間窗：只取檔案前後這段時間內的對話。沒有窗的話，安靜的群組會把
# 好幾天前的無關訊息當成「這份檔案的說明」端到人眼前——那會直接導致選錯案場。
CONTEXT_WINDOW_MS = 2 * 3600 * 1000


def _capture_db_path(override: Path | str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.getenv("LINE_CAPTURE_DB_PATH", "").strip()
    if env:
        return Path(env)
    return REPO_ROOT / "data" / "line_capture.db"


def document_id(message_id: str) -> str:
    """文件的候選 id。用 message_id 就夠穩定——同一則訊息永遠是同一份檔案。"""
    return f"lcdoc_{message_id}"


def _fmt_time(line_ts: Any) -> str:
    try:
        ms = int(line_ts or 0)
    except (TypeError, ValueError):
        ms = 0
    if ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=TAIPEI).strftime("%Y-%m-%d %H:%M")


def load_pending_documents(
    *,
    db_path: Path | str | None = None,
    days: int = 30,
    handled: set[str] | None = None,
) -> list[dict]:
    """撈已下載、還沒歸檔的檔案。預設只看近 30 天，避免整份歷史一次湧出。"""
    target = _capture_db_path(db_path)
    if not target.exists():
        return []

    start_ms, _end_ms, _day = taipei_day_bounds()
    window_start = start_ms - int(days) * 24 * 3600 * 1000
    handled = handled if handled is not None else set()

    conn = sqlite3.connect(str(target), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, message_id, group_id, project, user_id, message_type,
                   file_name, media_path, media_bytes, line_ts, text
              FROM line_capture_events
             WHERE media_state = 'saved'
               AND revoked_at = ''
               AND line_ts >= ?
             ORDER BY line_ts DESC, id DESC
            """,
            (window_start,),
        ).fetchall()
        documents = []
        for row in rows:
            doc_id = document_id(str(row["message_id"] or ""))
            if doc_id in handled:
                continue
            documents.append(
                {
                    "id": doc_id,
                    "message_id": str(row["message_id"] or ""),
                    "group_id": str(row["group_id"] or ""),
                    "project_hint": str(row["project"] or ""),
                    "kind": str(row["message_type"] or ""),
                    "file_name": str(row["file_name"] or ""),
                    "media_path": str(row["media_path"] or ""),
                    "media_bytes": int(row["media_bytes"] or 0),
                    "sent_at": _fmt_time(row["line_ts"]),
                    "context": _context_for(conn, row),
                }
            )
        return documents
    finally:
        conn.close()


def _context_for(conn: sqlite3.Connection, row: sqlite3.Row) -> list[dict]:
    """同群組、這則檔案前後的文字訊息——廠商常常是「這是乙案那批的」配一個附件。"""
    pivot = int(row["line_ts"] or 0)
    lo, hi = pivot - CONTEXT_WINDOW_MS, pivot + CONTEXT_WINDOW_MS
    before = conn.execute(
        """
        SELECT message_id, text, line_ts, id FROM line_capture_events
         WHERE group_id = ? AND line_ts <= ? AND line_ts >= ? AND id <> ?
           AND message_type = 'text' AND text <> '' AND revoked_at = ''
         ORDER BY line_ts DESC, id DESC LIMIT ?
        """,
        (row["group_id"], pivot, lo, row["id"], CONTEXT_BEFORE * 3),
    ).fetchall()
    after = conn.execute(
        """
        SELECT message_id, text, line_ts, id FROM line_capture_events
         WHERE group_id = ? AND line_ts > ? AND line_ts <= ? AND id <> ?
           AND message_type = 'text' AND text <> '' AND revoked_at = ''
         ORDER BY line_ts ASC, id ASC LIMIT ?
        """,
        (row["group_id"], pivot, hi, row["id"], CONTEXT_AFTER * 3),
    ).fetchall()

    def _latest_only(rows, limit):
        """訊息被編輯過會有兩列（原文＋編輯版，message_id 相同）。只留最新那版
        ——「甲案這批」被改成「乙案這批」時，兩個都端出去等於請人選錯案場。"""
        seen: dict[str, dict] = {}
        order: list[str] = []
        for r in rows:
            key = str(r["message_id"] or "") or f"__{r['id']}"
            if key not in seen:
                order.append(key)
                seen[key] = dict(r)
            elif int(r["id"]) > int(seen[key]["id"]):
                seen[key] = dict(r)
        return [seen[k] for k in order[:limit]]

    items = [
        {"text": str(r["text"]), "sent_at": _fmt_time(r["line_ts"]), "position": "before"}
        for r in reversed(_latest_only(before, CONTEXT_BEFORE))
    ]
    items.extend(
        {"text": str(r["text"]), "sent_at": _fmt_time(r["line_ts"]), "position": "after"}
        for r in _latest_only(after, CONTEXT_AFTER)
    )
    return items


def load_candidates(path: Path | str | None = None, *, handled: set[str] | None = None) -> list[dict]:
    """讀 L2 產出的語意候選。沒有檔案就當作今天沒有，不是錯誤。"""
    target = Path(path) if path else DEFAULT_CANDIDATES
    if not target.exists():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        # 檔案存在卻讀不動＝L2 壞掉或寫到一半，這是故障。回空清單會讓 build
        # 拿一份「看起來正常的空結果」蓋掉上一份好的產出。
        raise RuntimeError(f"L2 候選檔損毀或讀不到：{target}（{exc}）") from exc

    handled = handled if handled is not None else set()
    out = []
    for group in payload.get("groups", []) or []:
        for cand in group.get("candidates", []) or []:
            if str(cand.get("id") or "") in handled:
                continue
            item = dict(cand)
            item["group_id"] = str(group.get("group_id") or "")
            item["project_hint"] = str(group.get("project") or "")
            out.append(item)
    return out


def _looks_like_workspace(name: str) -> bool:
    """底線／數字開頭的都是工作區或分類夾，不是案場。"""
    return bool(name) and (name[0] in "_0123456789" or name.startswith("00-"))


def active_projects(
    files_root: Path | str | None = None,
    index_root: Path | str | None = None,
) -> list[str]:
    """下拉選單用的進行中案場。

    **交叉比對 Files\\ 與 Vault\\01-Projects\\ ——兩邊都有才算案場。**
    只看 Files 會撈到 `03_財務報表`、`06-TechGuide` 這種分類夾（實測撈到過），
    出現在下拉裡就是等著被誤選，把廠商對帳單歸進一個根本不是案場的地方。
    """
    root = Path(files_root or DEFAULT_FILES_ROOT)
    if not root.is_dir():
        return []
    files_names = {
        d.name for d in root.iterdir()
        if d.is_dir() and not _looks_like_workspace(d.name) and d.name != "廠商資料"
    }

    projects_dir = Path(index_root or DEFAULT_INDEX_ROOT)
    if projects_dir.is_dir():
        vault_names = {
            d.name for d in projects_dir.iterdir()
            if d.is_dir() and not _looks_like_workspace(d.name)
        }
        # **交集為空也照回空**，不退回寬鬆規則。交集空代表命名對不上（掛載、
        # 大小寫、遷移到一半），那是故障；此時放行「有兩個底線就算案場」會讓
        # `台北_財務_備份` 這種資料夾混進下拉，等著被誤選。
        return sorted(files_names & vault_names)

    # 只有在 Vault 整個讀不到時才退到命名規則
    return sorted(n for n in files_names if n.count("_") >= 2)


def vendor_index(
    files_root: Path | str | None = None,
    projects: list[str] | None = None,
) -> dict:
    """各案場各工種底下**既有**的廠商資料夾。

    廠商名在這個系統裡是漂的——同一家範例燈飾在磁碟上就有「範例燈飾國際有限公司」、
    「崁燈（範例燈飾）」兩種寫法。憑記憶打字幾乎必然開出第三個資料夾，把同一家
    的檔案拆散。把既有的列出來讓人直接選，是最省事的收斂方式。
    """
    out: dict[str, dict[str, list[str]]] = {}
    for project in projects or []:
        base = Path(files_root or DEFAULT_FILES_ROOT) / project / "01_報價發包" / "02_廠商發包"
        if not base.is_dir():
            continue
        trades: dict[str, list[str]] = {}
        try:
            for trade_dir in sorted(base.iterdir()):
                if not trade_dir.is_dir() or trade_dir.name.startswith("_"):
                    continue
                vendors = sorted(
                    d.name
                    for d in trade_dir.iterdir()
                    # old/ 與 _ 開頭的是備份與管理夾，不是廠商
                    if d.is_dir() and d.name.lower() != "old" and not d.name.startswith("_")
                )
                trades[trade_dir.name] = vendors
        except OSError:
            logger.exception("line_review 掃不到發包資料夾: %s", base)
            continue
        if trades:
            out[project] = trades
    return out


def build(
    *,
    db_path: Path | str | None = None,
    candidates_path: Path | str | None = None,
    out_path: Path | str | None = None,
    files_root: Path | str | None = None,
    queue_paths: list[Path] | None = None,
    write: bool = True,
) -> dict:
    # 「DB 不見」跟「今天沒東西」長得一樣，但前者是設定壞了。這種情況必須
    # ok=False 且**不寫檔**，否則會拿一份空結果蓋掉上一份好的審核表。
    db_target = _capture_db_path(db_path)
    if not db_target.exists():
        return {
            "generated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
            "ok": False,
            "error": f"capture db not found: {db_target}",
            "projects": [],
            "vendor_index": {},
            "document_count": 0,
            "candidate_count": 0,
            "documents": [],
            "candidates": [],
        }

    handled = already_handled_ids(queue_paths=queue_paths)
    documents = load_pending_documents(db_path=db_path, handled=handled)
    try:
        candidates = load_candidates(candidates_path, handled=handled)
    except RuntimeError as exc:
        return {
            "generated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
            "ok": False,
            "error": str(exc),
            "projects": [],
            "vendor_index": {},
            "document_count": 0,
            "candidate_count": 0,
            "documents": [],
            "candidates": [],
        }
    projects = active_projects(files_root)
    payload = {
        "generated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
        "ok": True,
        "error": "",
        "projects": projects,
        "vendor_index": vendor_index(files_root, projects),
        "document_count": len(documents),
        "candidate_count": len(candidates),
        "documents": documents,
        "candidates": candidates,
    }
    if write:
        target = Path(out_path) if out_path else DEFAULT_OUT
        target.parent.mkdir(parents=True, exist_ok=True)
        # 同目錄暫存 + os.replace：直接 write_text 會先截斷再寫，頁面若在那個
        # 空檔透過 /api/file 讀，看到的是空檔或半份 JSON。
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
        payload["written_to"] = str(target)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="組出 L3 審核表要用的 JSON")
    parser.add_argument("--out", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    payload = build(out_path=args.out, write=not args.dry_run)
    print(
        json.dumps(
            {k: v for k, v in payload.items() if k not in ("documents", "candidates")},
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"待歸檔文件 {payload['document_count']} 筆、待確認事項 {payload['candidate_count']} 筆")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
