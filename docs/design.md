# Agent Office 設計文書

汎用 herdr プラグイン。エージェントフリートを**ピクセルアートのオフィス**として 1 ペインに描画する。各エージェント（= herdr ペイン）は机に座るキャラクターになり、状態（idle / working / blocked / done）に応じてアニメーションする。blocked のキャラは挙手して吹き出しを出し、長時間 blocked はトースト + サウンドにエスカレートする。挙手中の机へワンアクションでフォーカスジャンプできる。

- ステータス: Stage 1（設計）。実装は本文書の人間レビュー後に別タスク。
- 前提調査: [research-herdr-plugin-authoring.md](research-herdr-plugin-authoring.md)（以下「調査ノート」）
  - **本設計文書の本文は herdr 0.7.4（protocol 16）実測を前提に書かれている。** 調査ノートは 0.7.5（protocol 17）
    で再実測済みで、いくつかの前提が無効化された。該当箇所には **[0.7.5 注記]** を付けて事実と参照先を示している。
    注記は事実の指摘のみで、**設計の再決定は行っていない**（特に §9 の placement は別途議論中でオーナー判断待ち）。経緯は Issue #38。
- キャラ状態遷移とスプライト仕様: [character-states.md](character-states.md)
- レンダリングモック: [`mock/office_mock.py`](../mock/office_mock.py)

## 1. ゴール / 非ゴール

**ゴール**
1. herdr で動く全エージェントペインの状態を、一目で楽しく把握できる常駐ビュー（"オフィス"）を提供する。
2. blocked（入力待ち）エージェントの見落としを防ぐ: 挙手 → 一定時間でトースト + サウンド。
3. 「気づく → 飛ぶ」を最短にする: オフィス内キー操作および herdr action で該当ペインへフォーカスジャンプ。
4. claude-org を含む**あらゆる herdr 利用者**が使える汎用 OSS プラグインであること。

**非ゴール**
- エージェントの操作・入力代行（読む/飛ぶまで。ペインへの send-text はしない）。
- herdr 外のエージェント基盤（tmux 等）対応。
- claude-org 固有概念（task_id, DELEGATE 等）の core への持ち込み（§10 の通り拡張点で吸収）。
- Stage 1 では実装本体（本文書はレビュー用設計）。

## 2. 全体アーキテクチャ

常駐プロセスは **office ペイン 1 個**だけ。マニフェストの `[[panes]]` entrypoint として起動される通常のターミナルペインであり、**自分の stdout に ANSI で描画する**（調査ノート §4: 自ペイン描画に `pane.graphics.set` は不要）。

```
                    herdr server (socket API)
                          ▲            │ NDJSON events
        short-lived conns │            │ long-lived conns
  ┌───────────────────────┴────────────▼──────────────────────┐
  │ agent-office pane process(TUI)                            │
  │                                                           │
  │  ┌────────────┐   ┌───────────────┐   ┌────────────────┐  │
  │  │ Subscriber │──▶│  OfficeState  │──▶│    Renderer    │  │
  │  │ (event     │   │ (pane→desk    │   │ tier0: ASCII   │  │
  │  │  loop)     │   │  state model) │   │ tier1: unicode │  │
  │  └────────────┘   └──────┬────────┘   │ tier2: kitty   │  │
  │  ┌────────────┐          │            └────────────────┘  │
  │  │ Input      │──────────┤ jump/select                    │
  │  │ (keyboard) │          ▼                                │
  │  └────────────┘   ┌───────────────┐                       │
  │                   │  Escalator    │─▶ notification.show   │
  │                   │ (blocked timer)│                      │
  │                   └───────────────┘                       │
  └───────────────────────────────────────────────────────────┘
```

構成要素:

| コンポーネント | 責務 |
|---|---|
| **Subscriber** | ソケット接続管理とイベント受信。受けたイベントを内部キューに正規化して流すだけ |
| **OfficeState** | 純粋な状態モデル（ペイン → 机の割当て、各机の状態と時刻）。描画にも herdr にも依存しない（単体テスト対象） |
| **Renderer** | OfficeState のスナップショットをフレームに変換して描画。tier 選択とアニメーション位相を持つ |
| **Input** | 自ペインの stdin（raw mode）。机の選択・ジャンプ・表示切替 |
| **Escalator** | blocked 経過時間の監視と `notification.show` 呼び出し（再通知・レートリミット対応） |

データフローは単方向（events → state → frame）。描画は「イベント到着」または「アニメーション tick」で dirty になったときのみ再描画する。

### プロセス/実行モデル
- 単一プロセス・イベントループ（実装言語の推奨は §12）。
- スレッド/タスク: ①購読受信、②stdin、③タイマー tick（アニメ + escalation）、④メインループ（state 更新と描画）。すべて 1 プロセス内。

## 3. イベント購読戦略

調査ノート §5 の制約（`pane.agent_status_changed` は per-pane 購読のみ / 購読の動的追加 API なし / 購読以外は 1 接続 1 リクエスト）を踏まえ、接続を役割で分ける:

1. **接続 L（lifecycle, 常設・張りっぱなし）**: `pane.created` / `pane.closed` / `pane.exited` / `pane.focused` / `pane.agent_detected` / `pane.updated` / `workspace.renamed` / `workspace.closed` を 1 本で購読。フリートのメンバーシップ・フォーカス・メタデータ変化を追う（`pane.focused` は §4 の `focused_pane_id` と FOCUSED オーバーレイの供給源）。
2. **接続 S（status, メンバー変化時に張り直し）**: 現在の全ペイン分の `pane.agent_status_changed` 購読を 1 本の接続に載せる。ペインの増減（接続 L で検知)のたびに新しい接続 S' を張ってから旧 S を閉じる（make-before-break、イベント欠落を防ぐ）。張り直しは 250ms 程度のデバウンスで連続増減をまとめる。
3. **コマンド用短命接続**: `pane.list`（起動時スナップショット）、`pane.focus`、`notification.show` 等は都度接続。

起動シーケンス（**subscribe-then-snapshot**、取りこぼし防止のため順序が重要）:
1. 接続 L を張る（以降のライフサイクルイベントはすべて捕捉される）。
2. `pane.list` で初期スナップショットを取得し OfficeState を構築。スナップショット中に L から届いた `pane_created` 等は upsert として適用（両方に現れても冪等）。
3. 接続 S を全既知ペイン分で張り、**張り終えた直後に `pane.list` をもう一度取得して status を上書き再同期**する（S 確立前に起きた status 変化を回収）。この再同期は接続 S を張り直したときにも毎回行う。

`pane.agent_status_changed` は変化時のみ発火し「購読開始時に現在値を配る」保証はないため、購読確立の前後ギャップは上記の snapshot 再同期でのみ埋まる（イベント待ちでの自然収束には頼らない）。スナップショットで発見された未知ペインも S 張り直しデバウンスのトリガになる。

再接続: サーバー再起動等で接続が切れたら指数バックオフで全接続を張り直し、`pane.list` で再同期。ただし**サーバー再起動では接続だけでなく office プロセス自体が失われる**ので、張り直しでは足りない。プロセスの復帰は `[[startup]]` フックが担う（下記）。

> **[0.7.5] サーバー再起動後のプロセス復帰（Issue #39 で実装）**
>
> 実測: サーバー再起動時にプラグイン所有ペインの**枠（label / cwd / タブ位置）は復元されるが、マニフェストの
> `command` は再実行されない**（復元されたペインには既定シェルが入る。`pane.process_info` で確認）。再起動後に
> 残るのは「Agent Office というラベルの付いた素のシェル」であり、トースト・エスカレーション・state.json の
> 書き出しがすべて黙って止まる。`herdr update` はサーバーを再起動するため、これは通常運用で毎回起きていた。
>
> 対策として `[[startup]]` フック（`python3 -m office startup` → `office/actions.py` の `action_startup`）を持つ。
> herdr は enabled なプラグインごとに、セッション復元完了後・ソケット利用可能な状態でこれを 1 回実行する。挙動:
>
> - **復元された枠がある場合だけ復帰させる。** ラベル `Agent Office` を持つペインの存在そのものが
>   「再起動前にユーザーがオフィスを開いていた」signal。枠が無ければ何もしない（意図的に閉じたオフィスを
>   勝手に開き直さない）。plugin が disabled ならフック自体が発火しない（実測）ので、その層でも守られる。
> - **復帰は「枠を `pane.close` してから `plugin.pane.open`」。** 復元された枠を再利用することはできない:
>   その枠はもはや plugin 所有ではなく（`plugin.pane.focus` / `plugin.pane.close` が `plugin_pane_not_found`
>   を返す）、`HERDR_PLUGIN_*` 環境変数も失っている（= そのシェルでコマンドを起動しても
>   `HERDR_PLUGIN_CONFIG_DIR` / `HERDR_PLUGIN_STATE_DIR` の無いオフィスになる）。閉じてから開くことで
>   「Agent Office タブが 2 枚」を作らない。
> - **focus は奪わない**（`focus: false`）。フックはユーザーがフォーカス中のペインを持った状態で走るため、
>   ここで focus を動かすのは Issue #21 で直した不具合を別経路から作り直すことになる。
>   `action-open` が渡す `focus: true` はユーザー起点の open にのみ正しい。
> - **生死の判定は state.json ではなく `pane.process_info`。** 再起動直後の state.json は `running: true` の
>   まま残り（オフィスは終了処理を走らせる間もなく落ちる）、記録された pane id は復元された枠に解決するため、
>   state.json だけでは死んだオフィスと生きたオフィスを区別できない。
> - **live handoff でもフックは発火するが、そのとき office プロセスは生き残っている**（実測）。
>   `pane.process_info` にオフィスが見えたら何もしないので、`herdr update` のたびに二重起動することはない。
> - 枠に**ユーザーが何か起動している場合は触らない**（`pane.close` は取り消せないため）。
>
> 実測の詳細は調査ノート §2。

**実装時検証事項**: `pane.updated`（ブロードキャスト、pane_id 不要）が agent_status 変化でも発火するなら、接続 S を廃止して L だけの単純構成にできる可能性がある。Stage 2 冒頭で実測し、発火するなら簡素化する（本設計は発火しない前提でも成立する保守的構成）。

## 4. 状態モデル（OfficeState）

```
OfficeState
├─ desks: OrderedMap<pane_id, Desk>
├─ focused_pane_id: Option<pane_id>     # 接続L の pane.focused から
├─ selected_desk: Option<pane_id>       # オフィス内カーソル
└─ rooms: Map<workspace_id, label>      # ワークスペース = 部屋/島

Desk
├─ pane_id, workspace_id, tab_id
├─ agent: Option<str>        # "claude" 等（pane_agent_detected / PaneInfo.agent）
├─ display_name: str         # 決定則: display_agent > label > terminal_title_stripped > agent > pane_id
├─ status: idle|working|blocked|done|unknown
├─ status_since: monotonic time
├─ blocked_since: Option<monotonic time>
├─ state_labels: Map<status, str>   # 吹き出し文言に利用（イベント同梱）
└─ escalation: {notified_at: Option<time>, repeat_count: int}
```

規則:
- **机の割当て**はワークスペース → タブ → pane_id の安定ソート順。同一 workspace の机は同じ「島」にまとめ、島に部屋ラベル（workspace 名）を付ける。ペイン消滅で机は撤去され、以降の机は詰め直す（アニメーション不要、次フレームで再レイアウト）。
- **表示対象**は agent が検出された（または report された）ペインのみを既定とする（`filter = "agents"`）。設定で全ペイン表示（`"all"`）にも切替可。エージェントのいない素のシェルペインまで机にするとノイズになるため。
- office ペイン自身（`HERDR_PANE_ID`）は常に除外。
- `status` は herdr の `AgentStatus` をそのまま採用し、独自の状態推定はしない（唯一の真実は herdr の検出）。

## 5. 描画設計（グラフィックス互換フォールバック）

**3 tier 構成**。上位 tier が使えない環境では自動で下位に落ちる。すべて同一の OfficeState スナップショットを入力とする。

| tier | 描画方式 | 判定条件 | 見た目 |
|---|---|---|---|
| **tier 1（既定）** | Unicode 半ブロック（`▀`）+ 24bit/256 色 ANSI による**テキストセル・ピクセルアート**。1 セル = 縦 2px | ほぼ全ての herdr 環境で動く（自ペインへの ANSI 出力のみ） | ドット絵オフィス（モック参照） |
| tier 0（fallback） | ASCII + 8/16 色。机 = 罫線ボックス、キャラ = スティックフィギュア | `--ascii` 指定、`TERM=dumb`、非 UTF-8 ロケール、色数不足 | 情報は等価（状態・挙手・名前） |
| tier 2（opt-in） | `pane.graphics.set`（PNG）で自ペインに真のピクセルアートを重畳 | 設定 `renderer = "kitty"` かつ `pane.graphics.info` が `feature_disabled` を返さない場合のみ | 高精細スプライト |

判定ロジック（起動時に 1 回 + 設定で強制上書き可）:
1. 設定 `renderer` が明示されていればそれに従う（`kitty` 指定でも `pane.graphics.info` が**成功しなければ** tier 1 へ**警告付き**フォールバック）。
2. 自動判定: tier 2 は**選ばない**（既定は tier 1。kitty は実験的機能のため opt-in のみ）。`LANG`/`LC_*` が UTF-8 かつ `TERM != dumb` → tier 1。それ以外 → tier 0。色数は `COLORTERM=truecolor` → 24bit、なければ 256 色パレットに量子化。
3. tier 2 有効時も、レイアウト計算・ネームプレート・凡例はテキストセルで描き、スプライト部分だけ graphics に置く（`pane.graphics.stream` は 0.7.4 に無いため全面画像アニメは行わない。将来 stream が来たら差し替え可能なよう Renderer を interface 化）。**[0.7.5 注記] `pane.graphics.stream` は 0.7.5 でも存在しない**（スキーマは `set`/`clear`/`info` の 3 つのみ、`info` は変わらず `feature_disabled`）。0.7.5 リリースノートの "Pane graphics streams now shut down cleanly..." は内部修正であり公開 API の追加ではないため、**この前提は据え置きで正しい**（調査ノート §4 で再確認済み）。

**tier 2 の判定条件（Stage 2 実測により精緻化）**: 当初は「`pane.graphics.info` が `feature_disabled` を返さない場合」としていたが、実測で 2 種類の拒否コードを確認したため「**`info` が成功した場合のみ**」に改めた。

| コード | 状況 | 判断 |
|---|---|---|
| `feature_disabled` | `[experimental].kitty_graphics = false`（既定） | 恒久的に不可 → tier 1 |
| `cell_size_unavailable` | kitty_graphics 有効だが herdr が外側端末のセルピクセルサイズを取得できない（WSL 実測） | 画像を配置できない → tier 1 |

重要な実測事実: `cell_size_unavailable` の状態でも **`pane.graphics.set` は `{"type":"ok"}` を返す**。つまり `set` の成功は「画面に出た」ことの証拠にならず、可否判定に使えるのは `info` だけである。

**tier 2 は加算的（additive）に実装する**: tier 1 のフレームを完全に描いた上に画像を重ねる。外側端末が kitty graphics を解さない場合（herdr からは検知不能）でも、ユーザーには動作する tier 1 オフィスが残り、空白にはならない。

> **[0.7.5 注記] 本節の tier は「office ペインの中をどう描くか」の選択肢であり、0.7.5 では
> office ペインの外に出せる描画面が増えた。** `pane.report_metadata` の `tokens` が
> herdr の sidebar（expanded Agent / Space 行）に描画されるようになり、sidebar はタブを跨いで常時見える。
> ただし表示位置と装飾はユーザーの `[ui.sidebar.*] rows` 設定側にあり、プラグインからは値しか送れない
> （調査ノート §10 に実測仕様）。この面を使うかどうかは §9 placement と同じ議論に属するため、
> ここでは選択肢の存在を記録するだけに留める。Issue #38。

共通事項:
- フレームは全画面再構成 + カーソルホーム書き換え（差分描画は Stage 2 で必要なら導入）。alternate screen + カーソル非表示。SIGWINCH（Windows ではリサイズポーリング）で再レイアウト。
- アニメーションは 2 FPS 固定 tick（working のタイピング、idle のコーヒー湯気、blocked の挙手点滅）。**全キャラのアニメ位相に pane_id ハッシュのオフセットを与え、機械的な同期を避ける**。フレームが端末に対して大きすぎる場合は自動で縮小表現（机 1 行サマリ）に切り替える。
- 机が多く 1 画面に収まらない場合: island 単位で折返し、収まらなければスクロール（選択カーソル追従）+ ヘッダに "8/23" 表示。blocked の机が画面外にあるときは端に矢印インジケータを出す。

## 6. インタラクション

### オフィス内キー操作（office ペインにフォーカスがあるとき）
| キー | 動作 |
|---|---|
| `←↑↓→` / `hjkl` | 机の選択カーソル移動 |
| `Enter` | 選択中の机のペインへ `pane.focus` |
| `b` | 最も長く blocked の机へジャンプ（= 挙手最古優先） |
| `Tab` | blocked の机を順に選択 |
| `a` | 表示フィルタ切替（agents / all） |
| `s` | サウンド/エスカレーションの一時ミュート切替 |
| `?` | ヘルプオーバーレイ |
| `q` | office ペインを閉じる |

**カーソル移動は描画レイアウトに従う（issue #27）**。`↑↓` は「画面上で真上/真下に描かれている机」へ移動する。island（workspace）ごとに room ラベル行と空行が入り行折返しも island 単位でリセットされるため、平坦な `ordered_desks()` に対する `idx ± per_row` では複数 workspace 表示時に別の机へ飛ぶ。移動先は `office/layout.py` の `Layout` が唯一の窓口として答え、`Renderer.layout()` が返す同じレイアウトを描画側も消費する（＝カーソルの格子と画面の格子が乖離しない）。行幅が異なる場合、列は移動先の行幅にクランプされる（goal column は保持しない）。`←→` はレイアウト全体の読み順で ±1 なので、行末・island 末からは次の行/island の先頭へ続く。両軸ともクランプのみで循環しない。縮小表現（机 1 行サマリ）も「1 机幅のレイアウト」として同じモデルで扱う。

**フォーカス獲得直後のキーは捨てる（issue #21）**: herdr はキー入力をフォーカス中のペインへ配送するため、ユーザーが入力途中で office を開くキーバインドを押すと、打ちかけの残りが office ペインへ届く。そこに `Enter` が混ざると「選択中の机へジャンプ」として実行され、獲得したばかりのフォーカスをそのまま手放してしまう（実測では focus から 13ms 後）。対策として、フォーカスを獲得してから `FOCUS_GRACE_S`（0.25 秒）の間に届いたキーは、ジャンプ系に限らず全て無視しステータス行に理由を出す。人間が office の出現を認識して意識的にキーを押すまでの時間より十分短いので、意図した操作がこの窓に入ることはない。

判定は二通りで、キーと `pane.focused` はそれぞれ別スレッド経由で届き到着順が入れ替わりうるため両方必要:
- `pane.focused`（自ペイン、かつ直前は他ペイン）を受けてから `FOCUS_GRACE_S` 未満。無視する。
- office が「他ペインがフォーカス中」と認識している間に届いたキー → そのペインのキーボードを読めるはずがないので、フォーカスは実際に移動済みで `pane.focused` がキーに追い越されただけ。無視する。ただし**最初の 1 キーから `FOCUS_GRACE_S` まで**に限る: ここからは「イベントが直後に来る」と「イベントは永遠に来ない」が区別できず、待つ価値があるのは前者だけ。無制限にすると L 接続断（分単位になりうる）中のフォーカス移動でキーが全部落ち、`q` すら効かない = フォーカスされているのに操作不能なペインになる。上限を 1 窓にすれば失うのはどちらの場合も同じ 0.25 秒で済む。認識中のフォーカスが変わればまた 1 窓ぶん復活する（見えている移動ごとに追い越しの機会があるため）。

いずれも**判定不能なら通す**（`HERDR_PANE_ID` 未設定、フォーカス未観測、窓を使い切った後）。`pane.focused` を取りこぼした場合の最悪ケースが「1 キー分だけ従来挙動」で済み、「キーボードが効かないペイン」にはならない側に倒す。フォーカス認識を動かすのは `pane.focused` イベントのみとし、`pane.list` スナップショット（Subscriber の再接続時・Reconciler の 60 秒スイープ・`a` のリフレッシュ）の `focused` は採用しない: どれもソケット 1 往復ぶん古く（issue #12）、適用済みの新しい `pane.focused` を巻き戻しうる。取りこぼしで古い認識が残った場合の復旧は上記の打ち切りが担う。加えて `run()` は起動時にも窓を張る: office が未起動のときの導線は `plugin.pane.open` なので、迷子キーは stdin を読む前に pty へバッファされ、窓を張るはずの `pane.focused` は Subscriber 接続前に発火して観測できない。どちらの経路でも「まだ何も描いていない」ことは共通なので、そこを根拠にする。

### herdr actions（マニフェスト定義、外部からの入口）
```toml
[[actions]]
id = "open"
title = "Open Agent Office"
contexts = ["global"]
command = ["<runtime>", "office", "action-open"]     # 既存 office ペインがあれば plugin.pane.focus、なければ plugin.pane.open

[[actions]]
id = "jump-blocked"
title = "Jump to longest-blocked agent"
contexts = ["global"]
command = ["<runtime>", "office", "action-jump-blocked"]
```
- `jump-blocked` は office ペインが**起動していなくても**動く単発コマンドとして実装する: `pane.list` → `agent_status == blocked` のうち起動が最古のペインへ `pane.focus`（blocked_since は単発コマンドでは分からないため pane_id 順のタイブレーク。office 稼働中は state ファイル（`HERDR_PLUGIN_STATE_DIR/state.json`、後述）を参照して正確な最古を選ぶ）。
- ユーザーは herdr のキーバインド（`[[keys.command]]` で `herdr plugin action invoke agent-office.jump-blocked`）に割り当てられる。README に設定例を載せる。

> **[0.7.5 注記] herdr 側に順序決定の機構が生えた。** 0.7.5 の `agent.view.set`（`filter` + `sort`）は
> sidebar の表示内容と **agent キーバインド（`[keys] focus_agent`）のジャンプ順**を再定義する。実測では
> `sort: [{field:"attention"}]` 等を張ると `alt+1` の飛び先が変わり、`filter` で絞ると対象から外れたペインには飛べない。
> これは本節の `jump-blocked`（最古 blocked へ飛ぶ）および `office/layout.py` のカーソル順と**機能が重なる**。
> 一方で `agent.view` はグローバル 1 枠・last-writer-wins・読み戻し API 無し・サーバー再起動で消える
> （調査ノート §9）ため、単純な置き換えにはならない。どう扱うかは設計判断として保留。Issue #38。

### トーストからの遷移
herdr のトーストはクリックアクションを持たないため、「トーストで気づく → キーバインド or office ペインでジャンプ」が導線。README で `open_notification_target`（herdr 組込み、prefix+o）too が blocked ペインに飛べることを案内する。

## 7. エスカレーション（Escalator）

- 状態遷移 `* → blocked` で `blocked_since` を記録。**起動時スナップショット（`pane.list`）の時点で既に `blocked` のペインは、`state.json` に前回の `blocked_since` があればそれを、なければスナップショット時刻を `blocked_since` として初期化し、最初から Escalator の監視対象に入れる**（office を後から開いたケースで通知が飛ばない穴を防ぐ）。**`blocked_threshold_s`（既定 90 秒）**を超えて blocked のままなら `notification.show`:
  - `title`: `"✋ {display_name} is waiting"`（80 字制限内に切詰め）
  - `body`: `"blocked for {duration} in {workspace label}"` + state_label があれば付加（240 字制限）
  - `sound`: `"request"`（設定で `none` に変更可）
- **再通知**: blocked が続く限り `renotify_interval_s`（既定 300 秒、0 で再通知なし）ごとに再送。repeat_count を body に含める（"2nd reminder"）。
- 複数同時 blocked は 1 通に集約する（`"✋ 3 agents are waiting"`）。集約ウィンドウは threshold 到達から 5 秒。
- レスポンスの `reason` を尊重: `rate_limited`/`busy` は 30 秒後リトライ、`disabled`/`no_foreground_client` はログのみ（ペイン内の視覚表現は継続しているので害はない）。
- blocked 解除（→ working/idle/done）で escalation 状態をリセット。
- `done` への遷移は既定では**通知しない**（herdr 本体の `[ui.sound]` と重複するため）。設定 `notify_done = true` でオプトイン。
- **セットアップ要件**: トーストは `[ui.toast].delivery` が既定 `off`（調査ノート §6）。README の Quick Start に `delivery = "herdr"` 設定を必須手順として明記する。

## 8. 設定

`HERDR_PLUGIN_CONFIG_DIR/config.toml`（無ければ全て既定値で動く zero-config）:

```toml
[office]
filter = "agents"            # "agents" | "all"
renderer = "auto"            # "auto" | "unicode" | "ascii" | "kitty"
fps = 2                      # 1..10, アニメーション tick
theme = "default"            # スプライトパレット名
name_template = "{name}"     # ネームプレート整形。{name} は §4 の決定則の値。
                             # 例 "{name:last-segment}" は '/' 区切りの末尾要素のみ表示
                             # (claude-org の長い label 対策、§10)
plate_lines = 1              # 1 | 2。ネームプレートの行数。全角は 2 桁幅で
                             # 折り返すため tier1/2 の 16 桁プレートは日本語
                             # 8 文字 (tier0 は 9 桁で 4 文字)。2 にすると
                             # 1 デスクあたり 1 行増える (issue #25)

[escalation]
blocked_threshold_s = 90
renotify_interval_s = 300    # 0 = 再通知しない
sound = "request"            # "request" | "none"
notify_done = false

[include]                    # 任意。表示対象の絞り込み
workspaces = []              # 空 = 全 workspace。workspace ラベルの glob
exclude_agents = []          # agent 名で除外 (例 ["codex"])
```

- 設定変更の反映はプロセス再起動（office ペインを開き直す）。ホットリロードは非ゴール。
- 実行時状態（blocked_since 等）は `HERDR_PLUGIN_STATE_DIR/state.json` に 10 秒毎 + 変化時に書き出す。単発 action（§6）とデバッグ用。スキーマに `version` を持たせる。

## 9. 配布形態

> **[0.7.5 注記] 本節の前提のうち 3 点が 0.7.5 で変わった。事実のみ記す（設計の再決定はしない。Issue #38）。**
> 1. **placement の議論の土台が変わった**: 本節が `placement = "tab"` を既定にした理由付けの一部（および
>    「office ペインは herdr に戻してもらえないので置き場所を人が決めて常駐させる」という前提）は、
>    `[[startup]]` の登場と「復元ではペインの枠だけ戻りコマンドは再実行されない」実測により再検討の対象になる
>    （§3 の注記を参照）。**placement 自体は別途議論中でオーナー判断待ちのため、ここでは変更しない。**
> 2. **プラグインの install / link はユーザーグローバルになった**（セッション隔離ではない）。
>    「このセッションだけ別バージョンで動かす」はできず、開発中の link は本番セッションにも効く（調査ノート §7）。
> 3. **`min_herdr_version` は必須項目になった**（0.7.5 では欠落マニフェストの link が拒否される）。
>    本リポジトリの `herdr-plugin.toml` は宣言済みなので現状は影響なし。

- **リポジトリ**: 独立 GitHub リポジトリ（例 `agent-office`）、topic **`herdr-plugin`** を付与しマーケットプレイス掲載（調査ノート §7）。ライセンス MIT。
- **インストール**: `herdr plugin install <owner>/agent-office`。ランタイム依存を増やさないため `[[build]]` は空にする方針（§12 の言語選択に依存）。開発は `herdr plugin link`。
- **マニフェスト骨子**:

```toml
id = "agent-office"
name = "Agent Office"
version = "0.1.0"
description = "Your agent fleet as a pixel-art office: see who's working, who's stuck, jump to them."
min_herdr_version = "0.7.5"
platforms = ["linux", "macos", "windows"]

[[panes]]
id = "office"
title = "Agent Office"
placement = "tab"        # 常駐ビュー。popup は pane_id を持たず不可 (調査ノート §4)
command = ["<runtime>", "office"]

# [[actions]] は §6 参照
```

- `placement = "tab"` を既定とする（オフィスは横長レイアウトのため split より tab/zoomed が向く。ユーザーは `plugin.pane.open` の placement 上書きで split にもできる）。
- バージョニング: semver。`min_herdr_version` は **0.7.5**。当初は依存 API（events.subscribe の per-pane 購読、plugin.*）が揃う 0.7.4 を下限としていたが、再起動後のプロセス復帰に使う `[[startup]]` フックが 0.7.5 の機能なので Issue #39 で引き上げた。0.7.4 を宣言し続けると「宣言した下限では再起動復帰が黙って効かない」（あるいは未知セクションでマニフェストごと拒否される。どちらになるかは未実測 — 調査ノート §2 の未実測欄）ことになり、下限の意味を満たさないため。tier 2 が `pane.graphics.stream` に依存する時点でさらに引き上げ（**[0.7.5 注記]** `stream` は 0.7.5 でも未提供なので引き上げ条件は未達）。
- Windows: named pipe 接続と ANSI 出力（Windows Terminal は対応）に依存。cp932 コンソールを考慮し、**CLI の `--help` や print は ASCII のみ**、tier 判定で非 UTF-8 なら tier 0。
  - tier 判定の材料は `LANG` だけでは足りない。Windows はロケール変数を設定しないため、`LANG` 未設定時は `sys.stdout.encoding` を見る。逆に encoder が UTF-8 でないと判明した場合は `renderer` 設定より優先して tier 0 に落とす（cp932 では半ブロックが encode できず、フレームの途中で `UnicodeEncodeError` になるため）。
  - tier 0 でも安全ではない。ペインラベルやエージェント名は herdr 由来で任意の文字を含みうるので、`Screen._write` に `errors="replace"` のフォールバックを置く。1 文字が化けるのは、alternate screen 上にトレースバックを吐いてフレームごと壊すよりましである。
  - `command` は herdr がシェルを介さず argv として spawn し、argv[0] を PATH（Windows では PATHEXT も）で解決する。3 プラットフォーム共通で解決できるインタープリタ名は存在しない（`python3.exe` は python.org のインストーラが作らない）ため、**pane / action は platform ごとに別 id で二重宣言する**（item-level `platforms` が top-level を上書きし、id はプラグイン内で一意である必要がある）。Windows 側は `py -3` を使う。`actions.py` の `plugin.pane.open` もこの id をプラットフォームで切り替える。

## 10. claude-org からの利用シナリオ（broker herdr バックエンド時）

前提: claude-org が broker の herdr バックエンドで各ワーカーを herdr ペインとして起動している場合、各ペインには既に `agent = "claude"` と長い `label`（例 `claude-org/{run_id}/g7/project:herdr-agent-office/a2` — 実測）が付いている。

- **core は何も知らない**: Agent Office は herdr の汎用フィールド（agent / label / display_agent / state_labels / agent_status）だけを読む。claude-org 固有の解釈はしない。
- **表示名の整形**は汎用機構 `name_template` で吸収する: claude-org ユーザーは `"{name:last-segment}"` を設定すれば `a2` や `project:herdr-agent-office/a2` 相当の短縮表示になる。より良い表示名を出したい組織側は、herdr 標準の `pane.report_metadata --display-agent` / `--state-label` を叩けばそのまま反映される（Office 側の変更不要）。
- **島 = workspace** の対応により、claude-org の「1 ワーカー = 1 workspace」運用ではワーカーごとに部屋が分かれて見える。
- blocked エスカレーションは、claude-org の窓口（人間）が席を外している時の「ワーカーが承認待ちで止まっている」検知にそのまま使える。
- 将来 claude-org 側がタスク ID 等を出したければ `pane.report_metadata --token`（汎用 key-value、実測で PaneInfo.tokens に載る）を使い、Office は `name_template` にトークン参照（`{token:task_id}` 等）を足すだけで対応できる（Stage 2 以降の拡張）。
  - **[0.7.5 注記] `tokens` は Office の外でも見えるようになった。** 0.7.5 では herdr の sidebar
    （expanded Agent / Space 行）に token 値が描画される。ただし表示するかどうかと装飾は**ユーザーの
    `[ui.sidebar.agents] rows` / `[ui.sidebar.spaces] rows` 設定**で決まり（`$task_id` のように `$` 前置で参照、
    `fg` は `#RRGGBB` のみ、`bold` / `dim` は真偽値）、プラグイン側から装飾や表示を強制する手段は無い。
    つまり claude-org が token を出せば、Office を経由せず herdr の sidebar にも出せる（調査ノート §10）。

## 11. 先行プロダクト比較と差別化

「コーディングエージェントをピクセルアートのキャラクターとして可視化する」先行プロダクトは複数実在する。比較（各プロジェクトのリポジトリ/サイトを 2026-07-24 に直接確認）:

| プロダクト | 実行環境 | 状態検知の方式 | 対応エージェント | blocked 表現 | 操作統合 | ライセンス |
|---|---|---|---|---|---|---|
| **Pixel Agents**（pablodelucca / pixelagent.space） | VS Code 拡張 + `npx` CLI（ローカル Fastify + ブラウザ SPA） | Claude Code hooks の POST（推奨）+ JSONL transcript ポーリング（fallback） | Claude Code（他はロードマップ） | speech bubble | 表示のみ | MIT |
| **pixtuoid**（IvanWng97/pixtuoid） | ターミナル（half-block ピクセルアート） | hook shim（unix socket / named pipe）+ JSONL transcript 監視 → state reducer | 10+ CLI | キャラ頭上に `?` — 本案の挙手と同型 | 表示のみ | MIT |
| **claude-office**（paulrobello/claude-office） | ブラウザ（PixiJS + Next.js） | Claude Code hooks（`make hooks-install`）+ HTTP/WebSocket | Claude Code（boss/サブエージェント階層） | — | 表示のみ（whiteboard 12 モード等） | MIT |
| **Pixel Office**（JetBrains plugin 31298） | JetBrains IDE 内 | （タイトルのみ確認、詳細未検証） | — | — | — | 未確認 |

### 差別化軸

1. **検知レイヤーの位置が異なる**。既存勢はすべて**クライアント側**の検知 — エージェントごとに hooks を仕込む・transcript ファイル形式を解釈する個別統合が必要で、対応エージェントの追加はそのプロダクトの実装作業になる。本案は **herdr サーバーの `pane.agent_status_changed` によるネイティブ検知**に乗るため、(a) herdr が統合するすべてのエージェント（claude / codex / gemini / cursor / droid 等、herdr の agent-detection マニフェストが更新されれば自動追随）に**統合作業ゼロ**で適用され、(b) 検知がサーバー側にあるので `herdr --remote` のリモートセッションでもローカルと同一に機能し、(c) エージェント側への hooks 設置・設定変更が一切不要（インストールだけで動く）。
2. **ターミナル完結 + 操作統合**。既存勢は「見る」ための別サーフェス（ブラウザ / IDE / 別ウィンドウ）が中心。本案はワークスペースそのもの（herdr ペイン）に住み、挙手キャラ → `pane.focus` ジャンプ（§6）、`notification.show` エスカレーション（§7）まで**操作と一体**。「気づく」から「対処する」までコンテキストスイッチがない。pixtuoid は同じくターミナル常駐だが、ペイン操作系を持たない汎用ダッシュボードである。
3. **herdr エコシステム内に先行例がない**。GitHub topic `herdr-plugin` の掲載リポジトリ（2026-07-24 時点、上位掲載分を確認）にキャラクター/ピクセルアート型のフリート可視化は存在しない（`herdr-remote` や `collie` など監視系はあるが、いずれも通知・リスト UI）。

### 借用候補の評価

| アイデア（出典） | 評価 | 反映 |
|---|---|---|
| half-block ターミナル描画（pixtuoid） | 本設計 tier 1 と同手法であり、実運用例として妥当性の裏付けになる。MIT なので実装時の参照可（コード流用時は MIT 表記を継承） | 反映済み（§5 tier 1） |
| モニタ発光色でツール種別を表現（pixtuoid） | herdr はツール粒度のイベントを配らない（`AgentStatus` 5 値のみ）ため現状は実現不可。`pane.output_matched` で部分近似は可能だがエージェント依存のパターン整備が必要 | 見送り（将来 herdr がツールイベントを配れば再検討） |
| レイアウトエディタ（Pixel Agents） | 楽しさへの寄与は大きいが Stage 2 には過剰。まず自動レイアウト（§4 の安定ソート + 島）で成立させる | 見送り。将来拡張として state 側にレイアウト上書きの余地だけ確保（§8 設定の拡張点） |
| エージェント種別ごとのキャラ差し替え（Pixel Agents の 6 キャラ） | 低コストで愛着に効く。`agent` フィールド（claude/codex/...）でスプライトを切替えるだけ | **採用・実装済み（issue #6）**: キャラは `theme` とは独立の軸とした（キャラ = 形状、テーマ = 配色。直交させたほうが組合せが増える）。claude / codex / gemini / cursor / droid + 既定キャラ、tier 0/1/2 すべてで有効 |
| boss/サブエージェント階層の可視化（claude-office） | herdr の可視単位はペインであり、サブエージェントはサーバーから見えない。原則（herdr が公開する情報だけで動く、§1 非ゴール）に反する | 対象外 |

ライセンス面: 比較 3 OSS はいずれも MIT であり、設計アイデアの参照は自由。コード・アセットを流用する場合は MIT 表記を継承する。スプライトアセットは自作する（Pixel Agents が用いる JIK-A-4 Metro City 等のサードパーティアセットは、アセット固有ライセンスの確認が必要になるため流用しない）。

## 12. 実装言語（推奨と根拠）

**推奨: Python 3.10+、stdlib のみ**（`socket` + `json` + `selectors`）。

- 根拠: NDJSON over unix socket / named pipe は stdlib で足りる。ピクセルアートは ANSI 文字列生成であり描画ライブラリ不要。`[[build]]` 無しで `plugin install` が完結する（Node は `npm ci`、Rust はビルドが必要）。モック（本リポジトリ）からの連続性も高い。
- Windows: named pipe への接続は **stdlib のみで成立する**（herdr 実機で検証済み。pywin32 も Node fallback も不要）。ただし素朴な `open(HERDR_SOCKET_PATH, ...)` は誤りで、以下の 4 点が必須:
  1. **パス変換**。herdr は `HERDR_SOCKET_PATH` にファイルシステムパスを渡すが、API 本体はその文字列をそのまま名前に持つ named pipe である。`r"\\.\pipe" + "\\" + HERDR_SOCKET_PATH` へ変換して開く。変換を忘れると**例外が出ない**: 同じパスに 25 バイトの `pid:timestamp` マーカーファイルが実在するため `open()` が成功し、その中身が返る。
  2. **接続直後の健全性検査**。上記の silent failure と、`"r+b"` で開いたマーカーファイルへ NDJSON を書いて herdr の生存マーカーを破壊する事故を、`os.fstat` + `stat.S_ISFIFO`（CPython は Windows で `GetFileType` を `st_mode` に写す）で送信前に潰す。追加の ping リクエストは不要。
  3. **`ERROR_PIPE_BUSY` のリトライ**。herdr は待受インスタンスを常に 1 本しか出さないため、2 本目の接続はこの隙間に入って失敗する（実測で約 1/3 が該当）。stdlib では `OSError errno=22 (EINVAL)` / `winerror=None` に化けるので、monotonic な総 deadline 付きの sleep リトライを自前で書く。**このリトライは Windows 専用の open にのみ置く**。共通の `connect()` に置くと unix socket の正当な `EINVAL` を誤ってリトライする。
  4. **socket 形状のアダプタ層**。pipe には `settimeout` がなく、他スレッドが read 中のハンドルを close すると**閉じた側もハングする**（実測）。よってアダプタは blocking read を一切張らず、`PeekNamedPipe`（ctypes、stdlib）で到着済みバイト数を見てからその分だけ読む。これで本物の read timeout が作れ、`close()` はフラグを立てるだけで待機中の reader を解放できる。`sendall` / `recv` / `settimeout` / `close` の 4 つを備えれば subscriber / commander / graphics / notifier は無改修で済む。
  - 残る差分: **送信側の timeout は Windows では再現できない**（サーバがドレインするまで write がブロックする）。送信はいずれも専用スレッド上なので描画ループは止まらない。
- 対抗案 Node.js（`net` が named pipe をネイティブサポート）は不要になった。stdlib で成立したため破棄してよい。

## 13. リスクとオープン事項

| # | 事項 | 影響 | 対応 |
|---|---|---|---|
| 1 | `pane.updated` が status 変化で発火するか未確認 | 接続管理の複雑さ | Stage 2 冒頭で実測（§3）。どちらでも設計は成立 |
| 2 | Python での Windows named pipe 接続 | Windows 対応時期 | **解消**。herdr 実機で stdlib のみで成立を確認し、§12 の 4 点（パス変換 / 健全性検査 / busy リトライ / アダプタ層）として実装済み。Node fallback は破棄 |
| 3 | 大規模フリート（50+ ペイン）での接続 S 張り直しコスト | パフォーマンス | デバウンス済み。実測して問題なら購読を island 単位に分割 |
| 4 | `ui.toast.delivery` 既定 off による「通知が来ない」問い合わせ | UX | README Quick Start + 初回起動時に delivery=off を検出したら画面内に 1 行警告 |
| 5 | 端末リサイズ・小画面での可読性 | UX | 縮小表現（§5）。モックで最小 80x24 を確認 |
| 6 | `pane.graphics.stream` 未提供（0.7.4 / **0.7.5 も未提供** — 調査ノート §4 で再確認） | tier 2 のアニメ | tier 2 は静止スプライト + テキスト合成に限定。stream 提供後に拡張 |
| 7 | done の直接 report 不可（`pane.report_agent` に done が無い） | 外部 report 組織での done 表現 | herdr 検出に委ねる。設計変更不要（表示は enum 準拠） |
| 8 | **[0.7.5]** サーバー再起動でペインの枠は戻るが office プロセスは再実行されない | 再起動後に「ラベルだけの素のシェル」が残る | 事実として記録。対応（`[[startup]]` の採用可否）は未決定 — §3 注記 / Issue #38 |
| 9 | **[0.7.5]** `agent.view` の順序決定が `jump-blocked` / カーソル順と機能重複 | 役割分担が未定 | 事実として記録。§6 注記 / Issue #38。グローバル 1 枠・奪取検知不可のため単純置換はできない |

## 14. Stage 2 への分割案（参考）

1. コア: Subscriber + OfficeState + tier 0/1 Renderer + ジャンプ（Linux/macOS）
2. Escalator + 設定 + 単発 action + state.json
3. Windows 対応 + マーケットプレイス公開（topic 付与、README、スクリーンショット）
4. tier 2（kitty graphics）+ テーマ + エージェント別キャラ — **実装済み（issue #6）**。tier 2 の判定条件は §5 のとおり実測で精緻化した
