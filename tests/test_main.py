"""
焚き火ボットの自動テスト（外部サービスに接続せずに動く）

実行: python -m unittest discover -s tests -v

LINE SDK と Anthropic SDK はダミーに差し替えるので、
ネットにつながらない環境やライブラリ未インストールの環境でも動く。
会社の自動運営では、このテストが全部通らない変更は本番に出さない。
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------- ダミーの外部ライブラリ ----------
SENT = []          # LINEに返信した内容
CLAUDE_CALLS = []  # Claudeに送った内容
CLAUDE_MODE = {"fail": False, "answer": "それはしんどいね。"}


def _install_fakes():
    linebot = types.ModuleType("linebot")
    v3 = types.ModuleType("linebot.v3")
    exceptions = types.ModuleType("linebot.v3.exceptions")
    messaging = types.ModuleType("linebot.v3.messaging")
    webhooks = types.ModuleType("linebot.v3.webhooks")

    class InvalidSignatureError(Exception):
        pass

    class WebhookHandler:
        def __init__(self, secret):
            self.handlers = {}

        def add(self, event_cls, message=None):
            def deco(fn):
                self.handlers[event_cls.__name__] = fn
                return fn
            return deco

        def handle(self, body, signature):
            if signature != "valid":
                raise InvalidSignatureError()

    class _Obj:
        def __init__(self, *args, **kwargs):
            self.__dict__.update(kwargs)

    class ApiClient(_Obj):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class MessagingApi(_Obj):
        def __init__(self, client):
            pass

        def reply_message(self, req):
            SENT.append(req)

    for name in ["Configuration", "MessageAction", "QuickReply", "QuickReplyItem",
                 "ReplyMessageRequest", "TextMessage"]:
        setattr(messaging, name, type(name, (_Obj,), {}))
    messaging.ApiClient = ApiClient
    messaging.MessagingApi = MessagingApi
    for name in ["FollowEvent", "MessageEvent", "TextMessageContent"]:
        setattr(webhooks, name, type(name, (_Obj,), {}))

    exceptions.InvalidSignatureError = InvalidSignatureError
    v3.WebhookHandler = WebhookHandler

    anthropic = types.ModuleType("anthropic")

    class _Messages:
        def create(self, **kwargs):
            CLAUDE_CALLS.append(kwargs)
            if CLAUDE_MODE["fail"]:
                raise RuntimeError("api down")
            block = types.SimpleNamespace(type="text", text=CLAUDE_MODE["answer"])
            return types.SimpleNamespace(content=[block])

    class Anthropic:
        def __init__(self, *a, **k):
            self.messages = _Messages()

    anthropic.Anthropic = Anthropic

    sys.modules.update({
        "linebot": linebot, "linebot.v3": v3, "linebot.v3.exceptions": exceptions,
        "linebot.v3.messaging": messaging, "linebot.v3.webhooks": webhooks,
        "anthropic": anthropic,
    })


_install_fakes()
os.environ.setdefault("LINE_CHANNEL_SECRET", "test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test")
import main  # noqa: E402


def _event(text, user="U1"):
    return types.SimpleNamespace(
        reply_token="tok",
        source=types.SimpleNamespace(user_id=user),
        message=types.SimpleNamespace(text=text),
    )


def _last_text():
    return SENT[-1].messages[0].text


class TakibiTest(unittest.TestCase):
    def setUp(self):
        SENT.clear()
        CLAUDE_CALLS.clear()
        CLAUDE_MODE.update(fail=False, answer="それはしんどいね。")
        main.sessions.clear()

    # --- エンドポイント ---
    def test_health_check(self):
        c = main.app.test_client()
        self.assertEqual(c.get("/").status_code, 200)

    def test_callback_rejects_bad_signature(self):
        c = main.app.test_client()
        r = c.post("/callback", data="{}", headers={"X-Line-Signature": "bad"})
        self.assertEqual(r.status_code, 400)

    def test_callback_accepts_post(self):
        c = main.app.test_client()
        r = c.post("/callback", data="{}", headers={"X-Line-Signature": "valid"})
        self.assertEqual(r.status_code, 200)

    # --- 会話 ---
    def test_reply_comes_from_claude_with_burn_button(self):
        main.on_message(_event("もう限界"))
        self.assertEqual(_last_text(), "それはしんどいね。")
        self.assertIsNotNone(SENT[-1].messages[0].quick_reply)

    def test_history_is_kept_per_user(self):
        main.on_message(_event("1つ目", "A"))
        main.on_message(_event("2つ目", "A"))
        main.on_message(_event("別の人", "B"))
        self.assertEqual(len(main.sessions["A"]["messages"]), 4)
        self.assertEqual(len(main.sessions["B"]["messages"]), 2)

    def test_history_is_capped(self):
        for i in range(main.MAX_TURNS + 10):
            main.on_message(_event(f"愚痴{i}"))
        hist = main.sessions["U1"]["messages"]
        self.assertLessEqual(len(hist), main.MAX_TURNS + 1)
        self.assertEqual(CLAUDE_CALLS[-1]["messages"][0]["role"], "user")

    def test_api_failure_sends_apology_and_keeps_history_clean(self):
        CLAUDE_MODE["fail"] = True
        main.on_message(_event("聞いて"))
        self.assertIn("もう一度", _last_text())
        self.assertEqual(main.sessions["U1"]["messages"], [])

    # --- 燃やす（このサービスの約束） ---
    def test_burn_erases_history_and_does_not_call_claude(self):
        main.on_message(_event("愚痴"))
        calls = len(CLAUDE_CALLS)
        main.on_message(_event(main.BURN_WORD))
        self.assertNotIn("U1", main.sessions)
        self.assertEqual(len(CLAUDE_CALLS), calls)
        self.assertEqual(_last_text(), main.BURN_REPLY)

    def test_idle_session_expires(self):
        main.on_message(_event("愚痴"))
        main.sessions["U1"]["updated"] -= main.IDLE_SECONDS + 1
        main.on_message(_event("また来た"))
        self.assertEqual(len(main.sessions["U1"]["messages"]), 2)

    def test_no_files_or_database_used(self):
        with open(main.__file__, encoding="utf-8") as f:
            src = f.read()
        for forbidden in ["sqlite", "open(", "redis", "psycopg", "pickle", "json.dump"]:
            self.assertNotIn(forbidden, src, f"会話を保存する処理は禁止: {forbidden}")

    # --- 安全（変更禁止の約束） ---
    def test_safety_rules_are_in_system_prompt(self):
        for must in ["まもろうよ こころ", "189", "地域包括支援センター", "110番", "119番"]:
            self.assertIn(must, main.SYSTEM_PROMPT)
        self.assertIn(main.SAFETY, main.SYSTEM_PROMPT)

    def test_system_prompt_is_sent_to_claude(self):
        main.on_message(_event("聞いて"))
        self.assertEqual(CLAUDE_CALLS[-1]["system"], main.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
