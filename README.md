# 愚痴の焚き火（takibi）

ムカついたことをLINEでAIに吐き出して、スッキリしたら「燃やす」で全部消せるLINEボットです。

- AI（Claude）が短く受け止める。説教はせず、悪口のエスカレートにも乗らない
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
3. Start Command: `gunicorn main:app`
4. Environment に上の環境変数を登録してデプロイ

## LINE側の設定

1. LINE Developers の「Messaging API設定」で、Webhook URL に `https://（RenderのURL）/callback` を入れて「検証」
2. 「Webhookの利用」をON
3. LINE Official Account Manager の応答設定で「応答メッセージ」をOFF

## スリープ対策（無料プランの場合）

Renderの無料プランは15分アクセスがないと停止します。cron-job.org などで `https://（RenderのURL）/` に10分おきにアクセスさせると、常に起きた状態を保てます。
