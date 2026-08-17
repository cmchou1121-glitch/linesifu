# -*- coding: utf-8 -*-
"""LINE 檔案歸檔 committer（L3 人工核可後才會跑到）。

這支會真的往 Files\\ 寫東西，而且四個路徑組件都是外部輸入：
- project 是人從下拉選的，但**直接打 API 可以送任何字串**
- trade / vendor 是人打的
- file_name 來自 LINE 訊息，是外部廠商可控的

2026-08-17 Codex 審出 1 fatal + 10 major，其中三條在這支：來源未綁定暫存區、
案場只檢查「資料夾存在」、全形保留裝置名繞過檢查。下面每條測試對應一個發現。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review import archive  # noqa: E402

PROJECT = "範例區_甲案_王先生"


@pytest.fixture
def env(tmp_path, monkeypatch):
    files = tmp_path / "Files"
    (files / PROJECT).mkdir(parents=True)
    # 非案場但真實存在的目錄——直接打 API 的攻擊面
    (files / "03_財務報表").mkdir()
    # 第二棵樹：用來交叉確認「什麼才算一個專案」。這裡直接放專案名，
    # 不像原系統還隔一層 01-Projects——蒸餾版把那層當成呼叫端的事。
    index_root = tmp_path / "index"
    (index_root / PROJECT).mkdir(parents=True)

    staging = tmp_path / "staging"
    src = staging / "2026-08" / "627500000000000001.pdf"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"%PDF-1.7 fake statement")
    monkeypatch.setenv("LINE_CAPTURE_MEDIA_DIR", str(staging))

    return {"files": files, "vault": index_root, "src": src, "staging": staging, "tmp": tmp_path}


def _entry(env, **overrides):
    payload = {
        "project": PROJECT,
        "trade": "燈具",
        "vendor": "範例燈飾",
        "file_name": "2026.8月-請款明細單.pdf",
        "media_path": str(env["src"]),
        "sent_at": "2026-08-16 14:32",
    }
    payload.update(overrides)
    return {"form": "line_archive", "payload": payload}


def _run(env, entry, *, commit):
    return archive.commit_line_archive(
        entry, commit=commit, files=env["files"], vault=env["vault"]
    )


# ── 正常路徑 ──────────────────────────────────────────────────────────────────

def test_dry_run_touches_nothing(env):
    out = _run(env, _entry(env), commit=False)
    assert out["action"] == "DRY-RUN"
    assert out["already_exists"] is False
    assert list((env["files"] / PROJECT).rglob("*.pdf")) == [], "dry-run 不得寫任何檔案"


def test_commit_files_into_the_vendor_subcontract_folder(env):
    out = _run(env, _entry(env), commit=True)
    assert out["ok"] is True
    target = Path(out["target"])
    assert target.read_bytes() == b"%PDF-1.7 fake statement"
    rel = target.relative_to(env["files"])
    assert rel.parts[:4] == (PROJECT, "vendors", "燈具", "範例燈飾")
    assert target.name.startswith("20260816_範例燈飾_")
    assert target.suffix == ".pdf"


def test_source_is_copied_not_moved(env):
    _run(env, _entry(env), commit=True)
    assert env["src"].exists(), "暫存那份要留著，案場選錯才能重來"


def test_no_partial_file_left_behind(env):
    """複製走同目錄暫存檔＋原子改名，成本文件資料夾裡不該出現 .part 殘骸。"""
    _run(env, _entry(env), commit=True)
    leftovers = [p.name for p in (env["files"]).rglob("*.part*")]
    assert leftovers == []


# ── 來源綁定（Codex major：任意檔案可被複製進 Files）──────────────────────────

def test_source_outside_staging_is_refused(env, tmp_path):
    """帶 finance 身分直接打 API 送 runtime\\.env 的路徑，就能把金鑰檔複製進案場。"""
    secret = tmp_path / "runtime" / ".env"
    secret.parent.mkdir(parents=True)
    secret.write_text("LINE_CHANNEL_ACCESS_TOKEN=super-secret", encoding="utf-8")

    out = _run(env, _entry(env, media_path=str(secret)), commit=True)
    assert out["ok"] is False
    assert "暫存區" in out["error"]
    assert list(env["files"].rglob("*.env")) == []


def test_symlink_escape_from_staging_is_refused(env, tmp_path):
    """暫存區裡的符號連結指到外面，resolve() 之後必須被擋下。"""
    secret = tmp_path / "outside.txt"
    secret.write_text("secret", encoding="utf-8")
    link = env["staging"] / "2026-08" / "link.pdf"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("這台機器不允許建立符號連結")

    out = _run(env, _entry(env, media_path=str(link)), commit=True)
    assert out["ok"] is False


def test_missing_source_file_is_refused(env):
    out = _run(env, _entry(env, media_path=str(env["staging"] / "不存在.pdf")), commit=True)
    assert out["ok"] is False
    assert list(env["files"].rglob("*.pdf")) == []


# ── 案場驗證（Codex major：只檢查資料夾存在不夠）──────────────────────────────

def test_existing_but_non_project_directory_is_refused(env):
    """03_財務報表 是 Files 底下真實存在的目錄，但它不是案場。"""
    out = _run(env, _entry(env, project="03_財務報表"), commit=True)
    assert out["ok"] is False
    assert "不是有效專案" in out["error"]
    assert list((env["files"] / "03_財務報表").rglob("*")) == []


def test_project_not_in_vault_is_refused(env):
    (env["files"] / "假案場_甲_乙").mkdir()
    out = _run(env, _entry(env, project="假案場_甲_乙"), commit=True)
    assert out["ok"] is False
    assert "不是有效專案" in out["error"]


def test_project_with_trailing_dot_is_refused(env):
    """`案場名\\.` 能通過原始字串檢查，洗完卻指向別的目錄。"""
    out = _run(env, _entry(env, project=PROJECT + "\\."), commit=True)
    assert out["ok"] is False


def test_missing_project_is_refused(env):
    out = _run(env, _entry(env, project=""), commit=True)
    assert out["ok"] is False
    assert "專案" in out["error"]


# ── 路徑組裝安全 ──────────────────────────────────────────────────────────────

def test_path_traversal_in_fields_cannot_escape(env):
    out = _run(
        env,
        _entry(env, trade="../../../..", vendor="../evil", file_name="../../x.pdf"),
        commit=True,
    )
    assert out["ok"] is True
    Path(out["target"]).resolve().relative_to((env["files"] / PROJECT).resolve())


def test_windows_reserved_names_do_not_break_mkdir(env):
    out = _run(env, _entry(env, trade="CON", vendor="PRN"), commit=True)
    assert out["ok"] is True
    assert "_CON" in out["target"] and "_PRN" in out["target"]


def test_fullwidth_reserved_names_are_normalised_first(env):
    """COM¹ / ＣＯＮ 是保留裝置名的變體，NFKC 正規化前檢查會漏掉。"""
    out = _run(env, _entry(env, trade="COM¹", vendor="ＣＯＮ"), commit=True)
    assert out["ok"] is True
    target = Path(out["target"])
    assert target.exists()
    assert "_COM1" in str(target) and "_CON" in str(target)


def test_overlong_path_is_refused_not_crashed(env):
    out = _run(env, _entry(env, trade="長" * 60, vendor="廠" * 60,
                           file_name="檔" * 60 + ".pdf"), commit=True)
    if out["ok"] is False:
        assert "過長" in out["error"]
    else:
        assert Path(out["target"]).exists()


def test_dangerous_extension_is_neutralised(env):
    out = _run(env, _entry(env, file_name="statement.pdf.....截斷"), commit=True)
    assert out["ok"] is True
    assert Path(out["target"]).suffix == ".bin"


def test_same_name_is_not_overwritten(env):
    first = _run(env, _entry(env), commit=True)
    env["src"].write_bytes(b"%PDF-1.7 second different statement")
    second = _run(env, _entry(env), commit=True)

    assert second["ok"] is True
    assert first["target"] != second["target"]
    assert Path(first["target"]).read_bytes() == b"%PDF-1.7 fake statement"
    assert Path(second["target"]).read_bytes() == b"%PDF-1.7 second different statement"


def test_dry_run_reports_a_target(env):
    out = archive.commit_line_archive(
        _entry(env), commit=False, vault=env["vault"], files=env["files"]
    )
    assert out["action"] == "DRY-RUN"
    assert "target" in out
