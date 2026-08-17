# -*- coding: utf-8 -*-
"""讓 tests/ 能 import capture/ extract/ review/ 三個套件。

同時把所有會寫檔的預設路徑導向 tmp，避免測試污染 repo 目錄——
原始系統就是因為測試沒指定暫存目錄，往正式暫存區倒了 149 個垃圾檔。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolated_paths(monkeypatch, tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("pipeline")
    monkeypatch.setenv("PIPELINE_DATA_DIR", str(sandbox / "data"))
    monkeypatch.setenv("LINE_CAPTURE_ENABLED", "false")
    monkeypatch.setenv("LINE_CAPTURE_DISCOVER", "false")
    monkeypatch.delenv("LINE_CAPTURE_GROUP_IDS", raising=False)
    monkeypatch.delenv("LINE_CAPTURE_DB_PATH", raising=False)
    monkeypatch.delenv("LINE_CAPTURE_MEDIA_DIR", raising=False)
    # 萃取層預設打不到真的 CLI：漏 mock 的路徑要得到 missing_cli（測試變紅），
    # 而不是默默去跑一次真的 Claude。
    monkeypatch.setenv("LINE_EXTRACT_CLAUDE_COMMAND", "claude-cli-disabled-in-tests")
    monkeypatch.setenv(
        "LINE_CAPTURE_GROUPS_CSV", str(sandbox / "absent-groups.csv")
    )