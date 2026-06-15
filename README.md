# dentsu_cxss — 月次記事リリース確認リマインド

毎月、小林 亜佳穂さん（`@UU33R4YCQ`）に「今月リリースする投稿記事があるか」を Slack で
確認依頼し、未返答ならフォローアップ → 返答があるまで監視する自動フローです。

別エージェント基盤（cron 運用・投稿者 bot「Mantaさん」）で動いていたものを、
**Claude Code on the web の Routine（スケジュール実行）** に移植したものです。
移植により、元基盤で発生していた **120 秒タイムアウト**（重い AI 処理に起因）を回避します。

**設計方針 — AI を判断ループから外す:** 判定・状態遷移・Slack 送受信のすべてを
決定論的な Python（`scripts/reminder.py`、標準ライブラリのみ）に集約し、bot「Mantaさん」の
トークンで Slack Web API を直接呼び出します。Routine セッション（AI）の役割は
「`run-*` コマンドを実行し、状態ファイルに差分が出たら commit & push」だけです。

---

## 全体像（3 つのジョブ）

| ジョブ | スケジュール（JST） | コマンド | 内容 |
| --- | --- | --- | --- |
| ① 依頼 | 毎週 **月曜 12:00** | `run-monday` | 第2月曜なら確認依頼を送信し ts を保存 |
| ② フォローアップ | 毎週 **水曜 12:00** | `run-wednesday` | 第2月曜+2日に、未返答なら催促・返答済みなら完了化 |
| ③ 監視 | **平日 10:00** | `run-monitor` | フォローアップ後、返答があれば完了化して監視終了 |

> cron は「第2月曜」を直接表現できない（標準 cron の曜日×日付は OR 判定）ため、
> スケジュールは単純な曜日指定にし、**「第2月曜か」「+2日か」「返答したか」の判定は
> `scripts/reminder.py` が決定論的に行う**設計です。Routine の最小実行間隔（1時間）にも適合します。

### 状態遷移

```
[なし] --月曜(第2月曜)--> pending(依頼送信) --水曜: 返答あり--> done
                                          \--水曜: 返答なし--> pending(+followup_ts)
                                                               --平日監視: 返答あり--> done
```

状態は `kobayashi_article_reminder_ts.json` に保持し、git にコミットして次回実行へ引き継ぎます
（Routine は毎回リポジトリを新規 clone するため、ファイル等の状態は git 保存が必須）。

---

## 前提条件（必須設定）

bot「Mantaさん」名義で送受信するため、**Routine 環境の環境変数に `SLACK_BOT_TOKEN`
（`xoxb-...`）を設定**してください。未設定の場合、送信・確認系コマンドは誤送信せず明確に
エラー終了します。

- 必要な Bot Token Scopes:
  - `chat:write` … メッセージ送信
  - `reactions:read` … リアクション確認
  - `channels:history`（公開チャンネルの場合）/ `groups:history`（非公開の場合）… 返信・投稿確認
- bot「Mantaさん」が **チャンネル `#sol-prj-dentsu-cxcc`（`C02MLKUPYK0`）のメンバー**であること
  （元々ここに投稿していた bot なので通常は満たしています）。

設定後、疎通確認:
```bash
python3 scripts/reminder.py check-auth   # ok / bot_user_id / bot_name / team が返れば成功
```

---

## 構成

```
.
├── CLAUDE.md                          # Routine 実行セッション向けの運用手順（必読）
├── README.md                          # このファイル（セットアップ手順）
├── kobayashi_article_reminder_ts.json # 状態ファイル（当月の進捗）
└── scripts/
    └── reminder.py                    # 判定・状態管理・Slack送受信エンジン（stdlib のみ）
```

> 任意: 起動時にロジック検証＋状態表示を出したい場合は、`.claude/settings.json` に SessionStart
> フックを追加できます（`python3 scripts/reminder.py selftest && python3 scripts/reminder.py status`）。
> 自動運用には必須ではありません。

---

## セットアップ：Routine を 3 つ作成する

> 前提: この内容を**既定ブランチ（main 等）にマージ済み**であること。Routine は既定ブランチを
> clone して実行するため、`scripts/`・`CLAUDE.md`・状態ファイルが既定ブランチに存在する必要が
> あります。

Claude Code on the web の **Routines** 画面で、このリポジトリに対して以下 3 つを作成します。
各 Routine の Instructions（プロンプト）は共通フォーマットで、ジョブ名だけ変えます。

### 共通プロンプト（`<job>` を monday / wednesday / monitor に置換）
> 月次記事リリース確認の自動運用ジョブです。`python3 scripts/reminder.py run-<job>` を実行し、
> 出力された `did` を確認してください。`kobayashi_article_reminder_ts.json` に差分が出た場合のみ
> commit して push してください（差分が無ければ何もせず終了）。詳細は `CLAUDE.md` を参照。

### Routine ① 依頼
- **Schedule**: Weekly / **Monday 12:00**（Asia/Tokyo） … 参考 cron（UTC）: `0 3 * * 1`
- **Instructions**: 上記共通プロンプト（`run-monday`）

### Routine ② フォローアップ
- **Schedule**: Weekly / **Wednesday 12:00**（Asia/Tokyo） … 参考 cron（UTC）: `0 3 * * 3`
- **Instructions**: 上記共通プロンプト（`run-wednesday`）

### Routine ③ 監視
- **Schedule**: Weekdays（月〜金）/ **10:00**（Asia/Tokyo） … 参考 cron（UTC）: `0 1 * * 1-5`
- **Instructions**: 上記共通プロンプト（`run-monitor`）

> 各 Routine で **Slack コネクタは不要**です（送受信は bot トークンで直接行うため）。ただし
> `SLACK_BOT_TOKEN` が環境変数として渡る必要があります。

---

## ローカル確認

```bash
python3 scripts/reminder.py selftest                 # 日付ロジックの回帰テスト（送信なし）
python3 scripts/reminder.py status                   # 現在の状態
python3 scripts/reminder.py decide --job monday --date 2026-07-13   # 判定のみ（送信・記録なし）
python3 scripts/reminder.py check-auth               # SLACK_BOT_TOKEN の疎通確認
```

`run-monday` / `run-wednesday` / `run-monitor` は実際に Slack へ送受信します。テスト時は対象日
（第2月曜・その+2日）以外であれば `skip` で安全に終了します。

---

## 現在の状態（2026-06 時点）

6/8（第2月曜）に依頼送信済み → 小林さんが「確認中」リアクション後 6/9 に返信 → **完了（done）**。
そのため 6 月分は監視・フォローアップとも自動で skip されます。次サイクルは 2026-07-13（第2月曜）。
