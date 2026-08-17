# -*- coding: utf-8 -*-
"""L3 審核頁的伺服器：只做三件事。

原始系統把這頁掛在一支一萬九千行的內部營運儀表板上——那支有登入、角色、
幾十個端點、還有它自己的排程。蒸餾版只留審核頁真正踩到的三條路徑：

    GET  /                           審核頁本身
    GET  /api/file?path=...          讀一份檔案（審核表 JSON、以及「開啟原檔」的媒體）
    POST /api/register/line_archive  人按下確認 → 交給 review/archive.py 真的複製

## 這支沒有認證，這件事必須先講清楚

預設綁 127.0.0.1，**「只有這台機器連得到」就是它唯一的防線**。原始系統前面
有反向代理擋著，並用 session cookie 分辨角色（財務可讀全區、現場人員只讀自己
那個案子）；蒸餾時那一整層被拿掉了。所以：

- 改 REVIEW_HOST、掛 tunnel、開 port forwarding 之前，**先自己補上認證**。
  沒有認證的 `/api/file` 等於把白名單裡那幾棵樹整個公開；沒有認證的歸檔端點
  等於任何人都能把暫存區的檔案塞進正式的專案資料夾。
- 白名單開得越大，上面那件事的代價越大。預設只放行管線自己的 data\\ 與暫存區。

## 環境變數（一律 call time 讀，不在 import 時凍結）

    REVIEW_HOST           預設 127.0.0.1（改之前先讀上一段）
    REVIEW_PORT           預設 8770
    REVIEW_ALLOWED_ROOTS  /api/file 可讀的根目錄，**分號**分隔——Windows 路徑
                          自帶冒號，用 `:` 當分隔會在 `C:\\` 就斷成兩半。
                          預設＝管線 data 目錄 ＋ 媒體暫存區。
    REVIEW_QUEUE_PATH     審核結果佇列（JSONL）。預設與
                          extract/daily_extract.py::_queue_paths() 是同一個檔
                          ——兩邊指到不同地方的話，已歸檔的東西會天天復活。
    REVIEW_DRY_RUN        設 1 只空跑（commit=False），不寫任何檔案。
    PROJECT_FILES_ROOT    歸檔目的地；PROJECT_INDEX_ROOT 是交叉確認專案的第二棵樹。
                          名字與預設值刻意與 review/build_review.py 一致，理由見
                          project_files_root()。

依賴 fastapi 與 uvicorn。啟動：`python review/app.py`。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from extract.daily_extract import TAIPEI, data_dir  # noqa: E402
from review.archive import commit_line_archive, staging_root  # noqa: E402

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
INDEX_HTML = STATIC_DIR / "review.html"

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}

#: 白名單外一律回這句，**不分「不存在」與「不給看」**。兩者回不同的訊息或狀態碼，
#: 這個端點就順便變成一台檔案存在性探測器。
_FORBIDDEN = "路徑不在允許的範圍內"

#: 明確安全、可以直接 inline 呈現的型別。**清單以外一律當成下載**，理由見 read_file()。
_INLINE_TYPES = {
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
}


# ── 設定 ──────────────────────────────────────────────────────────────────────

def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def review_host() -> str:
    return os.getenv("REVIEW_HOST", "").strip() or "127.0.0.1"


def review_port() -> int:
    raw = os.getenv("REVIEW_PORT", "").strip()
    try:
        return int(raw) if raw else 8770
    except ValueError:
        logger.warning("REVIEW_PORT=%r 不是數字，改用 8770", raw)
        return 8770


def dry_run() -> bool:
    """空跑模式：照樣驗證與組路徑，但不複製任何檔案。第一次接上真的專案樹時用。"""
    return _truthy(os.getenv("REVIEW_DRY_RUN", ""))


def queue_path() -> Path:
    """審核結果佇列。**預設值必須與 extract/daily_extract.py::_queue_paths() 相同。**

    那邊靠這個檔判斷「哪些候選已經處理完」。兩邊指到不同的檔案時不會有任何錯誤
    訊息，症狀是已經歸檔好的東西每天繼續出現在審核表上，被歸第二次、第三次。
    """
    env = os.getenv("REVIEW_QUEUE_PATH", "").strip()
    return Path(env) if env else data_dir() / "review_queue.jsonl"


def project_files_root() -> Path:
    """歸檔目的地的根目錄。

    名字與預設值刻意與 review/build_review.py 相同：下拉選單列出來的專案
    （build_review 算的）跟歸檔器認可的專案（archive.valid_projects 算的）
    必須是同一組。兩邊指到不同的樹，人選得到的專案會在按下確認時被判成
    「不是有效專案」——而畫面上完全看不出來是設定問題。
    """
    return Path(os.getenv("PROJECT_FILES_ROOT", "") or (REPO_ROOT / "example_projects"))


def project_index_root() -> Path:
    """交叉確認專案的第二棵樹（archive.commit_line_archive 的 `vault` 參數）。"""
    return Path(os.getenv("PROJECT_INDEX_ROOT", "") or (REPO_ROOT / "example_index"))


def _default_allowed_roots() -> list[Path]:
    """預設只放行管線自己的產出目錄與媒體暫存區——那正好是頁面要讀的全部：
    審核表 JSON 在 data\\，「開啟原檔」的媒體在暫存區。

    暫存區直接跟 review/archive.py 借同一個函式：歸檔器認可的來源目錄與頁面
    能預覽的目錄必須是同一個，否則會冒出「看得到卻歸不了」（或反過來）這種
    只有讀 code 才查得出原因的怪事。

    刻意**不**含專案樹（PROJECT_FILES_ROOT）。歸檔的目的地不需要被讀回來，
    而把整棵正式檔案樹掛在一個沒有認證的端點後面，是完全不同量級的決定。
    """
    return [data_dir(), staging_root()]


def allowed_roots() -> list[Path]:
    """/api/file 可讀的根目錄清單（已 resolve）。"""
    raw = os.getenv("REVIEW_ALLOWED_ROOTS", "").strip()
    # strip('"')：Windows 的 `set REVIEW_ALLOWED_ROOTS="C:\\data"` 會把引號一起
    # 吃進值裡，帶著引號的路徑永遠對不上任何東西，症狀是「什麼都 403」。
    configured = [Path(p.strip().strip('"')) for p in raw.split(";") if p.strip().strip('"')]
    out: list[Path] = []
    for root in configured or _default_allowed_roots():
        try:
            out.append(Path(root).resolve())
        except (OSError, ValueError):
            logger.warning("REVIEW_ALLOWED_ROOTS 這個根目錄洗不出絕對路徑，略過：%s", root)
    return out


# ── 應用程式 ──────────────────────────────────────────────────────────────────

# docs / openapi 關掉：這支沒有認證，沒必要再多開兩個端點把內部路徑與欄位攤出來。
app = FastAPI(title="LINE 審核頁", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    """審核頁本身。頁面實際上在 /static 底下，這條只是讓入口網址短一點。"""
    if not INDEX_HTML.is_file():
        raise HTTPException(status_code=500, detail=f"找不到審核頁：{INDEX_HTML}")
    return FileResponse(
        INDEX_HTML,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


def _resolve_readable(raw: str) -> Path:
    """把外部給的路徑洗成「確定落在白名單內」的絕對路徑，否則 403。

    順序是關鍵：**先 resolve() 再比對**。反過來先比字串的話，
    `<允許的根>\\..\\..\\secrets\\id_rsa` 會通過檢查、洗完卻指到外面去；
    白名單目錄裡若有人放了指向別處的符號連結或 junction 也是同一顆雷
    （review/archive.py 對來源檔做的是同一件事，那邊有對應的 regression test）。
    """
    roots = allowed_roots()
    if not roots:
        # fail closed：設定壞掉時什麼都不給讀，而不是什麼都給讀。
        # 這跟 L1 白名單預設空＝不擷取任何群組是同一個原則。
        logger.warning("沒有任何可讀的根目錄，/api/file 一律拒絕")
        raise HTTPException(status_code=403, detail=_FORBIDDEN)

    try:
        target = Path(raw).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=403, detail=_FORBIDDEN) from None

    for root in roots:
        # 大小寫交給 pathlib 判斷（Windows 不分、POSIX 分），別自己 lower()
        # ——那會在 Linux 上把兩個不同的檔案當成同一個。
        if target == root or root in target.parents:
            return target
    raise HTTPException(status_code=403, detail=_FORBIDDEN)


@app.get("/api/review-data")
def review_data() -> FileResponse:
    """審核表的資料。

    **刻意讓伺服器自己決定要端哪份檔案**，而不是讓前端傳一個絕對路徑進來。
    原始系統是前端硬編伺服器路徑再丟給 /api/file——那讓任何讀得到頁面原始碼的人
    看見內部目錄結構，也讓前端知道了它不該知道的事。
    """
    target = Path(
        os.getenv("PIPELINE_DATA_DIR", "").strip() or (REPO_ROOT / "data")
    ) / "line_review.json"
    if not target.is_file():
        raise HTTPException(
            404, "尚未產生審核表——請先跑 python -m review.build_review"
        )
    return FileResponse(target, media_type="application/json")


@app.get("/api/file")
def read_file(path: str) -> FileResponse:
    """讀一份白名單內的檔案。**這是這支伺服器最危險的端點。**

    少了 _resolve_readable() 那道白名單，這裡就是一個現成的目錄穿越端點：
    任何連得到這個 port 的人，送 `?path=...\\runtime\\.env` 或一串 `..\\`
    就能把這台機器讀光——而「這支沒有認證」會在那一刻從一句註記變成資料外洩。
    """
    target = _resolve_readable(path)
    if not target.is_file():
        # 走到這裡代表路徑已經確定落在白名單內，回 404 不會洩漏白名單外的任何事
        raise HTTPException(status_code=404, detail="檔案不存在")

    headers = {
        # nosniff：少了它瀏覽器會自己猜型別，下面那份 inline 白名單等於沒寫
        "X-Content-Type-Options": "nosniff",
        # no-store：審核表每天重產，讀到快取裡昨天那份，等於請人再歸檔一次已經
        # 歸過的東西；順帶讓專案檔案不留在瀏覽器的磁碟快取裡
        "Cache-Control": "no-store",
    }
    media_type = _INLINE_TYPES.get(target.suffix.lower(), "")
    if media_type:
        return FileResponse(target, media_type=media_type, headers=headers)

    # 白名單以外的型別一律 octet-stream ＋ attachment。這些檔案是外部人從 LINE
    # 傳進來的，內容完全由他決定：一份 .html 或 .svg 從**同源**被當網頁跑起來，
    # 就能替使用者讀走白名單內的任何檔案、並替他按下歸檔。
    # 附帶一提，L1 落地的 file 型訊息副檔名是 .bin，所以廠商傳的 PDF 也走這條
    # ——寧可多按一次下載，也不要靠副檔名猜型別然後直接 inline 呈現。
    return FileResponse(
        target,
        media_type="application/octet-stream",
        filename=target.name,
        headers=headers,
    )


# ── 歸檔登記 ──────────────────────────────────────────────────────────────────

_QUEUE_LOCK = threading.Lock()


def _append_queue(row: dict) -> None:
    """把這次核可的結果追加進佇列。**這是 L2/L3 判斷「哪些已經處理完」的唯一依據。**

    - `status` 只有 "committed" 會被 already_handled_ids() 當成處理完。失敗與空跑
      都要留下一列但不能算數，那份檔案下次才會再出現在審核表上。
    - 上鎖：FastAPI 把同步端點丟到 threadpool 跑，兩個人同時按確認時，Windows 的
      append 模式不保證兩列不互相踩（POSIX 的 O_APPEND 才有那個保證）。
    - default=str：哪天歸檔器多回一個 Path 欄位，不該讓整列寫不進佇列。

    **這個檔會裝進群組對話衍生的內容**（檔名、廠商、專案），跟 data\\ 一樣不要
    進 git、也不要放在會自動同步上雲的目錄。
    """
    target = queue_path()
    line = json.dumps(row, ensure_ascii=False, default=str)
    with _QUEUE_LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")


@app.post("/api/register/line_archive")
def register_line_archive(payload: Any = Body(...)) -> Response:
    """人按下「確認歸檔」時打的端點。

    **歸檔失敗一樣回 HTTP 200，錯誤寫在 body 的 `ok:false` 裡。** 這不是偷懶，
    是跟頁面講好的契約：review.html 檢查的是 body（`r.ok === false || out.ok === false`），
    因為「請求有沒有被受理」跟「檔案有沒有真的複製進去」是兩件事。原始系統就是
    把前者當成後者——伺服器回 200、卡片消失、檔案根本沒進去，直到對帳才發現。
    HTTP 狀態碼只描述請求本身（body 不是 JSON 物件才回 400），歸檔結果一律走 body。
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="請求內容必須是 JSON 物件")

    # archive.py 收的是 {"form": ..., "payload": ...}；欄位怎麼洗、專案是否有效、
    # 來源是否在暫存區內，全部由它負責。這裡**不重複驗證**——同一條規則寫兩份，
    # 早晚會有一份先漂掉，而漂掉的那份通常是比較鬆的那份。
    entry = {"form": "line_archive", "payload": dict(payload)}
    candidate_id = str(payload.get("line_candidate_id") or "")
    commit = not dry_run()

    try:
        result = commit_line_archive(
            entry,
            commit=commit,
            files=project_files_root(),
            vault=project_index_root(),
        )
    except Exception as exc:  # noqa: BLE001
        # 歸檔器丟出未預期的例外時不讓它變成 HTTP 500：頁面只會顯示「HTTP 500」，
        # 而佇列裡不會留下任何一列——那筆到底試過沒有、失敗在哪，事後查不出來。
        logger.exception("line_archive 歸檔器拋出未預期的例外")
        result = {
            "action": "COMMIT" if commit else "DRY-RUN",
            "ok": False,
            "error": f"歸檔器發生未預期的錯誤：{exc}",
        }

    ok = bool(result.get("ok")) and commit
    error = str(result.get("error") or "")
    if not commit and not error:
        # 空跑成功時歸檔器回的是 DRY-RUN 結果（連 ok 欄位都沒有）。這種情況
        # **故意**讓頁面顯示紅字：報一句「已歸檔到 X」而其實什麼都沒寫，
        # 正是這條管線最貴的那種假訊號。
        error = f"空跑模式（REVIEW_DRY_RUN）：沒有真的寫入。實際會複製到 {result.get('target', '')}"

    status = "committed" if ok else ("error" if commit else "dry-run")
    queue_error = ""
    try:
        _append_queue(
            {
                "ts": datetime.now(TAIPEI).isoformat(timespec="seconds"),
                "form": "line_archive",
                "status": status,
                "line_candidate_id": candidate_id,
                "payload": entry["payload"],
                "result": result,
            }
        )
    except OSError as exc:
        # 檔案已經複製進去、佇列卻沒寫成：這筆明天會再出現在審核表上，被歸第二次
        # （歸檔器同名不覆蓋，會多一份 _2）。仍然照實回報 ok——多一份看得見的重複
        # 檔案，好過謊報失敗讓人當場再按一次：結果一樣重複，還多一個誤會。
        logger.exception("審核佇列寫不進去：%s", queue_path())
        queue_error = str(exc)

    body = {
        "ok": ok,
        "error": error,
        "status": status,
        "line_candidate_id": candidate_id,
        "queue_error": queue_error,
        # 原樣回傳歸檔器的結果：頁面成功時要拿裡面的 target 顯示「已歸檔到哪」。
        "result": result,
    }
    return Response(
        content=json.dumps(body, ensure_ascii=False, default=str),
        media_type="application/json",
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # uvicorn 延後到這裡才 import：只是要 import 這個模組（測試、或掛到別的
    # ASGI 伺服器上）的人，不必先裝一個他不會用到的東西。
    import uvicorn

    host, port = review_host(), review_port()
    roots = allowed_roots()
    logger.info("審核頁 http://%s:%s/", host, port)
    logger.info("佇列：%s（空跑模式：%s）", queue_path(), "是" if dry_run() else "否")
    logger.info("可讀根目錄 %d 個：%s", len(roots), "；".join(str(r) for r in roots) or "（無）")
    if host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "REVIEW_HOST=%s：這支沒有任何認證，綁在非 loopback 位址等於把白名單內的"
            "檔案與歸檔端點對外公開。請先在前面加一層認證。",
            host,
        )
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
