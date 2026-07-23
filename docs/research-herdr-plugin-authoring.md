# herdr プラグイン authoring 規約 調査ノート

- 調査対象: ローカル herdr **0.7.4**（protocol 16, `~/.local/bin/herdr`）+ herdr.dev 公式 docs（`/docs/plugins/`, `/docs/socket-api/`）+ GitHub `ogulcancelik/herdr`
- 調査方法: `herdr api schema --json` の全スキーマ精読、`herdr --default-config`、および **稼働中サーバーへのソケット実測**（実測項目には ✔ を付す）
- 調査日: 2026-07-24

## 1. トランスポートとプロトコル

- **NDJSON（1 行 1 JSON）over ローカルソケット**。Unix では Unix domain socket（`~/.config/herdr/herdr.sock`）、Windows では named pipe。
- リクエスト: `{"id": "...", "method": "...", "params": {...}}` / レスポンス: `{"id", "result"}` または `{"id", "error": {code, message}}`。
- ✔ 通常メソッドは **1 接続 1 リクエスト**（レスポンス後にサーバーが接続を閉じる。同一接続への 2 発目は BrokenPipe）。
- ✔ `events.subscribe` のみ接続が維持され、ack `{"result":{"type":"subscription_started"}}` の後にイベント行がストリームされる。
- `ping` → `{"type":"pong","version":"0.7.4","protocol":16,"capabilities":{"live_handoff":true,"detached_server_daemon":true}}` ✔
- 「プラグイン専用 SDK はない。**herdr CLI / socket API 全体がプラグイン API**」（公式 docs）。CLI サブコマンド（`herdr pane ...` 等）はすべてソケット API の薄いラッパー。

## 2. マニフェスト形式（`herdr-plugin.toml`）

プラグインルート直下の **`herdr-plugin.toml`**（TOML）。スキーマは `plugin.list` レスポンスの `InstalledPluginInfo` で確認できる。

必須トップレベル: `id`（ASCII 英数 + `.:_-`）, `name`, `version`（semver）
任意: `min_herdr_version`（herdr.dev docs は必須と記載するが、0.7.4 スキーマ上は optional で default `""`）, `description`, `platforms`（`["linux","macos","windows"]`）

セクション（すべて配列テーブル）:

| セクション | 必須フィールド | 任意 | 説明 |
|---|---|---|---|
| `[[panes]]` | `id`, `title`, `command`(argv 配列) | `placement`, `width`, `height`, `description`, `platforms` | ペイン entrypoint。`placement` = `overlay`(既定) / `popup` / `split` / `tab` / `zoomed` |
| `[[actions]]` | `id`, `title`, `command` | `contexts`, `description`, `platforms` | `contexts` = `global` / `workspace` / `tab` / `pane` / `selection`。action id はドット不可、グローバルには `{plugin.id}.{action}` に修飾される |
| `[[events]]` | `on`, `command` | `platforms` | イベントフック（単発コマンド起動）。未知のイベント名は **非致命 warning** として `plugin.list` に載る |
| `[[link_handlers]]` | `id`, `title`, `pattern`, `action` | `platforms` | `pattern` は Rust regex、クリックされた URL にマッチ。`action` は同一プラグイン内の action 名 |
| `[[build]]` | `command` | `platforms` | `plugin install` 時のみ実行（確認後）。失敗するとインストール中止。`plugin link` では **スキップ** |
| `[[startup]]` | `command` | `platforms` | セッション復元 + ソケット準備完了後に enabled プラグインごとに 1 回実行。失敗してもサーバーは止まらない。**herdr.dev docs のみに記載、0.7.4 スキーマ（InstalledPluginInfo）には現れない**（新しい版の機能の可能性。依存する場合は実機検証すること） |

## 3. Entrypoint 規約（実行時環境変数）

プラグインのコマンドはすべて argv 配列で起動され、CWD はプラグインディレクトリ。herdr が注入する環境変数:

- 共通: `HERDR_SOCKET_PATH`, `HERDR_BIN_PATH`, `HERDR_ENV`, `HERDR_PLUGIN_ID`, `HERDR_PLUGIN_ROOT`, `HERDR_PLUGIN_CONFIG_DIR`, `HERDR_PLUGIN_STATE_DIR`, `HERDR_PLUGIN_CONTEXT_JSON`（+ 文脈があれば `HERDR_WORKSPACE_ID`, `HERDR_TAB_ID`, `HERDR_PANE_ID`）
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
