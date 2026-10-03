# 焚き火（takibi）

育児や介護の疲れ・イライラをLINEでAIに吐き出して、スッキリしたら「燃やす」で全部消せるLINEボットです。

- AI（Claude）が短く受け止める。「親なんだから」と説教せず、頼まれていないアドバイスもしない
- 子どもや家族に手が出そうなときは、責めずに相談先（189、親子のための相談LINE、地域包括支援センターなど）を案内
- 「🔥 燃やす」で、サーバー上の会話をその場で消去
- 会話はメモリに一時的に置くだけで、ファイルやデータベースには保存しない
- 返信はすべて Reply API なので、LINE公式アカウントの無料プランで運用できる

## 構成

```
利用者のLINE → LINEのサーバー → Render（main.py） → Claude API
                                   ↑
                        cron-job.org（10分おきに起こす）
```

## 必要な環境変数

| 名前 | 中身 |
| --- | --- |
| `LINE_CHANNEL_SECRET` | LINE Developers の「チャネル基本設定」にあるチャネルシークレット |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Developers の「Messaging API設定」で発行するチャネルアクセストークン（長期） |
| `ANTHROPIC_API_KEY` | Claude Console（platform.claude.com）の Settings → API keys で発行したキー |
| `CLAUDE_MODEL` | 任意。省略時は `claude-haiku-4-5-20251001` |

キーやトークンはこのリポジトリに書かず、必ずRenderの環境変数に設定してください。

## Renderへのデプロイ

1. Render で「New」→「Web Service」を選び、このリポジトリを選択
2. Build Command: `pip install -r requirements.txt`
3. Start Command: `gunicorn main:app --workers 1 --threads 4`（会話をメモリで持つため、ワーカーは1つにする）
4. Environment に上の環境変数を登録してデプロイ

## LINE側の設定

1. LINE Developers の「Messaging API設定」で、Webhook URL に `https://（RenderのURL）/callback` を入れて「検証」
2. 「Webhookの利用」をON
3. LINE Official Account Manager の応答設定で「応答メッセージ」をOFF

## スリープ対策（無料プランの場合）

Renderの無料プランは15分アクセスがないと停止します。cron-job.org などで `https://（RenderのURL）/` に10分おきにアクセスさせると、常に起きた状態を保てます。

## テスト

```
python -m unittest discover -s tests -v
```

LINEとClaudeのライブラリをダミーに差し替えて動くので、外部サービスなしで実行できます。

## 会社としての自動運営

このリポジトリは「焚き火カンパニー」として、Claudeが毎日1回自動で運営しています。
社内規則とお客さまとの約束は `CLAUDE.md`、部署（サブエージェント）は `.claude/agents/` にあります。

- `docs/backlog.md`：改善案と今日の改善
- `docs/owner_todo.md`：オーナーにお願いしたいこと
- `docs/research/`：調査部のレポート
- `docs/marketing/`：広報部の投稿下書き
- `reports/`：日報
