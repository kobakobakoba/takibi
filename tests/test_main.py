"""
焚き火ボットの自動テスト（外部サービスに接続せずに動く）

実行: python -m unittest discover -s tests -v

LINE SDK と Anthropic SDK はダミーに差し替えるので、
ネットにつながらない環境やライブラリ未インストールの環境でも動く。
会社の自動運営では、このテストが全部通らない変更は本番に出さない。
"""
import contextlib
import io
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
CLAUDE_MODE = {"fail": False, "answer": "それはしんどいね。", "during": None, "reply_fail": False}


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
            if CLAUDE_MODE["reply_fail"]:
                # 送ろうとした文をわざと例外に入れ、それが出力されないことを確かめる
                raise RuntimeError("line error: " + req.messages[0].text)
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


# 2026-10-13 に足した20語と、それぞれを含む自然な文（どの文も変更前の一覧には当たらない）
ADDED_SAFETY_SAMPLES = {
    "死にたく": "もう死にたくなる", "しにたく": "しにたくなる",
    "死んでしまいたい": "死んでしまいたい", "しんでしまいたい": "しんでしまいたい",
    "消えてしまいたい": "消えてしまいたい", "きえてしまいたい": "きえてしまいたい",
    "消えたくな": "消えたくなる", "消えたくて": "消えたくて眠れない",
    "きえたくな": "きえたくなる", "きえたくて": "きえたくて",
    "いなくなってしまいたい": "いなくなってしまいたい", "生きていくのが嫌": "生きていくのが嫌になった",
    "生きている意味がない": "生きている意味がない", "必要とされていない": "誰からも必要とされていない",
    "共に逝": "母と共に逝きたい", "一緒に逝": "一緒に逝きたい", "一緒に死": "母と一緒に死ぬ",
    "無理心中": "無理心中が頭をよぎる", "死んでしまえ": "早く死んでしまえと言った",
    "たたいてしまい": "子どもをたたいてしまいそう",
}
# 普通の愚痴（どれも一覧に当たってはいけない）
ORDINARY_COMPLAINTS = ["疲れた", "とにかく疲れた", "もう無理", "どうでもいい", "自分なんて",
                       "死ぬほど眠い", "痛みが消えたくらいで", "心中お察しします",
                       "自分で自分の首を締めてる", "家事が少しでも楽になりたい",
                       "金魚が死んじゃった", "金魚が死んじゃいました", "お腹すいて死んじゃう", "保存したデータが消えちゃいました",
                       "電気が消えちゃいそう", "眠くて死にそう", "夫が全然手伝わない", "手をたたいて喜んだ",
                       "ワンオペで疲れた", "ストレスがたまる", "ケアマネに電話した", "ミルクを作った", "ｵﾑﾂ替えが終わらない",
                       "肩をたたいてあげた", "叩かれた夢を見た"]
# 2026-10-14 に足した12語と、それぞれを含む自然な文（どの文も変更前の一覧（38語）には当たらない）
ADDED_SAFETY_SAMPLES_2 = {
    "叩いてしま": "つい子どもを叩いてしまった", "たたいてしま": "またたたいてしまう",
    "叩きたく": "子どもを叩きたくなる", "たたきたく": "たたきたくなる",
    "消えてなくなりたい": "消えてなくなりたい", "きえてなくなりたい": "きえてなくなりたい",
    "死んじゃいた": "もう死んじゃいたくなる", "しんじゃいた": "しんじゃいたい",
    "消えちゃいた": "消えちゃいたくなる", "きえちゃいた": "きえちゃいたい",
    "叩いちゃい": "子どもを叩いちゃいそう", "たたいちゃい": "たたいちゃいました",
}
# 2026-10-15：カタカナ・半角カナで書かれた危ない言葉（どの文も、そろえる前の一覧には当たらない）
KANA_SAFETY_SAMPLES = ["シニタイ", "ｼﾆﾀｲ", "死ニタイ", "キエタクナル", "タタイテシマッタ", "ｼﾝｼﾞｬｲﾀｲ"]
# 2026-10-16：叩いてしまった後のくだけた形（どの文も、変更前の50語には、そろえた後も当たらない）
ADDED_SAFETY_SAMPLES_3 = {"叩いちゃ": "なんで今日叩いちゃったんだろう", "たたいちゃ": "つい子どもをたたいちゃった"}
TATAICHA_SAMPLES = ["なんで今日叩いちゃったんだろう", "叩いちゃって自己嫌悪", "つい子どもをたたいちゃった",
                    "タタイチャッタ", "ﾀﾀｲﾁｬｯﾀ"]
# 受け入れる誤検知（安全審査部 2026-10-15 の条件）。当たってよい＝ORDINARY_COMPLAINTS に入れない
ACCEPTED_FALSE_POSITIVES_3 = ["夫に軽口を叩いちゃった"]


class TakibiTest(unittest.TestCase):
    def setUp(self):
        SENT.clear()
        CLAUDE_CALLS.clear()
        CLAUDE_MODE.update(fail=False, answer="それはしんどいね。", during=None, reply_fail=False)
        main.sessions.clear()
        main.inflight.clear()
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

    def test_persona_does_not_push_or_dismiss_repeats(self):
        # 励まし・発破をかけない／くり返しを軽く扱わない（PERSONA に足し、SAFETY は触らない）
        for phrase in ["「がんばって」", "励まし", "「また？」", "軽く扱わず", "しんどさが増して", "心配していると伝える"]:
            self.assertIn(phrase, main.PERSONA)
            self.assertNotIn(phrase, main.SAFETY)
        self.assertNotIn("はじめて聞くように", main.PERSONA)
        self.assertEqual(main.SYSTEM_PROMPT, main.PERSONA + "\n" + main.SAFETY)

    def test_persona_mentions_overlapping_care(self):
        # ダブルケア・複数の家族の世話（PERSONA の地の文に足し、SAFETY は触らない）
        for phrase in ["育児と介護が重なっている", "何人もの家族", "決めつけず", "重なったしんどさ"]:
            self.assertIn(phrase, main.PERSONA)
            self.assertNotIn(phrase, main.SAFETY)
        self.assertIn("相手は、子どもや親・家族の世話をしている人です。\n育児と介護", main.PERSONA)
        self.assertEqual(main.SYSTEM_PROMPT, main.PERSONA + "\n" + main.SAFETY)

    def test_persona_meets_self_blame_without_excusing_acts(self):
        # 自分を責める言葉への応じ方（PERSONA のふるまいに足し、SAFETY は触らない）
        for phrase in ["自分を責める言葉", "言い負かして", "疲れていることを受け止める",
                       "「仕方ない」「誰でもある」と軽くはしない", "責め立てもしない"]:
            self.assertIn(phrase, main.PERSONA)
            self.assertNotIn(phrase, main.SAFETY)
        for phrase in ["あなたは悪くない", "謝", "安全の指示に従う"]:
            self.assertNotIn(phrase, main.PERSONA)
        self.assertIn("本人の疲れに寄り添う。\n- 自分を責める言葉", main.PERSONA)
        self.assertEqual(main.SYSTEM_PROMPT, main.PERSONA + "\n" + main.SAFETY)

    def test_persona_describes_carers_without_daily_or_praise(self):
        # 39行：「毎日」「がんばっている」で決めつけず、世話という行為で書く（SAFETY は触らない）
        self.assertIn("\n相手は、子どもや親・家族の世話をしている人です。\n", main.PERSONA)
        for phrase in ["毎日がんばっている", "世話を毎日", "がんばっている人"]:
            self.assertNotIn(phrase, main.PERSONA)
        for phrase in ["ケアラー", "介護者"]:
            self.assertNotIn(phrase, main.PERSONA)
        self.assertEqual(main.SYSTEM_PROMPT, main.PERSONA + "\n" + main.SAFETY)

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

    def test_welcome_and_burn_reply_do_not_overpromise(self):
        banned = ["完全に", "どこにも残", "1時間で消", "自動で消",
                  "もう残っていません", "サーバーからぜんぶ"]
        for text in (main.WELCOME, main.BURN_REPLY):
            self.assertIn("焚き火のサーバー", text)
            self.assertEqual(text.count("サーバー"), text.count("焚き火のサーバー"))
            for word in banned:
                self.assertNotIn(word, text)

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
        CLAUDE_MODE["answer"] = "それはしんどいね。"
        main.on_message(_event("聞いて"))  # 会話にユーザー発言が残る状態にする（フラグがないと URL が出る）
        main.on_message(_event(main.BURN_WORD))
        self.assertNotIn(self.URL, _last_text())

    # --- おわびに 110番・119番の一文を添える（危ない言葉があった会話だけ） ---
    def test_api_failure_after_safety_words_adds_emergency_line(self):
        CLAUDE_MODE["fail"] = True
        main.on_message(_event("死にたい"))
        t = _last_text()
        self.assertEqual(len(SENT), 1)
        self.assertIn("もう一度", t)
        self.assertEqual(t.count(main.APOLOGY_SAFETY_LINE), 1)
        self.assertTrue(t.endswith("\n\n" + main.APOLOGY_SAFETY_LINE))
        self.assertNotIn("189", t)
        self.assertNotIn("まもろうよ", t)

    def test_empty_or_blank_answer_after_safety_words_adds_emergency_line(self):
        for ans in ["", " \n　"]:
            SENT.clear()
            main.sessions.clear()
            main.inflight.clear()
            CLAUDE_MODE["answer"] = ans
            main.on_message(_event("死にたい"))
            self.assertEqual(len(SENT), 1)
            self.assertIn("もう一度", _last_text())
            self.assertEqual(_last_text().count(main.APOLOGY_SAFETY_LINE), 1)

    def test_apology_without_safety_words_has_no_emergency_line(self):
        CLAUDE_MODE["fail"] = True
        main.on_message(_event("聞いて"))
        CLAUDE_MODE.update(fail=False, answer="")
        main.on_message(_event("聞いて", "U2"))
        self.assertEqual(len(SENT), 2)
        for req in SENT:
            self.assertIn("もう一度", req.messages[0].text)
            self.assertNotIn("110", req.messages[0].text)
            self.assertNotIn("119", req.messages[0].text)

    def test_emergency_line_matches_help_text(self):
        self.assertIn(main.APOLOGY_SAFETY_LINE, main.HELP_TEXT)

    def test_apology_with_safety_words_keeps_existing_guarantees(self):
        CLAUDE_MODE["fail"] = True
        main.on_message(_event("死にたい"))
        self.assertEqual(main.sessions["U1"]["messages"], [])
        self.assertIs(main.sessions["U1"]["safety"], True)
        self.assertIsNotNone(SENT[-1].messages[0].quick_reply)
        self.assertFalse(main.sessions_lock.locked())
        self.assertEqual(main.inflight, {})

    def test_apology_after_burn_during_wait_still_adds_emergency_line(self):
        CLAUDE_MODE["fail"] = True
        CLAUDE_MODE["during"] = lambda: main.sessions.pop("U1", None)
        main.on_message(_event("死にたい"))
        self.assertEqual(len(SENT), 1)
        self.assertEqual(_last_text().count(main.APOLOGY_SAFETY_LINE), 1)
        self.assertNotIn("U1", main.sessions)
        self.assertFalse(main.sessions_lock.locked())

    def test_apology_never_has_support_link(self):
        os.environ["SUPPORT_URL"] = self.URL
        CLAUDE_MODE["fail"] = True
        main.on_message(_event("死にたい"))
        main.on_message(_event("聞いて", "U2"))
        self.assertEqual(len(SENT), 2)
        for req in SENT:
            self.assertNotIn(self.URL, req.messages[0].text)
            self.assertNotIn("応援", req.messages[0].text)

    def test_apology_in_flagged_conversation_adds_emergency_line(self):
        CLAUDE_MODE["answer"] = "189に相談できるよ"
        main.on_message(_event("しんどい"))
        self.assertIs(main.sessions["U1"]["safety"], True)
        CLAUDE_MODE["fail"] = True
        main.on_message(_event("聞いて"))
        self.assertEqual(_last_text().count(main.APOLOGY_SAFETY_LINE), 1)

    # --- 危ない言葉の一覧（2026-10-13 追加分）。普通の愚痴には当たらない ---
    def _send_fresh(self, text):
        SENT.clear()
        main.sessions.clear()
        main.inflight.clear()
        CLAUDE_MODE["fail"] = True
        main.on_message(_event(text))

    def test_added_safety_words_are_in_list(self):
        self.assertEqual(len(ADDED_SAFETY_SAMPLES), 20)
        self.assertEqual(len(set(main.SAFETY_USER_WORDS)), len(main.SAFETY_USER_WORDS))
        for word, sentence in ADDED_SAFETY_SAMPLES.items():
            self.assertIn(word, main.SAFETY_USER_WORDS)
            self.assertIn(word, sentence)

    def test_added_safety_words_set_flag_and_emergency_line(self):
        for word, sentence in ADDED_SAFETY_SAMPLES.items():
            with self.subTest(word=word):
                self._send_fresh(sentence)
                self.assertIs(main.sessions["U1"].get("safety"), True)
                self.assertEqual(len(SENT), 1)
                self.assertEqual(_last_text().count(main.APOLOGY_SAFETY_LINE), 1)

    def test_ordinary_complaints_do_not_match_safety_words(self):
        for sentence in ORDINARY_COMPLAINTS:
            with self.subTest(sentence=sentence):
                self.assertFalse(main.has_safety_words(sentence))
                self._send_fresh(sentence)
                self.assertFalse(main.sessions["U1"].get("safety"))
                self.assertNotIn("110", _last_text())
                self.assertNotIn("119", _last_text())

    def test_existing_safety_words_are_kept(self):
        old = ["死にたい", "消えたい", "死のう", "自殺", "自分を傷つけ", "叩いてしまい", "手が出", "殺し",
               "しにたい", "きえたい", "しのう", "死んだほうが", "いなくなりたい",
               "傷つけ", "虐待", "限界", "首を絞め", "殴"]
        for word in old:
            self.assertIn(word, main.SAFETY_USER_WORDS)
        self.assertEqual(main.SAFETY_USER_WORDS[:18], old)

    # --- 危ない言葉の一覧（2026-10-14 追加分） ---
    def test_added_safety_words_2_are_in_list(self):
        self.assertEqual(len(ADDED_SAFETY_SAMPLES_2), 12)
        for word, sentence in ADDED_SAFETY_SAMPLES_2.items():
            self.assertIn(word, main.SAFETY_USER_WORDS)
            self.assertIn(word, sentence)
        self.assertEqual(len(set(main.SAFETY_USER_WORDS)), len(main.SAFETY_USER_WORDS))
        self.assertEqual(main.SAFETY_USER_WORDS[18:38], list(ADDED_SAFETY_SAMPLES))

    def test_added_safety_words_2_set_flag_and_emergency_line(self):
        for word, sentence in ADDED_SAFETY_SAMPLES_2.items():
            with self.subTest(word=word):
                self._send_fresh(sentence)
                self.assertIs(main.sessions["U1"].get("safety"), True)
                self.assertEqual(len(SENT), 1)
                self.assertEqual(_last_text().count(main.APOLOGY_SAFETY_LINE), 1)

    # --- 照合の前に文字をそろえる（2026-10-15）。そろえた文は判定にだけ使う ---
    def test_fold_kana_basics(self):
        self.assertEqual(main.fold_kana("シニタイ"), "しにたい")
        self.assertEqual(main.fold_kana("ｼﾝｼﾞｬｲﾀｲ"), "しんじゃいたい")
        self.assertEqual(main.fold_kana("死ニタイ"), "死にたい")
        self.assertEqual(main.fold_kana("シニターイ"), "しにたーい")
        self.assertEqual(main.fold_kana("シ ニ"), "し に")
        self.assertEqual(main.fold_kana("ヴ・ヶ"), "ゔ・ゖ")
        self.assertEqual(main.fold_kana("夫が手伝わない"), "夫が手伝わない")

    def test_safety_words_are_unchanged_by_folding(self):
        for w in main.SAFETY_USER_WORDS:
            self.assertEqual(main.fold_kana(w), w)

    def test_combining_voiced_mark_does_not_hide_words_matched_before(self):
        # 結合濁点（U+3099・半角 U+FF9E）は NFKC で手前の字とくっつくが、変更前に拾えた文は今も拾う
        for s in ["死のゔ", "殺しﾞ", "しにたぐなる"]:
            with self.subTest(text=s):
                self.assertTrue(any(w in s for w in main.SAFETY_USER_WORDS))
                self.assertTrue(main.has_safety_words(s))

    def test_kana_safety_samples_set_flag_and_emergency_line(self):
        for s in KANA_SAFETY_SAMPLES:
            with self.subTest(text=s):
                self.assertFalse(any(w in s for w in main.SAFETY_USER_WORDS))
                self._send_fresh(s)
                self.assertIs(main.sessions["U1"].get("safety"), True)
                self.assertEqual(len(SENT), 1)
                self.assertEqual(_last_text().count(main.APOLOGY_SAFETY_LINE), 1)

    def test_kana_safety_words_on_apology_path_after_burn_during_wait(self):
        CLAUDE_MODE["fail"] = True
        CLAUDE_MODE["during"] = lambda: main.sessions.pop("U1", None)
        main.on_message(_event("ｼﾆﾀｲ"))
        self.assertEqual(len(SENT), 1)
        self.assertEqual(_last_text().count(main.APOLOGY_SAFETY_LINE), 1)
        self.assertNotIn("U1", main.sessions)
        self.assertFalse(main.sessions_lock.locked())

    def test_folded_text_is_not_sent_or_stored(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            main.on_message(_event("ｼﾆﾀｲ"))
        self.assertEqual(CLAUDE_CALLS[-1]["messages"][-1]["content"], "ｼﾆﾀｲ")
        self.assertEqual(main.sessions["U1"]["messages"][0]["content"], "ｼﾆﾀｲ")
        self.assertIs(main.sessions["U1"].get("safety"), True)
        self.assertEqual(out.getvalue() + err.getvalue(), "")

    # --- 危ない言葉の一覧（2026-10-16 追加分：叩いちゃ・たたいちゃ） ---
    def test_added_safety_words_3_are_in_list(self):
        self.assertEqual(len(main.SAFETY_USER_WORDS), 52)
        self.assertEqual(len(set(main.SAFETY_USER_WORDS)), len(main.SAFETY_USER_WORDS))
        self.assertEqual(main.SAFETY_USER_WORDS[38:50], list(ADDED_SAFETY_SAMPLES_2))
        self.assertEqual(main.SAFETY_USER_WORDS[50:], list(ADDED_SAFETY_SAMPLES_3))
        for word, sentence in ADDED_SAFETY_SAMPLES_3.items():
            self.assertIn(word, sentence)

    def test_tataicha_samples_set_flag_and_emergency_line(self):
        old = main.SAFETY_USER_WORDS[:50]
        for s in TATAICHA_SAMPLES:
            with self.subTest(text=s):
                self.assertFalse(any(w in s or w in main.fold_kana(s) for w in old))
                self._send_fresh(s)
                self.assertIs(main.sessions["U1"].get("safety"), True)
                self.assertEqual(len(SENT), 1)
                self.assertEqual(_last_text().count(main.APOLOGY_SAFETY_LINE), 1)

    def test_accepted_false_positive_karukuchi(self):
        for s in ACCEPTED_FALSE_POSITIVES_3:
            with self.subTest(text=s):
                self.assertTrue(main.has_safety_words(s))
                self.assertNotIn(s, ORDINARY_COMPLAINTS)

    # --- 返事待ちの間に燃やしても応援リンクを出さない（会話の中身は覚えず、件数だけ見る） ---
    def test_no_support_link_when_burned_while_older_call_waits(self):
        os.environ["SUPPORT_URL"] = self.URL
        burned = []

        def second_and_burn():
            CLAUDE_MODE["during"] = None
            main.on_message(_event("2つ目"))
            main.on_message(_event(main.BURN_WORD))
            burned.append(_last_text())
        CLAUDE_MODE["during"] = second_and_burn
        main.on_message(_event("1つ目"))
        self.assertTrue(burned[0].startswith(main.BURN_REPLY))
        self.assertNotIn(self.URL, burned[0])
        self.assertNotIn("U1", main.inflight)

    def test_inflight_key_removed_after_every_path(self):
        for mode in [{}, {"fail": True}, {"answer": ""}, {"reply_fail": True}]:
            CLAUDE_MODE.update({"fail": False, "answer": "それはしんどいね。", "reply_fail": False, **mode})
            main.on_message(_event("聞いて"))
            self.assertEqual(main.inflight, {}, mode)
            self.assertFalse(main.sessions_lock.locked(), mode)

    def test_safety_reply_flags_new_conversation_started_during_wait(self):
        os.environ["SUPPORT_URL"] = self.URL
        CLAUDE_MODE["answer"] = "189に相談できるよ"
        new_msg = {"role": "user", "content": "新しい話"}

        def restart():
            main.sessions["U1"] = {"messages": [new_msg], "updated": time.time()}
        CLAUDE_MODE["during"] = restart
        main.on_message(_event("しんどい"))
        self.assertIs(main.sessions["U1"]["safety"], True)
        self.assertEqual(main.sessions["U1"]["messages"], [new_msg])
        CLAUDE_MODE["during"] = None
        main.on_message(_event(main.BURN_WORD))
        self.assertNotIn(self.URL, _last_text())

    # --- LINE への返信が失敗しても落ちない・届かなかった1往復を残さない ---
    def test_reply_failure_does_not_raise_and_drops_unseen_turn(self):
        main.on_message(_event("1つ目"))
        CLAUDE_MODE["reply_fail"] = True
        main.on_message(_event("2つ目"))
        self.assertEqual(main.sessions["U1"]["messages"], [
            {"role": "user", "content": "1つ目"},
            {"role": "assistant", "content": "それはしんどいね。"}])
        self.assertFalse(main.sessions_lock.locked())

    def test_reply_failure_on_fixed_replies_does_not_raise(self):
        CLAUDE_MODE["reply_fail"] = True
        main.on_follow(_event(""))
        main.on_message(_event("使い方"))
        CLAUDE_MODE["fail"] = True
        main.on_message(_event("聞いて"))
        main.on_message(_event(main.BURN_WORD))
        self.assertNotIn("U1", main.sessions)
        self.assertEqual(SENT, [])
        self.assertFalse(main.sessions_lock.locked())

    def test_reply_failure_keeps_safety_flag(self):
        os.environ["SUPPORT_URL"] = self.URL
        CLAUDE_MODE.update(answer="189に相談できるよ", reply_fail=True)
        main.on_message(_event("死にたい"))
        self.assertIs(main.sessions["U1"]["safety"], True)
        CLAUDE_MODE["reply_fail"] = False
        main.on_message(_event(main.BURN_WORD))
        self.assertNotIn(self.URL, _last_text())

    def test_reply_failure_outputs_no_content(self):
        CLAUDE_MODE["reply_fail"] = True
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            main.on_message(_event("夫が手伝わない"))
        logged = out.getvalue() + err.getvalue()
        for secret in ["夫が手伝わない", "それはしんどいね", "tok", "U1"]:
            self.assertNotIn(secret, logged)
        self.assertIn("reply failed", logged)

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
        self.assertEqual(main.inflight, {})


if __name__ == "__main__":
    unittest.main()
