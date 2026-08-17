# -*- coding: utf-8 -*-
"""L1 的門口：一支獨立可跑的 LINE webhook 接收器。

這一層刻意做得很少——**驗簽、把每則事件交給 `line_capture.capture_event()`、
收尾補一次媒體下載**，就這三件事。不解析語意、不回覆訊息、不寫任何 SoT。
分類是 L2 的事，歸屬是 L3 由人決定的事。

之所以獨立成一支，是因為原本的擷取掛在一個三千行的公司專用 webhook 主檔裡：
那支同時處理財務入帳、出工打卡、OA 回覆，capture 只是排在最前面的一小段。
要把這條管線交給別人重用，不能連那三千行一起搬——所以這裡重寫一個最小接收器，
`line_capture.py` 完全不必改。

紀律：

1. **一律先回 200，工作全部丟背景執行緒**。回應路徑上除了把 body 讀進來，
   不做任何 I/O、不解析 JSON、不看內容。
2. **驗簽也在背景做**。回應碼與請求內容徹底無關，見 `_handle` 上的說明。
3. **一則事件壞掉不能拖垮整批**。每則各自 try/except——我們已經回過 200，
   這批沒處理完就是永久遺失，沒有人會重送。
4. **預設只綁 127.0.0.1**。對外曝光是前面那層隧道／反向代理的職責。
5. 內容不進 log。案場群組訊息含姓名、電話、報價、銀行帳號，log 只記
   事件型別與統計數字。

部署形狀（LINE 只接受公開 HTTPS，所以前面一定有一層）：

    LINE Platform → HTTPS 隧道／反向代理 → 127.0.0.1:8090/webhook

環境變數：

    LINE_CHANNEL_SECRET         必填，驗簽用
    LINE_CHANNEL_ACCESS_TOKEN   必填（媒體下載關掉時可省），下載圖片／語音用
    HOST                        預設 127.0.0.1
    PORT                        預設 8090
    （擷取行為本身的開關在 line_capture.py：LINE_CAPTURE_ENABLED、
      LINE_CAPTURE_GROUPS_CSV、LINE_CAPTURE_MEDIA_ENABLED …）

用法：

    python capture/receiver.py            # 開發／自架，走 Flask 內建 server
    waitress-serve --call capture.receiver:create_app   # 正式環境建議
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from capture import line_capture  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090

# body 上限。LINE 實際的 webhook body 都在幾十 KB 以內，但這個 port 前面掛著
# 隧道，任何人打得到隧道就打得到這裡；沒有上限的話，一個超大 body 在
# `request.get_data()` 那一行就會把記憶體吃光——那是在我們有機會驗簽之前。
# 超過上限 Flask 直接回 413，這是「先回 200」的唯一例外，且不影響 LINE
# （它沒有理由送出這麼大的東西）。
_MAX_BODY_BYTES = 1 * 1024 * 1024

# 連續幾次驗簽失敗就把 log 等級升到 error。單次失敗通常只是掃描器亂打，
# 不值得驚動；但 secret 貼錯／channel 換掉時，失敗會「安靜地」持續下去，
# 外觀是「LINE 群組明明有訊息，DB 卻一列都沒有」——那種故障要很久才被發現。
_SIGNATURE_ALERT_EVERY = 10

_signature_lock = threading.Lock()
_signature_bad_streak = 0


# ── 驗簽 ──────────────────────────────────────────────────────────────────────

def verify_signature(body: bytes, x_line_signature: str, channel_secret: str) -> bool:
    """驗證 LINE webhook 簽章（X-Line-Signature）。

    `body` 必須是**原封不動收到的 bytes**。若先 `json.loads` 再 `json.dumps`
    回來當作驗簽輸入，鍵序、空白、跳脫都可能跟 LINE 算簽章時用的原文不同，
    簽章永遠對不上，於是每一則訊息都被當成偽造丟掉。

    比對用 `hmac.compare_digest` 而不是 `==`：後者逐字元短路，會把「你猜對前
    幾個字元」洩漏成可測量的時間差。
    """
    if not channel_secret or not x_line_signature:
        return False
    mac = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, x_line_signature)


def _note_signature(ok: bool) -> None:
    """記錄驗簽結果，連續失敗達門檻就升級成 error。"""
    global _signature_bad_streak
    with _signature_lock:
        if ok:
            _signature_bad_streak = 0
            return
        _signature_bad_streak += 1
        streak = _signature_bad_streak

    if streak % _SIGNATURE_ALERT_EVERY == 0:
        logger.error(
            "LINE webhook 連續 %d 次驗簽失敗——請確認 LINE_CHANNEL_SECRET "
            "是否與這個 channel 相符（期間所有事件都被丟棄）",
            streak,
        )
    else:
        logger.warning("LINE webhook 驗簽失敗，已丟棄（連續第 %d 次）", streak)


# ── 事件解析 ──────────────────────────────────────────────────────────────────

def _parse_events(body: bytes) -> list[dict[str, Any]]:
    """從已驗簽的 body 取出 events 陣列。壞掉就回空 list，不拋。

    驗簽過了不代表解析得動：LINE console 的「Verify」按鈕送的是空 events，
    未來新增的欄位或格式也可能讓假設落空。這裡拋例外沒有任何好處——200 早就
    回出去了，拋只是讓背景執行緒死掉並在 log 留一個看不懂的 traceback。

    非 dict 的元素直接濾掉：`capture_event` 內部整路 `.get()`，餵一個字串
    進去會炸在 AttributeError，而那則事件的內容還會跟著 traceback 進 log。
    """
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("webhook body 不是合法 JSON（len=%d），已丟棄", len(body))
        return []
    if not isinstance(payload, dict):
        logger.warning("webhook payload 不是物件，已丟棄")
        return []
    events = payload.get("events")
    if not isinstance(events, list):
        return []
    return [event for event in events if isinstance(event, dict)]


# ── 背景處理 ──────────────────────────────────────────────────────────────────

def process_webhook(body: bytes, signature: str, *, secret: str, token: str) -> dict[str, Any]:
    """背景執行緒的全部工作。回傳統計字典，方便測試直接呼叫、不必起 HTTP。

    這個函式**不對外表達任何結果**——呼叫端早就回過 200 了。回傳值只給 log
    與測試看。
    """
    if not verify_signature(body, signature, secret):
        _note_signature(False)
        # 丟棄就是丟棄：不重試、不回報、**不把 body 寫進 log**。
        # 驗不過的 body 要嘛是攻擊 payload，要嘛是設定錯時的真實訊息，
        # 兩種都不該落進純文字日誌裡。
        return {"ok": False, "reason": "bad_signature"}
    _note_signature(True)

    events = _parse_events(body)
    stored = skipped = failed = 0
    for event in events:
        # 每則各自吞例外。LINE 一個 POST 可以帶多則事件，其中一則遇到 SQLite
        # 鎖住或未預期的結構而拋出時，若不攔就會把同一批後面的訊息一起帶走——
        # 而且因為已經回過 200，LINE 不會再送第二次，那是永久遺失。
        try:
            result = line_capture.capture_event(event)
        except Exception:  # noqa: BLE001
            logger.exception("capture_event 失敗，該則事件已丟棄（其餘照常處理）")
            failed += 1
            continue
        # None ＝ 刻意略過（未啟用／不在白名單／不是群組訊息），不是錯誤。
        if result is None:
            skipped += 1
        else:
            stored += 1

    logger.info(
        "webhook 處理完成 events=%d stored=%d skipped=%d failed=%d",
        len(events), stored, skipped, failed,
    )

    # drain 放在迴圈後面跑一次，不是每則跑一次：它自己會撈全表的 pending 列，
    # 一批訊息裡五張圖跑一次就全處理掉，逐則呼叫只是多開五次 DB 連線做同一件事。
    # 也順手把上一批沒抓完的補掉——媒體有保存期限，愈晚抓愈可能抓不到。
    # 這裡不趕時間：200 早就回了，而且被動擷取不回覆訊息，沒有 replyToken
    # 那一分鐘的時限壓在頭上。
    drained: dict[str, Any] = {}
    try:
        drained = line_capture.drain_media(access_token=token)
    except Exception:  # noqa: BLE001
        logger.exception("drain_media 失敗（落地的文字不受影響，下一批會再試）")
    else:
        if drained.get("drained"):
            logger.info("媒體下載 %s", drained)

    return {
        "ok": True,
        "events": len(events),
        "stored": stored,
        "skipped": skipped,
        "failed": failed,
        "media": drained,
    }


# ── Flask ─────────────────────────────────────────────────────────────────────

def create_app():  # noqa: ANN201
    """組出 app。金鑰不齊就在這裡拒絕啟動，不留到執行期才靜默失敗。"""
    from flask import Flask, request  # 延遲匯入：只測驗簽／解析時不必裝 Flask

    secret = os.getenv("LINE_CHANNEL_SECRET", "").strip()
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()

    # fail-loud。缺 secret 的話 verify_signature 會把每一個 webhook 都判成
    # 偽造並靜默丟棄——服務看起來活著、healthz 也綠、log 只有幾行 warning，
    # 但一則訊息都不會落地。這種「半死不活」比直接起不來難查太多了。
    if not secret:
        message = (
            "[RECEIVER_CONFIG] 缺 LINE_CHANNEL_SECRET → 拒絕啟動。"
            "否則每一則 webhook 都會被判為驗簽失敗而丟棄，外觀是「群組有訊息、DB 卻空的」。"
        )
        logger.critical(message)
        raise RuntimeError(message)

    # token 只有媒體下載會用到。媒體關掉時它就真的不需要，硬要求反而逼人
    # 塞一個假值進 .env；媒體開著卻沒 token 則是另一種半殘——文字照樣落地，
    # 圖片全部卡在 pending／failed，同樣安靜。所以按開關決定嚴格程度。
    if not token:
        if line_capture.media_enabled():
            message = (
                "[RECEIVER_CONFIG] 缺 LINE_CHANNEL_ACCESS_TOKEN 且媒體下載為開啟 → 拒絕啟動。"
                "否則文字會正常落地、圖片／語音卻全部下載失敗（LINE 的媒體有保存期限，"
                "過期就再也拿不回來）。只想先跑文字請設 LINE_CAPTURE_MEDIA_ENABLED=false。"
            )
            logger.critical(message)
            raise RuntimeError(message)
        logger.warning(
            "[RECEIVER_CONFIG] 未設 LINE_CHANNEL_ACCESS_TOKEN，媒體下載已關閉 → 只落地文字。"
        )

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = _MAX_BODY_BYTES

    _log_startup_state()

    def _handle():  # noqa: ANN202
        """收下 body → 開執行緒 → 立刻回 200。中間不做任何判斷。

        **為什麼先回 200**：LINE 對 webhook 有秒級逾時，逾時就判定失敗並重送
        整批事件。落地端雖然有 dedupe_key 擋得住重複，但重送會讓同一批事件把
        DB 再打一輪、媒體下載再排一次隊；重送次數多了 LINE 還會把 webhook
        標成不穩定。所以回應路徑上一行 I/O 都不放。

        **為什麼驗簽也在背景**：驗簽本身很快，搬到前面看似無害，但那會讓
        「回什麼狀態碼」開始取決於請求內容——一旦開了這個頭，之後很自然會把
        JSON 解析、白名單判斷也一起搬到回應之前，於是 LINE 的逾時預算就被押
        在自己的 parser 上。而且對簽章失敗回 401，等於免費送給對方一個
        「這個 secret 對不對」的探測器。統一規則：**一律回 200，代表「我收到
        了」，不代表「我接受了」**，回應時間與內容徹底無關。
        """
        body = request.get_data()
        signature = request.headers.get("X-Line-Signature", "")
        threading.Thread(
            target=_process_async,
            args=(body, signature, secret, token),
            name="line-webhook",  # 執行緒取名，log 交錯時才分得出是哪一批
            daemon=True,
        ).start()
        return ("OK", 200)

    # 兩個路徑都掛：反向代理／隧道有沒有把 "/line" 前綴 strip 掉，各家設定不同，
    # 兩種都接得到就不必為此重設隧道（設錯的症狀是 404，而 LINE 那頭只顯示
    # 「錯誤」，很難一眼看出是路徑問題）。
    app.add_url_rule("/webhook", "webhook", _handle, methods=["POST"])
    app.add_url_rule("/line/webhook", "line_webhook", _handle, methods=["POST"])

    @app.get("/healthz")
    def _health():  # noqa: ANN202
        # 刻意不碰 DB、不查統計：健檢端點若跟著 SQLite 一起卡住，監控會把
        # 「聊天流量暫時擠住」誤報成「服務死了」而觸發重啟，反而更糟。
        return ("line-group-capture ok", 200)

    @app.get("/line/healthz")
    def _health_line():  # noqa: ANN202
        return ("line-group-capture ok", 200)

    return app


def _process_async(body: bytes, signature: str, secret: str, token: str) -> None:
    """執行緒進入點：最外層再兜一次 except，讓意外死法留下可讀的 traceback。

    背景執行緒裡未捕捉的例外只會印到 stderr，服務照跑、訊息照掉，沒有任何
    人會知道。這裡兜住並走 logger，至少會進日誌檔。
    """
    try:
        process_webhook(body, signature, secret=secret, token=token)
    except Exception:  # noqa: BLE001
        logger.exception("webhook 背景處理整批失敗")


def _log_startup_state() -> None:
    """啟動時把擷取的實際狀態記一行。

    最常見的「裝好了卻什麼都沒錄到」有兩種：`LINE_CAPTURE_ENABLED` 沒開，
    或白名單是空的。兩者都不是錯誤（空白名單配 discover 模式，正是撈 groupId
    的標準做法），但值得在第一秒就看得見，而不是隔天發現 DB 空的才回頭查。
    """
    try:
        groups = line_capture.group_map()
        stats = line_capture.capture_stats()
    except Exception:  # noqa: BLE001
        logger.exception("讀取擷取狀態失敗（不影響接收）")
        return

    logger.info(
        "[RECEIVER] capture_enabled=%s media_enabled=%s discover=%s "
        "白名單群組=%d 既有資料列=%d 媒體待下載=%d",
        line_capture.capture_enabled(),
        line_capture.media_enabled(),
        line_capture.discover_mode(),
        len(groups),
        stats.get("rows", 0),
        stats.get("media_pending", 0),
    )
    if not line_capture.capture_enabled():
        logger.warning("[RECEIVER] LINE_CAPTURE_ENABLED 未開 → 收得到 webhook，但不會落地任何東西。")
    elif not groups:
        logger.warning(
            "[RECEIVER] 白名單是空的 → 不擷取任何對話。"
            "這是刻意的預設（否則會連 1:1 私訊與財務群組一起錄）；"
            "要開始擷取請把 group_id 填進群組對應表。"
        )


# ── 進入點 ────────────────────────────────────────────────────────────────────

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        logger.warning("%s 不是合法整數，改用預設 %s", name, default)
        return default


def main() -> None:
    # 只有當成程式跑才設定 root logger；被 import 時動它會蓋掉宿主的設定。
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = create_app()

    # **預設綁 127.0.0.1，不是 0.0.0.0。** LINE 只打得到公開 HTTPS，所以正式
    # 環境前面本來就有一層隧道／反向代理負責 TLS 與對外曝光；這支程式只需要
    # 對本機開。綁 0.0.0.0 的問題是它「剛好也能動」——於是隧道那層被省掉，
    # 一個沒有 TLS、全靠 HMAC 撐著的 HTTP 端點就直接掛在辦公室區網或雲主機的
    # 公網介面上。驗簽確實擋得住偽造事件，但擋不住有人對它灌流量把媒體下載
    # 佇列和日誌塞爆，也擋不住明文流量在區網上被看光。
    # 真要對外請顯式設 HOST，讓那是一個有意識的決定。
    host = os.getenv("HOST", "").strip() or DEFAULT_HOST
    port = _int_env("PORT", DEFAULT_PORT)

    logger.info("LINE 群組擷取接收器啟動：http://%s:%d/webhook", host, port)
    if host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "[RECEIVER] HOST=%s（非 loopback）→ 這個 port 會對外可見。"
            "請確認前面有 TLS 與存取控制。", host,
        )

    # Flask 內建 server 是開發用的：單進程、沒有連線數保護、重啟沒有優雅收尾。
    # 長期跑請改用 waitress／gunicorn 掛 create_app。
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
