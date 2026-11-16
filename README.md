# 跨境出口申报与到账事实对账数据服务

外贸财务月末核对一笔出口业务时，发票有多行商品、报关单经历海关更正、税务回执按申报批次返回、
银行以一笔合并流水到账。本服务在**行级别**回答：哪部分出口事实已被申报、审核和收款证据支持，
哪些金额只是候选关联、待分摊或相互矛盾，并把每一条可审计结论追溯到来源文件。

**本服务只交付事实，不代办业务**：不提交报关或退税申报，不操作库存、税务余额或银行资金，
不生成付款、授信或税务处理指令。

技术栈：Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 18（后端服务，带完整 OpenAPI 3.1 与 Swagger UI）。

---

## 1. 一键启动

### 1.1 Docker Compose（推荐）

```bash
docker compose up --build
```

- API：http://127.0.0.1:8000
- Swagger UI：http://127.0.0.1:8000/docs
- OpenAPI：http://127.0.0.1:8000/openapi.json
- 容器启动时自动等待 PostgreSQL、建表、播种演示数据（已存在则跳过）。

### 1.2 本地开发（容器内已装运行时）

```bash
/opt/python-3.12/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 本地 PostgreSQL 已在 127.0.0.1:5432 运行（库/用户/密码均为 ccdev）
.venv/bin/python scripts/wait_for_db.py
.venv/bin/python scripts/seed.py --reset          # 建表 + 演示数据 + 打印账号
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

覆盖数据库连接（默认值即为本地开发库）：

```bash
export DATABASE_URL='postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev'
```

---

## 2. 测试账号与 API Key

所有 `/api/v1/**` 接口都需要请求头 `X-API-Key: <key>`。Swagger UI 右上角 **Authorize** 填入同一个 key 即可试调。
`scripts/seed.py` 每次运行都会打印下列账号：

| 用户名 | 角色 | API Key | 授权主体 |
| --- | --- | --- | --- |
| `admin` | admin | `demo-admin-key` | 全部主体，可建主体/用户 |
| `analyst1` | analyst | `demo-analyst-1-key` | `ENT-A` |
| `analyst2` | analyst | `demo-analyst-2-key` | `ENT-A`（用于并发确认场景） |
| `entity-b-analyst` | analyst | `demo-analyst-b-key` | `ENT-B`（用于跨主体拒绝验证） |
| `viewer` | viewer | `demo-viewer-key` | `ENT-A`、`ENT-B`，只读 |

角色语义：`admin` 全权；`analyst` 可在授权主体内写；`viewer` 只读。
任何越权访问返回 **403**，缺少/错误 key 返回 **401**。

---

## 3. 领域模型（要点）

| 表 | 作用 |
| --- | --- |
| `source_documents` | **来源受理账**，append-only。保存 9 类来源（发票、装箱单、报关档案/更正、税务回执、银行流水、主体目录、合同、退运凭证、其他）的原文位置、事件时间、接收时间、外部编号、币种、金额、数量、SHA-256、来源版本、修订指向。 |
| `invoice_lines` / `customs_lines` / `packing_lines` / `tax_lines` / `bank_transactions` | 文档行。`customs_lines.revises_id` 构成**修订链**，`is_current` 标记当前有效行，`changed_fields` 记录变更前后值。 |
| `export_lines` | **原出口行**：发票行与当前报关行的规范合并行，是行级对账的单位。 |
| `declaration_ledgers` | **申报状态账**：`draft / submitted / verified / rejected / correction_pending`。 |
| `receipt_ledgers` | **到账状态账**：`unmatched / pending_allocation / partial / matched / conflict`。 |
| `balances` + `balance_holds` | **余额控制器**：银行流水侧与原出口行侧各一行（`total = committed + available`，CHECK 约束），每次占用/冻结/释放/转移都写入 `balance_holds` 流水。 |
| `dossiers` / `dossier_members` | 行级身份解析器产出的**带证据等级（A/B/C）的候选案卷**。 |
| `reconciliation_candidates` | 候选关联（`unique_reference` 或 `constrained_allocation`），**不占用余额**。 |
| `reconciliation_lines` | **已证实对账行**，只能由余额控制器创建；状态 `active / needs_review / voided / reassigned`。 |
| `difference_cases` | 差异原因（超额尝试、回执乱序、更正后复核、占用超更正金额、装箱数量不符…）。 |
| `compile_runs` | 事实编译快照，保存已证实事实、未分摊余额与差异原因。 |
| `party_profiles` / `party_name_changes` / `party_control_relations` | **主体档案册**：法人主体/分支机构、统一社会标识、有效期、控制关系、名称变更（需 USCC 或官方继承文件核验）。 |
| `authorization_credentials` / `credential_revocations` | **授权凭证册**：委托出口、报关授权、货权转让、集团内部协议；签发方、授权范围、商品/地区限制、生效终止日、接收时间、原文哈希、替代关系与撤销通知。 |
| `audit_cases` / `audit_evidence_links` | **审计案卷**与**证据关系图**：从发票行+报关商品行建案，锚定发票/货权/报关主体与合同、装箱、运输、货权证据的时间区间主张。 |
| `audit_conclusions` / `conclusion_credential_refs` | **审计结论册**：每个出口行的版本化结论、分段、见证链、冻结版本与凭证依赖索引。 |
| `audit_shared_change_events` / `audit_review_events` | 共享凭证变动事件（定位/暂停/逐行重判）与复审历史。 |

**两个状态账不共享任何已结清字段**：`declaration_ledgers` 与 `receipt_ledgers` 除主键/版本/更新时间外无同名业务列，
状态词表互斥，且都不存在 `settled` 字段；结清事实只存在于 `reconciliation_lines`。`tests/test_acceptance.py::test_settlement_ledgers_do_not_share_settled_fields` 在 schema 层断言这一点。

### 处理顺序（强约束）

1. **先确认唯一关联**：付款附言含 `SKU-xxxx` / `INV-.../n` / `DECL-.../L<n>` 等行级唯一键时，直接建立关联，速度快、复核少。
2. **再对合并流水生成人工确认候选**：无唯一批次键时，按主体 + 币种 + 合同 + 出口日期（≤ 到账日）+ 已审核税务回执列出受约束候选。
3. **未确认金额永远留在待分摊余额**中；**绝不**按金额比例或行数自动分摊。
4. **并发确认**时控制器在同一事务中按 `(side, ref_id)` 字典序 `FOR UPDATE` 锁定两侧余额行，
   校验 `amount ≤ min(银行可用, 出口行可用)` 后原子占用；任一越界返回 409 并记录 `over_allocation_attempt`。
5. **已确认关联进入复审**（海关更正或人工复审）时：占用金额**冻结不释放**，新的编译运行不再把它计入已证实到账量；
   只有 `void`（释放）或 `reassign`（转移）才按 `balance_holds` 审计记录变动余额。

### 其余规则

- **海关更正**：原行与更正行通过 `revises_id` 连接为修订链；已提交/已审核行转为 `correction_pending`，
  旧审核记录通过 `declaration_events` 仍可回查。更正**不会改写**受理账中已提交的原始金额。
  若更正后申报金额低于已冻结占用，余额上限保持为已占用金额（`available = 0`）并生成
  `occupancy_exceeds_corrected_amount` 差异；撤销/改配后上限回落到更正金额。
- **税务回执乱序**：以签发时间为主序、可验证序号为同序时的次序依据；乱序到达但签发更早的回执按签发位置生效。
  次序无法证明（签发时间相同且无可用序号）时进入**回执冲突**，不推进申报状态；若此前已据此转换状态，转换被撤回并留痕。
- **退运凭证**：比较凭证生效时间、运输时序与原行证据；接收时间不足以证明出口日不存在资格。
  无法唯一归属时保留为**反向数量候选**（`reverse_candidates`），不改变申报数量、金额和已证实到账量。
- **装箱单**：仅佐证数量，从不引入金额；数量与申报不符时生成 `packing_quantity_mismatch`。

---

## 4. 跨主体出口证据链一致性与授权时效审计

开票主体、货权主体、报关主体不一致并不必然错误。本服务在**出口日**逐段判定这些主体关系、
授权链与商品数量能否共同支撑同一条出口事实，判断依据是文件在出口日的**实际生效状态与覆盖范围**，
而不是文件何时被系统接收。它只产出可展开的审计结论，不代办申报、不创建调拨或库存、
不处理税务余额、银行资金或付款。

### 4.1 结构与判定规则

**三层结构**：主体档案册 → 授权凭证册 → 证据关系图/审计结论册。

- **主体档案册**：法人主体与分支机构、统一社会标识（USCC）、有效期、控制关系、名称变更。
  资格判定只看出口日的有效期，**不使用当前状态**。名称变更必须由 USCC 或官方继承文件核验，
  仅凭名称相似不得归并；旧名称出现在报关档案中时，审计案卷需指定 `recorded_names`，
  身份核验失败即判为 `party_conflict`。
- **授权凭证册**：`entrusted_export` / `customs_authorization` / `title_transfer` /
  `intragroup_agreement` / `other`。每份凭证独立记录 `issued_at`、`received_time`、
  `effective_from/to`、商品/HS/地区/报关主体范围、数量上限、原文哈希、`supersedes` 替代关系。
- **证据关系图**：主体节点 + 带时间区间的授权边；每行结论仍冻结一条**具体见证链**及数量区间。

**资格判定器**（`app/services/audit.py::evaluate_case`）在出口日检查：

1. 三个主体当日有效（有效期区间，而非当前状态）；
2. 报关主体身份可由 USCC/继承文件核验；
3. 货权链连续：发票主体→货权主体→报关主体（可多路径）；
4. 授权有效且覆盖：商品/HS/地区/报关主体范围 + 出口日生效 + 未被有效撤销；
5. 数量证据一致：装箱/运输/货权证据覆盖不足时整行不得通过。

**结论状态**（`audit_conclusions.state`）：
`cross_entity_chain_confirmed` / `restricted_confirmation` / `authorization_insufficient` /
`title_chain_broken` / `party_conflict` / `pending_evidence`；复审期间旧结论为 `under_review`。

**关键规则**：

- **出口后接收、出口日前已生效**：可判为已证实，但 `usable_for_new_dossier=false`，
  复核核准后（`POST /audit-cases/{id}/review`）才可被新对账案卷引用。
- **出口后才签发且无可验证追溯范围**：只能 `pending_evidence`；即使复核核准也不能回填出口日资格。
  必须 `retroactive=true` 且 `retroactive_scope_verified=true` 才按追溯范围生效（创建时校验）。
- **部分货权覆盖**：整行拆为 `restricted_confirmation`（有覆盖部分）+ `title_chain_broken`（剩余部分），
  绝不因部分数量有证据而整行通过。
- **多条路径的同一货权份额只算一次（max，不求和）**；授权链绕回已访问主体被拒绝，不会凭空增加覆盖量。
- **路径缺少必要授权范围/被冻结/后补签发** → 仅为 `pending_evidence` 候选；**搜索被截断**
  （`MAX_DEPTH`/`MAX_PATHS`）也只给 `pending_evidence`，不得断言不存在有效授权。
- **互斥撤销**：两份不同签发方、相同生效日或相同序号的撤销通知 → 凭证 `frozen`，
  资格判定器不得任选一份形成已证实状态。
- **共享变动传播**：撤销/替换/范围修订产生 `audit_shared_change_events`，先定位所有依赖该凭证的出口行，
  再逐行重新判定；**只有失去有效见证或依据变化**的行被暂停（`paused_case_ids`），
  其余行保持原结论与冻结版本（`unchanged_case_ids`）。处理某行不复制其结论到其他行。
  依赖索引按行记录：已证实行只依赖其见证链上的凭证；未证实行还依赖它考虑过但被阻断的凭证，
  以便这些凭证被替换或修订时该行能被重新判定。
- **后补材料**（`POST /audit-cases/{id}/supplements`）改变关键时间区间时，当前结论置为复审中并暂停新案卷引用；
  历史结论保留冻结版本与依据。撤销复审或覆盖不足时转为受限/断链，**不把数量转移给其他主体**。

### 4.2 样例数据（seed 已包含，3 行各展示一种结论）

| 出口行 | 场景 | 结论 |
| --- | --- | --- |
| `SKU-AA-100` 100 单位 | 授权签发/生效于出口日前、**接收于出口后** | `cross_entity_chain_confirmed`；复核核准前 `usable_for_new_dossier=false`（已证实但暂停新案卷引用） |
| `SKU-BB-200` 50 单位 | 授权**出口后才签发**且无追溯范围 | `pending_evidence`（复核核准也不回填） |
| `SKU-CC-300` 20 单位 | 货权转让仅覆盖 8 单位 | `restricted_confirmation` 8 + `title_chain_broken` 12 |

> seed 完成后 `held_export_lines` 为出口行 1、2：它们分别是"待复核"和"待补证"，按规则暂停新对账案卷引用；
> 复核核准出口行 1 后其暂停即解除（见 4.3 第③步）。

### 4.3 手工验证（seed 数据已就绪）

```bash
K='X-API-Key: demo-analyst-1-key'
B=http://127.0.0.1:8000

# ① 审计案卷列表与三种结论
curl -s -H "$K" $B/api/v1/entities/ENT-A/audit-cases
curl -s -H "$K" $B/api/v1/entities/ENT-A/audit-conclusions

# ② 单行全展开：主体档案、授权文本、生效/接收时间、货权/运输证据、商品地区范围、冻结版本、复审历史
curl -s -H "$K" $B/api/v1/audit-cases/1

# ③ 出口后接收但出口日前已生效：先不可引用，复核核准后可引用
curl -s -X POST -H "$K" -H 'Content-Type: application/json' \
  -d '{"action":"approve","note":"原始签署件已核验"}' $B/api/v1/audit-cases/1/review

# ④ 出口后签发且无追溯依据：复核核准也不改变
curl -s -X POST -H "$K" -H 'Content-Type: application/json' \
  -d '{"action":"approve","note":"晚到文件"}' $B/api/v1/audit-cases/2/review

# ⑤ 部分货权覆盖：拆为受限确认 + 货权断链两段
curl -s -H "$K" $B/api/v1/audit-cases/3 | python3 -c \
  'import json,sys; c=json.load(sys.stdin)["current_conclusion"]; print(c["state"], c["segments"])'

# ⑥ 共享授权范围修订：定位依赖行、仅暂停失去有效见证的行、逐行重判
CA=$(curl -s -H "$K" $B/api/v1/entities/ENT-A/credentials \
     | python3 -c 'import json,sys; print([c["id"] for c in json.load(sys.stdin) if c["credential_no"]=="CA-3"][0])')
curl -s -X POST -H "$K" -H 'Content-Type: application/json' \
  -d '{"scope":{"regions":["CN"],"skus":["SKU-XX"]},"quantity_limit":"10"}' \
  $B/api/v1/credentials/$CA/scope-revision
curl -s -H "$K" $B/api/v1/credentials/$CA/shared-changes
curl -s -H "$K" $B/api/v1/audit-cases/3/conclusions      # 历史结论保留冻结版本

# ⑦ 复审/暂停后新对账案卷被拦截（resolver 记录 audit_hold_new_dossier 差异）
curl -s -X POST -H "$K" $B/api/v1/entities/ENT-A/resolve
curl -s -H "$K" $B/api/v1/entities/ENT-A/difference-cases
```

### 4.4 审计验收测试（25 项）

```bash
.venv/bin/python -m pytest tests/test_audit.py -q
```

覆盖：出口后接收但出口日前生效（复核后可用）、出口后签发无追溯依据（只待补证）、
可验证追溯范围、追溯标记缺范围被拒、部分货权覆盖拆段、装箱数量不足阻止整行通过、
旧名称必须经 USCC 核验（相似名不得归并）、USCC 核验后旧名称可解析、
互斥撤销冻结授权链、双路径同一份额只算一次、回环不增加覆盖、后补材料触发复审并暂停新案卷、
共享范围修订逐行重判且仅暂停失去见证的行、共享撤销不影响有独立见证的行、结论全展开含冻结版本、
跨主体读取拒绝、出口日主体有效期判定（当前已注销不影响）、出口日未生效即主体冲突、
无授权→授权不足、搜索截断→待补证而非断言不存在、范围缺失→待补证、
补传材料复审核准后新链可用且历史结论保留、复审撤销降级为受限确认、
范围外的凭证变动不牵连无关出口行、复核核准后解除新案卷暂停（差异原因转 resolved）。

### 4.5 审计相关 API

| 方法与路径 | 说明 |
| --- | --- |
| `POST/GET /api/v1/audit/parties`、`GET /api/v1/audit/parties/{code}` | 主体档案登记与查询（含名称变更/控制关系） |
| `POST /api/v1/audit/parties/{code}/name-changes` / `/control-relations` | 名称变更（需 USCC/继承文件）、控制关系 |
| `POST/GET /api/v1/entities/{code}/credentials`、`GET /api/v1/credentials/{id}` | 授权凭证登记与查询（含撤销通知） |
| `POST /api/v1/credentials/{id}/revocations` | 撤销通知（互斥撤销自动冻结授权链并触发共享传播） |
| `POST /api/v1/credentials/{id}/scope-revision` / `/replace` | 按版本修订范围 / 替换凭证，触发共享变动传播 |
| `GET /api/v1/credentials/{id}/shared-changes` | 共享变动事件：受影响行、暂停行、未变行、逐行重判 |
| `POST/GET /api/v1/entities/{code}/audit-cases`、`GET /api/v1/audit-cases/{id}` | 建案/列表/全展开 |
| `POST /api/v1/audit-cases/{id}/evidence` | 追加合同、装箱、运输、货权证据主张 |
| `POST /api/v1/audit-cases/{id}/evaluate` | 出口日逐段重判并冻结版本 |
| `POST /api/v1/audit-cases/{id}/supplements` | 补传材料 → 复审中、暂停新案卷引用 |
| `POST /api/v1/audit-cases/{id}/review` | 复核核准/撤销（核准可签发修订结论） |
| `GET /api/v1/audit-cases/{id}/conclusions`、`GET /api/v1/entities/{code}/audit-conclusions` | 结论历史 / 当前结论（可 `usable_only=true`） |

---

## 5. 验证方式

### 5.1 自动化验收测试（50 项，含真实并发）
```bash
.venv/bin/python -m pytest tests -q
```

测试使用独立数据库 `ccdev_test`（自动创建，不影响演示库 `ccdev`）。覆盖：

| 场景 | 测试 |
| --- | --- |
| 合并流水无唯一键 → 只生成候选、金额留在待分摊 | `test_combined_remittance_without_reference_stays_unallocated` |
| 唯一附言快速关联 + 部分到账 | `test_unique_reference_quick_reconciliation_and_partial_receipt` |
| 并发确认不得超额（含两侧余额不变式） | `test_concurrent_confirmations_never_over_allocate`、`test_two_analysts_same_candidate_only_one_wins`、`test_concurrency.py`（真实 uvicorn + 线程并发） |
| 海关更正：修订链、旧审核可回查、原始申报依据不被改写 | `test_customs_correction_preserves_submitted_basis_and_revision_chain` |
| 更正后金额低于已占用：冻结而非改写、撤销后上限回落 | `test_customs_correction_below_committed_amount_freezes_instead_of_rewriting` |
| 乱序回执按签发时间生效 / 次序不可证 → 回执冲突 | `test_out_of_order_tax_receipts_ordered_by_issue_time`、`test_tax_receipt_sequence_breaks_an_issue_time_tie`、`test_tax_receipt_order_conflict_when_ordering_unprovable`、`test_conflicted_tax_receipt_does_not_advance_declaration` |
| 跨主体授权 | `test_cross_entity_authorization_is_enforced` |
| 退运候选不改变申报与到账事实 | `test_return_document_becomes_reverse_candidate_not_eligibility_change`、`test_return_evidence_that_cannot_be_attributed_stays_a_candidate` |
| 事实来源展开（来源文件/状态账/余额占用/分摊情景/修订链/判定规则） | `test_drilldown_contains_ledgers_balances_scenarios_revision_chain` |
| 已确认关联进入复审 → 新编译不计入已证实到账量、占用冻结 | `test_frozen_confirmed_line_excluded_from_facts` |
| 撤销释放占用并留审计记录 | `test_void_releases_occupancy_with_audit_records` |
| 装箱单只佐证数量 | `test_packing_list_corroborates_quantity_without_touching_amounts` |
| 出口行侧限额约束（两笔唯一关联合计不得超过申报额） | `test_export_line_side_bounds_cumulative_confirmation` |
| 撤销后释放金额可再次确认 | `test_void_then_pass_can_reconfirm_freed_amount` |
| 更正必须指向被更正行（不允许静默改写申报基准） | `test_correction_without_revises_pointer_is_rejected` |
| 单次运行中后一笔被阻塞不影响前一笔成功（SAVEPOINT） | `test_blocked_unique_link_does_not_undo_earlier_confirmations_in_the_same_pass` |
| 两状态账无共享已结清字段 | `test_settlement_ledgers_do_not_share_settled_fields` |
| 跨主体出口证据链/授权时效审计（24 项，见 4.4） | `tests/test_audit.py` 全部用例 |

> 并发用例做过变异验证：临时移除余额行的 `FOR UPDATE` 行锁后，`test_concurrency.py` 立即失败
> （3 个并发确认全部返回 201，累计超出银行实际金额），恢复行锁后通过——说明该用例确实在检验原子性。

> `test_concurrency.py` 会自行拉起一个本地 uvicorn 进程（随机端口）并发发起确认请求，结束后关闭。

> 说明：本工作容器内没有可用的 `docker` 可执行文件，因此 `Dockerfile` / `docker-compose.yml` 未在此环境实际构建验证；
> 已验证的是本地 PostgreSQL 18 + venv 方式的完整启动、播种、接口与测试（见下）。

### 5.2 手工验证（seed 数据已就绪）

演示数据：主体 `ENT-A` 有 1 张发票 3 行（`SKU-AA-100` 1200 / `SKU-BB-200` 1800 / `SKU-CC-300` 2000，申报号 `DECL-2026-100`），
报关档案、税务回执（批次 `B-2026-09-01`）齐备，三行均已 `submitted → verified`；
银行到账两笔：`TXN-2026-10-A` 1000.00（附言含 `SKU-AA-100`）、`TXN-2026-10-B` 2500.00（合并付款，无行级键）。

```bash
K='X-API-Key: demo-analyst-1-key'
B=http://127.0.0.1:8000

# ① 两个状态账：申报状态与到账状态彼此独立
curl -s -H "$K" $B/api/v1/entities/ENT-A/status-ledgers

# ② 余额：银行侧 1000 已占用，出口行 1 已占用 1000 / 可用 200；合并流水 2500 全部可用
curl -s -H "$K" $B/api/v1/entities/ENT-A/balances

# ③ 候选：合并流水按约束生成 3 条候选（不自动分摊）
curl -s -H "$K" "$B/api/v1/entities/ENT-A/candidates?status=proposed"

# ④ 确认一条候选的部分金额（少于候选额），余额两侧同步占用
curl -s -X POST -H "$K" -H 'Content-Type: application/json' \
  -d '{"amount":"200.00","note":"line 1 残余"}' $B/api/v1/candidates/<candidate_id>/confirm

# ⑤ 超额尝试被拒（409）并留下差异原因
curl -s -X POST -H "$K" -H 'Content-Type: application/json' -d '{}' $B/api/v1/candidates/<candidate_id>/confirm

# ⑥ 编译事实：只有 active 对账行计入已证实事实，其余进入未分摊/差异
curl -s -X POST -H "$K" -H 'Content-Type: application/json' \
  -d '{"entity_code":"ENT-A"}' $B/api/v1/facts/compile

# ⑦ 任意对账行展开：来源文件、状态账、余额占用、分摊情景、修订链、判定规则
curl -s -H "$K" $B/api/v1/reconciliation-lines/<line_id>

# ⑧ 海关更正（改为 1150.00）→ correction_pending + 修订链 + 已确认行进入复审
curl -s -X POST -H "$K" -H 'Content-Type: application/json' -d '{
 "doc_type":"customs_correction","entity_code":"ENT-A","external_no":"DECL-2026-100-REV1",
 "original_location":"s3://demo/customs/DECL-2026-100-rev1.xml","received_time":"2026-10-10T08:00:00Z",
 "lines":[{"line_no":1,"sku":"SKU-AA-100","description":"Widget A","quantity":"98","amount":"1150.00",
           "currency":"USD","declaration_no":"DECL-2026-100","revises_line_no":1}]}' \
  $B/api/v1/intake/documents
curl -s -H "$K" $B/api/v1/export-lines/1        # 修订链 / 状态 / 余额 / 冻结行

# ⑨ 跨主体拒绝（403）
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-API-Key: demo-analyst-b-key' \
  $B/api/v1/entities/ENT-A/status-ledgers
```

---

## 6. API 一览（全部在 Swagger UI 中可直接调用）

| 方法与路径 | 说明 |
| --- | --- |
| `POST /api/v1/admin/entities`、`POST /api/v1/admin/users`、`POST /api/v1/admin/users/{name}/grants` | 主体与账号管理（admin） |
| `POST /api/v1/intake/documents` | 受理任意来源文档（含行）；append-only |
| `GET /api/v1/intake/documents`、`GET /api/v1/intake/documents/{id}` | 查询来源文档、行与更正链 |
| `POST /api/v1/entities/{code}/resolve` | 运行行级身份解析器，生成/刷新候选案卷 |
| `GET /api/v1/entities/{code}/dossiers`、`GET /api/v1/dossiers/{id}` | 候选案卷与证据等级 |
| `POST /api/v1/entities/{code}/reconcile-pass` | 先确认唯一关联，再生成合并流水候选 |
| `GET /api/v1/entities/{code}/candidates` | 候选列表（含理由） |
| `POST /api/v1/candidates/{id}/confirm` | 确认候选（原子占用两侧余额，可部分确认） |
| `POST /api/v1/reconciliation-lines/{id}/void` / `/reassign` / `/review` | 撤销释放 / 改配转移 / 进入复审冻结 |
| `GET /api/v1/reconciliation-lines/{id}` | **对账行全展开**：来源文件、状态账与历史、余额占用与流水、分摊情景、修订链、判定规则 |
| `POST /api/v1/facts/compile`、`GET /api/v1/facts/latest`、`GET /api/v1/facts/runs/{id}` | 事实编译（只发布已证实事实、未分摊余额、差异原因） |
| `GET /api/v1/entities/{code}/balances` | 两侧余额 |
| `GET /api/v1/entities/{code}/status-ledgers` | 申报状态账、到账状态账、出口行到账概况 |
| `POST /api/v1/export-lines/{id}/declaration-events` | 申报状态事件（submitted / verified / rejected / draft_recorded） |
| `GET /api/v1/export-lines/{id}` | 出口行详情（状态、余额、修订链、对账行） |
| `GET /api/v1/entities/{code}/tax-receipts` / `/reverse-candidates` / `/packing-lines` / `/difference-cases` | 回执顺序、退运候选、装箱证据、差异原因 |

---

## 7. 目录结构

```
app/
  main.py                 FastAPI 应用、Swagger UI、OpenAPI
  config.py db.py        配置与数据库
  models.py              全部表模型（含 CHECK 约束与唯一约束）
  schemas.py             请求/响应模型
  auth.py                API Key + 主体授权
  services/
    intake.py            受理账写入、行解析、海关更正链、回执排序、退运候选
    resolver.py          行级身份解析器、带证据等级的案卷、受约束分摊候选
    ledgers.py           申报状态账 / 到账状态账（互不共享已结清字段）
    balance.py           余额控制器：唯一关联优先、原子占用、冻结/释放/转移
    facts.py             事实编译器与对账行全展开
    audit_registry.py    主体档案册、授权凭证册（撤销/替换/范围修订）
    audit.py             时态授权图 + 行级见证链：资格判定、共享变动传播、复审
  routers/               admin / intake / reconciliation / audit
scripts/                 wait_for_db.py、seed.py
tests/                   conftest.py、helpers.py、test_acceptance.py、test_audit.py、test_concurrency.py
```

---

## 8. 明确的非目标

- 不提交报关单或退税申报，不向海关/税务/银行发起任何写操作。
- 不操作库存数量、税务余额、银行资金，不生成付款、授信或税务处理指令。
- 不按金额比例、行数或任何启发式规则自动分摊合并回款；未确认金额始终保留为待分摊余额。
- 退运凭证不会自动冲减申报数量或已证实到账量。
- 审计服务不代办申报、不创建调拨或库存操作、不处理税务余额、银行资金或付款；
  仅产出可按主体/授权/证据版本展开的结论。
