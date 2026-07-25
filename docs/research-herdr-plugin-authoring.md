# herdr プラグイン authoring 規約 調査ノート

## 計測対象バージョン（読む前に必ず確認）

| 版 | protocol | 実測日 | 本文中の印 |
|---|---|---|---|
| herdr **0.7.4** | 16 | 2026-07-24 | 印なしの ✔ は 0.7.4 実測 |
| herdr **0.7.5** | 17 | 2026-07-25 | **[0.7.5 実測]** と明記 |

`ping` の生応答で確認した版数（0.7.5 実測 ✔）:

```json
{"type":"pong","version":"0.7.5","protocol":17,
 "capabilities":{"live_handoff":true,"detached_server_daemon":true}}
```

0.7.5 で**再実測して結論が変わった**箇所は §2（`[[startup]]` / `min_herdr_version`）、§3（環境変数）、
§7（プラグインの導入スコープ）、および新設の §9（`agent.view.*`）/ §10（sidebar の token 描画）。
0.7.4 のまま**変わっていない**ことを再確認した箇所は §4（graphics）と §5（`AgentStatus`）。
経緯と設計への影響は Issue #38。

- 調査対象: ローカル herdr（`~/.local/bin/herdr`）+ herdr.dev 公式 docs（`/docs/plugins/`, `/docs/socket-api/`）+ GitHub `ogulcancelik/herdr`
- 調査方法: `herdr api schema --json` の全スキーマ精読、`herdr --default-config`、および **稼働中サーバーへのソケット実測**（実測項目には ✔ を付す）
- 0.7.5 の実測環境: WSL2 上の使い捨て headless セッション（検証後 delete 済み）。本番セッションには非破壊の読み取りのみ。
  手順は claude-org-ja `knowledge/raw/2026-07-25-herdr-headless-session-harness.md` と
  同 `2026-07-25-herdr-075-api-measurement.md`（0.7.5 実測で追加した隔離手法）を参照。
- **未実測（推測で埋めていない項目）**: `[[startup]]` の live handoff 時の発火（§2）、
  `agent.view` の mobile / mouse ナビゲーションへの影響（§9）、Windows での挙動全般。

## 1. トランスポートとプロトコル

- **NDJSON（1 行 1 JSON）over ローカルソケット**。Unix では Unix domain socket（`~/.config/herdr/herdr.sock`）、Windows では named pipe。
- リクエスト: `{"id": "...", "method": "...", "params": {...}}` / レスポンス: `{"id", "result"}` または `{"id", "error": {code, message}}`。
- ✔ 通常メソッドは **1 接続 1 リクエスト**（レスポンス後にサーバーが接続を閉じる。同一接続への 2 発目は BrokenPipe）。
- ✔ `events.subscribe` のみ接続が維持され、ack `{"result":{"type":"subscription_started"}}` の後にイベント行がストリームされる。
- `ping` → `{"type":"pong","version":"0.7.4","protocol":16,"capabilities":{"live_handoff":true,"detached_server_daemon":true}}` ✔
- 「プラグイン専用 SDK はない。**herdr CLI / socket API 全体がプラグイン API**」（公式 docs）。CLI サブコマンド（`herdr pane ...` 等）はすべてソケット API の薄いラッパー。

## 2. マニフェスト形式（`herdr-plugin.toml`）

プラグインルート直下の **`herdr-plugin.toml`**（TOML）。スキーマは `plugin.list` レスポンスの `InstalledPluginInfo` で確認できる。

必須トップレベル: `id`（ASCII 英数 + `.:_-`）, `name`, `version`（semver）, **`min_herdr_version`**
任意: `description`, `platforms`（`["linux","macos","windows"]`）

**[0.7.5 実測] `min_herdr_version` は必須になった** ✔。0.7.4 では「docs は必須と書くがスキーマ上は optional
（default `""`）」だったが、0.7.5 では欠落したマニフェストの `plugin.link` が**拒否される**:

```json
{"error":{"code":"invalid_plugin_min_herdr_version",
          "message":"plugin min_herdr_version is required"},"id":"cli:plugin"}
```

リリースノートに記載のない変更である。本リポジトリの `herdr-plugin.toml` は既に宣言済み（`0.7.4`）なので
link は通る。`InstalledPluginInfo` 側は引き続き optional + default `""`（= 既存レジストリの読み戻しは壊れない）。

セクション（すべて配列テーブル）:

| セクション | 必須フィールド | 任意 | 説明 |
|---|---|---|---|
| `[[panes]]` | `id`, `title`, `command`(argv 配列) | `placement`, `width`, `height`, `description`, `platforms` | ペイン entrypoint。`placement` = `overlay`(既定) / `popup` / `split` / `tab` / `zoomed` |
| `[[actions]]` | `id`, `title`, `command` | `contexts`, `description`, `platforms` | `contexts` = `global` / `workspace` / `tab` / `pane` / `selection`。action id はドット不可、グローバルには `{plugin.id}.{action}` に修飾される |
| `[[events]]` | `on`, `command` | `platforms` | イベントフック（単発コマンド起動）。未知のイベント名は **非致命 warning** として `plugin.list` に載る |
| `[[link_handlers]]` | `id`, `title`, `pattern`, `action` | `platforms` | `pattern` は Rust regex、クリックされた URL にマッチ。`action` は同一プラグイン内の action 名 |
| `[[build]]` | `command` | `platforms` | `plugin install` 時のみ実行（確認後）。失敗するとインストール中止。`plugin link` では **スキップ** |
| `[[startup]]` | `command` | `platforms` | サーバー起動後に enabled プラグインごとに 1 回実行。失敗してもサーバーは止まらない。**[0.7.5 実測] スキーマに存在し正式機能** ✔（0.7.4 スキーマには現れなかった。詳細は下記） |

### [0.7.5 実測] `[[startup]]` フックの実測結果 ✔

`[[startup]] command = ["sh", "./startup.sh"]` を持つ使い捨てプラグインを link し、サーバーを再起動して計測した。
`plugin.link` の応答に `"startup":[{"command":["sh","./startup.sh"]}]` がそのまま載り、`warnings` は空
（= 未知セクション扱いではなく正式に解釈されている）。`PluginManifestStartup` は `command`（必須）+ `platforms` のみ。

発火時の実測事実:

- **CWD はプラグインルート**。`HERDR_PLUGIN_EVENT=startup`。
- **ソケットは既に使用可能**（フック内から `herdr pane list` が成功）。
- **セッション復元は完了済み**（フック実行時点で復元されたペインが `pane.list` に出ている）。
- **`plugin.pane.open` がフック内から成功する** ✔ — つまりプラグインは自前のペインを起動時に復帰させられる。
- `HERDR_PLUGIN_CONTEXT_JSON` に `"invocation_source":"startup"`, `"correlation_id":"plugin.startup"` と
  当時の workspace / tab / focused pane が入る。`HERDR_PANE_ID` は**プラグイン自身ではなくフォーカス中のペイン**。
- **disabled のプラグインでは発火しない** ✔（`plugin disable` 後の再起動でフックのログが作られない）。
- 未実測: **live handoff 時の発火**。リリースノートは "after server startup and live handoff" と書くが、
  今回計測したのはサーバー起動のみ。live handoff 経路は測っていない。

**セッション復元との役割分担（設計に直結する実測）** ✔:

| 何が起きるか | 実測結果 |
|---|---|
| プラグイン所有ペインの**枠**（label / cwd / タブ位置） | サーバー再起動後に**自動で復元される** |
| そのペインで動いていた**プラグインのコマンド** | **再実行されない**。復元されたペインには既定シェル（`zsh`）が入る |

`pane.process_info` の実測: 復元後の foreground process は `/usr/bin/zsh` で、マニフェストの
`command`（`sh -c "while true; do sleep 5; done"`）のプロセスは 1 つも生存していない。
つまり**「ペインは戻るがプロセスは戻らない」**。`[[startup]]` はこの穴を埋めるための機構である
（→ design.md §3 / §9 の前提に影響。Issue #38）。

## 3. Entrypoint 規約（実行時環境変数）

プラグインのコマンドはすべて argv 配列で起動され、CWD はプラグインディレクトリ。herdr が注入する環境変数:

- 共通: `HERDR_SOCKET_PATH`, `HERDR_BIN_PATH`, `HERDR_ENV`, `HERDR_PLUGIN_ID`, `HERDR_PLUGIN_ROOT`, `HERDR_PLUGIN_CONFIG_DIR`, `HERDR_PLUGIN_STATE_DIR`, `HERDR_PLUGIN_CONTEXT_JSON`（+ 文脈があれば `HERDR_WORKSPACE_ID`, `HERDR_TAB_ID`, `HERDR_PANE_ID`）
- **[0.7.5 実測] `HERDR_SESSION`**（セッション名。実測値 `HERDR_SESSION=view075`）が追加で注入される ✔。
  0.7.4 の一覧には無かった。セッション名を知りたい用途で自前推測は不要。
- **[0.7.5 実測] `HERDR_PLUGIN_STATE_DIR` は config dir 配下ではない** ✔: `XDG_CONFIG_HOME` を差し替えても
  `~/.local/state/herdr/plugins/<id>` を指した（= XDG state に従う）。config と state は別ルートで動く。
- pane 起動時: `HERDR_PLUGIN_ENTRYPOINT_ID`
- action 起動時: `HERDR_PLUGIN_ACTION_ID`
- event フック: `HERDR_PLUGIN_EVENT`, `HERDR_PLUGIN_EVENT_JSON`（startup フックは `HERDR_PLUGIN_EVENT=startup`）

規約上のポイント:
- ポータビリティのため CLI 呼び出しは `HERDR_BIN_PATH` を使う。
- 資格情報・永続状態は `HERDR_PLUGIN_CONFIG_DIR` / `HERDR_PLUGIN_STATE_DIR` に置く（`HERDR_PLUGIN_ROOT` はアップデートで消え得る）。

## 4. `plugin.pane` と `pane.graphics.set` の制約

### plugin.pane.open（`PluginPaneOpenParams`）
- 必須: `plugin_id`, `entrypoint`（マニフェスト `[[panes]].id`）。
- 任意: `placement`（マニフェスト既定を上書き）, `direction`, `width`/`height`（popup 用; セル数 or `"80%"`）, `target_pane_id`, `workspace_id`, `cwd`, `env`, `focus`。
- **popup はセッションモーダルで pane_id を持たず、pane/agent API の対象外**。常駐 UI には不向き。
- `plugin.pane.focus` / `plugin.pane.close` は `pane_id` 指定。enabled かつプラットフォーム互換のプラグインのみ起動可。

### pane.graphics.*（`pane.graphics.set` / `clear` / `info`）
- ✔ **`[experimental].kitty_graphics = true` でない限り全メソッドが `feature_disabled`** を返す（実測: `{"code":"feature_disabled","message":"pane graphics require experimental.kitty_graphics"}`）。デフォルト設定はコメントアウト（= false）。
- 有効時も **Kitty graphics 対応の外側ターミナルが必要**（config コメント: "Requires a Kitty graphics-compatible outer terminal"）。
- `PaneGraphicsSetParams`: `pane_id`, `format`（`png` / `rgb` / `rgba`）, `image_width`, `image_height`, `data_base64`, `placement`（`viewport_col`/`viewport_row`/`grid_cols`/`grid_rows` = セルグリッドへの配置）。
- 最新 docs には連続フレーム用 `pane.graphics.stream`（1 JSON ヘッダ + 生バイト列、ストリームがペインの graphics レイヤを占有、競合は `stream_conflict`）の記載があるが、**0.7.4 のスキーマには存在しない**（`set`/`clear`/`info` のみ）。
  - **[0.7.5 でも存在しない**（再確認済み）✔。0.7.5 スキーマの method 一覧も `pane.graphics.set` / `.clear` / `.info` の 3 つのみ。
    `pane.graphics.info` は 0.7.5 でも `{"code":"feature_disabled","message":"pane graphics require experimental.kitty_graphics"}` を返す ✔。
    0.7.5 のリリースノートの一文 "Pane graphics streams now shut down cleanly when a client disconnect races stream teardown"
    は**内部機構の修正であり、公開メソッドの追加ではない**（一見 API が来たように読めるので注意）。
    → tier 2 を静止スプライトに限る前提（design.md §5 / §13, `office/graphics.py`）は 0.7.5 でも**正しいまま**。
- 注: 自プラグインのペインは通常のターミナルペインなので、**自ペインへの描画は stdout への ANSI 出力で足りる**。`pane.graphics.set` は「任意ペインの上に画像を重ねる」ための API。

## 5. イベント購読（events.subscribe / events.wait）

`AgentStatus` enum: **`idle` / `working` / `blocked` / `done` / `unknown`**。

購読タイプは 2 系統:

1. **ブロードキャスト系**（`{"type": "..."}` のみでフリート全体を購読可能）:
   `workspace.created/updated/metadata_updated/renamed/moved/closed/focused`, `worktree.created/opened/removed`, `tab.*`, `pane.created/closed/updated/focused/moved/exited/agent_detected`, `layout.updated`。
   ✔ `pane_created` イベントは **PaneInfo 全体**（`agent_status`, `cwd`, `label`, `agent` 等を含む）をペイロードで運ぶ。
2. **パラメータ付き per-pane 系**（`SubscriptionEventKind`）: `pane.output_matched`（`pane_id`+`source`+`match` 必須）, `pane.agent_status_changed`, `pane.scroll_changed`。
   - ✔ `pane.agent_status_changed` は **`pane_id` が必須**。省略すると `invalid_request: missing field pane_id`。全ペイン一括購読は不可 → フリート監視はペインごとに購読を張る必要がある。
   - 任意の `agent_status` フィルタを付けられる（例: `blocked` のみ）✔。
   - ペイロード `PaneAgentStatusChangedEvent`: `pane_id`, `workspace_id`, `agent_status`（必須）+ `agent`, `display_agent`, `title`, `state_labels`。
- 1 回の `events.subscribe` で複数 subscription を同一接続に載せられる ✔。**購読の動的追加 API はない** → メンバー変更時は接続ごと張り直す。
- `events.wait`: `match_event`（`EventMatch`）+ `timeout_ms` の単発ブロッキング待ち。`pane_agent_status_changed` のマッチには `pane_id` と `agent_status` を指定する。
- 関連: エージェント状態は組み込み integration の検出のほか、`pane.report_agent`（`--state idle|working|blocked|unknown`、`done` は直接報告不可）や `pane.report_metadata`（`display_agent`, `state_labels`, カスタム `tokens`, `ttl_ms`）で外部から報告できる。

## 6. notification.show

- `NotificationShowParams`: `title`（必須）, `body`, `position`（`top-left` 等 4 隅; **herdr 内トースト時のみ有効**）, `sound`（`none` / `done` / `request`）。
- 正規化（改行・連続空白の潰し）後、**title は 80 文字、body は 240 文字に切り詰め**。
- レスポンスは `shown` + 理由: `shown` / `disabled` / `rate_limited` / `no_foreground_client` / `busy` — **レートリミットあり**。
- 配信先は `[ui.toast].delivery` = `off`（**デフォルト**）/ `herdr` / `terminal` / `system`。サウンドは `[ui.sound]`（`enabled`, mp3 差し替え, per-agent mute）。
- → プラグインからのトースト escalation は **ユーザーが `ui.toast.delivery` を有効化していることが前提**（README に明記すべき setup 要件）。

## 7. 配布・plugin.link の流儀

### [0.7.5 破壊的変更] 導入スコープはセッション隔離から**ユーザーグローバル**へ

0.7.5 のリリースノート: "Installed and linked plugins, including their enabled state, are now global to the
current user instead of isolated by Herdr session."（0.7.3 で名前付きセッションにのみ入れたプラグインは再導入が必要）

実測で確認した粒度 ✔: レジストリは**セッション単位ではなく config ディレクトリ単位**。
`XDG_CONFIG_HOME` を差し替えた隔離インスタンスに link したプラグインは、その config dir の
`plugin.list` にのみ現れ、本番 config dir の `plugin.list` には現れなかった（本番は `agent-office` 1 件のまま）。
逆に言えば、**同一ユーザー・同一 config dir なら全セッションで共有される**。

帰結:

- `herdr plugin link` / `install` は**全セッションに効く**。「このセッションだけ別バージョンのプラグインで動かす」はできない。
- 開発中の WIP を link すると、人間が使っている本番セッションの office も WIP で動く。
- 検証ハーネスでプラグイン差し替えを隔離したい場合は **`XDG_CONFIG_HOME` を差し替えて herdr インスタンスごと分離する**のが唯一実測できた方法
  （`herdr --session <name>` だけでは分離されない）。ただし unix socket のパス長上限（約 108 バイト）に当たるため、
  差し替え先は `/tmp/xxx` のような短いパスにする必要がある。
- リリースノート "Plugins can now be installed or linked while no Herdr server is running." も同方向の変更。

### 基本（0.7.4 実測、0.7.5 でも維持）

- **開発時**: `herdr plugin link <path> [--disabled]`（API: `plugin.link {path, enabled, source?}`）。build はスキップ。`plugin unlink / enable / disable / list [--json]` で管理。
- **配布**: `herdr plugin install <owner>/<repo>[/subdir] [--ref REF] [--yes]` — **GitHub shorthand のみ**。`git` で clone → 対話端末ではプレビュー表示 → `[[build]]` 実行 → herdr 管理領域に checkout を保存。`source.kind` = `local` | `github`（`owner`/`repo`/`subdir`/`requested_ref`/`resolved_commit` を記録）。
- **マーケットプレイス**: public リポジトリに GitHub topic **`herdr-plugin`** を付けると herdr.dev/plugins に掲載（インデックスは 30 分毎更新）。
- リンク時の警告（マニフェスト不備・未知イベント名等）は非致命で `plugin.list` の `warnings` に載る。
- ログ: プラグイン起動コマンドの記録は `plugin.log.list`（`herdr plugin log list`）で参照可能。

## 8. 設計に効く確定事項まとめ

| 事実 | 出典 | 帰結 |
|---|---|---|
| graphics はデフォルト無効（`feature_disabled`）+ 対応端末必須 | 実測 ✔ | **デフォルト描画はテキストセル（ANSI/Unicode）必須**。Kitty graphics はオプトインの強化 tier |
| 自ペインへは stdout 描画で十分 | docs/構造 | office ペインは普通の TUI として実装できる |
| `pane.agent_status_changed` は per-pane 購読のみ | 実測 ✔ | ライフサイクル購読 + ペイン毎購読の張り直し戦略が必要 |
| 購読接続は張りっぱなし、他は 1 接続 1 リクエスト | 実測 ✔ | 接続管理を分離（購読用長命接続 + コマンド用短命接続） |
| status イベントに `display_agent` / `state_labels` / `title` が同梱 | schema | ネームプレート描画に十分。追加問い合わせ不要 |
| popup placement は pane_id なし | docs | office ペインは `tab` / `split` / `zoomed` を使う |
| notification はレートリミット + デフォルト off | schema/config | escalation は再通知間隔を持ち、setup 手順に delivery 設定を明記 |
| `plugin install` は GitHub shorthand のみ | docs | 公開リポジトリ + topic `herdr-plugin` が配布の前提 |
| **[0.7.5]** セッション復元はペインの枠を戻すが**コマンドは再実行しない** | 実測 ✔ | 常駐プロセスの復帰には `[[startup]]` が必要（§2）。design.md §3 の「自動で復帰する」は誤り |
| **[0.7.5]** `[[startup]]` から `plugin.pane.open` が成功する | 実測 ✔ | 起動時のペイン復帰は API 上可能（§2）。design.md §9 の前提に影響 |
| **[0.7.5]** `agent.view.*` は sidebar と agent キーバインド順を変えるが `agent.list` は変えない | 実測 ✔ | 「フリートの表示順を herdr 側に宣言する」経路が存在する。ただし読み戻し API は無い（§9） |
| **[0.7.5]** agent view はグローバル 1 枠・last-writer-wins・transient | 実測 ✔ | 他プラグインに黙って奪われうる。奪われたことは検知できない（§9） |
| **[0.7.5]** `tokens` は sidebar に描画されるが、**表示位置と装飾はユーザー設定側** | 実測 ✔ | プラグインは値を出すだけ。装飾を送る手段は無い（§10） |
| **[0.7.5]** `min_herdr_version` は必須 | 実測 ✔ | 欠落マニフェストは link 拒否（§2） |
| **[0.7.5]** プラグイン導入はユーザーグローバル | 実測 ✔ | セッション別のプラグイン差し替えは不可。隔離は `XDG_CONFIG_HOME` 単位（§7） |

## 9. [0.7.5 新設] `agent.view.set` / `agent.view.clear` 実測

0.7.5 の追加分: "Added transient declarative Agent view queries through `agent.view.set/clear`; filtered and
sorted views now define sidebar, mobile, mouse, and agent-keybind navigation order."
以下はすべて 0.7.5 の稼働サーバーに対するソケット実測 ✔。

### パラメータ（`AgentViewSetParams`）

| フィールド | 必須 | 内容 |
|---|---|---|
| `source` | ✔ | 宣言元の識別子。**非空、120 文字以内、ASCII 英数 + `:._-`** のみ（違反は `invalid_agent_view`） |
| `label` | | sidebar のヘッダに表示される文字列（実測: 既定の `grouped` 表示が `label` の値に置き換わる） |
| `filter` | | 再帰的な述語ツリー |
| `sort` | | `[{field, order}]`。`order` = `asc`（既定）/ `desc` |

`filter` の演算子は `all` / `any` / `not`（論理結合）と `eq` / `in` / `exists`（比較）。
比較対象の `field` は組み込み **`status` / `workspace_id` / `tab_id` / `pane_id` / `agent` / `seen` / `state_change_seq`**
または `{"token": "<name>"}`（`pane.report_metadata` の任意 token を参照できる ✔）。
`value` は文字列 / 真偽 / 非負整数 / `{"context": "current_workspace_id" | "current_tab_id"}`（実行時文脈への参照 ✔）。
`sort` の `field` は `workspace_order` / `tab_order` / `pane_order` / **`attention`** / `status` / `agent` / `seen` /
`state_change_seq` または `{"token": ...}`。

未知フィールドは `invalid_request`（`data did not match any variant of untagged enum AgentViewField`）で拒否される ✔。

### 応答と観測可能性（ここが設計上いちばん重要）

```
>>> agent.view.set {"source":"agent-office","label":"HANDS UP",
                    "filter":{"op":"eq","field":"status","value":"blocked"}}
<<< {"type":"agent_view","active":true,"source":"agent-office","label":"HANDS UP"}
>>> agent.view.clear {}
<<< {"type":"agent_view","active":false}
```

- **`agent.list` / `pane.list` の結果は view の影響を受けない** ✔。filter を張っても両方とも全エージェントを返した。
  つまり view は**クライアント表示の指示**であり、API から見た真実は常に無加工。
- 効果は**実際に描画に出る** ✔。使い捨てセッションに実クライアントを繋いで sidebar を読み出した実測:

```
# view なし                        # filter status==blocked, label "HANDS UP"
 agents                   grouped   agents                  HANDS UP
 ◉ ao-herdr-075-research · alpha    ◉ ao-herdr-075-research · alpha
 ⠹ ao-herdr-075-research · beta       （codex/beta は消える）
```

- **agent キーバインドの順序も実際に変わる** ✔。`[keys] focus_agent = "alt+1..9"` を設定して `alt+1` を送った実測:

| 状態 | `alt+1` のフォーカス先 |
|---|---|
| view なし | `w1:p2`（claude, spaces 順） |
| `sort: [{field:"agent",order:"desc"}]` | `w1:p3`（codex） |
| `filter: status == working` | `w1:p3`（codex） |

  → **`office/layout.py` のカーソル順や `jump-blocked` の順序決定と機能が重なる領域が herdr 側に生えた**
  （どう扱うかは設計判断。Issue #38 の論点であり本ノートでは決めない）。
- 未実測: リリースノートが挙げる **mobile / mouse のナビゲーション順**への影響。sidebar と agent キーバインドのみ計測した。

### 所有権とライフサイクル（プラグイン相互運用の罠）

- **アクティブな view はサーバー全体で 1 つだけ**。別 `source` の `set` は**エラーにならず既存 view を上書きする**（last-writer-wins）✔。
- `clear {"source": X}` は **X が現在の所有者でなければ何もしない**。応答は現在アクティブな view を返す ✔:

```
>>> agent.view.set   {"source":"plugin-A","label":"A view", ...}   -> active:true, source:plugin-A
>>> agent.view.set   {"source":"plugin-B","label":"B view"}        -> active:true, source:plugin-B  (A は黙って失効)
>>> agent.view.clear {"source":"plugin-A"}   -> active:true, source:plugin-B  (no-op、B は残る)
>>> agent.view.clear {"source":"plugin-B"}   -> active:false
```

- `clear {}`（source 省略）は**所有者を問わず強制解除**する ✔。他プラグインの view を消せる。
- **`agent.view.get` に相当する読み出しメソッドは無い**。自分の view が生きているかを知る唯一の手段は
  「別 source で `clear` を投げて応答の `source` を見る」という副作用付きの手であり、常用できない。
- **transient は文字通り**: サーバー再起動をまたいで残らない ✔（再起動後に別 source で `clear` を投げると `active:false`）。

## 10. [0.7.5 新設] `tokens` の sidebar 描画と装飾の所在

0.7.5 の追加分: "Added per-token foreground, bold, and dim styling to expanded Space and Agent sidebar row layouts."

**要注意: 装飾を決めるのはプラグインではなくユーザーの config である。** リリースノートの一文は
「token が fg / bold / dim を持てる」と読めるが、実測すると API 側の `tokens` は**ただの文字列マップ**で、
装飾フィールドは存在しない ✔:

- `PaneReportMetadataParams.tokens` / `WorkspaceReportMetadataParams.tokens`:
  `additionalProperties: {"type": ["string","null"]}`, **最大 16 個**, キーは `^[A-Za-z0-9_-]{1,32}$`。
  0.7.4 から**変わっていない**（`ttl_ms` は 1..86400000）。
- 装飾は `~/.config/herdr/config.toml` の `[ui.sidebar.agents] rows` / `[ui.sidebar.spaces] rows` で
  **ユーザーが宣言する**。プラグインからは送れない。

### 行レイアウトの実測仕様 ✔

```toml
[ui.sidebar.agents]
row_gap = 0
rows = [
  ["state_icon", "workspace", "tab"],
  ["agent", { token = "$task_id", fg = "#ff0000", bold = true }],
  [{ token = "$phase", dim = true }],
]
```

- 組み込み token は **agents 側**: `state_icon` / `state_text` / `workspace` / `tab` / `pane` / `agent` /
  `terminal_title` / `terminal_title_stripped`、**spaces 側**: `state_icon` / `state_text` / `workspace` /
  `branch` / `git_status`。**両者の語彙は別**で、agents 専用の `tab` を spaces の `rows` に書くと config parse error ✔。
- カスタム token は **`$` 前置が必須** ✔。`$` 無しの `"task_id"` は未知の組み込み扱いで parse error になる。
  装飾付きテーブル形式でも `token = "$task_id"` と書く。
- `fg` は **`#RGB` / `#RRGGBB` のみ** ✔。`fg = "red"` のような名前付き色は parse error
  （`ui.accent` は名前付き色を許すので、ここだけ厳しい）。`bold` / `dim` は真偽値。
- 省略した装飾フィールドは「文脈上の既定」を引き継ぐ（config コメント）。実測でも確認できた（下記）。
- `herdr config check` が上記すべてを検証する（`config: ok` / `config: issues found`）。

### 実際に描画された内容 ✔

`pane.report_metadata {tokens:{task_id:"AO-075", phase:"review"}, display_agent:"Claude/a2"}` と
`workspace.report_metadata {tokens:{jj_status:"3 files changed"}}` を投げ、実クライアントの sidebar を読み出した:

```
 spaces                             <- [ui.sidebar.spaces]
 ● ao-herdr-075-research              row 1: state_icon, workspace
   3 files changed                    row 2: $jj_status  (workspace token)

 agents                   grouped   <- [ui.sidebar.agents]
 ◉ ao-herdr-075-research · alpha      row 1: state_icon, workspace, tab
   Claude/a2 · AO-075                 row 2: agent, $task_id   (pane token)
   review                             row 3: $phase
 ⠹ ao-herdr-075-research · beta
   codex · AO-099
   impl
```

- **同一行の要素は ` · ` で連結される**。
- 組み込み `agent` は **`display_agent` があればそれを使う**（`Claude/a2`）、無ければ生の `agent`（`codex`）✔。
- 状態は行頭の `state_icon` に出る（blocked = `◉`、working = スピナー `⠹` のアニメーション）。
- 装飾はバイト列で確認 ✔。`$task_id` の 1 文字ごとに次の SGR が出ていた:
  `ESC[0;1;2;38;2;255;0;0;49m` = reset + **bold(1)** + dim(2) + **truecolor fg #ff0000(38;2;255;0;0)**。
  config で指定した `fg` / `bold` が適用され、指定しなかった `dim` はその行の文脈既定を引き継いでいる。
- **クライアントは 1 セルずつ「カーソル移動 + SGR + 1 文字」で描く**ため、生出力を文字列検索しても
  `AO-075` のような連続文字列は見つからない（画面スクレイプ時の罠）。

### 設計に効く点

- `tokens` は**フリート横断で見える場所（sidebar）に出せる**。sidebar はタブを跨いで常時見えるため、
  「office ペインを見ていないときの周辺視野」の経路になりうる（design.md §9 / Issue #38 の論点）。
- ただし**ユーザーが `rows` に `$name` を書かないと何も出ない**。プラグイン側から表示を強制できないので、
  この経路を使うなら README で config 例を案内する形になる（`ui.toast.delivery` と同種の setup 要件）。
