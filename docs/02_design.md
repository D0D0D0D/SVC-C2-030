# テンプレート設計仕様 — SVC-C2-030 Lodging Guest Multilingual Concierge Agent

> 本書は Cat 2 実装の設計方針を定義する。

## Position in AgentCore Architecture

- **Agent Class**: `LodgingConciergeGraph`（外側 outer graph）
- **L1 Base**: **AgentBaseGraph**（← `L1 Base` type。`AutonomousBaseGraph` ではない）
- **Inner Graph Base**: **`BaseGraph`**（内側 `ConciergeWorkflowGraph` の継承元。`AgentBaseGraph` ではない）。理由: 内側グラフは外側 `AgentBaseGraph` の backbone gate（S-1〜S-4）で既に保護されており、内側を `AgentBaseGraph` にすると **二重 AgentState・二重 S-gate 実行**になるため。内側は `BaseGraph` を継承し、ドメインノード（`FunctionNode` 群）が個別にゲートを持つ（F-01 対応・2026-07-06）。
- **Marketplace entry adapter**: `cli.py` の `MarketplaceGraph` が runner の context を破棄して `MARKETPLACE_PROPERTY_ID` だけから再構築し、Azure LLM と KB を all-or-nothing で配線する。
- **Cat**: Cat 2（外側は固定 5 ノード backbone、ドメインの複数ステップは `main` スロットの `GraphNode` = 内側 `domain_workflow_graph` に隠蔽）
- **Three-Layer Separation**:
  - **State**: flat TypedDict composition（primitives + JSON-serializable のみ。Pydantic 不可 = msgpack 非互換）
  - **Node**: L1 inheritance（Template Method: `execute(self, state) -> dict` の override のみ。部分 dict 返却）
  - **Graph**: composition（`register_nodes()` でノード登録。外側の `add_edges()` は override しない）

## Architecture Overview

Cat 2 の二層構造:

- **Outer graph（`src/graph/graph.py` = `LodgingConciergeGraph`）**: `AgentBaseGraph` を継承。固定 backbone（initialize → pre_process → main → post_process → finalize）。`main` スロットに `ConciergeWorkflowGraphNode`（`GraphNode` サブクラス）を割り当て、ドメインの複数ステップを内側グラフに委譲する。**`add_edges()` は override しない**。
- **Inner graph（`src/graph/domain_workflow_graph.py` = `ConciergeWorkflowGraph`）**: **`BaseGraph` を継承**（F-01）。宿泊コンシェルジュの多段トポロジ（IntentResolve → KBRetrieve → GroundedAnswer → {MedicalDisclaimerGate | EscalateToFrontDesk}）を実装。`get_output()` で outer に返す構造化結果を確定する。**語調整形（HospitalityLocalize）は内側でなく外側 post_process で行う**（両ルート＝通常回答・エスカレーション回答の双方が post_process を通り、言語一致と S-3 を一括適用する。F-02）。

### Node Configuration（外側 backbone）

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | schema_version / session_id / trust_level 設定 | user_input | session context | InitializeNode (default) |
| pre_process | 入力受領・言語判定（InputParse/LanguageDetect）・入力検証 | user_input | validated_input, detected_language | PreProcessNode（S-2 入力ゲート拡張） |
| main | 内側コンシェルジュ workflow を呼び出す（GraphNode） | validated_input, detected_language, conversation_history | result（構造化回答 or エスカレーションフラグ） | `ConciergeWorkflowGraphNode(GraphNode)` |
| post_process | おもてなし語調で言語整形（HospitalityLocalize）・S-3 他ゲスト情報非開示。**通常回答・エスカレーション回答（フロント誘導文言）の両ルートともここを通り、検出言語に一致させる**（F-02） | result | **formatted_output**（ゲスト向け最終回答） | PostProcessNode（S-3 出力ゲート拡張） |
| finalize | response_metadata / total_time_ms 構築 | formatted_output | response | FinalizeNode (default) |

> **設計上の割り切り**: 言語判定（LanguageDetect）は pre_process に、語調整形（HospitalityLocalize）は post_process に置く。IntentResolve / KBRetrieve / GroundedAnswer / 各ゲートは内側グラフ（`ConciergeWorkflowGraph`）のノードとして実装し、outer backbone は薄く保つ。
>
> **HospitalityLocalize は固定テンプレ集を持たない**: おもてなし語調・敬語・言語一致は **LLM の system prompt 指示**で生成時に担保する（4 言語 × 意図数のテンプレ集を保守しない・§保守アセットの範囲）。post_process は「生成回答を検出言語に一致させ、語調 prompt を適用し、S-3 で他ゲスト情報を非開示化」する。

### Data Flow（外側）

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry, max 3)
                                          pre_process
```

`route()` は main の後に `state["status"]` を読む（`success` → post_process / `retry` → pre_process / `error` → finalize）。**pre → main は無条件辺**であり、main（GraphNode）は上流 `status="error"` を必ずパススルーする。

### Inner Graph トポロジ（`ConciergeWorkflowGraph`）

```
[validated_input, detected_language, conversation_history]
    v
[IntentResolve]  宿泊意図オントロジーへ解決（+health フラグ付与、+escalate-only フラグ）
    v
[KBRetrieve]  施設 KB をベクトル検索（施設事実 / 温泉分析書 / 任意の文化情報）
    v
[GroundedAnswer]  検索結果の事実のみで回答生成（捏造禁止）
    │
    ├─(KB ヒットあり + health フラグ)→ [MedicalDisclaimerGate / S-5]  免責付き・診断なし ─┐
    ├─(KB ヒットあり + 通常)         → そのまま ──────────────────────────────┤
    └─(KB 未収載 / 苦情 / 予約変更)   → [EscalateToFrontDesk]  エスカレーションフラグ ──┤
    v                                                                          │
[get_output()]  outer の merge_output() に渡す構造化結果（3 ルート合流）◄────────────┘
    v
（外側 post_process = HospitalityLocalize / S-3 で全ルートを言語一致・語調整形・非開示化）
```

> **分岐条件**: 分岐は並列でなく **「KB ヒット有無を先に判定」→ ヒット時のみ health フラグで S-5 経由に分岐**する。**KB 未収載は S-5 免責ゲートを通さず直接 `EscalateToFrontDesk`** へ。GroundedAnswer 内で `kb_hit` を先に評価し、`kb_hit and health_flag` のときのみ `MedicalDisclaimerGate` を経由する。

> 🛠 **旅館業法・法令絡み意図の扱い（保守スリム化・2026-07-06 確定）**: 旅館業法準拠の回答テンプレは **我々の保守対象から外す**。法令の定型回答を同梱すると法改正追従の保守義務と誤案内時の法的責任を負うため。法令絡み意図（旅券確認・宿泊者名簿等）は IntentResolve で **escalate 扱い**とし、「詳細はフロントにお問い合わせください」という **エスカレーション誘導文言**で返す。内部書式生成も当然スコープ外。→ moat から「旅館業法テンプレ」を削除。

### State Definition

flat TypedDict（`State(AgentState)`）。会話文脈・言語・意図・KB・エスカレーションを保持する。

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `user_input` | `str` | ゲストの自由文質問 | ✅ |
| `validated_input` | `str` | pre_process 検証後の入力 | ✅ |
| `detected_language` | `str` | 言語タグ（`ja`/`en`/`zh`/`ko`） | ✅ |
| `conversation_history` | `list[dict]` | マルチターン文脈（role/text の JSON）。**PII サニタイズ済み・直近 N ターンで上限**（F-03・下記） | ✅ |
| `resolved_intent` | `str` | 宿泊意図オントロジーの解決結果 | ✅ |
| `health_flag` | `bool` | 温泉健康質問フラグ（S-5 経由判定） | ✅ |
| `escalate_flag` | `bool` | フロントエスカレーション要否 | ✅ |
| `kb_chunks` | `list[dict]` | ベクトル検索でヒットした KB チャンク（参照 ID は保持、実 PII は載せない） | ✅ |
| `kb_hit` | `bool` | KB ヒット有無（分岐の第一判定） | ✅ |
| `result` | `str \| dict` | GroundedAnswer / エスカレーションの構造化結果 | ✅ |
| `formatted_output` | `str` | HospitalityLocalize 後のゲスト向け最終回答（**State 上の最終出力キー**。`AgentBaseGraph.get_output()` が読むのはこのキーで、`state["output"]` は読まれない — API レスポンス側のキー名 `output` と混同しないこと） | ✅ |
| `status` | `str` | `AgentStatus` 値（小文字: success/error/retry） | ✅ |

**State Constraints（必須）:**
- Flat TypedDict のみ（primitives + JSON-serializable）
- State に JWT / API keys / credentials を入れない（checkpoint DB へ平文漏洩）
- 他ゲストの個人情報・予約情報を `kb_chunks` / `result` に載せない（S-3・センシティブ値は参照 ID のみ、resolve は API アダプタで）
- **`conversation_history` の PII 方針（F-03・2026-07-06）**: State は checkpoint DB に永続化されるため、`conversation_history` にゲスト自由文の PII（氏名・部屋番号・個人状況）を平文で溜めない。方針:
  - **長さ上限**: 直近 `history_max_turns`（既定 8 ターン）で truncate。古いターンは破棄。
  - **PII サニタイズ**: pre_process の S-2 ゲート（`_extra_security_gate_input()`）で検出した PII をマスクした `sanitized` テキストを history に格納（生の `user_input` は history に残さない）。
  - **保持は会話文脈の解決に必要な最小限**（追問の指示語解決に足るだけ）。
- InvocationContext は `config["configurable"]` 経由のみ（State に入れない）
- Pydantic / dataclass / 任意 Python オブジェクト不可（msgpack 非互換）

## Framework Utilization

### Shared Components Used
- [x] InvocationContext（correlation_id, session_id, caller_trust_level, hitl_allowed）
- [x] ConnectionPolicy（retry/timeout：max_retry=3, timeout=30s）
- [x] SecurityViolationError
- [x] **S-2**: `_extra_security_gate_input()` — pre_process で入力 PII スキャンに加え、入力サイズ上限・言語検出の妥当性を domain check（`FunctionNode` の `@final _security_gate_input()` は override しない）
- [x] **S-3**: `_extra_security_gate_output()` — post_process で **他ゲスト情報非開示（PII 再チェック）** ＋ **医療免責注記の保全検証（preservation variant）**。health_flag が立っていた回答に免責注記が保全されているかを検証し、欠落時は reject（`@final _security_gate_output()` は override しない）
- [x] **S-4**: `emit_trace_event()` — 各 `execute()` 内で最低 1 回、ドメインイベントを emit（`node_start`/`node_complete`/`node_error` は `__call__()` が自動 emit するため重複させない）
- [x] **S-5**: MedicalDisclaimerGate — 温泉健康質問に免責注記付与・診断回避・医師/フロント誘導（`GroundedAnswer` 後の内側ノードとして実装）

> **S-2/S-3 gate behaviour by node type（ADR-017）:**
> - `FunctionNode` サブクラス（pre_process / post_process / 内側ノード）→ framework の `@final` ゲートが自動実行。domain check は `_extra_security_gate_input/output()` のみで拡張。
> - `GraphNode`（`main` = `ConciergeWorkflowGraphNode`）→ 意図的 no-op（内側ノードのゲートが適用済み）。`_security_gate_input/output()` を no-op passthrough で override。
> - Custom `BaseNode` サブクラス → 使わない（全て `FunctionNode` / `GraphNode` で構成）。

### Composition Pattern

- **Pattern**: **GraphNode（subgraph）** — outer の `main` スロットが内側 `ConciergeWorkflowGraph` を包む
- **Composition target**: `src/graph/domain_workflow_graph.py :: ConciergeWorkflowGraph`
- **Error propagation strategy**: **propagate**（内側の例外は `SubgraphError` として再送出し、fail fast する）
- **GraphNode 契約**: `get_subgraph()`（**内側グラフとその checkpointer を `self._subgraph` にキャッシュ**＝invoke/resume で同一 checkpointer を維持。毎回新規生成は resume で状態喪失し無限ループになる）/ `extract_input()`（`validated_input` を渡す）/ `merge_output()`（内側 `get_output()` の `output`/`status` を outer state の変更キーのみ返す）
- **HITL**: 本テンプレートは壁打ち対話 refine（ゲストの追問・条件変更）を Cat 2 の正常な使い方として想定。初版では `interrupt()` を使わない自動応答を基本とし、対話文脈は `conversation_history` で保持する。将来 HITL を有効化する場合は inner/outer 両方に明示 `compile(checkpointer=)` が必要（D6 interrupt パターン・memory_enabled: true 必須）。**初版では HITL 無効**。

### 内側 route の State 注釈（実装上の注意）

内側グラフの `add_conditional_edges` の分岐関数・`get_output()` は **`state: State`（自 schema）で注釈**する。`state: AgentState` で注釈すると LangGraph が AgentState に射影し、`health_flag`/`kb_hit`/`escalate_flag` 等のカスタム欄が消えて常に既定分岐に落ちる。

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)（`from agenticstar` / `import agenticstar` 禁止）
- [x] Import targets: `framework/` と `shared/` のみ（`agents/base/` は不要）。LLM は `src/services/llm_client.py`（注入シーム）、credential scan は `src/security.py`（wheel に `framework.llm`/`framework.security` は無い）

## KB スキーマ（オープン・運用側投入）

我々はスキーマとゲートを出荷し、KB の中身は運用側（導入ホテル）が投入する。

| ドキュメント種別 | 内容 | 必須 |
|---|---|---|
| `facility_facts` | チェックイン/アウト時刻・Wi-Fi・朝食・駐車・周辺・アメニティ等の施設事実 | ✅ |
| `onsen_analysis`（温泉分析書） | 泉質・成分表・禁忌症・適応症（温泉宿のみ） | 温泉宿で必須 |
| `culture`（任意） | 文化情報・館内マナー等（膨大になり得るため同梱せず運用側裁量） | 任意 |

各チャンクは `{doc_type, source_id, lang, text}` を持ち、`source_id` で出典追跡。ベクトル検索は施設単位に名前空間分離。

### KB 投入・更新の口（エージェントが所有する ingest/upsert）

KB の中身は運用側が用意するが、**その投入・更新経路はエージェント自身が提供する**（客に別ツールでの KB 構築を強いない＝隠れ依存を作らない）。エージェントが KB のデータモデルを設計・管理する。

| モード | エンドポイント | 用途 |
|---|---|---|
| **ingest（バルク投入）** | `src/api/server.py` に `/ingest`（施設単位） | 施設事実・温泉分析書・文化情報の初回一括投入（コールドスタート解消）。`{property_id, doc_type, docs:[{source_id, lang, text}]}` を受け、チャンク化 + embedding + upsert |
| **upsert（差分更新）** | `/ingest` を upsert セマンティクスで再利用（`source_id` キー） | チェックイン時刻変更・新メニュー等の差分更新。同一 `source_id` は上書き、新規は追加 |
| **invoke（質問応答）** | `/invoke` | ゲスト質問への回答（本体フロー） |

- **冪等性**: `(property_id, doc_type, source_id)` を一意キーに upsert。再投入で重複しない。
- **削除**: `source_id` 指定の削除（`/ingest` の `deletes:[source_id]`）で古い施設事実を除去。
- **セキュリティ**: `/ingest` は `required_trust_level: INTERNAL`（運用管理者のみ）。`/invoke` は `VERIFIED_EXTERNAL`（ゲスト）。投入経路とゲスト経路のトラストレベルを分離。
- **スコープ**: 埋め込みモデル・ベクトルストア接続は `src/services/` の注入シームに閉じる。ベクトルストア自体（Qdrant 等）は運用側インフラ想定で、エージェントは接続 + upsert ロジックを提供。

> `/ingest` アダプタ + upsert サービスを提供する。初版は同期 upsert（大量投入の非同期化は後続拡張）。

### Qdrant retrofit と起動時の設定契約

- SDK 1.0.2 で実装済みの `QdrantVectorStore` と `AzureEmbedding` を採用し、両者を同じ
  `AsyncServiceRuntime` に接続する。`KBService(store, embedding, top_k=...)` の既存注入契約は変えない。
- Qdrant の hit は `{id, payload, score, similarity}`、読み側 `KBService` は
  `{id, metadata, text}` を読むため、`HotelKBQdrantAdapter` が `payload` を明示的に写す。
  同 adapter は `(property_id, doc_type, source_id)` の複合 ID を SDK が受理する決定的 UUID に変換し、
  upsert/delete の同一性も保つ。
- 本 AG は request/customer scope の `SCOPE_KEYS` を持たず、顧客内 KB の区画もないため、
  `qdrant-scoped-backend` 部品は使わない。SDK の `namespace + agent_name` 分離と、この AG 固有の
  `property_id` 複合 ID/filter が実際に消費される境界であり、未使用 scope を追加しない。
- `config.vector_store` が未設定または `enabled: false` の場合は、従来どおり LLM/KB の両 seam を
  未配線にして deterministic offline fallback（空 KB → フロントエスカレーション）へ縮退する。
  `enabled: true` の場合は全設定・credential・接続を必須とし、不正なら起動を失敗させる。
- LLM、Qdrant、Azure embedding をすべて構築してから LLM/KB seam を公開する。途中失敗時は runtime を
  close し、`{両方配線}` / `{両方未配線}` 以外の状態を作らない。

## 保守アセットの範囲（スリム化・2026-07-06 確定）

我々が継続保守する範囲を最小化する。過剰な同梱アセットは「作って終わり」でなく運用製品化してしまうため、以下に絞る。

| アセット | 我々の保守 | 方式 |
|---|---|---|
| **意図オントロジー（最小版）** | ✅ 保守（軽い） | エスカすべき種別（苦情・予約変更・法令絡み）の列挙 ＋ 温泉 health フラグが核。網羅的な意図表は持たず、細かな意図分類は LLM few-shot に委ねる |
| **温泉医療免責の定型文** | ✅ 保守（軽い） | 免責注記は固定文言 1 種。分析書の中身は運用側 KB |
| **グラウンディング/反ハルシネーション境界テスト** | ✅ 保守 | 一度書けば回る品質資産 |
| **KB スキーマ + ingest/upsert 口** | ✅ 保守 | 定義 + 投入アダプタ |
| ~~旅館業法準拠テンプレ~~ | ❌ **削除** | 法改正追従・法的責任を負うため。法令絡み意図は escalate 文言へ |
| ~~多言語おもてなし語調テンプレ集~~ | ❌ **持たない** | 固定テンプレ集は 4 言語 × 意図数で膨張。**system prompt の語調指示**（おもてなし・敬語・言語一致）で生成時に担保 |

> **moat は「意図オントロジー最小版 / 温泉免責ゲート / 境界テスト / KB スキーマ+投入口」で成立**。汎用 RAG（VectorRAGAgent）との差別化はこれで十分で、法令・語調テンプレの抱え込みは不要。

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | **AgentBaseGraph** | AutonomousBaseGraph | **AgentBaseGraph** | 固定 5 ノード backbone の Cat 2。自律ループ不要（終了条件は固定パイプライン）。VectorRAG パターン |
| Composition pattern | Standalone | **GraphNode (subgraph)** | **GraphNode** | 多段ドメインワークフロー（IntentResolve〜各ゲート）を `main` に隠蔽。error_strategy=propagate |
| 内側グラフ base | AgentBaseGraph | **BaseGraph** | **BaseGraph** | 内側は外側 backbone gate で保護済み。AgentBaseGraph だと二重 AgentState・二重 S-gate（F-01） |
| 語調整形の位置 | 内側ノード | **外側 post_process** | **外側 post_process** | 通常/エスカレーション両ルートが post_process を通り言語一致・S-3 を一括適用（F-02） |
| KB 分岐（S-5 経由判定） | 並列分岐 | **KB ヒット先判定** | **KB ヒット先判定** | KB 未収載は S-5 を通さず直接エスカレ、ヒット + health のみ S-5 経由 |
| 旅館業法テンプレ | 同梱テンプレ保守 | **保守せず escalate 文言** | **保守せず escalate 文言** | 法改正追従・法的責任を負うため保守対象から削除。法令絡み意図はフロント誘導文言で返す（2026-07-06 ユーザー確定） |
| 多言語おもてなし語調 | 固定テンプレ集 | **system prompt 指示** | **system prompt 指示** | テンプレ集は 4 言語 × 意図数で膨張。語調は生成時に prompt で担保（2026-07-06 ユーザー確定） |
| 意図オントロジー粒度 | 網羅的意図表 | **最小版（エスカ種別+health）** | **最小版** | 細かな意図分類は LLM few-shot に委ね、保守は軽い（2026-07-06 ユーザー確定） |
| KB 投入経路 | 客が別ツールで用意 | **エージェントが ingest/upsert 口を提供** | **エージェントが ingest/upsert 口を提供** | 客に別ツールを強いると隠れ依存で単品導入性が落ちる。エージェントがデータモデルを所有する（2026-07-06 確定） |
| 医療免責の S-3 実装 | filtering | **preservation variant** | **preservation variant** | 免責注記の欠落を reject（安全クリティカル content の保全検証） |
| Vector/embedding 実装 | SDK 1.0.2 の STUB (`PgVectorStore` / `OpenAIEmbedding`) | **実装済み `QdrantVectorStore` / `AzureEmbedding`** | **Qdrant / Azure** | 旧二実装は呼出し時に `NotImplementedError`。既存 `KBService` の store/embedding 分離は維持し、hit/ID 差分だけ adapter に閉じる |
| KB 未設定と設定不正 | すべて縮退 | **未設定/無効のみ縮退、有効時不正は起動失敗** | **明示的な二姿勢** | 意図的な未 provision は既存 offline 契約を維持し、設定したのに壊れている状態は運用障害として fail fast |
| Qdrant scope 部品 | `qdrant-scoped-backend` を追加 | **使わない** | **使わない** | 本 AG は `SCOPE_KEYS` がなく顧客内 KB 区画もない。消費されない scope は隔離保証にならない |
