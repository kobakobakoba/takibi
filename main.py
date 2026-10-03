"""
愚痴を受け止めて「燃やす」LINEボット（最小構成）

- 会話はサーバーのメモリにだけ一時保存し、ファイルやDBには残さない
- 「燃やす」で会話をその場で消去する
- 返信はすべて Reply API なので、LINEの無料プランでも通数制限にかからない
"""
import os
import time

import anthropic
from flask import Flask, abort, request
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessageAction,
    MessagingApi,
    QuickReply,
    QuickReplyItem,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import FollowEvent, MessageEvent, TextMessageContent

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

BURN_WORD = "燃やす"
MAX_TURNS = 20             # 1人あたり保持する発言数の上限
IDLE_SECONDS = 60 * 60     # 1時間話さなければ自動で消去

SYSTEM_PROMPT = """あなたはLINEの「愚痴の焚き火」です。ユーザーが誰にも言えない怒りや愚痴を吐き出す場所です。

ふるまい:
- まず気持ちを受け止める。「それはムカつくね」「そりゃ腹立つよ」のように短く共感する。
- 説教しない。正論で諭さない。ユーザーを否定しない。
- 一緒になって特定の人を罵ったり、悪口をエスカレートさせたりはしない。怒っている「気持ち」に寄り添い、相手への攻撃には加わらない。
- 2〜3往復したら、「一番引っかかってるのはどこ？」のように、気持ちを軽く整理する質問を1つだけする。
- 仕返しや、SNSでの晒し・書き込みを考えている様子なら、否定せずに「送る前に一晩おいてみない？」と提案する。
- ひと通り吐き出せたようなら、「スッキリしたら『燃やす』で全部燃やせるよ」と一言添える。
- 返信はLINEらしく短く、2〜4文。絵文字は控えめ。

安全:
- 死にたい、消えたい、自分を傷つけたいという話や、誰かに危害を加える具体的な話が出たら、受け止めたうえで、信頼できる人や専門の相談窓口に話すよう優しく勧める（例：厚生労働省「まもろうよ こころ」で電話・SNSの相談先を探せる）。命に関わる緊急時は110番・119番。
"""

WELCOME = (
    "ここは愚痴の焚き火です🔥\n"
    "ムカついたこと、言えなかったこと、ここでなら何でも吐き出してOK。\n"
    "スッキリしたら「燃やす」で、話した内容はぜんぶ消えます。"
)

BURN_REPLY = (
    "🔥🔥🔥\n"
    "……ぜんぶ燃えて灰になりました。\n"
    "今の話はもうどこにも残っていません。おつかれさま。"
)

app = Flask(__name__)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
claude = anthropic.Anthropic()  # ANTHROPIC_API_KEY を環境変数から読む

# user_id -> {"messages": [...], "updated": float}
sessions: dict[str, dict] = {}

burn_button = QuickReply(
    items=[QuickReplyItem(action=MessageAction(label="🔥 燃やす", text=BURN_WORD))]
)


def get_history(user_id: str) -> list[dict]:
    s = sessions.get(user_id)
    if s is None or time.time() - s["updated"] > IDLE_SECONDS:
        s = {"messages": [], "updated": time.time()}
        sessions[user_id] = s
    return s["messages"]


def reply(reply_token: str, text: str, with_button: bool = True) -> None:
    with ApiClient(line_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[
                    TextMessage(
                        text=text,
                        quick_reply=burn_button if with_button else None,
                    )
                ],
            )
        )


@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@app.get("/")
def health():
    return "ok"


@handler.add(FollowEvent)
def on_follow(event: FollowEvent):
    reply(event.reply_token, WELCOME, with_button=False)


@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    user_id = event.source.user_id
    text = event.message.text.strip()

    if text == BURN_WORD:
        sessions.pop(user_id, None)
        reply(event.reply_token, BURN_REPLY, with_button=False)
        return

    history = get_history(user_id)
    history.append({"role": "user", "content": text})
    del history[:-MAX_TURNS]
    # Claude API は user から始まる必要がある
    while history and history[0]["role"] != "user":
        history.pop(0)

    try:
        res = claude.messages.create(
            model=MODEL,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=history,
        )
        answer = "".join(b.text for b in res.content if b.type == "text").strip()
    except Exception:
        history.pop()
        answer = "ごめん、いま火の調子が悪いみたい。少ししてからもう一度送ってみて。"
        reply(event.reply_token, answer)
        return

    history.append({"role": "assistant", "content": answer})
    sessions[user_id]["updated"] = time.time()
    reply(event.reply_token, answer)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
