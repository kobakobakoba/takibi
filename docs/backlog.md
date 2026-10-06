# バックログ

企画部（product-planner）が毎日更新する。

## 今日の改善（2026-10-06）

### 送信取消（UnsendEvent）が届いたら、その人の会話を消す（返事待ち中に消えても落ちないようにする）

根拠：`docs/research/2026-10-06.md`（調査部の示唆1・2）。分類：お客さまとの約束を守る仕組み（約束1「燃やしたら消える」・約束2「保存しない」を、送信取消にも広げる）。

#### なぜこれを選んだか（ほかの候補との比較）

- 利用者がLINEで「送信取消」をしても、今の焚き火はイベントを受けていないため、取り消した文がサーバーのメモリに残り、次の返事を作るときにAIへ送られ続ける。LINE Developersも、取消を受けたら利用者の意図を尊重して消すよう推奨している。取消は「やっぱり書かなきゃよかった」という気持ちの表れで、このサービスの約束に直結する。
- 調査で実装方法が確定した（`UnsendEvent`、返信用トークンなし、`source.user_id` は `None` もありうる）。保存は増えない（`messageId` も持たない）。
- 消す経路が増えると、既知の問題（返事待ちの間に会話が消えると `sessions[user_id]` で KeyError、旧候補4）に当たりやすくなる。本番は `gunicorn --workers 1 --threads 4`（`README.md`）なので、同時実行は実際に起きうる。そこで **KeyError を防ぐ最小限の守りを今日の改善に含める**（ロックなどの本格対応は候補2に残す）。両方でテスト込み約90行に収まる見込み。
- SAFETYの相談窓口の表現改善（0120-189-783の併記など）はSAFETYブロックの変更なので、今日の改善にしない。オーナーへの承認のお願いとして `docs/owner_todo.md` に回した。

#### 目的（お客さまにとって何が良くなるか）

- 送信取消をした人の会話が、焚き火のサーバーからその場で消える（取り消した1通だけでなく、その人の会話まるごと。燃やすと同じ）。取り消した内容が、その後の返事のためにAIへ送られ続けることがなくなる。
- 取消のあとに、ボットから何か届いて驚かされることがない（返事は送らない）。
- 返事を待っている間に「燃やす」や送信取消をしても、ボットがエラーで黙らず、消した会話が復活もしない。

#### 変更内容（どのファイルの何を変えるか）

`main.py`（約20行）：

1. import を変更：`from linebot.v3.webhooks import FollowEvent, MessageEvent, TextMessageContent, UnsendEvent`
2. `on_follow` の下に新しいハンドラを追加：
   ```python
   @handler.add(UnsendEvent)
   def on_unsend(event: UnsendEvent):
       # 送信取消：その人の会話をまるごと消す（燃やすと同じ）。返信用トークンがないので返事はしない
       user_id = getattr(event.source, "user_id", None)
       if user_id:
           sessions.pop(user_id, None)
   ```
   - `reply` を呼ばない。プッシュメッセージも送らない。
   - `event.unsend.message_id` は使わない・保存しない。`print`/`logging` を足さない。
3. `on_message` の Claude 呼び出しのあと（`if any(m in answer for m in SAFETY_REPLY_MARKERS):` から `sessions[user_id]["updated"] = time.time()` まで）を、次の形に置き換える：
   ```python
   s = sessions.get(user_id)
   if s is not None and s["messages"] is history:
       # 返事を待つ間に「燃やす」や送信取消で消されていたら、記録し直さない
       if any(m in answer for m in SAFETY_REPLY_MARKERS):
           s["safety"] = True
       history.append({"role": "assistant", "content": answer})
       s["updated"] = time.time()
   reply(event.reply_token, answer)
   ```
   - `is history` で比べるのは、待っている間に消され、さらに同じ人の新しい会話が始まっていた場合に、古い返事を新しい会話へ混ぜないため。
   - **返事（`reply`）は消されていても送る。** 理由：その発言への返事であり返信用トークンも有効、また返事に相談先の案内（SAFETY）が入っている場合に黙って捨てると「命と安全が最優先」に反する。返事はLINEのトーク画面に届くだけで、焚き火のサーバーには残らない（`HELP_TEXT` の説明どおり）。
   - Claude 呼び出し前の `sessions[user_id]["safety"] = True`（`get_history` 直後）と、失敗時の `history.pop()` は変えない（前者は `get_history` が直前に作った会話を指し、後者は会話が消えていても KeyError にならない）。
4. `get_history`、燃やす処理、「使い方」の処理、`WELCOME`、`BURN_REPLY`、`HELP_TEXT` は変えない（`HELP_TEXT` に送信取消のことを書くのは、本番で動きを確かめてから。候補3）。

`tests/test_main.py`（約65行）：

- 偽 `linebot.v3.webhooks` のクラス一覧に `"UnsendEvent"` を足す（`["FollowEvent", "MessageEvent", "TextMessageContent", "UnsendEvent"]`）。
- 偽 Claude の `create` に「呼ばれている途中で何かをする」仕掛けを足す：`CLAUDE_MODE` に `"during": None` を足し、`create` の中で `if CLAUDE_MODE["during"]: CLAUDE_MODE["during"]()` を呼ぶ。`setUp` の `CLAUDE_MODE.update(...)` にも `during=None` を足す。
- 補助関数 `_unsend(user="U1")`：`types.SimpleNamespace(source=types.SimpleNamespace(user_id=user), unsend=types.SimpleNamespace(message_id="m1"))`（`reply_token` を持たせない。返事しようとすると `AttributeError` になるので分かる）。

| テスト名 | 確かめること |
| --- | --- |
| `test_unsend_handler_is_registered` | `"UnsendEvent"` が `main.handler.handlers` にあり、`main.on_unsend` を指す |
| `test_unsend_erases_conversation_without_reply` | 「愚痴」のあと `main.on_unsend(_unsend())` → `"U1"` が `sessions` にない。`SENT` の件数と Claude 呼び出し回数が増えていない |
| `test_unsend_only_erases_that_user` | A・B が話したあと A の取消 → A は消え、B の履歴は2件のまま |
| `test_unsend_without_conversation_is_noop` | 会話がない人の取消、燃やしたあとの取消でエラーにならず、何も送らない |
| `test_unsend_without_user_id_is_noop` | `user_id=None` の取消で、ほかの人の会話が消えず、何も送らない |
| `test_erased_while_waiting_does_not_crash_or_restore` | `CLAUDE_MODE["during"]` で `sessions.pop("U1", None)` → `on_message("愚痴")` が例外を出さず、返事（Claudeの答え）は1通送られ、`"U1"` は `sessions` にない |
| `test_safety_answer_still_sent_after_erase` | 答えを「189に相談できるよ」にし、待っている間に消す → 返事に「189」が含まれて送られ、`"U1"` は `sessions` にない |
| `test_new_conversation_during_wait_is_not_mixed` | 待っている間に消され、`sessions["U1"] = {"messages": [], "updated": time.time()}` と新しい会話ができた場合 → 新しい会話の `messages` は空のまま |

既存30本（特に燃やす系、`test_no_files_or_database_used`、`test_safety_rules_are_in_system_prompt`、`test_safety_flag_stores_no_content`、`test_api_failure_sends_apology_and_keeps_history_clean`）はそのまま通ること。

#### 完了条件

- `python -m unittest discover -s tests -v` が全部合格（既存30本＋新規8本＝38本）。
- `git diff main.py` に `SAFETY START`〜`SAFETY END`、`PERSONA` の変更がない。
- `on_unsend` が `reply` もプッシュも呼ばず、`message_id` を保存していない。
- 差分合計がテスト込みでおおむね100行以内（本体約20行、テスト約65行）。超えそうなら、`test_unsend_handler_is_registered` を `test_unsend_erases_conversation_without_reply` に統合して削る（返事待ち中の守りのテストは削らない）。
- `requirements.txt`、モデル、`max_tokens`、起動コマンドは変更なし。
- 安全審査部の承認。

#### リスクと戻し方

- リスク1：本番の `line-bot-sdk`（`>=3.11`）に `UnsendEvent` がないと、起動時の import で落ちる。→ 調査部は v3 のモデル群に含まれると判断（3.11 時点は厳密には未確認）。開発部は、手元に実物のSDKがあれば `python -c "from linebot.v3.webhooks import UnsendEvent"` で確かめ、確かめられなければ日報に「未確認」と書く。Renderの起動失敗は会社からは見えないため、オーナーに「デプロイ後に一度話しかけて返事が来るか」を確認してもらうよう日報に書く。
- リスク2：取消1通で会話がまるごと消え、「続きを話したかったのに」と感じる人がいる。→ 約束（消す側に倒す）を優先。返事は送らないので、次に話しかけると新しい会話として続けられる。
- リスク3：返事待ち中に消された場合、AIの返事は届くが、その返事は会話に記録されない（次の返事には前の流れが入らない）。→ 利用者が消すことを選んだ結果として妥当。相談先の案内は届く。
- リスク4：返事待ち中に取消された場合、取り消した発言への返事が届く（LINEのトーク画面に残る）。→ 安全の案内を捨てないことを優先した。安全審査部が「取消時は返事を送らない」方がよいと判断すれば、`reply` を `if s is not None and s["messages"] is history` の内側に入れる案に切り替えてよい（ただしその場合も、答えに `SAFETY_REPLY_MARKERS` を含むときは送ること）。
- 戻し方：該当コミットを `git revert`。データの移行や環境変数はない。

#### 触らないもの

`SAFETY` ブロック、`PERSONA`、`SYSTEM_PROMPT` の組み立て、モデル、`max_tokens`、`requirements.txt`、起動コマンド（ワーカー数1）、`get_history`、燃やす処理と `BURN_REPLY`、応援リンクの条件、`SAFETY_USER_WORDS`／`SAFETY_REPLY_MARKERS` の中身、`HELP_TEXT`／`WELCOME` の文言。

## 今日の改善（2026-10-05）（完了）

「使い方」と送ると、消えるもの・残るもの・自分で消す方法を固定文で説明する。本番反映済み。仕様の詳細は git の履歴（2026-10-05 のコミット）と `reports/2026-10-05.md` を参照。

申し送り：「ヘルプ」はSOSとして送られうるため反応語にしない（安全審査部の差し戻し）。文言のルール（「完全に消える」「どこにも残らない」「1時間で消える」「自動で消える」と書かない、未確認事項は書かない）は今後の文言変更でも守る。

## 今日の改善（2026-10-04）（完了）

応援リンク（`SUPPORT_URL` 設定時のみ、燃やしたあとに一言。安全フラグが立った会話では出さない）。本番反映済み。仕様の詳細は git の履歴（2026-10-04 のコミット）と `reports/2026-10-04.md` を参照。

申し送り：1日1回までの回数制限は「燃やしたら消える」と緊張するため見送り（候補4）。

## 候補（優先度順）

- **応援リンクを有効にする前に必ず直す（安全審査部の申し送り 2026-10-06）**：相談先の案内を含む返事を待つ間に「燃やす」か送信取消をして、すぐ新しい会話を始めると、新しい会話に安全フラグが立たず、そのあと燃やすと応援リンクが出ることがある（`SUPPORT_URL` 未設定なら影響なし）。直し方の例：返事に相談先の目印があり、その時点で `sessions.get(user_id)` があれば、別の会話でも `safety=True` を立てる。

1. **放置会話の即時削除**（約束を守る仕組み）：今は1時間放置しても、その人が次に話しかけるまでメモリに残る。メッセージを受けるたびに、全員分の「1時間以上たった会話」をまとめて消す（スレッドやタイマーは使わない）。完全に時間どおりではないので、それでも説明文に「1時間で消える」とは書かない。全員分を回す処理は、他スレッドが同時に `sessions` を変えると `RuntimeError` になりうるので、`list(sessions.items())` で写してから回すこと（候補2とも関係）。
2. **同時アクセスの安全性（残り）**（安全性）：2026-10-06 の改善で、返事待ち中に会話が消えたときの KeyError は防ぐ。残りは、本番が `--threads 4` であることを踏まえ、`sessions` の読み書きを `threading.Lock` でまとめて守るか検討する（同じ人の連投が同時に処理されたときの履歴の乱れなど）。候補1と一緒にやるのが自然。
3. **文言の見直し（あいさつ・燃やした後・使い方）**（広報部の指摘＋送信取消）：`WELCOME` の「サーバーからぜんぶ消えます」は `HELP_TEXT` に比べて言い過ぎに聞こえるので、「焚き火のサーバーから消えます」など言い方をそろえる。あわせて、送信取消の対応が本番で動くことをオーナーに確認してもらえたら、`HELP_TEXT` の【消えるもの】に「メッセージを送信取消すると、焚き火のサーバーにある会話がまるごと消えます（取消できる時間はLINEの仕様によります）」を足す。文言だけの小さな変更。
4. **応援リンクの出しすぎ防止**：1日1回までなどの上限。燃やしたあとに何も残さない方法（例：上限ではなく「ユーザーの発言が3つ以上あった会話のあとだけ」など、記録不要の条件）を先に検討し、記録が要る案は安全審査部の判断を仰ぐ。
5. **1日の利用回数の上限**：環境変数 `DAILY_LIMIT` が設定されていれば、1人1日その回数まで。上限に達したら優しく伝える。コスト対策と、有料プランの土台。（収益化 第2段階の準備。回数の記録が要るため、候補4と同じ論点あり）
6. **説明文の定期見直し**：`HELP_TEXT` の根拠（Anthropicの保持期間・学習利用、LINEの仕様）を調査部が月1回ほど確認し、変わっていれば文言を直す。見直し対象に「メッセージ編集」を加える（2026-10-06 の調査では、編集イベントは1対1トークには届かないため、焚き火には編集前の文が残る。今の `HELP_TEXT` は編集に触れていないので食い違いはない。LINEの仕様が変わって1対1でも届くようになったら、送信取消と同じく会話を消す対応を検討する）。
7. **長文の分割**：LINEの文字数上限を超える返事を安全に切る。
8. **育児／介護のモード分け**：最初のあいさつで「育児」「介護」のボタンを出し、選んだほうに合わせて受け止め方を少し変える（例：介護なら終わりの見えなさ、育児なら睡眠不足への共感）。
9. **夜間モード**：深夜（日本時間0〜5時）は、より短く静かな返事にする。寝かしつけや夜間介護中の人向け。

## オーナーの承認待ち（会社では作らない）

- **SAFETYブロックの相談窓口の書き方**（`docs/research/2026-10-06.md`）：誤った窓口は見つかっていないが、(1) 189 は「児童相談所虐待対応ダイヤル」なので、子育ての悩みの相談専用ダイヤル 0120-189-783 を併記する、(2) 「親子のための相談LINE」は24時間ではなく地域によって別窓口なので、夜間は 189（24時間）を先に案内する、の2点を提案中。SAFETYブロックの変更はオーナーの明示的な承認が必要なため、`docs/owner_todo.md` に記載。承認があった日に、文言案を安全審査部と詰めて実装する。

## 完了

- 送信取消で会話を消す＋返事待ち中に消えても落ちない守り（2026-10-06、予定。安全審査の承認後に本番反映）
- 「使い方」と送ると、消えるもの・残るもの・自分で消す方法を説明する固定文（2026-10-05）
- 応援リンク（`SUPPORT_URL` 設定時のみ、燃やしたあとに一言。安全フラグが立った会話では出さない）（2026-10-04）
- 育児・介護向けに受け止め方と相談先を特化（2026-10-03）
- 自動テストの整備（2026-10-03）
