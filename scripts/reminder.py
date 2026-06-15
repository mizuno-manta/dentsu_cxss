#!/usr/bin/env python3
"""月次記事リリース確認リマインド — 日付判定・状態管理エンジン（決定論的）。

このスクリプトは Slack への送信/読み取りを一切行いません。
日付（JST）と状態ファイルだけを根拠に「今日このジョブで何をすべきか」を
決定論的に判定し、JSON で出力します。

Slack の送受信は、Claude Code on the web の Routine から起動された
セッションが Slack コネクタ（MCP）経由で実行し、その結果を本スクリプトの
record-* サブコマンドで状態ファイルに書き戻します。

これにより:
  - 「第2月曜」「その2日後（水曜）」といった日付計算を LLM に依存しない
  - 状態遷移（pending / done / followup）を一元管理する
  - 元基盤で起きていた 120 秒タイムアウト相当の重い AI 処理を排除する

状態ファイル: リポジトリ直下 kobayashi_article_reminder_ts.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ---- 定数（運用対象）---------------------------------------------------------

# 日本標準時。DST が無いため常に UTC+9 固定で十分（tzdata 依存を避ける）。
JST = timezone(timedelta(hours=9))

CHANNEL_ID = "C02MLKUPYK0"      # #sol-prj-dentsu-cxcc
KOBAYASHI_ID = "UU33R4YCQ"      # 小林 亜佳穂 / Akane Kobayashi
MENTION = f"<@{KOBAYASHI_ID}>"  # Slack 送信時にメンションとして展開される

STATE_PATH = Path(__file__).resolve().parent.parent / "kobayashi_article_reminder_ts.json"

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
                "reason": "返答有無を確認し、未返答ならリマインドを送信してください。",
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
                "reason": "フォローアップ後の返答を監視。返答があれば record-done を実行してください。",
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
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_decide = sub.add_parser("decide", help="ジョブの実行可否とアクションを判定")
    p_decide.add_argument("--job", required=True, choices=["monday", "wednesday", "monitor"])
    p_decide.add_argument("--date", help="基準日 YYYY-MM-DD（省略時は JST 今日）")

    p_req = sub.add_parser("record-request", help="依頼送信を記録")
    p_req.add_argument("--ts", required=True, help="送信した依頼メッセージの ts")
    p_req.add_argument("--date", help="基準日 YYYY-MM-DD（省略時は JST 今日）")

    p_fu = sub.add_parser("record-followup", help="フォローアップ送信を記録")
    p_fu.add_argument("--ts", required=True, help="送信したリマインドメッセージの ts")
    p_fu.add_argument("--date", help="基準日 YYYY-MM-DD（省略時は JST 今日）")

    p_done = sub.add_parser("record-done", help="返答確認済み（完了）を記録")
    p_done.add_argument("--date", help="基準日 YYYY-MM-DD（省略時は JST 今日）")

    sub.add_parser("status", help="現在の状態ファイルを表示")
    sub.add_parser("selftest", help="日付ロジックの回帰テスト")

    args = parser.parse_args(argv)

    if args.cmd == "decide":
        print(json.dumps(decide(args.job, _resolve_date(args)), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "record-request":
        print(json.dumps(record_request(args.ts, _resolve_date(args)), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "record-followup":
        print(json.dumps(record_followup(args.ts, _resolve_date(args)), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "record-done":
        print(json.dumps(record_done(_resolve_date(args)), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "status":
        print(json.dumps(load_state(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "selftest":
        return selftest()

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
