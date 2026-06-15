# 月次記事リリース確認リマインド — 運用手順（Routine 実行用）

このリポジトリは、毎月 小林 亜佳穂さん（`@UU33R4YCQ`）に「今月リリースする投稿記事
があるか」を確認依頼し、未返答ならフォローアップするまでの一連の自動フローを、
**Claude Code on the web の Routine（スケジュール実行）** で動かすためのものです。

元々は別エージェント基盤（投稿者 = bot「Mantaさん」）で cron 運用していたものを移植
しています。日付計算・状態遷移は `scripts/reminder.py`（決定論的）に集約し、Slack の
送受信のみをこのセッションが Slack コネクタ（MCP）で行います。

---

## 運用対象（固定値）

| 項目 | 値 |
| --- | --- |
| Slack チャンネル | `C02MLKUPYK0`（`#sol-prj-dentsu-cxcc`） |
| 確認相手 | 小林 亜佳穂さん `@UU33R4YCQ`（kobayashi@100inc.jp） |
| 状態ファイル | リポジトリ直下 `kobayashi_article_reminder_ts.json` |
| タイムゾーン | JST（Asia/Tokyo）固定 |

---

## 鉄則

1. **日付判定は必ず `scripts/reminder.py` に委ねる。** 「第2月曜か」「フォローアップ日か」
   を自分で計算しない。`decide` の `action` に従うこと。
2. **状態を変更したら必ず状態ファイルを commit & push する。** Routine 実行は毎回リポジトリ
   を新規 clone するため、git に保存しないと次回に引き継がれない。コミット先は
   **このセッションが動いている（= clone された既定）ブランチ**。
3. `action: "skip"` の場合は **何も送信せず、何もコミットせず終了**する。
4. 送信文面は `decide` が返す `message` / `followup_message` を**そのまま**使う（改変しない）。
5. 迷ったら送信しない。重複送信より未送信の方が安全。

---

## 「小林さんが返答した」の判定基準（WEDNESDAY / MONITOR 共通）

以下の **いずれか** を満たせば「返答あり」とみなす。

- 依頼メッセージ（`request_ts`）または リマインド（`followup_ts`）への **リアクション**
  が `@UU33R4YCQ` から付いている → `slack_get_reactions` で確認。
- 依頼メッセージのスレッド返信に `@UU33R4YCQ` の発言がある → `slack_read_thread`。
- チャンネル内に、依頼送信日時（`request_sent_at` 以降）の **`@UU33R4YCQ` の新規投稿**
  がある → `slack_read_channel`。過去実績では小林さんはスレッドではなくチャンネルに
  トップレベルで返信する（例:「今月は下記の記事を公開予定です…」「今月公開予定の記事は
  ありません」）。

> 注: bot や自分（水野）の投稿は返答に含めない。判定対象は `@UU33R4YCQ` のみ。

---

## ジョブ手順

### MONDAY ジョブ（毎週 月曜 12:00 JST）

1. `python3 scripts/reminder.py decide --job monday` を実行。
2. `action == "skip"` → 終了。
3. `action == "send_request"`:
   1. `message` を `channel_id` に送信（`slack_send_message`）。
   2. 返ってきたメッセージの **ts** を控える。
   3. `python3 scripts/reminder.py record-request --ts <ts>` で記録。
   4. `kobayashi_article_reminder_ts.json` を commit & push。

### WEDNESDAY ジョブ（毎週 水曜 12:00 JST）

1. `python3 scripts/reminder.py decide --job wednesday` を実行。
2. `action == "skip"` → 終了。
3. `action == "check_and_followup"`:
   1. 上記「返答判定基準」で小林さんの返答有無を確認。
   2. **返答あり** → `python3 scripts/reminder.py record-done` → commit & push → 終了。
   3. **返答なし** → `followup_message` を `channel_id` に送信（過去実績に合わせ、スレッドで
      はなくチャンネルにトップレベル投稿）。返ってきた ts を控え、
      `python3 scripts/reminder.py record-followup --ts <ts>` → commit & push。

### MONITOR ジョブ（平日 月〜金 10:00 JST）

1. `python3 scripts/reminder.py decide --job monitor` を実行。
2. `action == "skip"` → 終了。
3. `action == "monitor"`:
   1. 上記「返答判定基準」で返答有無を確認。
   2. **返答あり** → `python3 scripts/reminder.py record-done` → commit & push（以降の実行は
      自動で skip になり監視終了）。
   3. **返答なし** → 何も送信せず終了（翌営業日に再チェック）。**追加のリマインドは送らない。**

---

## 状態ファイルのスキーマ

```json
{
  "month": "2026-06",          // 当該サイクルの対象月（YYYY-MM）
  "ts": "1780887730.810799",   // 依頼メッセージの ts
  "followup_ts": null,         // リマインドの ts（未送信は null）
  "sent_at": "2026-06-08",     // 依頼送信日
  "status": "done"             // "pending"=返答待ち / "done"=返答確認済み・完了
}
```

月が変わると `decide` は前月の状態を無視し、新サイクルとして扱う（`month` キーで判定）。

---

## ローカル/手動確認用コマンド

```bash
python3 scripts/reminder.py selftest                      # 日付ロジックの回帰テスト
python3 scripts/reminder.py status                        # 現在の状態を表示
python3 scripts/reminder.py decide --job monday           # 月曜ジョブの判定（送信はしない）
python3 scripts/reminder.py decide --job monday --date 2026-07-13  # 任意日で判定
```

`decide` は **判定のみ**で Slack 送信や状態変更は行わない。送信・記録はこの手順書に沿って
セッションが実行する。
