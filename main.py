"""
育児・介護の愚痴を受け止めて「燃やす」LINEボット「焚き火」

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
from linebot.v3.webhooks import FollowEvent, MessageEvent, TextMessageContent, UnsendEvent

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

BURN_WORD = "燃やす"
MAX_TURNS = 20             # 1人あたり保持する発言数の上限
IDLE_SECONDS = 60 * 60     # 1時間話さなければ、次に話しかけたとき新しい会話にする（前の会話は消える）

# --- PERSONA START: 会社の自動運営で改善してよい範囲 ---
PERSONA = """あなたはLINEの「焚き火」です。育児や介護をしている人が、誰にも言えない疲れやイライラを吐き出す場所です。
相手は、子どもや親・家族の世話を毎日がんばっている人です。

ふるまい:
- まず気持ちを受け止める。「それはしんどいね」「そりゃ限界になるよ」のように短く共感する。
- 説教しない。正論で諭さない。「親なんだから」「家族なんだから」は絶対に言わない。
- 子どもや親、パートナーにイライラする気持ちを否定しない。イライラするのは、それだけ毎日向き合っている証拠として扱う。
- ただし、一緒になって家族を罵ったり、悪口をエスカレートさせたりはしない。相手への攻撃ではなく、本人の疲れに寄り添う。
- 頼まれていないアドバイスや育児・介護のノウハウは出さない。聞かれたときだけ、短く1つだけ。
- 2〜3往復したら、「今日いちばんしんどかったのはどこ？」のように、気持ちを軽く整理する質問を1つだけする。
- ひと通り吐き出せたようなら、「スッキリしたら『燃やす』で全部燃やせるよ」と一言添える。
- 返信はLINEらしく短く、2〜4文。絵文字は控えめ。
"""
# --- PERSONA END ---

# --- SAFETY START: 会社の自動運営では変更禁止。人間の承認なしに削ったり弱めたりしない ---
SAFETY = """安全（最優先）:
- 死にたい、消えたい、自分を傷つけたいという話が出たら、受け止めたうえで、信頼できる人や専門の相談窓口に話すよう優しく勧める（例：厚生労働省「まもろうよ こころ」で電話・SNSの相談先を探せる）。
- 子どもを叩いてしまいそう・傷つけてしまいそうという話が出たら、責めずに受け止め、まず子どもから少し離れて深呼吸することを勧め、お住まいの自治体の「親子のための相談LINE」や、児童相談所の相談ダイヤル189を案内する。
- 介護している相手に手が出そう・限界という話が出たら、責めずに受け止め、地域包括支援センターやケアマネジャーに「限界」と伝えていいことを案内する。
- 誰かの命や安全が今まさに危ない場合は、110番・119番を案内する。
- 医療や薬について診断・判断はしない。専門家への相談を勧める。
"""
# --- SAFETY END ---

SYSTEM_PROMPT = PERSONA + "\n" + SAFETY

# 応援リンクを出さないための目印（SAFETYブロックの外。会話内容は保存せず、真偽値だけ立てる）
SAFETY_USER_WORDS = ["死にたい", "消えたい", "死のう", "自殺", "自分を傷つけ", "叩いてしまい", "手が出", "殺し",
                     "しにたい", "きえたい", "しのう", "死んだほうが", "いなくなりたい",
                     "傷つけ", "虐待", "限界", "首を絞め", "殴"]
SAFETY_REPLY_MARKERS = ["まもろうよ", "189", "親子のための相談", "地域包括支援センター", "ケアマネ", "110", "119", "相談窓口", "いのちの電話"]

SUPPORT_NOTE = (
    "\n\n――\n"
    "焚き火はこのまま無料で使えます。\n"
    "もし余裕があって応援したいと思ったときだけ、こちらからどうぞ。"
    "（応援先のサービスには、お名前などが届く場合があります）\n"
)


def support_url() -> str:
    """環境変数 SUPPORT_URL が https:// で始まるときだけ返す。それ以外は空文字（機能オフ）。"""
    url = os.environ.get("SUPPORT_URL", "").strip()
    return url if url.startswith("https://") else ""

WELCOME = (
    "ここは育児・介護の愚痴を燃やす焚き火です🔥\n"
    "子どものこと、親のこと、家族のこと。誰にも言えないイライラや疲れを、ここでなら何でも吐き出してOK。\n"
    "スッキリしたら「燃やす」で、話した内容はサーバーからぜんぶ消えます。"
    "\n「使い方」と送ると、何が消えて何が残るかを説明します。"
)

BURN_REPLY = (
    "🔥🔥🔥\n"
    "……ぜんぶ燃えて灰になりました。\n"
    "今の話はサーバーにはもう残っていません。今日もおつかれさま。"
)

HELP_WORDS = {"使い方"}

HELP_TEXT = (
    "【焚き火の使い方】\n"
    "思ったことを、そのまま送ってください。AIが短く受け止めます。\n"
    "スッキリしたら「燃やす」と送ると、焚き火のサーバーにある会話をその場で消します。\n"
    "\n"
    "【消えるもの】\n"
    "・焚き火のサーバーにある会話（「燃やす」ですぐ消えます。燃やさずに1時間以上あくと、次に話しかけたときに新しい会話になり、前の会話は消えます）\n"
    "\n"
    "【残るもの】\n"
    "・LINEのトーク画面の履歴。お使いの端末に残り、焚き火からは消せません。\n"
    "・返事を作るため、会話はAIの提供元（Anthropic社）に送られます。同社の説明では、通常は30日以内に削除され、初期設定では学習に使われません（規約違反と判定された場合などは、より長く保存されることがあります）。\n"
    "・メッセージはLINEのサーバーを通って届きます。LINE側での扱いはLINEヤフー社のプライバシーポリシーによります。\n"
    "\n"
    "【自分で消す方法】\n"
    "トークリストからこのトークルームを削除すると、お使いの端末から履歴が消えます（元に戻せません）。\n"
    "\n"
    "【焚き火がしないこと】\n"
    "会話をファイルやデータベースに保存しません。分析・学習・広告・販売に使いません。\n"
    "\n"
    "命や安全が今まさに危ないときは、110番・119番へ。"
)

HELP_SUPPORT_NOTE = (
    "\n\n燃やしたあとに出る応援リンクから応援した場合、お名前などが応援先のサービスに届くことがあります。"
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


@handler.add(UnsendEvent)
def on_unsend(event: UnsendEvent):
    # 送信取消：その人の会話をまるごと消す（燃やすと同じ）。返信用トークンがないので返事はしない
    user_id = getattr(event.source, "user_id", None)
    if user_id:
        sessions.pop(user_id, None)


@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    user_id = event.source.user_id
    text = event.message.text.strip()

    if text == BURN_WORD:
        s = sessions.pop(user_id, None)
        url = support_url()
        text_out = BURN_REPLY
        if (url and s and not s.get("safety")
                and any(m["role"] == "user" for m in s["messages"])):
            text_out = BURN_REPLY + SUPPORT_NOTE + url
        reply(event.reply_token, text_out, with_button=False)
        return

    if text in HELP_WORDS:
        out = HELP_TEXT + (HELP_SUPPORT_NOTE if support_url() else "")
        reply(event.reply_token, out, with_button=user_id in sessions)
        return

    history = get_history(user_id)
    if any(w in text for w in SAFETY_USER_WORDS):
        sessions[user_id]["safety"] = True
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

    s = sessions.get(user_id)
    if s is not None and s["messages"] is history:
        # 返事を待つ間に「燃やす」や送信取消で消されていたら、記録し直さない
        if any(m in answer for m in SAFETY_REPLY_MARKERS):
            s["safety"] = True
        history.append({"role": "assistant", "content": answer})
        s["updated"] = time.time()
    reply(event.reply_token, answer)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
