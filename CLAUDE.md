# 月次記事リリース確認リマインド — 運用手順（Routine 実行用）

このリポジトリは、毎月 小林 亜佳穂さん（`@UU33R4YCQ`）に「今月リリースする投稿記事
があるか」を確認依頼し、未返答ならフォローアップするまでの一連の自動フローを、
**Claude Code on the web の Routine（スケジュール実行）** で動かすためのものです。

元々は別エージェント基盤（投稿者 = bot「Mantaさん」）で cron 運用していたものを移植して
います。**判定・状態遷移・Slack 送受信のすべてを `scripts/reminder.py` が決定論的に実行**
します（bot「Mantaさん」のトークンで Slack Web API を直接呼び出し）。Routine セッション
（AI）の役割は **「適切な `run-*` コマンドを実行し、状態ファイルに差分が出たら commit &
push する」だけ**に最小化されています。

---

## 運用対象（固定値）

| 項目 | 値 |
| --- | --- |
| Slack チャンネル | `C02MLKUPYK0`（`#sol-prj-dentsu-cxcc`） |
| 確認相手 | 小林 亜佳穂さん `@UU33R4YCQ`（kobayashi@100inc.jp） |
| 投稿者 | bot「Mantaさん」（`SLACK_BOT_TOKEN` の xoxb- トークン名義） |
| 状態ファイル | リポジトリ直下 `kobayashi_article_reminder_ts.json` |
| タイムゾーン | JST（Asia/Tokyo）固定 |

> **前提（2点）**:
> 1. 環境変数 `SLACK_BOT_TOKEN`（bot「Mantaさん」の `xoxb-` トークン）が Routine 環境に設定されていること。
>    未設定だと送信・確認系コマンドは明確にエラー終了します（誤送信はしません）。
> 2. Routine 環境の **Network access が `slack.com` を許可**していること（Custom で `slack.com` /
>    `*.slack.com` を追加）。既定の Trusted では `slack.com` 直叩きが HTTP 403 になります。詳細は README 参照。

---

## 鉄則

1. **日付判定も返答判定も `scripts/reminder.py` に委ねる。** 「第2月曜か」「フォローアップ日か」
   「小林さんが返答したか」を自分で計算・判断しない。`run-*` の出力 `did` に従うこと。
2. **状態ファイルに差分が出たら必ず commit & push する。** Routine 実行は毎回リポジトリを
   新規 clone するため、git に保存しないと次回に引き継がれない。コミット先は
   **このセッションが動いている（= clone された既定）ブランチ**。
3. `did: "skip"` / `"still_waiting"` の場合は **状態に差分が出ない**ので、何もコミットせず終了する。
4. スクリプトを介さず Slack に手動投稿しない（文面・重複制御はスクリプトが担保している）。
5. 迷ったら追加送信しない。重複送信より未送信の方が安全。

---

## ジョブ手順（各ジョブ共通の流れ）

各 Routine は、対応する 1 コマンドを実行し、状態差分があれば commit & push するだけです。

```bash
# 1) ジョブ実行（判定→Slack送受信→状態記録まで一括）
python3 scripts/reminder.py run-<job>

# 2) 状態ファイルに差分があれば commit & push（差分が無ければ何もしない）
git add kobayashi_article_reminder_ts.json
git diff --cached --quiet || { git commit -m "月次記事リリース確認: 状態更新"; git push; }
```

### MONDAY ジョブ（毎週 月曜 12:00 JST）
```bash
python3 scripts/reminder.py run-monday
```
出力 `did` の意味:
- `skip` … 第2月曜でない/当月送信済み → 何もしない。
- `sent_request` … 依頼を送信し、状態を `pending` に記録済み（`ts` が出力される）。

### WEDNESDAY ジョブ（毎週 水曜 12:00 JST）
```bash
python3 scripts/reminder.py run-wednesday
```
出力 `did` の意味:
- `skip` … フォローアップ日でない/未送信/送信済み/既に done → 何もしない。
- `marked_done` … 小林さんの返答を検知 → `done` に記録（フォローアップ不要）。
- `sent_followup` … 未返答のためリマインドを送信し、`followup_ts` を記録済み。

### MONITOR ジョブ（平日 月〜金 10:00 JST）
```bash
python3 scripts/reminder.py run-monitor
```
出力 `did` の意味:
- `skip` … 当月レコードなし/既に done/フォローアップ未送信 → 何もしない。
- `marked_done` … フォローアップ後の返答を検知 → `done` に記録（以降は自動 skip で監視終了）。
- `still_waiting` … 未返答。**何も送らず終了**（翌営業日に再チェック）。追加リマインドは送らない。

---

## 「小林さんが返答した」の判定基準（スクリプトが自動判定）

`run-wednesday` / `run-monitor` は、以下の **いずれか** を満たせば「返答あり」とみなして
`done` に記録します（`kobayashi_responded()`）。

- 依頼（`ts`）または リマインド（`followup_ts`）への `@UU33R4YCQ` の **リアクション**。
- 依頼メッセージの **スレッド返信** に `@UU33R4YCQ` の発言。
- 依頼送信（`ts`）以降の、`@UU33R4YCQ` による **チャンネルへのトップレベル投稿**
  （過去実績では小林さんはスレッドではなくチャンネルに直接返信する）。

> bot や水野さんの投稿は返答に含めない（判定対象は `@UU33R4YCQ` のみ）。

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
python3 scripts/reminder.py selftest                 # 日付ロジックの回帰テスト（送信なし）
python3 scripts/reminder.py status                   # 現在の状態を表示
python3 scripts/reminder.py decide --job monday --date 2026-07-13  # 判定のみ（送信・記録なし）
python3 scripts/reminder.py check-auth               # SLACK_BOT_TOKEN の疎通と bot 名義を確認
```

`decide` は判定のみ。`record-request` / `record-followup` / `record-done` は障害時の手動復旧用。
`run-*` 以外は Slack に送信しない（`check-auth` を除き Slack を呼ばない）。
