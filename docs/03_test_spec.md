# Test Specification — SVC-C2-030 Lodging Guest Multilingual Concierge Agent

## AGENTIC STAR Marketplace 実機疎通（2026-08-31・PASS）

AGENTIC STAR DEV 環境の User Page から実際に invoke し、runner Pod の実行結果と応答を確認した。

| 項目 | 実測 |
|---|---|
| 入力 | 「チェックイン前に荷物を預かりたいです。あと、最寄り駅からの行き方と、朝食の時間を教えてください。」 |
| runner Pod | 実行 7.0 秒 / exit 0 / agent コンテナのログ 0 行（正常完了時は出力しない） |
| 応答 | 「恐れ入りますが、この件はフロントにてご案内しております。フロントへお繋ぎいたします。」 |

KB 未投入のため該当情報が無く、推測で回答せず人間（フロント）へエスカレーションする fail-closed 挙動を確認。

KB は未投入のため、確認できたのは「該当情報が無いときに正しくそう返すこと」までである。
KB 投入後の検索精度はこの記録の対象外。


## Test Strategy
- Test types: Unit / Integration / Proof-of-Boundary
- ローカル・CI とも実 wheel（`agenticstar-agentcore==1.0.2`）で実行。LLM / 埋め込み / ベクトルストアは
  注入シーム（`src/services/llm_client`・`src/services/service`）に fake を注入してテストする。

## Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Result |
|-------|------|----------------|--------|
| TC-01 | State contract: flat TypedDict | `State(AgentState)` は primitives + JSON-serializable のみ・Pydantic/dataclass なし。`test_state_safety` で AST 検証 | ✅ PASS |
| TC-02 | 入力検証エラー | 空入力で `status=ERROR`（`PreProcessNode`）。`test_nodes::TestPreProcess::test_empty_input_errors` | ✅ PASS |
| TC-03 | No JWT/Credential in State/src | CI の credential スキャン: 0 violations | ✅ PASS |
| TC-04 | InvocationContext via configurable only | State に格納しない（`src/schemas/state.py`）。`input_context` のみ流通 | ✅ PASS |
| TC-05 | S-4: no duplicate lifecycle events in `execute()` | `node_start`/`node_complete`/`node_error` を execute 内で emit しない（ドメインイベントのみ） | ✅ 0 duplicates |
| TC-06 | S-2: `_security_gate_input()` 非 override | `@final`。ドメイン拡張は `_extra_security_gate_input()`（`PreProcessNode`） | ✅ PASS |
| TC-07 | S-3: `_security_gate_output()` 非 override | `@final`。ドメイン拡張は `_extra_security_gate_output()`（`PostProcessNode`） | ✅ PASS |
| TC-08 | `required_trust_level` 宣言・強制 | 全 `FunctionNode` が明示宣言（CI の trust-level チェック PASS）。guest ノードは `VERIFIED_EXTERNAL`。PB-6 の `test_s1_denial_refuses_execution_before_execute` が `execute()` が呼ばれなかったことを証明する | ✅ PASS |
| TC-09 | S-2 `_extra_security_gate_input()` 非自明 | 入力サイズ上限チェック（`PreProcessNode`）。PII は framework `@final` gate + history サニタイズ | ✅ 非自明 |
| TC-10 | S-3 `_extra_security_gate_output()` 非自明 | 他ゲスト PII 非開示 + 医療免責注記の保全検証（preservation variant）。`PostProcessNode` | ✅ 非自明 |
| TC-11 | S-4: 各 `execute()` に ≥1 ドメイン `emit_trace_event()` | 全ノードで emit（`concierge_*` イベント） | ✅ ≥1 per node |

## Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Test | Expected Result | Result |
|-------|----------|------|----------------|--------|
| PB-1 | BaseNode → AuditLogger | `emit_trace_event()` が発火 | No silent failures | ✅ |
| PB-2 | State serialization | invoke 後の State は primitives のみ | No Pydantic/dataclass | ✅ `test_state_safety` |
| PB-3 | L1 → 外部サービス | ベクトル検索・LLM は注入シーム経由（本番は実クライアント、テストは fake） | 検索・生成が動作 | ✅ integration |
| PB-4 | Import isolation | Level 0 を import しない | AST scan: 0 violations | ✅ `test_import_isolation` |
| PB-5 | Checkpoint safety | `conversation_history` に生 PII を残さない・履歴上限 | 検査 PASS | ✅ `test_pii_checkpoint_safety` |
| PB-6 | Invoke execution order | S-1 → node_start → S-2 → execute → S-3 → node_complete | 順序検証 PASS | ✅ `test_pb_invoke_order` |

## Business Logic Tests（ドメイン境界）

| TC-ID | Test | Input | Expected Result | Result |
|-------|------|-------|----------------|--------|
| BL-01 | 通常グラウンディング回答 | 施設KBヒットありの質問 | KB事実に基づく回答 | ✅ `test_concierge_flow::test_normal_grounded_answer` |
| BL-02 | 反ハルシネーション（KB未収載） | KB空の質問（LLMは誤答を返す設定） | 誤答を出さずフロントへエスカレ | ✅ `test_grounding_boundary::test_not_in_kb_never_fabricates` |
| BL-03 | NO_ANSWER トークンでエスカレ | KBヒットだが回答不能 | エスカレーション | ✅ `test_grounding_boundary::test_llm_no_answer_token_escalates` |
| BL-04 | 苦情 → エスカレ | 苦情意図 | フロント誘導文言 | ✅ `test_concierge_flow::test_complaint_escalates_to_front_desk` |
| BL-05 | 温泉健康質問 → 医療免責 | 温泉分析書ヒット + health意図 | 免責注記付き回答（S-5） | ✅ `test_concierge_flow::test_onsen_health_gets_disclaimer` |
| BL-06 | 免責保全（S-3） | health 回答から免責注記が欠落 | S-3 gate が reject | ✅ `test_grounding_boundary::test_s3_gate_blocks_dropped_disclaimer` |
| BL-11 | 分岐優先順（escalate > health） | escalate_flag と health_flag が同時に立つ | escalate を優先（S-5 を通さずフロントへ） | ✅ `test_nodes::TestInnerRoute::test_escalate_wins_over_health` |

> **免責保全の多層防御（I-01）**: `post_process` の LLM localize が免責 marker を落とした場合、まず raw result（marker を含む）で上書きするフォールバックが働く。ただしこれが silent に skip される稀ケース（localize 前に既に marker が欠落）でも、**S-3 gate（`_extra_security_gate_output`）が ultimate backstop** として health 出力の marker 欠落を検知し reject する。フォールバックは利便性、S-3 gate が最終的な安全保証。BL-06 が後者を証明する。
| BL-07 | 言語判定 | JP/EN/ZH/KO 各入力 | 正しい言語タグ | ✅ `test_nodes::test_language_detection` |
| BL-08 | KB ingest/upsert 冪等 | 同一 source_id 再投入 | 冪等キーで上書き | ✅ `test_kb_service::TestUpsert` |
| BL-09 | KB クロスプロパティ漏洩防止 | 他施設のヒット | property_id で除外 | ✅ `test_kb_service::test_cross_property_leak_guard` |
| BL-10 | PII サニタイズ（履歴） | email/phone 含む入力 | 履歴に生 PII を残さない | ✅ `test_pii_checkpoint_safety` |
| BL-12 | Qdrant hit 形状変換 | `{id,payload,score,similarity}` | `metadata/text` に写され `KBService` が本文を返す | ✅ `test_hotel_kb_qdrant_adapter` |
| BL-13 | Qdrant 書込 ID 変換 | `property::type::source` | upsert/delete が同じ決定的 UUID を使う | ✅ `test_hotel_kb_qdrant_adapter` |
| BL-14 | 起動配線の原子性 | embedding 構築失敗 | LLM/KB の片方だけを公開せず runtime close | ✅ `test_server_dependency_wiring` |
| BL-15 | 設定姿勢 | 未設定/無効、および有効だが不正 | 前者は既存縮退、後者は起動失敗 | ✅ `test_server_dependency_wiring` |
| BL-16 | deployment SDK 版一致 | CI / local STG の実経路 | 完全 extras + `==1.0.2` が一致 | — 廃止（CI 設定を scaffold の共通定義に切り替えたため廃止。版は共通定義が一元管理する） |

## Test Execution Summary
- Total tests: 83（unit + integration + proof_of_boundary）
- Pass: 81 / Fail: 0 / Skip: 2（PB-7 auto-waived — non-HITL）
- 実行: `pytest tests -q -p no:cacheprovider --no-header -rs`（SDK 1.0.2 環境）

## 実 LLM 1 回転検証（generation_mode: "llm" の実測）

`config/agent.yaml` は `generation_mode: "llm"` を宣言している。実 LLM 1 回転で宣言と実装の一致を
確認したところ、**3 件の欠陥が見つかった**（いずれもモックでは検出不能）。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-20 |
| SDK | `agenticstar-agentcore==1.0.1` |
| 使用 LLM | Azure OpenAI のモデル（`BaseLLM` 実装を検証用に注入） |
| 注入シーム | `set_default_llm` / `get_llm`（本番と同一。`src/api/server.py` が起動時に呼ぶ process-wide シーム） |
| LLM 呼び出し回数 | **3**（intent_resolve / grounded_answer / post_process のローカライズ） |
| 結果（修正後） | ✅ `status=success`、`output` に**ローカライズ済み**本文 |
| 非空だった出力キー | `output`（テキスト出力・59字） |
| 入力 | `case_payload` = `"What time is check-in?"`、`live_input_context` = `{"property_id": "hotelA"}`（`test_concierge_flow.py::test_normal_grounded_answer` と同一） |

### 実 LLM で見つかった欠陥

**欠陥1 — ローカライズのプロンプトが出力契約を伝えていない（S-3 で全体が error）**

実 LLM がローカライズで `"Dear Guest, kindly note that check-in is at 15:00."` を返した。
`detect_pii` が `"Dear Guest"` を `type: name` と判定し、S-3 非開示ゲートが正しく遮断 → invoke 全体が
`status=error`。原因は `localization_system_prompt` が "appropriate honorifics" とだけ指示し、
**冒頭の呼びかけ（vocative）を禁じていなかった**こと。共有 fake は整形済み文字列を返すため統合テストでは出ない。

→ 修正: プロンプトで**冒頭の呼びかけのみ**を禁止（`Dear ...` / 尊敬的宾客 / 客户先生 / 고객님 / 손님 /
お客様・様・さん の呼びかけ・個人名）。通常の丁寧な二人称（you / 您 等）は F-02 のおもてなし調に必要なので
禁止していない。**ゲートは一切変更していない。**

**欠陥2 — ローカライズ結果が丸ごと捨てられていた（F-02 が実は未達）**

`AgentBaseGraph.get_output()` は `state["formatted_output"] or state["result"]` を返し、
**`state["output"]` を読まない**。`PostProcessNode` は `{"output": localized}` だけを書いていたため、
LLM 呼び出し 1 回分のローカライズ結果は常に破棄され、呼び出し元は内側の未ローカライズ `result` を
受け取っていた。`src/schemas/state.py` / `docs/02_design.md` は `output` を「最終出力キー」と
記載していたが、フレームワークはそれを読まない。

共有 fake がローカライズ呼び出しで入力をそのままエコーしていたため、
`assert "15:00" in out["output"]` は**ローカライズが届いていなくても通り**、既存テストは全部緑だった。

→ 修正: `formatted_output` を書く（同系テンプレート共通の形）。fake も `LOCALIZED_MARKER` を付けて観測可能にし、
ローカライズが届いていることを実グラフで assert するテストを追加。docs/02 と State のコメントも実態に修正。

**欠陥3 — S-3 が遮断した本文が、フレームワークのフォールバックで公開されていた**

S-3 が公開を拒否して raise すると `formatted_output` は未書き込みのまま残り、
`get_output()` の `formatted_output or result` が **`state["result"]`（未ゲートの内側回答）に
フォールバック**する。PII を含む grounded answer で実測したところ、`status=error` にもかかわらず
**PII 本文が呼び出し元に返っていた**。空のローカライズ結果（`""` は falsy）でも同じ経路が発火し、
そちらは `status=success` で返る。

→ 修正: `LodgingConciergeGraph.get_output()` を override し、**S-3 を通過した `formatted_output` だけを
公開**する（success 以外は payload なし）。加えて空のローカライズ結果は S-3 の**手前**で grounded answer に
差し替え、差し替えた本文自体もゲートを通るようにした。

### 採らなかったレビュー指摘（理由つき）

初回の修正では「ローカライズ結果に PII が出たら grounded answer にフォールバックして success を返す」
という緩和を入れていたが、**レビュー指摘を受けて撤回した**。撤回理由（自分で再現して確認済み）:

```
detect_pii("山田太郎にご連絡ください。")            == []          # 裸の CJK 氏名は未検出
detect_pii("Dear Guest, 山田太郎にご連絡ください。") == ["Dear Guest"]
```

フォールバックがあると、`"Dear Guest"` だけを検出して**未検出の氏名を含む本文へ差し替えて公開**して
しまう。ゲートが遮断しようとしていた応答を、検出器の盲点を洗濯して通す形になる。
**PII 検出は fail-closed が正しい**と判断し、緩和を削除。常用経路（呼びかけの付与）はプロンプト側で塞ぐ。
この漏洩は `TestPIIFindingFailsClosed` で回帰テスト化した。

### 追加テスト

| テスト | 何を守るか |
|---|---|
| `tests/integration/test_concierge_flow.py::TestLocalizedAnswerReachesTheCaller` | ローカライズ済み本文が実グラフ経由で呼び出し元に届くこと（通常・エスカレーションの両ルート） |
| `tests/integration/test_concierge_flow.py::TestOnlyTheGatedTextIsPublished` | S-3 遮断時・空ローカライズ時に未ゲートの `result` を公開しないこと |
| `tests/integration/test_concierge_flow.py::TestPIIFindingFailsClosed` | PII 検出時に fail-closed すること／検出器が裸の CJK 氏名に盲目であるという前提自体も検証 |
| `tests/unit/test_nodes.py::TestOuterGetOutputPublishesOnlyGatedText` | 出力境界の不変条件（公開してよいのは `formatted_output` だけ） |

### 未解決（正直な記録）

- **プロンプト文言そのものは CI で回帰検出できない。** 呼びかけ禁止の一文を削除しても全テストは緑のまま
  （プロンプト文字列を assert するのは定数 assert であり空虚）。実 LLM 実行でのみ検証可能。
  CI 側の防御は「S-3 が fail-closed であること」＋「未ゲート本文を公開しないこと」で担保している。
- **F-02（言語一致）はローカライズ失敗時に満たされない**: 既存の `except` フォールバックと免責保全
  フォールバックは未ローカライズの `result` を success で返す。`grounded_answer` は回答言語を指定して
  いないため、非英語の問い合わせで英語回答が返り得る。本改版の範囲外（欠陥2 の修正により、
  そもそも今まで誰もローカライズ結果を受け取れていなかった状態からは改善）。別途の判断を要する。
