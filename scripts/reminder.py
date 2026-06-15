#!/usr/bin/env python3
"""月次記事リリース確認リマインド — 判定・状態管理・Slack送受信エンジン。

Slack への送信/読み取りは、bot「Mantaさん」のトークンを使って本スクリプトが
直接 Slack Web API を叩いて行う（環境変数 SLACK_BOT_TOKEN）。
日付計算・状態遷移も本スクリプトに集約されており、AI（Routine セッション）の
役割は「適切な run-* コマンドを実行し、状態ファイルに差分が出たら commit & push」
だけに最小化される。

これにより:
  - 「第2月曜」「その2日後（水曜）」といった日付計算を LLM に依存しない
  - 「小林さんが返答したか」の判定も決定論的に行う（リアクション/スレッド返信/
    チャンネルへのトップレベル投稿のいずれか）
  - 状態遷移（pending / done / followup）を一元管理する
  - 元基盤で起きていた 120 秒タイムアウト相当の重い AI 処理を排除する

主なコマンド:
  run-monday / run-wednesday / run-monitor  : 判定→Slack送受信→状態記録を一括実行
  decide --job ...                          : 判定のみ（送信・記録なし／デバッグ用）
  record-request / record-followup / record-done : 状態の手動記録（復旧用）
  check-auth                                : SLACK_BOT_TOKEN の疎通と bot 名義の確認
  selftest / status                         : 日付ロジックの回帰テスト / 状態表示

状態ファイル: リポジトリ直下 kobayashi_article_reminder_ts.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ---- 定数（運用対象）---------------------------------------------------------

# 日本標準時。DST が無いため常に UTC+9 固定で十分（tzdata 依存を避ける）。
JST = timezone(timedelta(hours=9))

CHANNEL_ID = "C02MLKUPYK0"      # #sol-prj-dentsu-cxcc
KOBAYASHI_ID = "UU33R4YCQ"      # 小林 亜佳穂 / Akane Kobayashi
MENTION = f"<@{KOBAYASHI_ID}>"  # Slack 送信時にメンションとして展開される

STATE_PATH = Path(__file__).resolve().parent.parent / "kobayashi_article_reminder_ts.json"

SLACK_API = "https://slack.com/api/"

# 送信文面（過去の実投稿と一致）。
REQUEST_MESSAGE = (
    "【月次記事リリース確認依頼】\n"
    f"{MENTION} お疲れ様です。今月リリースの投稿記事があるかチェックをお願いします！"
)
FOLLOWUP_MESSAGE = (
    "【月次記事リリース確認 リマインド】\n"
    f"{MENTION} 先日お送りした今月のリリース記事確認について、まだご確認いただけて"
    "いないでしょうか。お手すきのタイミングでご確認よろしくお願いします！"
)


# ---- 日付ユーティリティ ------------------------------------------------------

def today_jst() -> date:
    """JST における今日の日付を返す。"""
    return datetime.now(JST).date()


def second_monday(year: int, month: int) -> date:
    """指定年月の「第2月曜日」を返す。"""
    first = date(year, month, 1)
    # weekday(): Mon=0 ... Sun=6。最初の月曜までの日数を足す。
    days_to_first_monday = (0 - first.weekday()) % 7
    first_monday = first + timedelta(days=days_to_first_monday)
    return first_monday + timedelta(days=7)


def followup_day(year: int, month: int) -> date:
    """フォローアップ日 = 第2月曜の2日後（水曜日）を返す。"""
    return second_monday(year, month) + timedelta(days=2)


def ym(d: date) -> str:
    """YYYY-MM 形式の月キー。"""
    return d.strftime("%Y-%m")


# ---- 状態ファイル ------------------------------------------------------------

def load_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    text = STATE_PATH.read_text(encoding="utf-8").strip()
    if not text:
        return None
    return json.loads(text)


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def active_state_for(d: date) -> dict | None:
    """当月のサイクル状態を返す（月が変わっていれば None 扱い）。"""
    state = load_state()
    if state is None:
        return None
    if state.get("month") != ym(d):
        return None
    return state


# ---- Slack Web API（bot トークン）-------------------------------------------

def _bot_token() -> str:
    tok = os.environ.get("SLACK_BOT_TOKEN")
    if not tok:
        raise SystemExit(
            "環境変数 SLACK_BOT_TOKEN が未設定です。"
            "bot「Mantaさん」の xoxb- トークンを Routine 環境の環境変数に設定してください。"
        )
    return tok


def _slack_call(method: str, params: dict, token: str | None = None) -> dict:
    """Slack Web API を呼び出す（form-encoded, Bearer 認証, stdlib のみ）。"""
    token = token or _bot_token()
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        SLACK_API + method,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok"):
        raise SystemExit(f"Slack API {method} 失敗: error={body.get('error')}")
    return body


def post_message(text: str, token: str | None = None) -> str:
    """チャンネルにトップレベル投稿し、ts を返す。"""
    body = _slack_call(
        "chat.postMessage",
        {"channel": CHANNEL_ID, "text": text,
         "link_names": "true", "unfurl_links": "false"},
        token,
    )
    return body["ts"]


def kobayashi_responded(request_ts: str | None,
                        followup_ts: str | None = None,
                        token: str | None = None) -> bool:
    """小林さんが返答したかを判定する。

    以下のいずれかを満たせば True:
      1. 依頼/リマインドへの小林さんのリアクション
      2. 依頼スレッドへの小林さんの返信
      3. 依頼送信以降の、小林さんによるチャンネルへのトップレベル投稿
    """
    token = token or _bot_token()

    # 1) リアクション（依頼・リマインド双方を確認）
    for ts in [t for t in (request_ts, followup_ts) if t]:
        body = _slack_call(
            "reactions.get",
            {"channel": CHANNEL_ID, "timestamp": ts, "full": "true"},
            token,
        )
        for rx in body.get("message", {}).get("reactions", []):
            if KOBAYASHI_ID in rx.get("users", []):
                return True

    if request_ts:
        # 2) スレッド返信
        rep = _slack_call(
            "conversations.replies",
            {"channel": CHANNEL_ID, "ts": request_ts, "limit": "200"},
            token,
        )
        for m in rep.get("messages", []):
            if m.get("ts") != request_ts and m.get("user") == KOBAYASHI_ID:
                return True

        # 3) 依頼送信以降のチャンネル新規投稿（過去実績では小林さんはここに返信）
        hist = _slack_call(
            "conversations.history",
            {"channel": CHANNEL_ID, "oldest": request_ts,
             "inclusive": "false", "limit": "200"},
            token,
        )
        for m in hist.get("messages", []):
            if m.get("user") == KOBAYASHI_ID:
                return True

    return False


def check_auth() -> dict:
    """SLACK_BOT_TOKEN の疎通と bot 名義を確認する。"""
    body = _slack_call("auth.test", {})
    return {"ok": True, "bot_user_id": body.get("user_id"),
            "bot_name": body.get("user"), "team": body.get("team")}


# ---- 判定（decide）----------------------------------------------------------

def decide(job: str, d: date) -> dict:
    """ジョブ種別と日付から、取るべきアクションを決定論的に判定する。

    返り値の "action":
      - send_request        : 依頼メッセージを新規送信（MONDAY）
      - check_and_followup  : 返答有無を確認し、未返答ならリマインド送信（WEDNESDAY）
      - monitor             : 返答有無を監視し、返答があれば完了化（MONITOR）
      - skip                : 何もしない（reason に理由）
    """
    sm = second_monday(d.year, d.month)
    fd = followup_day(d.year, d.month)
    state = active_state_for(d)

    base = {"job": job, "today": d.isoformat(), "month": ym(d),
            "channel_id": CHANNEL_ID, "kobayashi_id": KOBAYASHI_ID}

    if job == "monday":
        if d != sm:
            return {**base, "action": "skip",
                    "reason": f"本日は第2月曜（{sm.isoformat()}）ではありません。"}
        if state is not None:
            return {**base, "action": "skip",
                    "reason": f"当月の依頼は送信済みです（status={state.get('status')}）。",
                    "state": state}
        return {**base, "action": "send_request",
                "reason": "本日は第2月曜です。当月の依頼が未送信のため送信します。",
                "message": REQUEST_MESSAGE}

    if job == "wednesday":
        if d != fd:
            return {**base, "action": "skip",
                    "reason": f"本日はフォローアップ日（第2月曜+2日={fd.isoformat()}）ではありません。"}
        if state is None:
            return {**base, "action": "skip",
                    "reason": "当月の依頼レコードが見つかりません（月曜ジョブ未実行の可能性）。"}
        if state.get("status") == "done":
            return {**base, "action": "skip",
                    "reason": "既に返答確認済み（done）のためフォローアップ不要です。",
                    "state": state}
        if state.get("followup_ts"):
            return {**base, "action": "skip",
                    "reason": "フォローアップは既に送信済みです。",
                    "state": state}
        return {**base, "action": "check_and_followup",
                "reason": "返答有無を確認し、未返答ならリマインドを送信します。",
                "request_ts": state.get("ts"),
                "request_sent_at": state.get("sent_at"),
                "followup_message": FOLLOWUP_MESSAGE,
                "state": state}

    if job == "monitor":
        if state is None:
            return {**base, "action": "skip",
                    "reason": "当月の依頼レコードがありません。監視対象なし。"}
        if state.get("status") == "done":
            return {**base, "action": "skip",
                    "reason": "既に完了（done）。監視終了済み。",
                    "state": state}
        if not state.get("followup_ts"):
            return {**base, "action": "skip",
                    "reason": "フォローアップ未送信のため監視対象外（水曜ジョブで処理）。",
                    "state": state}
        return {**base, "action": "monitor",
                "reason": "フォローアップ後の返答を監視。返答があれば完了化します。",
                "request_ts": state.get("ts"),
                "followup_ts": state.get("followup_ts"),
                "request_sent_at": state.get("sent_at"),
                "state": state}

    raise SystemExit(f"unknown job: {job}")


# ---- 記録（record-*）--------------------------------------------------------

def record_request(ts: str, d: date) -> dict:
    state = {
        "month": ym(d),
        "ts": ts,
        "followup_ts": None,
        "sent_at": d.isoformat(),
        "status": "pending",
    }
    save_state(state)
    return state


def record_followup(ts: str, d: date) -> dict:
    state = active_state_for(d) or {"month": ym(d), "ts": None,
                                    "sent_at": d.isoformat(), "status": "pending"}
    state["followup_ts"] = ts
    state["status"] = "pending"
    save_state(state)
    return state


def record_done(d: date) -> dict:
    state = active_state_for(d)
    if state is None:
        state = {"month": ym(d), "ts": None, "followup_ts": None,
                 "sent_at": d.isoformat()}
    state["status"] = "done"
    save_state(state)
    return state


# ---- 実行（run-*）: 判定→Slack送受信→記録 を一括 ---------------------------

def run_monday(d: date) -> dict:
    dec = decide("monday", d)
    if dec["action"] != "send_request":
        return {"did": "skip", "reason": dec["reason"]}
    ts = post_message(REQUEST_MESSAGE)
    state = record_request(ts, d)
    return {"did": "sent_request", "ts": ts, "state": state}


def run_wednesday(d: date) -> dict:
    dec = decide("wednesday", d)
    if dec["action"] != "check_and_followup":
        return {"did": "skip", "reason": dec["reason"]}
    if kobayashi_responded(dec.get("request_ts")):
        state = record_done(d)
        return {"did": "marked_done", "reason": "小林さんの返答を確認", "state": state}
    ts = post_message(FOLLOWUP_MESSAGE)
    state = record_followup(ts, d)
    return {"did": "sent_followup", "ts": ts, "state": state}


def run_monitor(d: date) -> dict:
    dec = decide("monitor", d)
    if dec["action"] != "monitor":
        return {"did": "skip", "reason": dec["reason"]}
    if kobayashi_responded(dec.get("request_ts"), dec.get("followup_ts")):
        state = record_done(d)
        return {"did": "marked_done", "reason": "小林さんの返答を確認・監視終了", "state": state}
    return {"did": "still_waiting", "reason": "未返答。翌営業日に再チェック（追加送信なし）。"}


# ---- セルフテスト ------------------------------------------------------------

def selftest() -> int:
    """日付ロジックの回帰テスト。失敗時は非ゼロ終了。"""
    cases = {
        # (year, month): (second_monday, followup_wednesday)
        (2026, 6): (date(2026, 6, 8), date(2026, 6, 10)),
        (2026, 5): (date(2026, 5, 11), date(2026, 5, 13)),
        (2026, 4): (date(2026, 4, 13), date(2026, 4, 15)),
        (2026, 7): (date(2026, 7, 13), date(2026, 7, 15)),
        (2026, 1): (date(2026, 1, 12), date(2026, 1, 14)),
    }
    ok = True
    for (y, m), (exp_sm, exp_fd) in cases.items():
        got_sm = second_monday(y, m)
        got_fd = followup_day(y, m)
        sm_ok = got_sm == exp_sm
        fd_ok = got_fd == exp_fd
        ok = ok and sm_ok and fd_ok
        print(f"{y}-{m:02d}: 第2月曜 {got_sm} "
              f"[{'OK' if sm_ok else 'NG exp=' + str(exp_sm)}], "
              f"フォロー {got_fd} "
              f"[{'OK' if fd_ok else 'NG exp=' + str(exp_fd)}]")
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ---- CLI --------------------------------------------------------------------

def _resolve_date(args) -> date:
    if getattr(args, "date", None):
        return date.fromisoformat(args.date)
    return today_jst()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, helptext in [
        ("run-monday", "MONDAY: 第2月曜なら依頼を送信して記録"),
        ("run-wednesday", "WEDNESDAY: フォローアップ日に返答確認→未返答ならリマインド送信"),
        ("run-monitor", "MONITOR: フォローアップ後の返答を監視→返答あれば完了化"),
    ]:
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--date", help="基準日 YYYY-MM-DD（省略時は JST 今日）")

    p_decide = sub.add_parser("decide", help="判定のみ（送信・記録なし）")
    p_decide.add_argument("--job", required=True, choices=["monday", "wednesday", "monitor"])
    p_decide.add_argument("--date", help="基準日 YYYY-MM-DD（省略時は JST 今日）")

    p_req = sub.add_parser("record-request", help="依頼送信を手動記録（復旧用）")
    p_req.add_argument("--ts", required=True)
    p_req.add_argument("--date", help="基準日 YYYY-MM-DD")

    p_fu = sub.add_parser("record-followup", help="フォローアップ送信を手動記録（復旧用）")
    p_fu.add_argument("--ts", required=True)
    p_fu.add_argument("--date", help="基準日 YYYY-MM-DD")

    p_done = sub.add_parser("record-done", help="完了を手動記録（復旧用）")
    p_done.add_argument("--date", help="基準日 YYYY-MM-DD")

    sub.add_parser("check-auth", help="SLACK_BOT_TOKEN の疎通と bot 名義を確認")
    sub.add_parser("status", help="現在の状態ファイルを表示")
    sub.add_parser("selftest", help="日付ロジックの回帰テスト")

    args = parser.parse_args(argv)

    def emit(obj):
        print(json.dumps(obj, ensure_ascii=False, indent=2))

    if args.cmd == "run-monday":
        emit(run_monday(_resolve_date(args)))
        return 0
    if args.cmd == "run-wednesday":
        emit(run_wednesday(_resolve_date(args)))
        return 0
    if args.cmd == "run-monitor":
        emit(run_monitor(_resolve_date(args)))
        return 0
    if args.cmd == "decide":
        emit(decide(args.job, _resolve_date(args)))
        return 0
    if args.cmd == "record-request":
        emit(record_request(args.ts, _resolve_date(args)))
        return 0
    if args.cmd == "record-followup":
        emit(record_followup(args.ts, _resolve_date(args)))
        return 0
    if args.cmd == "record-done":
        emit(record_done(_resolve_date(args)))
        return 0
    if args.cmd == "check-auth":
        emit(check_auth())
        return 0
    if args.cmd == "status":
        emit(load_state())
        return 0
    if args.cmd == "selftest":
        return selftest()

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
