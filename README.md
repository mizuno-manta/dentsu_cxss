# dentsu_cxss — 月次記事リリース確認リマインド

毎月、小林 亜佳穂さん（`@UU33R4YCQ`）に「今月リリースする投稿記事があるか」を Slack で
確認依頼し、未返答ならフォローアップ → 返答があるまで監視する自動フローです。

別エージェント基盤（cron 運用・投稿者 bot「Mantaさん」）で動いていたものを、
**Claude Code on the web の Routine（スケジュール実行）** に移植したものです。
移植により、元基盤で発生していた **120 秒タイムアウト**（重い AI 処理に起因）を回避します。
日付判定・状態遷移は決定論的な Python スクリプトに寄せ、AI には Slack のやりとりだけを
任せる構成です。

---

## 全体像（3 つのジョブ）

| ジョブ | スケジュール（JST） | 内容 |
| --- | --- | --- |
| ① 依頼 | 毎週 **月曜 12:00** → 第2月曜のみ実行 | 確認依頼を送信し ts を保存 |
| ② フォローアップ | 毎週 **水曜 12:00** → 第2月曜+2日のみ実行 | 未返答なら催促、返答済みなら完了化 |
| ③ 監視 | **平日 10:00** | フォローアップ後、返答があれば完了化して監視終了 |

> cron は「第2月曜」を直接表現できない（標準 cron の曜日×日付は OR 判定）ため、
> スケジュールは単純な曜日指定にし、**「第2月曜か」「+2日か」の判定は
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

## 構成

```
.
├── CLAUDE.md                          # Routine 実行セッション向けの運用手順（必読）
├── README.md                          # このファイル（セットアップ手順）
├── kobayashi_article_reminder_ts.json # 状態ファイル（当月の進捗）
└── scripts/
    └── reminder.py                    # 日付判定・状態管理エンジン（決定論的・stdlib のみ）
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
各 Routine で **Slack コネクタを有効**にしてください（既定で全コネクタが含まれます）。

### Routine ① 依頼（第2月曜）
- **Schedule（Trigger）**: Weekly / **Monday 12:00**（タイムゾーン **Asia/Tokyo**）
  - 参考 cron（UTC）: `0 3 * * 1`
- **Instructions（プロンプト）**:
  > 月次記事リリース確認の自動運用ジョブです。リポジトリの `CLAUDE.md` の手順に従い、
  > 「**MONDAY ジョブ**」を実行してください。日付判定は必ず `scripts/reminder.py` に委ね、
  > `action: skip` の場合は何も送らず終了してください。

### Routine ② フォローアップ（水曜）
- **Schedule**: Weekly / **Wednesday 12:00**（Asia/Tokyo）
  - 参考 cron（UTC）: `0 3 * * 3`
- **Instructions**:
  > 月次記事リリース確認の自動運用ジョブです。リポジトリの `CLAUDE.md` の手順に従い、
  > 「**WEDNESDAY ジョブ**」を実行してください。小林さんの返答有無の判定基準は CLAUDE.md
  > の通り。`action: skip` なら何もせず終了してください。

### Routine ③ 監視（平日）
- **Schedule**: Weekdays（月〜金）/ **10:00**（Asia/Tokyo）
  - 参考 cron（UTC）: `0 1 * * 1-5`
- **Instructions**:
  > 月次記事リリース確認の自動運用ジョブです。リポジトリの `CLAUDE.md` の手順に従い、
  > 「**MONITOR ジョブ**」を実行してください。返答があれば完了化し、なければ何も送らず終了
  > （追加リマインドは送らない）。`action: skip` なら何もせず終了してください。

> 1 つの Routine に複数トリガーを付けられますが、プロンプトは Routine 単位で 1 つです。
> ジョブごとに文面・挙動が異なるため、上記のように **3 つに分けて**ください。

---

## 投稿者（要確認ポイント）

- 元基盤では bot **「Mantaさん」**（`U0ARMGK1KEX`）として投稿していました。
- Claude Code の Slack コネクタは現在 **水野さん（`U04RMLC4XS6`）** として認証されているため、
  この構成では **投稿者が「水野」になります**。
- 「Mantaさん（bot）として投稿し続けたい」場合は、別途 bot トークンを使う方式に切り替える必要が
  あります（その場合は `reminder.py` に Slack 送信処理を内蔵し、トークンを環境変数で渡す構成へ
  変更します）。ご希望があればお知らせください。

---

## ローカル確認

```bash
python3 scripts/reminder.py selftest                 # 日付ロジックの回帰テスト
python3 scripts/reminder.py status                   # 現在の状態
python3 scripts/reminder.py decide --job monday --date 2026-07-13   # 任意日で判定（送信なし）
```

`decide` は判定のみで、Slack 送信・状態変更は行いません。

---

## 現在の状態（2026-06 時点）

6/8（第2月曜）に依頼送信済み → 小林さんが「確認中」リアクション後 6/9 に返信 → **完了（done）**。
そのため 6 月分は監視・フォローアップとも自動で skip されます。次サイクルは 2026-07-13（第2月曜）。
