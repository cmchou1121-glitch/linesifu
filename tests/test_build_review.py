# -*- coding: utf-8 -*-
"""L3 審核表的資料組裝。

重點：撤回的檔案不得出現、已送出過的不得重複冒出來、上下文要抓對——
那幾句「這是乙案那批的」正是判斷專案歸屬的依據。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from capture import line_capture  # noqa: E402
from review import build_review as blr  # noqa: E402

# 合成 ID。真實群組 ID 不進版控——.gitignore 特意排除了 line_capture_groups.csv，
# 測試檔卻是追蹤的，寫真的等於繞過那道排除。
VENDOR_GROUP = "Cvendorgroup00000000000000000001"


def _ms(date_str: str, hhmm: str) -> int:
    from datetime import datetime

    dt = datetime.strptime(f"{date_str} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=blr.TAIPEI)
    return int(dt.timestamp() * 1000)


def _seed(db: Path, rows: list[dict]) -> None:
    conn = line_capture._connect(db)
    try:
        for i, row in enumerate(rows):
            conn.execute(
                """
                INSERT INTO line_capture_events (
                    dedupe_key, message_id, group_id, project, user_id, event_type,
                    message_type, text, file_name, media_state, media_path,
                    media_bytes, line_ts, revoked_at
                ) VALUES (
                    :dedupe_key, :message_id, :group_id, :project, :user_id, :event_type,
                    :message_type, :text, :file_name, :media_state, :media_path,
                    :media_bytes, :line_ts, :revoked_at
                )
                """,
                {
                    "dedupe_key": row.get("dedupe_key") or f"msg:{row['message_id']}",
                    "message_id": row["message_id"],
                    "group_id": row.get("group_id", VENDOR_GROUP),
                    "project": row.get("project", ""),
                    "user_id": row.get("user_id", "Uvendor"),
                    "event_type": row.get("event_type", "message"),
                    "message_type": row.get("message_type", "text"),
                    "text": row.get("text", ""),
                    "file_name": row.get("file_name", ""),
                    "media_state": row.get("media_state", "none"),
                    "media_path": row.get("media_path", ""),
                    "media_bytes": row.get("media_bytes", 0),
                    "line_ts": row["line_ts"],
                    "revoked_at": row.get("revoked_at", ""),
                },
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def today():
    from datetime import datetime

    return datetime.now(blr.TAIPEI).strftime("%Y-%m-%d")


def test_saved_media_becomes_a_pending_document(tmp_path, today):
    db = tmp_path / "cap.db"
    _seed(
        db,
        [
            {
                "message_id": "MPDF1",
                "message_type": "file",
                "file_name": "2026.8月-請款明細單.pdf",
                "media_state": "saved",
                "media_path": str(tmp_path / "a.pdf"),
                "media_bytes": 1234,
                "line_ts": _ms(today, "14:32"),
            }
        ],
    )
    docs = blr.load_pending_documents(db_path=db)
    assert len(docs) == 1
    assert docs[0]["id"] == "lcdoc_MPDF1"
    assert docs[0]["file_name"] == "2026.8月-請款明細單.pdf"
    assert docs[0]["project_hint"] == "", "廠商群組沒有案場，這格要留給人選"


def test_revoked_media_never_shows_up(tmp_path, today):
    db = tmp_path / "cap.db"
    _seed(
        db,
        [
            {
                "message_id": "MGONE",
                "message_type": "image",
                "media_state": "saved",
                "media_path": str(tmp_path / "b.jpg"),
                "line_ts": _ms(today, "10:00"),
                "revoked_at": "2026-08-16 02:00:00",
            }
        ],
    )
    assert blr.load_pending_documents(db_path=db) == []


def test_already_submitted_document_is_suppressed(tmp_path, today):
    db = tmp_path / "cap.db"
    _seed(
        db,
        [
            {
                "message_id": "MPDF1",
                "message_type": "file",
                "media_state": "saved",
                "media_path": str(tmp_path / "a.pdf"),
                "line_ts": _ms(today, "14:32"),
            }
        ],
    )
    handled = {blr.document_id("MPDF1")}
    assert blr.load_pending_documents(db_path=db, handled=handled) == []


def test_context_carries_the_surrounding_chatter(tmp_path, today):
    """「乙案那批崁燈跟甲案的軌道燈都在裡面」——這句才是判斷案場的依據。"""
    db = tmp_path / "cap.db"
    _seed(
        db,
        [
            {"message_id": "MA", "text": "王總這是八月的明細", "line_ts": _ms(today, "14:31")},
            {
                "message_id": "MPDF1",
                "message_type": "file",
                "media_state": "saved",
                "media_path": str(tmp_path / "a.pdf"),
                "line_ts": _ms(today, "14:32"),
            },
            {"message_id": "MB", "text": "乙案那批崁燈都在裡面", "line_ts": _ms(today, "14:33")},
        ],
    )
    docs = blr.load_pending_documents(db_path=db)
    texts = [c["text"] for c in docs[0]["context"]]
    assert "王總這是八月的明細" in texts
    assert "乙案那批崁燈都在裡面" in texts


def test_pending_media_is_not_offered_yet(tmp_path, today):
    """還沒下載完的東西不能拿去歸檔——來源檔根本還不存在。"""
    db = tmp_path / "cap.db"
    _seed(
        db,
        [
            {
                "message_id": "MWAIT",
                "message_type": "file",
                "media_state": "pending",
                "line_ts": _ms(today, "14:32"),
            }
        ],
    )
    assert blr.load_pending_documents(db_path=db) == []


def test_candidates_are_flattened_with_group_context(tmp_path):
    cand_file = tmp_path / "line_candidates.json"
    cand_file.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "group_id": VENDOR_GROUP,
                        "project": "",
                        "candidates": [{"id": "lc_1", "kind": "quote", "summary": "八月請款"}],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out = blr.load_candidates(cand_file)
    assert len(out) == 1
    assert out[0]["group_id"] == VENDOR_GROUP
    assert out[0]["project_hint"] == ""


def test_missing_candidate_file_is_not_an_error(tmp_path):
    assert blr.load_candidates(tmp_path / "不存在.json") == []


def test_active_projects_cross_checks_vault(tmp_path):
    """只看 Files 會撈到 03_財務報表、06-TechGuide 這種分類夾（實測撈到過），
    出現在下拉裡就是等著被誤選。"""
    files_root = tmp_path / "Files"
    index_root = tmp_path / "index"
    for name in ("範例區_甲案_王先生", "範例區_乙案_李小姐", "03_財務報表",
                 "06-TechGuide", "00-已結案", "_templates", "廠商資料"):
        (files_root / name).mkdir(parents=True)
    for name in ("範例區_甲案_王先生", "範例區_乙案_李小姐", "00-公司營運支出", "_已結案"):
        (index_root / name).mkdir(parents=True)

    # 用排序後的清單比對：中文的排序依 Unicode 碼點，「乙」在「甲」之前，
    # 寫死順序只會製造一條跟行為無關的脆弱斷言。
    assert blr.active_projects(files_root, index_root) == sorted(
        ["範例區_甲案_王先生", "範例區_乙案_李小姐"]
    )


def test_active_projects_falls_back_without_vault(tmp_path):
    """第二棵樹讀不到時退回嚴格命名規則，仍不得放行分類夾。"""
    files_root = tmp_path / "Files"
    for name in ("範例區_甲案_王先生", "03_財務報表", "06-TechGuide"):
        (files_root / name).mkdir(parents=True)
    assert blr.active_projects(files_root, tmp_path / "沒有第二棵樹") == ["範例區_甲案_王先生"]


def test_build_writes_a_complete_payload(tmp_path, today):
    db = tmp_path / "cap.db"
    _seed(
        db,
        [
            {
                "message_id": "MPDF1",
                "message_type": "file",
                "media_state": "saved",
                "media_path": str(tmp_path / "a.pdf"),
                "line_ts": _ms(today, "14:32"),
            }
        ],
    )
    files_root = tmp_path / "Files"
    (files_root / "範例區_甲案_王先生").mkdir(parents=True)
    out = tmp_path / "line_review.json"

    payload = blr.build(
        db_path=db, candidates_path=tmp_path / "none.json", out_path=out,
        files_root=files_root, queue_paths=[],
    )
    assert payload["document_count"] == 1
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["projects"] == ["範例區_甲案_王先生"]
    assert written["documents"][0]["id"] == "lcdoc_MPDF1"
