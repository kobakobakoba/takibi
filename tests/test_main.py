"""
焚き火ボットの自動テスト（外部サービスに接続せずに動く）

実行: python -m unittest discover -s tests -v

LINE SDK と Anthropic SDK はダミーに差し替えるので、
ネットにつながらない環境やライブラリ未インストールの環境でも動く。
会社の自動運営では、このテストが全部通らない変更は本番に出さない。
"""
import os
import sys
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------- ダミーの外部ライブラリ ----------
SENT = []          # LINEに返信した内容
CLAUDE_CALLS = []  # Claudeに送った内容
CLAUDE_MODE = {"fail": False, "answer": "それはしんどいね。", "during": None}


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
    for name in ["FollowEvent", "MessageEvent", "TextMessageContent", "UnsendEvent"]:
        setattr(webhooks, name, type(name, (_Obj,), {}))

    exceptions.InvalidSignatureError = InvalidSignatureError
    v3.WebhookHandler = WebhookHandler

    anthropic = types.ModuleType("anthropic")

    class _Messages:
        def create(self, **kwargs):
            CLAUDE_CALLS.append(kwargs)
            if CLAUDE_MODE["during"]:
                CLAUDE_MODE["during"]()
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


def _unsend(user="U1"):
    # reply_token を持たせない（返事しようとすると AttributeError で分かる）
    return types.SimpleNamespace(source=types.SimpleNamespace(user_id=user),
                                 unsend=types.SimpleNamespace(message_id="m1"))


def _last_text():
    return SENT[-1].messages[0].text


class TakibiTest(unittest.TestCase):
    def setUp(self):
        SENT.clear()
        CLAUDE_CALLS.clear()
        CLAUDE_MODE.update(fail=False, answer="それはしんどいね。", during=None)
        main.sessions.clear()
        os.environ.pop("SUPPORT_URL", None)

    def tearDown(self):
        os.environ.pop("SUPPORT_URL", None)

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

    # --- 応援リンク（燃やしたあと、条件を満たすときだけ） ---
    URL = "https://example.com/x"

    def test_burn_without_support_url_is_unchanged(self):
        main.on_message(_event("愚痴"))
        main.on_message(_event(main.BURN_WORD))
        self.assertEqual(_last_text(), main.BURN_REPLY)

    def test_burn_shows_support_link_when_set(self):
        os.environ["SUPPORT_URL"] = self.URL
        main.on_message(_event("愚痴"))
        main.on_message(_event(main.BURN_WORD))
        t = _last_text()
        self.assertTrue(t.startswith(main.BURN_REPLY))
        self.assertIn(self.URL, t)
        self.assertIn("無料", t)
        self.assertIsNone(SENT[-1].messages[0].quick_reply)
        self.assertEqual(len(SENT[-1].messages), 1)
        self.assertNotIn("U1", main.sessions)

    def test_support_link_not_in_normal_reply(self):
        os.environ["SUPPORT_URL"] = self.URL
        main.on_message(_event("愚痴"))
        self.assertNotIn(self.URL, _last_text())

    def test_no_support_link_after_safety_reply(self):
        os.environ["SUPPORT_URL"] = self.URL
        CLAUDE_MODE["answer"] = "189に相談できるよ"
        main.on_message(_event("しんどい"))
        main.on_message(_event(main.BURN_WORD))
        self.assertNotIn(self.URL, _last_text())

    def test_no_support_link_after_safety_words_even_if_api_fails(self):
        os.environ["SUPPORT_URL"] = self.URL
        CLAUDE_MODE["fail"] = True
        main.on_message(_event("死にたい"))
        self.assertIs(main.sessions["U1"].get("safety"), True)
        main.on_message(_event(main.BURN_WORD))
        self.assertNotIn(self.URL, _last_text())

    def test_no_support_link_after_hiragana_or_related_words(self):
        os.environ["SUPPORT_URL"] = self.URL
        CLAUDE_MODE["fail"] = True
        for word in ["しにたい", "もう限界"]:
            main.on_message(_event(word))
            self.assertIs(main.sessions["U1"].get("safety"), True, word)
            main.on_message(_event(main.BURN_WORD))
            self.assertNotIn(self.URL, _last_text(), word)

    def test_no_support_link_without_conversation(self):
        os.environ["SUPPORT_URL"] = self.URL
        main.on_message(_event(main.BURN_WORD))
        self.assertEqual(_last_text(), main.BURN_REPLY)

    def test_support_url_must_be_https(self):
        for bad in ["http://example.com/x", "javascript:alert(1)", "   "]:
            os.environ["SUPPORT_URL"] = bad
            main.on_message(_event("愚痴"))
            main.on_message(_event(main.BURN_WORD))
            self.assertEqual(_last_text(), main.BURN_REPLY, bad)

    def test_safety_flag_stores_no_content(self):
        main.on_message(_event("死にたい"))
        s = main.sessions["U1"]
        self.assertLessEqual(set(s.keys()), {"messages", "updated", "safety"})
        self.assertIs(s["safety"], True)

    def test_support_note_has_no_guilt_words(self):
        for bad in ["必要です", "続けるには", "使い続ける"]:
            self.assertNotIn(bad, main.SUPPORT_NOTE)
        self.assertIn("無料", main.SUPPORT_NOTE)

    # --- 使い方（固定文。Claudeを呼ばず、会話を作らない） ---
    def test_help_returns_fixed_text_without_claude(self):
        main.on_message(_event("使い方"))
        self.assertEqual(_last_text(), main.HELP_TEXT)
        self.assertEqual(CLAUDE_CALLS, [])
        self.assertNotIn("U1", main.sessions)
        self.assertIsNone(SENT[-1].messages[0].quick_reply)

    def test_help_keeps_existing_conversation(self):
        main.on_message(_event("愚痴"))
        updated = main.sessions["U1"]["updated"]
        main.on_message(_event("使い方"))
        self.assertEqual(_last_text(), main.HELP_TEXT)
        self.assertEqual(len(main.sessions["U1"]["messages"]), 2)
        self.assertEqual(main.sessions["U1"]["updated"], updated)
        self.assertEqual(len(CLAUDE_CALLS), 1)
        self.assertIsNotNone(SENT[-1].messages[0].quick_reply)

    def test_help_word_variants(self):
        for word in ["使い方", " 使い方 "]:
            main.on_message(_event(word))
            self.assertEqual(_last_text(), main.HELP_TEXT, word)
        self.assertEqual(CLAUDE_CALLS, [])

    def test_help_katakana_goes_to_claude(self):
        # 「ヘルプ」はSOSとして送られうるので、説明文ではなくClaudeに回す
        main.on_message(_event("ヘルプ"))
        self.assertEqual(len(CLAUDE_CALLS), 1)
        self.assertNotEqual(_last_text(), main.HELP_TEXT)

    def test_help_word_inside_sentence_goes_to_claude(self):
        main.on_message(_event("使い方がわからない親にイライラ"))
        self.assertEqual(len(CLAUDE_CALLS), 1)
        self.assertEqual(_last_text(), "それはしんどいね。")

    def test_help_text_is_accurate(self):
        for must in ["トーク", "30日", "学習", "燃やす", "削除"]:
            self.assertIn(must, main.HELP_TEXT)
        for bad in ["完全に", "どこにも残", "1時間で消", "自動で消"]:
            self.assertNotIn(bad, main.HELP_TEXT)

    def test_help_mentions_support_only_when_set(self):
        main.on_message(_event("使い方"))
        self.assertNotIn("応援", _last_text())
        os.environ["SUPPORT_URL"] = self.URL
        main.on_message(_event("使い方"))
        self.assertIn(main.HELP_SUPPORT_NOTE, _last_text())
        self.assertNotIn(self.URL, _last_text())

    def test_welcome_mentions_help(self):
        self.assertIn("使い方", main.WELCOME)

    # --- 送信取消（燃やすと同じく会話を消す。返事はしない） ---
    def test_unsend_handler_is_registered(self):
        self.assertIs(main.handler.handlers.get("UnsendEvent"), main.on_unsend)

    def test_unsend_erases_conversation_without_reply(self):
        main.on_message(_event("愚痴"))
        sent, calls = len(SENT), len(CLAUDE_CALLS)
        main.on_unsend(_unsend())
        self.assertNotIn("U1", main.sessions)
        self.assertEqual((len(SENT), len(CLAUDE_CALLS)), (sent, calls))

    def test_unsend_only_erases_that_user(self):
        main.on_message(_event("1つ目", "A"))
        main.on_message(_event("2つ目", "B"))
        main.on_unsend(_unsend("A"))
        self.assertNotIn("A", main.sessions)
        self.assertEqual(len(main.sessions["B"]["messages"]), 2)

    def test_unsend_without_conversation_is_noop(self):
        main.on_unsend(_unsend())
        main.on_message(_event("愚痴"))
        main.on_message(_event(main.BURN_WORD))
        sent = len(SENT)
        main.on_unsend(_unsend())
        self.assertEqual(len(SENT), sent)
        self.assertNotIn("U1", main.sessions)

    def test_unsend_without_user_id_is_noop(self):
        main.on_message(_event("愚痴"))
        sent = len(SENT)
        main.on_unsend(_unsend(None))
        self.assertEqual(len(main.sessions["U1"]["messages"]), 2)
        self.assertEqual(len(SENT), sent)

    # --- 返事待ちの間に会話が消えても落ちない・復活しない ---
    def test_erased_while_waiting_does_not_crash_or_restore(self):
        CLAUDE_MODE["during"] = lambda: main.sessions.pop("U1", None)
        main.on_message(_event("愚痴"))
        self.assertEqual(len(SENT), 1)
        self.assertEqual(_last_text(), "それはしんどいね。")
        self.assertNotIn("U1", main.sessions)

    def test_safety_answer_still_sent_after_erase(self):
        CLAUDE_MODE["answer"] = "189に相談できるよ"
        CLAUDE_MODE["during"] = lambda: main.sessions.pop("U1", None)
        main.on_message(_event("しんどい"))
        self.assertIn("189", _last_text())
        self.assertNotIn("U1", main.sessions)

    def test_new_conversation_during_wait_is_not_mixed(self):
        def restart():
            main.sessions["U1"] = {"messages": [], "updated": time.time()}
        CLAUDE_MODE["during"] = restart
        main.on_message(_event("愚痴"))
        self.assertEqual(main.sessions["U1"]["messages"], [])

    # --- 放置会話の一括削除とロック ---
    def test_idle_sessions_of_others_are_swept(self):
        main.on_message(_event("愚痴", "A"))
        main.sessions["A"]["updated"] -= main.IDLE_SECONDS + 1
        main.on_message(_event("聞いて", "B"))
        self.assertNotIn("A", main.sessions)
        self.assertEqual(len(main.sessions["B"]["messages"]), 2)

    def test_recent_sessions_are_not_swept(self):
        main.on_message(_event("愚痴", "A"))
        main.sessions["A"]["updated"] -= main.IDLE_SECONDS - 60
        main.on_message(_event("聞いて", "B"))
        self.assertEqual(len(main.sessions["A"]["messages"]), 2)

    def test_lock_not_held_while_waiting_for_claude(self):
        got = []

        def try_lock():
            if main.sessions_lock.acquire(blocking=False):
                main.sessions_lock.release()
                got.append(True)
        CLAUDE_MODE["during"] = try_lock
        main.on_message(_event("愚痴"))
        self.assertEqual(got, [True])
        self.assertEqual(len(SENT), 1)

    def test_lock_released_after_every_path(self):
        main.on_message(_event("愚痴"))
        self.assertFalse(main.sessions_lock.locked())
        CLAUDE_MODE["fail"] = True
        main.on_message(_event("聞いて"))
        self.assertFalse(main.sessions_lock.locked())
        for ev in [_event("使い方"), _event(main.BURN_WORD)]:
            main.on_message(ev)
            self.assertFalse(main.sessions_lock.locked())
        main.on_unsend(_unsend())
        self.assertFalse(main.sessions_lock.locked())

    def test_api_failure_removes_only_own_message(self):
        CLAUDE_MODE["fail"] = True
        CLAUDE_MODE["during"] = lambda: main.sessions["U1"]["messages"].append(
            {"role": "user", "content": "別の発言"})
        main.on_message(_event("聞いて"))
        self.assertEqual(main.sessions["U1"]["messages"], [{"role": "user", "content": "別の発言"}])

    # --- AI の返事が空なら、API 失敗と同じくおわびを返す ---
    def test_empty_answer_sends_apology_and_keeps_history_clean(self):
        CLAUDE_MODE["answer"] = ""
        main.on_message(_event("聞いて"))
        self.assertEqual(len(SENT), 1)
        self.assertIn("もう一度", _last_text())
        self.assertIsNotNone(SENT[-1].messages[0].quick_reply)
        self.assertEqual(main.sessions["U1"]["messages"], [])
        self.assertFalse(main.sessions_lock.locked())

    def test_whitespace_answer_is_treated_as_empty(self):
        CLAUDE_MODE["answer"] = " \n　"
        main.on_message(_event("聞いて"))
        self.assertIn("もう一度", _last_text())
        self.assertEqual(main.sessions["U1"]["messages"], [])
        CLAUDE_MODE["answer"] = "それはしんどいね。"
        main.on_message(_event("もう一回"))
        self.assertEqual(_last_text(), "それはしんどいね。")
        self.assertTrue(all(m["content"] for m in main.sessions["U1"]["messages"]))

    def test_empty_answer_keeps_safety_flag(self):
        os.environ["SUPPORT_URL"] = self.URL
        CLAUDE_MODE["answer"] = ""
        main.on_message(_event("死にたい"))
        self.assertIs(main.sessions["U1"]["safety"], True)
        main.on_message(_event(main.BURN_WORD))
        self.assertNotIn(self.URL, _last_text())

    def test_parallel_messages_do_not_crash(self):
        old = time.time() - main.IDLE_SECONDS - 1
        for i in range(30):
            main.sessions[f"old{i}"] = {"messages": [], "updated": old}
        errors = []

        def work(n):
            try:
                for i in range(30):
                    user = f"U{(n + i) % 3}"
                    if i % 7 == 6:
                        main.on_message(_event(main.BURN_WORD, user))
                    elif i % 11 == 10:
                        main.on_unsend(_unsend(user))
                    else:
                        main.on_message(_event(f"愚痴{i}", user))
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            self.assertFalse(t.is_alive(), "デッドロックの疑い")
        self.assertEqual(errors, [])
        self.assertFalse(main.sessions_lock.locked())
        self.assertFalse(any(k.startswith("old") for k in main.sessions))


if __name__ == "__main__":
    unittest.main()
