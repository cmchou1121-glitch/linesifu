# -*- coding: utf-8 -*-
"""L3 的寫入端：把暫存區的檔案，依人工指定的專案歸進專案資料夾。

**這是整條管線唯一會寫進正式檔案樹的地方，而且只在人按下確認之後才會跑到。**

路徑的四個組件全部是外部輸入，這是本檔大部分程式碼在處理的事：
- `project`：人從下拉選的，但**直接打 API 可以送任何字串**
- `trade` / `vendor`：人打字輸入的
- `file_name`：來自 LINE 訊息，是外部人可控的

原始系統上線前的稽核在這支抓到三個問題，修法都保留在下面：
1. 來源沒綁定暫存區 → 帶權限的呼叫者可以送 `.env` 的路徑，把金鑰檔複製進共用資料夾
2. 專案只檢查「資料夾存在嗎」 → 同一個根目錄下真實存在的非專案目錄會過關
3. 保留裝置名只比對 ASCII → 全形與上標（`COM¹`、`ＣＯＮ`）繞過檢查後讓 mkdir 拋錯
"""
from __future__ import annotations

import os
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path

# Windows 檔名非法字元
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SAFE_EXT = re.compile(r"^\.[0-9A-Za-z]{1,10}$")

# Windows 保留裝置名。判定看的是第一個句點之前那段，所以 "CON.txt" 一樣中。
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

#: 歸檔目的地的相對樣板。這是原公司的分類法，換一家公司就換這個。
#: 可用 ARCHIVE_SUBPATH 覆寫，支援 {trade} 與 {vendor} 兩個佔位符。
DEFAULT_SUBPATH = "vendors/{trade}/{vendor}"

#: 組出來的完整路徑上限。Windows 沒開長路徑支援時，五段各 60 字很容易破表；
#: 與其讓它在 copy 當下拋 OSError，不如提早擋下並告訴人要縮短哪一欄。
MAX_PATH_CHARS = 240


def safe_token(value, fallback: str) -> str:
    """把外部輸入洗成可以當單一路徑片段的字串。"""
    # 先 NFKC：全形與上標（ＣＯＮ、COM¹）會正規化成 ASCII，否則繞過下面的保留名檢查
    cleaned = unicodedata.normalize("NFKC", str(value or "")).strip()
    cleaned = _UNSAFE.sub("_", cleaned).strip(". ")
    if not cleaned:
        return fallback
    if cleaned.split(".")[0].upper() in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:60]


def staging_root() -> Path:
    """擷取層存放媒體的暫存區。歸檔來源只准落在這底下。"""
    env = os.getenv("LINE_CAPTURE_MEDIA_DIR", "").strip()
    if env:
        return Path(env).resolve()
    return (Path(__file__).resolve().parents[1] / "data" / "line_capture_media").resolve()


def valid_projects(files_root: Path, index_root: Path) -> set[str]:
    """有效專案＝兩棵樹都認得的名字。

    只問「這個資料夾存在嗎」是不夠的：同一個根目錄下往往還有報表、範本之類真實
    存在的目錄，那些也會過關。而 `專案名\\.` 這種寫法能通過原始字串比對、
    正規化之後卻指向別的地方——所以要用**精確集合比對**，不是存在性檢查。
    """
    def usable(name: str) -> bool:
        return bool(name) and name[0] not in "_0123456789" and not name.startswith("00-")

    try:
        left = {d.name for d in Path(files_root).iterdir() if d.is_dir() and usable(d.name)}
    except OSError:
        return set()
    index = Path(index_root)
    if not index.is_dir():
        return set()
    try:
        right = {d.name for d in index.iterdir() if d.is_dir() and usable(d.name)}
    except OSError:
        return set()
    return left & right


def archive_target(payload: dict, files_root: Path) -> Path:
    project = safe_token(payload.get("project"), "")
    trade = safe_token(payload.get("trade"), "_未分類")
    vendor = safe_token(payload.get("vendor"), "_未辨識")
    src = Path(str(payload.get("media_path") or ""))

    original = str(payload.get("file_name") or "").strip() or src.name
    stem = safe_token(Path(original).stem, "檔案")
    suffix = Path(original).suffix or src.suffix or ".bin"
    if not _SAFE_EXT.match(suffix):
        # 副檔名只從原檔名取，而且必須是乾淨的短字串；否則退回 .bin。
        # 少了這道，"x.pdf.....截斷" 這種檔名會產生奇怪的結果。
        suffix = ".bin"

    date_token = re.sub(r"\D", "", str(payload.get("sent_at") or ""))[:8]
    if len(date_token) != 8:
        date_token = datetime.now().strftime("%Y%m%d")

    subpath = os.getenv("ARCHIVE_SUBPATH", DEFAULT_SUBPATH).format(trade=trade, vendor=vendor)
    name = f"{date_token}_{vendor}_{stem}{suffix}"
    return Path(files_root) / project / subpath / name


def commit_line_archive(
    entry: dict,
    *,
    commit: bool,
    files: Path | str,
    vault: Path | str,
) -> dict:
    """把暫存的檔案複製進專案資料夾。

    **複製不移動**：暫存那份留著。專案選錯是可預期的（一個廠商群組往往橫跨多個
    專案），留著原檔才能重來。暫存區本來就該有保留期，不會無限長大。

    參數 `vault` 是「另一棵用來確認專案清單的樹」，沿用原系統的名字。
    """
    payload = entry.get("payload", {}) or {}
    action = "COMMIT" if commit else "DRY-RUN"
    project = str(payload.get("project") or "").strip()
    src = Path(str(payload.get("media_path") or ""))
    files_root = Path(files)

    if not project:
        return {"action": action, "ok": False,
                "error": "缺少專案——一個群組可能橫跨多個專案，這格只能由人指定，不接受推測"}

    if project not in valid_projects(files_root, Path(vault)):
        return {"action": action, "ok": False,
                "error": f"「{project}」不是有效專案（需同時存在於兩棵樹）"}

    # 來源必須落在暫存區內。少了這道，帶權限的呼叫者可以送任意本機路徑，
    # 把設定檔或金鑰複製進共用資料夾。resolve() 之後再比對，順便擋掉符號連結。
    try:
        src_resolved = src.resolve(strict=True)
        src_resolved.relative_to(staging_root())
    except (ValueError, OSError):
        return {"action": action, "ok": False, "error": "來源檔不在暫存區內（或已不存在），拒絕歸檔"}
    if not src_resolved.is_file():
        return {"action": action, "ok": False, "error": f"來源不是檔案：{src_resolved}"}

    target = archive_target(payload, files_root)
    # 洗完之後**再檢查一次**是否仍在該專案底下——組件內容有可能把路徑帶出去
    try:
        target.resolve().relative_to((files_root / project).resolve())
    except (ValueError, OSError):
        return {"action": action, "ok": False, "error": "目標路徑逸出專案資料夾，拒絕歸檔"}
    if len(str(target)) > MAX_PATH_CHARS:
        return {"action": action, "ok": False,
                "error": f"目標路徑過長（{len(str(target))} 字元），請縮短欄位內容"}

    if not commit:
        return {
            "action": "DRY-RUN",
            "source": str(src_resolved),
            "target": str(target),
            "already_exists": target.exists(),
            "note": "複製不移動；暫存那份保留，專案選錯可重來。",
        }

    try:
        # mkdir 一定要納入例外處理：保留裝置名、權限、路徑過長都會讓它拋錯。
        # 在批次迴圈裡拋出去的話，一列壞掉會讓整批中斷。
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"action": "COMMIT", "ok": False, "error": f"建立目標資料夾失敗：{exc}"}

    final = target
    if final.exists():
        # 同名不覆蓋：同一天同一來源送兩份不同內容是常態
        for serial in range(2, 100):
            candidate = target.with_name(f"{target.stem}_{serial}{target.suffix}")
            if not candidate.exists():
                final = candidate
                break
        else:
            return {"action": "COMMIT", "ok": False, "error": f"同名檔過多，無法命名：{target}"}

    # 先複製到同目錄暫存檔再原子改名。中途失敗（磁碟滿、IO 錯）不會在正式資料夾
    # 留下半份損毀的檔案，也不會覆蓋另一個行程剛建立的同名檔。
    tmp = final.with_name(final.name + f".part{os.getpid()}")
    try:
        shutil.copy2(src_resolved, tmp)
        os.replace(tmp, final)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return {"action": "COMMIT", "ok": False, "error": f"複製失敗：{exc}"}

    return {
        "action": "COMMIT",
        "ok": True,
        "source": str(src_resolved),
        "target": str(final),
        "project": project,
    }
