# -*- coding: utf-8 -*-
"""L1 被動落地測試。

每一條都對應 2026-08-15 三鏡頭抗辯抓到的一個缺陷，不是為了湊覆蓋率：
- capture 拋錯不得傷到同批 finance 事件（skeptic / red-team 各判 fatal）
- unsend 用墓碑不用 DELETE，否則重送會讓已撤回內容復活（skeptic fatal）
- 墓碑要能先於原訊息建立（webhook 每個 POST 一條 thread，無序）
- 去重雜湊要剔除 deliveryContext，否則 isRedelivery 翻轉會多出一列（red-team major）
- 白名單預設空，否則會錄到 1:1 私訊與財務群組（red-team major）
- 媒體下載與 unsend 的競態要互鎖（skeptic major）
"""
import base64
import hashlib
import hmac
import json
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from capture import receiver  # noqa: E402
from capture import line_capture  # noqa: E402

GROUP_ID = "Cgroup0000000000000000000000000001"
OTHER_GROUP = "Cgroup0000000000000000000000000099"


@pytest.fixture
def capture_on(monkeypatch, tmp_path):
    """開啟 capture 並把 DB／媒體都導到 tmp_path。"""
    db = tmp_path / "line_capture.db"
    media = tmp_path / "media"
    monkeypatch.setenv("LINE_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("LINE_CAPTURE_GROUP_IDS", GROUP_ID)
    monkeypatch.setenv("LINE_CAPTURE_DB_PATH", str(db))
    monkeypatch.setenv("LINE_CAPTURE_MEDIA_DIR", str(media))
    return {"db": db, "media": media}


def _sign(body: bytes, secret: str) -> str:
    return base64.b64encode(
        hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("utf-8")


def _text_event(text="泥作明天進場", message_id="MID001", group_id=GROUP_ID, **kw):
    event = {
        "type": "message",
        "webhookEventId": kw.pop("webhook_event_id", "01EVENT0000000000000000001"),
        "mode": "active",
        "timestamp": 1755230000000,
        "deliveryContext": {"isRedelivery": False},
        "source": {"type": "group", "groupId": group_id, "userId": "Uworker001"},
        "message": {"type": "text", "id": message_id, "text": text},
    }
    event.update(kw)
    return event


def _rows(db: Path):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM line_capture_events ORDER BY id"
        ).fetchall()]
    finally:
        conn.close()


# ── 閘門 ──────────────────────────────────────────────────────────────────────

def test_disabled_by_default_writes_nothing(tmp_path, monkeypatch):
    db = tmp_path / "line_capture.db"
    monkeypatch.setenv("LINE_CAPTURE_DB_PATH", str(db))
    assert line_capture.capture_event(_text_event()) is None
    assert not db.exists(), "capture 未啟用時不得建立 DB 檔"


def test_empty_allowlist_captures_nothing(monkeypatch, tmp_path):
    """白名單預設空＝什麼都不錄。這是隱私上的預設拒絕。"""
    db = tmp_path / "line_capture.db"
    monkeypatch.setenv("LINE_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("LINE_CAPTURE_DB_PATH", str(db))
    monkeypatch.delenv("LINE_CAPTURE_GROUP_IDS", raising=False)
    assert line_capture.capture_event(_text_event()) is None
    assert not db.exists()


def test_group_not_in_allowlist_is_skipped(capture_on):
    assert line_capture.capture_event(_text_event(group_id=OTHER_GROUP)) is None


def test_discover_mode_logs_group_id_but_captures_nothing(monkeypatch, tmp_path, caplog):
    """LINE 不會告訴你 groupId，只能從實際事件裡看。
    探索模式記下 id 就好，絕不能連內容一起記。"""
    db = tmp_path / "line_capture.db"
    monkeypatch.setenv("LINE_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("LINE_CAPTURE_DB_PATH", str(db))
    monkeypatch.delenv("LINE_CAPTURE_GROUP_IDS", raising=False)
    monkeypatch.setenv("LINE_CAPTURE_DISCOVER", "true")

    with caplog.at_level(logging.INFO, logger="capture.line_capture"):
        assert line_capture.capture_event(_text_event(text="機密報價三十五萬")) is None

    assert GROUP_ID in caplog.text, "探索模式要記下 group_id 才撈得到"
    assert "機密報價三十五萬" not in caplog.text, "探索模式不得記錄訊息內容"
    assert not db.exists(), "探索模式不得建立 DB"


def test_groups_csv_maps_project_and_acts_as_allowlist(monkeypatch, tmp_path):
    """CSV 同時是白名單與案場對應：沒列進去的群組不擷取。"""
    csv_path = tmp_path / "groups.csv"
    csv_path.write_text(
        "line_group_id,project,note\n"
        f"{GROUP_ID},範例區_乙案_李小姐,案場群組\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LINE_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("LINE_CAPTURE_DB_PATH", str(tmp_path / "cap.db"))
    monkeypatch.setenv("LINE_CAPTURE_GROUPS_CSV", str(csv_path))
    monkeypatch.delenv("LINE_CAPTURE_GROUP_IDS", raising=False)

    assert line_capture.capture_event(_text_event()) is not None
    assert line_capture.capture_event(_text_event(group_id=OTHER_GROUP)) is None

    rows = _rows(tmp_path / "cap.db")
    assert len(rows) == 1
    assert rows[0]["project"] == "範例區_乙案_李小姐"


def test_csv_with_bom_is_read(monkeypatch, tmp_path):
    """Excel 存出來的 CSV 帶 BOM，不處理的話第一個欄名會變成 ﻿line_group_id。"""
    csv_path = tmp_path / "groups.csv"
    csv_path.write_text(
        "line_group_id,project,note\n" f"{GROUP_ID},範例區_甲案_王先生,\n",
        encoding="utf-8-sig",
    )
    monkeypatch.setenv("LINE_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("LINE_CAPTURE_DB_PATH", str(tmp_path / "cap.db"))
    monkeypatch.setenv("LINE_CAPTURE_GROUPS_CSV", str(csv_path))
    monkeypatch.delenv("LINE_CAPTURE_GROUP_IDS", raising=False)

    assert line_capture.capture_event(_text_event()) is not None
    assert _rows(tmp_path / "cap.db")[0]["project"] == "範例區_甲案_王先生"


def test_env_group_ids_still_work_without_csv(capture_on):
    """既有 .env 設定要繼續有效，只是沒有案場名。"""
    out = line_capture.capture_event(_text_event())
    assert out["inserted"] is True
    assert _rows(capture_on["db"])[0]["project"] == ""


def test_existing_db_gets_new_columns(capture_on):
    """舊 DB 已經有資料了，新欄位要靠 ALTER 補上，不能整張表重建。"""
    db = capture_on["db"]
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE line_capture_events (
            id INTEGER PRIMARY KEY,
            dedupe_key TEXT NOT NULL UNIQUE,
            message_id TEXT NOT NULL DEFAULT '',
            group_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL DEFAULT '',
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
            revoked_at TEXT NOT NULL DEFAULT '',
            captured_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO line_capture_events (dedupe_key, text) VALUES ('msg:OLD', '舊資料');
        """
    )
    conn.commit()
    conn.close()

    assert line_capture.capture_event(_text_event()) is not None
    rows = _rows(db)
    assert len(rows) == 2
    assert rows[0]["text"] == "舊資料", "既有資料必須保留"
    assert rows[0]["project"] == ""
    assert "file_name" in rows[0]


def test_discover_off_is_silent(monkeypatch, tmp_path, caplog):
    db = tmp_path / "line_capture.db"
    monkeypatch.setenv("LINE_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("LINE_CAPTURE_DB_PATH", str(db))
    monkeypatch.delenv("LINE_CAPTURE_GROUP_IDS", raising=False)
    # discover 自 2026-08-16 起預設開啟，要關就得明確設 false
    monkeypatch.setenv("LINE_CAPTURE_DISCOVER", "false")

    with caplog.at_level(logging.INFO, logger="capture.line_capture"):
        assert line_capture.capture_event(_text_event()) is None

    assert GROUP_ID not in caplog.text


def test_discover_is_on_by_default(monkeypatch, tmp_path, caplog):
    """關著的代價是「群組加了、訊息也收到了，卻撈不到 id」——範例燈飾群組實際踩過。"""
    monkeypatch.setenv("LINE_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("LINE_CAPTURE_DB_PATH", str(tmp_path / "cap.db"))
    monkeypatch.delenv("LINE_CAPTURE_GROUP_IDS", raising=False)
    monkeypatch.delenv("LINE_CAPTURE_DISCOVER", raising=False)

    with caplog.at_level(logging.INFO, logger="capture.line_capture"):
        assert line_capture.capture_event(_text_event(text="不該被記下來的內容")) is None

    assert GROUP_ID in caplog.text
    assert "不該被記下來的內容" not in caplog.text


def test_direct_message_is_never_captured(capture_on):
    """1:1 私訊含廠商銀行帳號與業主資料，永遠不錄——即使 userId 在白名單裡。"""
    event = _text_event()
    event["source"] = {"type": "user", "userId": GROUP_ID}
    assert line_capture.capture_event(event) is None


def test_allowlisted_group_is_captured(capture_on):
    out = line_capture.capture_event(_text_event())
    assert out["inserted"] is True
    rows = _rows(capture_on["db"])
    assert len(rows) == 1
    assert rows[0]["text"] == "泥作明天進場"
    assert rows[0]["group_id"] == GROUP_ID
    assert rows[0]["dedupe_key"] == "msg:MID001"
    assert rows[0]["media_state"] == "none"


# ── 去重 ──────────────────────────────────────────────────────────────────────

def test_redelivery_does_not_duplicate(capture_on):
    line_capture.capture_event(_text_event())
    again = _text_event()
    again["deliveryContext"] = {"isRedelivery": True}
    out = line_capture.capture_event(again)
    assert out["inserted"] is False
    assert len(_rows(capture_on["db"])) == 1


def test_hash_key_ignores_delivery_context(capture_on):
    """LINE 重送時 isRedelivery 由 false 翻 true；雜湊必須先剔除它，
    否則沒有 message.id 也沒有 webhookEventId 的事件會多出一列。"""
    base = _text_event(message_id="", webhook_event_id="")
    resent = _text_event(message_id="", webhook_event_id="")
    resent["deliveryContext"] = {"isRedelivery": True}
    assert line_capture._dedupe_key(base) == line_capture._dedupe_key(resent)

    line_capture.capture_event(base)
    line_capture.capture_event(resent)
    assert len(_rows(capture_on["db"])) == 1


def test_blank_ids_fall_through_to_hash_not_literal_prefix(capture_on):
    """空 message.id 不能退化成字面 'msg:'，否則之後同類訊息全被 IGNORE 丟掉。"""
    a = _text_event(text="第一則", message_id="", webhook_event_id="")
    b = _text_event(text="第二則", message_id="", webhook_event_id="")
    key_a = line_capture._dedupe_key(a)
    key_b = line_capture._dedupe_key(b)
    assert key_a.startswith("hash:") and key_b.startswith("hash:")
    assert key_a != key_b

    line_capture.capture_event(a)
    line_capture.capture_event(b)
    assert len(_rows(capture_on["db"])) == 2


def test_message_edited_lands_as_new_row(capture_on):
    """編輯事件帶的是原訊息 id，若用 msg: 會撞掉原列而被 IGNORE。
    改用 evt: 另存一列，原文保留，由 L2 讀取時取最新版。"""
    line_capture.capture_event(_text_event(text="泥作明天進場"))
    edited = {
        "type": "messageEdited",
        "webhookEventId": "01EVENT0000000000000000002",
        "mode": "active",
        "timestamp": 1755230500000,
        "deliveryContext": {"isRedelivery": False},
        "source": {"type": "group", "groupId": GROUP_ID, "userId": "Uworker001"},
        "message": {"type": "text", "id": "MID001", "text": "泥作後天進場"},
    }
    line_capture.capture_event(edited)

    rows = _rows(capture_on["db"])
    assert len(rows) == 2
    assert rows[0]["text"] == "泥作明天進場", "原文必須保留"
    assert rows[1]["text"] == "泥作後天進場"
    assert rows[1]["dedupe_key"].startswith("evt:")


# ── 收回訊息（LINE 明文要求從資料庫刪除） ─────────────────────────────────────

def _unsend_event(message_id="MID001"):
    return {
        "type": "unsend",
        "webhookEventId": "01EVENT0000000000000000003",
        "mode": "active",
        "timestamp": 1755231000000,
        "deliveryContext": {"isRedelivery": False},
        "source": {"type": "group", "groupId": GROUP_ID, "userId": "Uworker001"},
        "unsend": {"messageId": message_id},
    }


def test_unsend_scrubs_text_and_raw_json(capture_on):
    """只清 text 不清 raw_json 等於沒清——原文就在 raw_json 裡。"""
    line_capture.capture_event(_text_event(text="這句話講錯了"))
    assert "這句話講錯了" in _rows(capture_on["db"])[0]["raw_json"]

    out = line_capture.capture_event(_unsend_event())
    assert out["revoked"] is True

    row = _rows(capture_on["db"])[0]
    assert row["text"] == ""
    assert row["raw_json"] == ""
    assert row["revoked_at"] != ""
    assert row["media_state"] == "revoked"


def test_unsent_message_does_not_resurrect_on_redelivery(capture_on):
    """抗辯抓到的 fatal：若 unsend 用 DELETE，LINE 重送原訊息時
    INSERT OR IGNORE 找不到列，已撤回的內容會永久復活。"""
    line_capture.capture_event(_text_event(text="這句話講錯了"))
    line_capture.capture_event(_unsend_event())

    resent = _text_event(text="這句話講錯了")
    resent["deliveryContext"] = {"isRedelivery": True}
    line_capture.capture_event(resent)

    rows = _rows(capture_on["db"])
    assert len(rows) == 1
    assert rows[0]["text"] == "", "撤回的內容不得因重送而復活"
    assert rows[0]["raw_json"] == ""


def test_unsend_arriving_before_message_still_blocks_it(capture_on):
    """每個 webhook POST 各跑一條 daemon thread，unsend 可能先於原訊息處理完。
    墓碑要能預先建立，否則原訊息之後落地就永遠留著了。"""
    out = line_capture.capture_event(_unsend_event())
    assert out["created_ahead"] is True

    line_capture.capture_event(_text_event(text="這句話講錯了"))

    rows = _rows(capture_on["db"])
    assert len(rows) == 1
    assert rows[0]["text"] == ""
    assert rows[0]["media_state"] == "revoked"


def test_unsend_unlinks_downloaded_media(capture_on, monkeypatch):
    event = _text_event(message_id="MIDIMG1")
    event["message"] = {"type": "image", "id": "MIDIMG1"}
    line_capture.capture_event(event)

    line_capture.drain_media(
        access_token="tok",
        fetch_fn=lambda mid, tok, **kw: b"JPEGBYTES",
    )
    saved = _rows(capture_on["db"])[0]["media_path"]
    assert Path(saved).exists()

    line_capture.capture_event(_unsend_event(message_id="MIDIMG1"))
    assert not Path(saved).exists(), "撤回訊息的照片必須一併刪除"


# ── 媒體 ──────────────────────────────────────────────────────────────────────

def _image_event(message_id="MIDIMG1"):
    event = _text_event(message_id=message_id)
    event["message"] = {"type": "image", "id": message_id}
    return event


def test_media_marked_pending_then_saved(capture_on):
    line_capture.capture_event(_image_event())
    assert _rows(capture_on["db"])[0]["media_state"] == "pending"

    stats = line_capture.drain_media(
        access_token="tok",
        fetch_fn=lambda mid, tok, **kw: b"JPEGBYTES",
    )
    assert stats["saved"] == 1
    row = _rows(capture_on["db"])[0]
    assert row["media_state"] == "saved"
    assert row["media_bytes"] == len(b"JPEGBYTES")
    assert Path(row["media_path"]).read_bytes() == b"JPEGBYTES"


def test_media_download_failure_is_recorded_not_raised(capture_on):
    line_capture.capture_event(_image_event())

    def boom(mid, tok, **kw):
        raise RuntimeError("content expired")

    stats = line_capture.drain_media(access_token="tok", fetch_fn=boom)
    assert stats["failed"] == 1
    row = _rows(capture_on["db"])[0]
    assert row["media_attempts"] == 1
    assert "content expired" in row["media_error"]


def test_media_revoked_midflight_is_deleted(capture_on):
    """下載途中訊息被收回：檔案已寫下去，必須立刻刪掉且不得標記 saved。"""
    line_capture.capture_event(_image_event())

    def fetch_then_revoke(mid, tok, **kw):
        line_capture.capture_event(_unsend_event(message_id=mid))
        return b"JPEGBYTES"

    stats = line_capture.drain_media(access_token="tok", fetch_fn=fetch_then_revoke)
    assert stats["saved"] == 0
    row = _rows(capture_on["db"])[0]
    assert row["media_state"] == "revoked"
    assert row["media_path"] == ""
    media_root = capture_on["media"]
    leftovers = list(media_root.rglob("*")) if media_root.exists() else []
    assert [p for p in leftovers if p.is_file()] == [], "競態輸掉時不得留下檔案"


def _file_event(message_id="MIDPDF1", file_name="甲案_泥作報價單.pdf"):
    event = _text_event(message_id=message_id)
    event["message"] = {
        "type": "file",
        "id": message_id,
        "fileName": file_name,
        "fileSize": 123456,
    }
    return event


def test_pdf_keeps_extension_and_original_filename(capture_on):
    """PDF 存成 .bin 的話 L2 還要猜型別；fileName 只出現這一次，之後拿不到。"""
    line_capture.capture_event(_file_event())
    row = _rows(capture_on["db"])[0]
    assert row["message_type"] == "file"
    assert row["file_name"] == "甲案_泥作報價單.pdf"
    assert row["media_state"] == "pending"

    line_capture.drain_media(
        access_token="tok",
        fetch_fn=lambda mid, tok, **kw: b"%PDF-1.7 fake",
    )
    row = _rows(capture_on["db"])[0]
    assert row["media_state"] == "saved"
    assert Path(row["media_path"]).suffix == ".pdf"


def test_file_without_usable_extension_falls_back_to_bin(capture_on):
    line_capture.capture_event(_file_event(file_name="沒有副檔名的檔"))
    line_capture.drain_media(access_token="tok", fetch_fn=lambda mid, tok, **kw: b"x")
    assert Path(_rows(capture_on["db"])[0]["media_path"]).suffix == ".bin"


def test_malicious_filename_cannot_escape_media_root(capture_on):
    """fileName 是對方給的字串，只准拿來取副檔名，不准參與組路徑。"""
    line_capture.capture_event(_file_event(file_name="../../../../evil.exe"))
    line_capture.drain_media(access_token="tok", fetch_fn=lambda mid, tok, **kw: b"x")

    saved = Path(_rows(capture_on["db"])[0]["media_path"]).resolve()
    saved.relative_to(capture_on["media"].resolve())  # 逃出 root 就丟 ValueError
    assert saved.name.startswith("MIDPDF1")


def test_media_is_filed_under_project_folder(monkeypatch, tmp_path):
    """要的分類：照片/檔案落地時就按案場分資料夾，人才找得到。"""
    csv_path = tmp_path / "groups.csv"
    csv_path.write_text(
        f"line_group_id,project,note\n{GROUP_ID},範例區_甲案_王先生,\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LINE_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("LINE_CAPTURE_DB_PATH", str(tmp_path / "cap.db"))
    monkeypatch.setenv("LINE_CAPTURE_MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("LINE_CAPTURE_GROUPS_CSV", str(csv_path))
    monkeypatch.delenv("LINE_CAPTURE_GROUP_IDS", raising=False)

    line_capture.capture_event(_image_event())
    line_capture.drain_media(access_token="tok", fetch_fn=lambda mid, tok, **kw: b"JPEG")

    row = _rows(tmp_path / "cap.db")[0]
    assert row["project"] == "範例區_甲案_王先生"
    assert "範例區_甲案_王先生" in row["media_path"]
    assert Path(row["media_path"]).exists()


def test_media_without_project_falls_back_to_group_folder(capture_on):
    line_capture.capture_event(_image_event())
    line_capture.drain_media(access_token="tok", fetch_fn=lambda mid, tok, **kw: b"JPEG")
    assert GROUP_ID in _rows(capture_on["db"])[0]["media_path"]


def test_losing_the_drain_race_does_not_delete_the_winners_file(capture_on):
    """Codex 抓到的：兩條 webhook thread 同時抓同一列會寫同一個路徑，
    晚到的那條 commit 不到，若無腦刪檔就會把先到那條剛存好的有效檔案帶走
    ——DB 標 saved、磁碟上卻沒東西。"""
    line_capture.capture_event(_image_event())
    db = capture_on["db"]

    def fetch_while_another_thread_finishes(mid, tok, **kw):
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE line_capture_events SET media_state='saved', media_path=? "
            "WHERE message_id=?",
            ("C:/fake/winner.jpg", mid),
        )
        conn.commit()
        conn.close()
        return b"JPEGBYTES"

    stats = line_capture.drain_media(
        access_token="tok", fetch_fn=fetch_while_another_thread_finishes
    )
    assert stats["saved"] == 0
    files = [p for p in capture_on["media"].rglob("*") if p.is_file()]
    assert len(files) == 1, "輸掉競賽不得刪掉贏家的檔案"


def test_second_drain_skips_a_row_already_claimed(capture_on):
    """認領之後狀態變 downloading，另一輪 drain 不該再抓一次。"""
    line_capture.capture_event(_image_event())
    row_id = _rows(capture_on["db"])[0]["id"]

    assert line_capture._claim_media(capture_on["db"], row_id) is True
    calls = []
    stats = line_capture.drain_media(
        access_token="tok",
        fetch_fn=lambda mid, tok, **kw: (calls.append(mid), b"X")[1],
    )
    assert calls == [], "已被認領的列不該被重複下載"
    assert stats["saved"] == 0


def test_expired_claim_is_picked_up_again(capture_on, monkeypatch):
    """認領的 thread 死掉時，租約過期後要能重撿，否則檔案永遠抓不回來。"""
    line_capture.capture_event(_image_event())
    row_id = _rows(capture_on["db"])[0]["id"]
    line_capture._claim_media(capture_on["db"], row_id)

    monkeypatch.setenv("LINE_CAPTURE_MEDIA_LEASE_SECONDS", "0")
    stats = line_capture.drain_media(
        access_token="tok", fetch_fn=lambda mid, tok, **kw: b"JPEGBYTES"
    )
    assert stats["saved"] == 1


def test_windows_reserved_project_name_still_downloads(monkeypatch, tmp_path):
    """CSV 的 project 填到 Windows 保留裝置名（CON/PRN/NUL/COM1…）時，
    mkdir 會直接拋錯；沒接住的話整個 drain 迴圈會中斷。"""
    csv_path = tmp_path / "groups.csv"
    csv_path.write_text(
        f"line_group_id,project,note\n{GROUP_ID},CON,保留裝置名\n", encoding="utf-8"
    )
    monkeypatch.setenv("LINE_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("LINE_CAPTURE_DB_PATH", str(tmp_path / "cap.db"))
    monkeypatch.setenv("LINE_CAPTURE_MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("LINE_CAPTURE_GROUPS_CSV", str(csv_path))
    monkeypatch.delenv("LINE_CAPTURE_GROUP_IDS", raising=False)

    line_capture.capture_event(_image_event())
    stats = line_capture.drain_media(
        access_token="tok", fetch_fn=lambda mid, tok, **kw: b"JPEGBYTES"
    )
    assert stats["saved"] == 1
    saved_path = _rows(tmp_path / "cap.db")[0]["media_path"]
    assert Path(saved_path).exists()
    assert "_CON" in saved_path


def test_mkdir_failure_is_recorded_not_raised(capture_on, monkeypatch):
    """任何 mkdir 失敗都不能讓 drain 迴圈整個中斷——否則該列永遠停在原狀態，
    attempts 不增加，每次 webhook 都再失敗一次。"""
    line_capture.capture_event(_image_event())
    real_mkdir = Path.mkdir
    media_root = str(capture_on["media"])

    def selective_boom(self, *args, **kwargs):
        if str(self).startswith(media_root):
            raise OSError("mkdir denied")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", selective_boom)
    stats = line_capture.drain_media(
        access_token="tok", fetch_fn=lambda mid, tok, **kw: b"JPEGBYTES"
    )
    assert stats["failed"] == 1
    row = _rows(capture_on["db"])[0]
    assert "mkdir denied" in row["media_error"]
    assert row["media_attempts"] == 1, "失敗要計次，否則會無限重試"


def test_unsend_also_scrubs_the_edited_copy(capture_on):
    """Codex 順帶點出的合規漏洞：編輯過的訊息另存成 evt: 一列，
    只清 msg: 那列的話，被收回的文字仍完整留在編輯列裡。"""
    line_capture.capture_event(_text_event(text="原始說法"))
    line_capture.capture_event(
        {
            "type": "messageEdited",
            "webhookEventId": "01EVENT0000000000000000009",
            "mode": "active",
            "timestamp": 1755230500000,
            "deliveryContext": {"isRedelivery": False},
            "source": {"type": "group", "groupId": GROUP_ID, "userId": "Uworker001"},
            "message": {"type": "text", "id": "MID001", "text": "改過的說法"},
        }
    )
    assert len(_rows(capture_on["db"])) == 2

    line_capture.capture_event(_unsend_event(message_id="MID001"))

    rows = _rows(capture_on["db"])
    assert len(rows) == 2
    for row in rows:
        assert row["text"] == "", f"第 {row['id']} 列仍留著被收回的內容"
        assert row["raw_json"] == ""
        assert row["revoked_at"] != ""


def test_invalid_message_id_never_reaches_filesystem(capture_on):
    """message_id 會被組進 URL 與檔名，格式不合就不准往下走。"""
    event = _image_event(message_id="../../etc/passwd")
    line_capture.capture_event(event)
    row = _rows(capture_on["db"])[0]
    assert row["media_state"] == "none", "非法 id 不該被排進下載佇列"


# ── 與接收器串起來（整條 webhook 路徑） ──────────────────────────────────────

def test_capture_failure_never_breaks_the_batch(monkeypatch, capture_on):
    """一則事件處理失敗，不得把同一批後面的事件一起帶走。

    這條在原始系統是最高優先：擷取是後加的旁路，絕不能成為既有業務流程的殺手。
    而且因為 webhook 早就回過 200，LINE 不會再送第二次——被帶走的就是永久遺失。
    """
    def exploding_capture(event, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(line_capture, "capture_event", exploding_capture)

    secret = "shhh"
    body = json.dumps({"events": [_text_event(), _text_event(message_id="MID002")]}).encode("utf-8")
    out = receiver.process_webhook(body, _sign(body, secret), secret=secret, token="tok")

    assert out["ok"] is True
    assert out["events"] == 2
    assert out["failed"] == 2, "兩則都要被走過，不能第一則爆掉就中斷"


def test_webhook_captures_group_text_end_to_end(capture_on):
    secret = "shhh"
    body = json.dumps({"events": [_text_event(text="三樓浴室磁磚有兩片空心")]}).encode("utf-8")
    out = receiver.process_webhook(body, _sign(body, secret), secret=secret, token="tok")

    assert out["ok"] is True
    assert out["stored"] == 1
    rows = _rows(capture_on["db"])
    assert len(rows) == 1
    assert rows[0]["text"] == "三樓浴室磁磚有兩片空心"


def test_bad_signature_is_discarded(capture_on):
    """驗簽不過就丟棄——不重試、不落地。webhook 是對外開放的端點，
    任何人都能往它 POST，簽章是唯一的門。"""
    body = json.dumps({"events": [_text_event(text="偽造的訊息")]}).encode("utf-8")
    out = receiver.process_webhook(body, _sign(body, "wrong-secret"), secret="shhh", token="tok")

    assert out["ok"] is False
    assert out["reason"] == "bad_signature"
    # 連 DB 都不該被建出來——驗簽失敗代表這個 body 根本不該被信任
    assert not capture_on["db"].exists()


def test_capture_stays_off_when_flag_unset(monkeypatch, tmp_path):
    """旗標沒開時，整條 webhook 路徑不得產生 capture DB。"""
    db = tmp_path / "line_capture.db"
    monkeypatch.setenv("LINE_CAPTURE_DB_PATH", str(db))
    secret = "shhh"
    body = json.dumps({"events": [_text_event()]}).encode("utf-8")

    out = receiver.process_webhook(body, _sign(body, secret), secret=secret, token="tok")

    assert out["ok"] is True
    assert not db.exists()
