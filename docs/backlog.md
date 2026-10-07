# バックログ

企画部（product-planner）が毎日更新する。

## 今日の改善（2026-10-07）

### 1時間以上たった会話を全員分まとめて消す＋会話の読み書きをロック1つで守る

根拠：`docs/research/2026-10-07.md`（調査部の示唆1）。分類：お客さまとの約束を守る仕組み（約束2「保存しない」：放置された会話をメモリに長く残さない）＋安全性（同時アクセスで会話が乱れたり落ちたりしない）。旧候補1と旧候補2をまとめたもの。

#### なぜこれを選んだか（ほかの候補との比較）

- 今は1時間放置された会話も、その人が次に話しかけるまで（話しかけなければいつまでも）メモリに残る。「保存しない」の約束からすると、使われない会話を長く持ち続ける理由はない。
- 本番は `gunicorn --workers 1 --threads 4`（gthread）で、4スレッドが同じ `sessions` を触る（調査 2-1）。全員分を回す一括削除は、ほかのスレッドが同時に `sessions` を変えると `RuntimeError` になりうる（調査 2-2）。だから一括削除はロックと一緒に入れる必要がある。ロックは標準ライブラリの `threading` だけで済み、**LINE SDK に新しく依存しない**（10-06 の `UnsendEvent` の起動確認がオーナー待ちの間に、LINE SDK 依存の変更を重ねない）。
- 「応援リンクを有効にする前に必ず直す」項目は、`SUPPORT_URL` 未設定の今は影響がないため明日以降（ロックが入ったあとの方が素直に書ける）。
- 文言の見直し（候補3）は、送信取消の本番確認待ちのため後回し。
- SAFETYの「24時間の窓口名の併記」は SAFETY ブロックの変更なので作らない。「オーナーの承認待ち」欄と `docs/owner_todo.md` の既存の提案に3点目として追記した。

#### 目的（お客さまにとって何が良くなるか）

- 1時間以上やり取りのない会話は、本人が戻ってこなくても、だれかが焚き火に話しかけたときにまとめて消える。放置された愚痴がサーバーのメモリに残り続けない。
- 同じ人が続けて送ったり、何人かが同時に話しかけたりしても、ボットがエラーで黙ったり、会話の順番が乱れたりしにくくなる。
- 話しかけたときの返事の速さは変わらない（AIの返事を待つ間はロックを持たない）。

#### 変更内容（どのファイルの何を変えるか）

`main.py`（追加・変更あわせて約30行）：

1. `import threading` を足す（標準ライブラリ。`requirements.txt` は変えない）。
2. `sessions` の定義の直後にロックと一括削除の関数を足す：
   ```python
   # sessions を読み書きする短い区間だけで持つ。Claude API の呼び出しと LINE への返信の間は持たない。
   # Lock は同じスレッドで2回取ると詰まるので、ロックを取るのは on_message / on_unsend の中だけにする
   sessions_lock = threading.Lock()


   def sweep_idle(now: float) -> None:
       """1時間以上たった会話を全員分まとめて消す。sessions_lock を持った状態で呼ぶ（自分では取らない）"""
       for uid, s in list(sessions.items()):
           if now - s["updated"] > IDLE_SECONDS:
               sessions.pop(uid, None)
   ```
   - `get_history` は中身を変えない（自分ではロックを取らない）。docstring かコメントで「sessions_lock を持った状態で呼ぶ」と書く。
3. `on_unsend`：`sessions.pop(user_id, None)` を `with sessions_lock:` の中に入れる。
4. `on_message` の燃やす処理：`s = sessions.pop(user_id, None)` を `with sessions_lock:` の中に入れる。`reply` はロックの外。
5. 「使い方」の処理は変えない（`user_id in sessions` は単発の読み取り）。一括削除もここではしない。
6. `on_message` の通常の流れ（`history = get_history(user_id)` から Claude 呼び出し前まで）を次の形にする：
   ```python
   msg = {"role": "user", "content": text}
   with sessions_lock:
       now = time.time()
       sweep_idle(now)
       history = get_history(user_id)
       s = sessions[user_id]
       if any(w in text for w in SAFETY_USER_WORDS):
           s["safety"] = True
       s["updated"] = now  # 返事待ちの間に、ほかの人の一括削除で消されないように
       history.append(msg)
       del history[:-MAX_TURNS]
       # Claude API は user から始まる必要がある
       while history and history[0]["role"] != "user":
           history.pop(0)
       to_send = list(history)  # ロックの外でほかのスレッドが履歴を変えても、送る内容が乱れないよう写しを送る
   ```
   - `claude.messages.create(..., messages=to_send)` に変える。**Claude の呼び出しはロックの外。**
   - 失敗時（`except Exception:`）の `history.pop()` を次に変える（ほかのスレッドが同じ人の履歴に足していても、自分の発言だけを取り除く）：
     ```python
     with sessions_lock:
         history[:] = [m for m in history if m is not msg]
     ```
     `reply` はロックの外。
   - Claude 呼び出しのあとの `s = sessions.get(user_id)` 〜 `s["updated"] = time.time()` のブロック（10-06 に入れた「消されていたら記録し直さない」守り）を、そのまま `with sessions_lock:` の中に入れる。`reply(event.reply_token, answer)` はロックの外（10-06 と同じく、消されていても返事は送る）。
7. ロックを取る場所は、`on_unsend` に1か所、`on_message` に4か所（燃やす・Claude前・失敗時・Claude後）。**どれも入れ子にしない。** ロックの中で `reply`、`claude.messages.create`、`print`/`logging` を呼ばない。

`tests/test_main.py`（約45行）：

- `import threading` を足す。

| テスト名 | 確かめること |
| --- | --- |
| `test_idle_sessions_of_others_are_swept` | A が話し、`sessions["A"]["updated"] -= main.IDLE_SECONDS + 1` にしてから B が話す → `"A"` が `sessions` にない。B の履歴は2件 |
| `test_recent_sessions_are_not_swept` | A が話し、`updated -= main.IDLE_SECONDS - 60` にしてから B が話す → A の履歴は2件のまま |
| `test_sweep_alone_removes_only_old` | `sessions` に古い会話と新しい会話を直接入れ、`with main.sessions_lock: main.sweep_idle(time.time())` → 古い方だけ消える |
| `test_lock_not_held_while_waiting_for_claude` | `CLAUDE_MODE["during"]` で `main.sessions_lock.acquire(blocking=False)` を試し、取れたらすぐ `release()`。取れたことを記録 → 「取れた」（Claude待ちの間ロックを持っていない）。返事は1通送られる |
| `test_lock_released_after_every_path` | 通常の発言、API失敗、「使い方」、「燃やす」、送信取消のあと、毎回 `main.sessions_lock.locked()` が `False` |
| `test_api_failure_removes_only_own_message` | `CLAUDE_MODE["fail"]=True` と、`during` で同じ人の履歴に `{"role": "user", "content": "別の発言"}` を足す仕掛け → 失敗後の履歴は「別の発言」だけ（自分の発言は消え、ほかは消えない） |
| `test_parallel_messages_do_not_crash` | 古い会話を `sessions` に30件ほど入れたうえで、4スレッドが各30回、数人分の `on_message`（ときどき「燃やす」、`on_unsend`）を同時に呼ぶ → 例外が1つも出ない、終了後 `sessions_lock.locked()` が `False`、古い会話は残っていない。各スレッドは `join(timeout=10)` し、終わらなければ失敗にする（デッドロック検出） |

既存38本（特に `test_idle_session_expires`、`test_api_failure_sends_apology_and_keeps_history_clean`、`test_history_is_capped`、`test_help_keeps_existing_conversation`、送信取消と返事待ち中の守りの5本、`test_no_files_or_database_used`、`test_safety_rules_are_in_system_prompt`、`test_safety_flag_stores_no_content`）はそのまま通ること。既存テストの `during` の仕掛けはロックを取らずに `sessions` を触るので、今の形のまま通るはず（Claude待ちの間ロックを持たないため）。

#### 完了条件

- `python -m unittest discover -s tests -v` が全部合格（既存38本＋新規7本＝45本）。何度か続けて実行しても落ちない（スレッドのテストが不安定でないこと）。
- `git diff main.py` に `SAFETY START`〜`SAFETY END`、`PERSONA`、`HELP_TEXT`／`WELCOME`／`BURN_REPLY` の変更がない。
- ロックの中に `reply(`、`claude.messages.create(`、`print(`、`logging` がない（安全審査部が diff で確認）。ロックを取る `with sessions_lock:` が入れ子になっていない。
- `sessions` の1件に入るキーが `messages`／`updated`／`safety` のままで、新しく保存する情報がない。
- 差分合計がテスト込みでおおむね100行以内（本体約30行、テスト約45行）。超えそうなら `test_sweep_alone_removes_only_old` を削る（ロックを持たないことの確認と、並行実行のテストは削らない）。
- `requirements.txt`、モデル、`max_tokens`、起動コマンド（ワーカー数1・スレッド4）は変更なし。LINE SDK から新しく import するものがない。
- 安全審査部の承認。

#### リスクと戻し方

- リスク1：ロックの入れ子や取り忘れでデッドロックすると、ボットが全員に対して黙る。→ ロックを取る場所を5か所に限り、入れ子にしない。`sweep_idle`／`get_history` は自分でロックを取らない。並行テストに `join(timeout=10)` を入れて検出する。
- リスク2：ロックの中が長いと返事が遅くなる。→ ロックの中は dict とリストの操作だけ。一括削除は会話の件数ぶん回るが、小規模なので問題にならない見込み（利用者数は会社から見えないので推測）。
- リスク3：「1時間たったら必ず消える」わけではない（だれも話しかけなければ消えない、サーバー再起動では消える）。→ 説明文に「1時間で消える」「自動で消える」とは書かない（2026-10-05 の申し送り）。今の `HELP_TEXT`「燃やさずに1時間以上あくと、次に話しかけたときに新しい会話になり、前の会話は消えます」は今回の変更後も正しいので変えない。
- リスク4：`updated` を発言を受けた時点でも更新するので、「1時間」の数え始めが「最後の返事」から「最後の発言または返事」に変わる。→ 利用者から見て自然な方向で、説明文とも食い違わない。
- 戻し方：該当コミットを `git revert`。データの移行や環境変数はない。10-06 の送信取消の変更とは別コミットなので、どちらか一方だけ戻せる。

#### 触らないもの

`SAFETY` ブロック、`PERSONA`、`SYSTEM_PROMPT` の組み立て、モデル、`max_tokens`、`requirements.txt`、起動コマンド、LINE SDK の import、`IDLE_SECONDS`／`MAX_TURNS` の値、応援リンクの条件、`SAFETY_USER_WORDS`／`SAFETY_REPLY_MARKERS` の中身、`HELP_TEXT`／`WELCOME`／`BURN_REPLY` の文言、「使い方」の処理。

## 今日の改善（2026-10-06）（完了）

送信取消（`UnsendEvent`）で会話をまるごと消す＋返事待ち中に会話が消えても落ちない・復活しない守り。本番反映済み（push済み）。仕様の詳細は git の履歴（2026-10-06 のコミット）と `reports/2026-10-06.md` を参照。

申し送り：`UnsendEvent` が本番の line-bot-sdk で import できるかは会社の環境で確かめられておらず、**オーナーの起動確認待ち**（`docs/owner_todo.md`）。確認が取れるまで、LINE SDK に新しく依存する変更は重ねない。起動失敗が分かったら、その日の最優先は 10-06 のコミットの `git revert`。

## 今日の改善（2026-10-05）（完了）

「使い方」と送ると、消えるもの・残るもの・自分で消す方法を固定文で説明する。本番反映済み。仕様の詳細は git の履歴（2026-10-05 のコミット）と `reports/2026-10-05.md` を参照。

申し送り：「ヘルプ」はSOSとして送られうるため反応語にしない（安全審査部の差し戻し）。文言のルール（「完全に消える」「どこにも残らない」「1時間で消える」「自動で消える」と書かない、未確認事項は書かない）は今後の文言変更でも守る。

## 今日の改善（2026-10-04）（完了）

応援リンク（`SUPPORT_URL` 設定時のみ、燃やしたあとに一言。安全フラグが立った会話では出さない）。本番反映済み。仕様の詳細は git の履歴（2026-10-04 のコミット）と `reports/2026-10-04.md` を参照。

申し送り：1日1回までの回数制限は「燃やしたら消える」と緊張するため見送り（候補4）。

## 候補（優先度順）

- **応援リンクを有効にする前に必ず直す（安全審査部の申し送り 2026-10-06）**：相談先の案内を含む返事を待つ間に「燃やす」か送信取消をして、すぐ新しい会話を始めると、新しい会話に安全フラグが立たず、そのあと燃やすと応援リンクが出ることがある（`SUPPORT_URL` 未設定なら影響なし）。直し方の例：返事に相談先の目印があり、その時点で `sessions.get(user_id)` があれば、別の会話でも `safety=True` を立てる。2026-10-07 のロック導入後は、Claude後の `with sessions_lock:` の中に数行足す形で書ける。**次に作る第一候補**（LINE SDK に依存しない）。

1. （2026-10-07 の改善に統合：放置会話の一括削除）
2. （2026-10-07 の改善に統合：`threading.Lock` による同時アクセス対策）
3. **文言の見直し（あいさつ・燃やした後・使い方）**（広報部の指摘＋送信取消）：`WELCOME` の「サーバーからぜんぶ消えます」は `HELP_TEXT` に比べて言い過ぎに聞こえるので、「焚き火のサーバーから消えます」など言い方をそろえる。あわせて、送信取消の対応が本番で動くことをオーナーに確認してもらえたら、`HELP_TEXT` の【消えるもの】に「メッセージを送信取消すると、焚き火のサーバーにある会話がまるごと消えます（取消できる時間はLINEの仕様によります）」を足す。文言だけの小さな変更。調査（2026-10-07 の3）のとおり、「焚き火のサーバーから消える／LINEのトーク画面には残る」の書き分けは競合との違いなので崩さない。放置会話の一括削除が入っても「1時間で消える」「自動で消える」とは書かない。
4. **応援リンクの出しすぎ防止**：1日1回までなどの上限。燃やしたあとに何も残さない方法（例：上限ではなく「ユーザーの発言が3つ以上あった会話のあとだけ」など、記録不要の条件）を先に検討し、記録が要る案は安全審査部の判断を仰ぐ。
5. **1日の利用回数の上限**：環境変数 `DAILY_LIMIT` が設定されていれば、1人1日その回数まで。上限に達したら優しく伝える。コスト対策と、有料プランの土台。（収益化 第2段階の準備。回数の記録が要るため、候補4と同じ論点あり）
6. **説明文の定期見直し**：`HELP_TEXT` の根拠（Anthropicの保持期間・学習利用、LINEの仕様）を調査部が月1回ほど確認し、変わっていれば文言を直す。見直し対象に「メッセージ編集」を加える（2026-10-06 の調査では、編集イベントは1対1トークには届かないため、焚き火には編集前の文が残る。今の `HELP_TEXT` は編集に触れていないので食い違いはない。LINEの仕様が変わって1対1でも届くようになったら、送信取消と同じく会話を消す対応を検討する）。
7. **長文の分割**：LINEの文字数上限を超える返事を安全に切る。
8. **育児／介護のモード分け**：最初のあいさつで「育児」「介護」のボタンを出し、選んだほうに合わせて受け止め方を少し変える（例：介護なら終わりの見えなさ、育児なら睡眠不足への共感）。
9. **夜間モード**：深夜（日本時間0〜5時）は、より短く静かな返事にする。寝かしつけや夜間介護中の人向け。

## オーナーの承認待ち（会社では作らない）

- **SAFETYブロックの相談窓口の書き方**（`docs/research/2026-10-06.md`）：誤った窓口は見つかっていないが、(1) 189 は「児童相談所虐待対応ダイヤル」なので、子育ての悩みの相談専用ダイヤル 0120-189-783 を併記する、(2) 「親子のための相談LINE」は24時間ではなく地域によって別窓口なので、夜間は 189（24時間）を先に案内する、の2点を提案中。2026-10-07 に (3) を追加：「まもろうよ こころ」は有効で要修正なし（`docs/research/2026-10-07.md`）。ただし今はサイト名だけの案内なので、深夜に自分で探す手間が残る。サイト内にある24時間の窓口名（例：#いのちSOS、よりそいホットライン、チャットの「あなたのいばしょ」）を1つ併記する。番号は変わりうるので書くなら窓口名まで、書く場合はその日に公式ページで再確認する。SAFETYブロックの変更はオーナーの明示的な承認が必要なため、`docs/owner_todo.md` に記載。承認があった日に、文言案を安全審査部と詰めて実装する。

## 完了

- 放置会話の一括削除＋ロックによる同時アクセス対策（2026-10-07、予定。テスト合格・安全審査の承認後に本番反映）
- 送信取消で会話を消す＋返事待ち中に消えても落ちない守り（2026-10-06。本番反映済み、オーナーの起動確認待ち）
- 「使い方」と送ると、消えるもの・残るもの・自分で消す方法を説明する固定文（2026-10-05）
- 応援リンク（`SUPPORT_URL` 設定時のみ、燃やしたあとに一言。安全フラグが立った会話では出さない）（2026-10-04）
- 育児・介護向けに受け止め方と相談先を特化（2026-10-03）
- 自動テストの整備（2026-10-03）
