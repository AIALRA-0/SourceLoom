# SourceLoom 第一轮主链切换记录

本轮只切换新任务的内容主链，存量任务继续按创建时的版本运行；以下记录形成于生产部署前

## 新任务实际路径

原件冻结 → 形成一份文章计划 → 按语义部分写出完整候选稿 → 一次整稿完整性核对 → 必要时集中局部修复一次 → 交付

全部通过时标记为 `Verified`；证据不足或明确问题仍未解决时保留可阅读的 `Candidate`，不把它标记为已核实

新任务在最后一个写作部分完成后直接进入 `active_integrity`，不再经过每部分的 `active_review → active_revision → active_format`，这些旧入口仅供显式标记 `core_chain_version=0` 的存量任务使用；缺少标记的新任务会被拒绝

三类状态各自回答不同问题：任务 `status` 只表示执行是否完成；`integrity_result.verdict` 只表示五项正确性判断是 `PASS`、`FAIL` 还是 `UNKNOWN`；`production.publication_status` 只表示当前稿是 `Candidate`、`Verified` 还是 `Published`，完成执行不自动意味着内容通过

## 本轮退出主路径的机制

- 每个写作批次的独立审核、修复和格式复审退出新任务，批次只负责写作执行；存量任务继续原路径
- 批次审核记录不再证明整篇语义通过，只有完整候选稿的核对结果拥有这项判断权；旧字段保留读取能力
- 保存对象不再自动要求正文覆盖，原件始终保留，正文展示由计划决定；存量任务继续旧检查规则
- 图片进入正文不再自动要求单独解释，展示与解释分别决定；存量任务继续旧写作规则
- `independent_review.status` 不再充当整篇审核，新任务不写入该字段；旧回执保留读取能力

新任务通过 `core_chain_version=1` 与存量任务隔离；重新改写时只复用同版本的视觉、规划和写作检查点，不把旧批次审核状态带入新链

存量任务迁移后的删除条件是：旧任务均已结束或完成明确续接迁移，旧回执与前端读取者不再依赖旧字段，再删除 `active_review`、`active_revision`、`active_format` 及相应兼容代码，本轮不提前删除仍有调用者的路径

## 对象与交付判断

每个原对象在原件档案中保留，文章计划分别决定它是否进入正文、是否需要解释，正文和阅读包因此承担不同职责

整稿核对聚焦五件事：来源身份、原件与资源、外部事实来源、关键语义、最终文件可用性；一般文字样式不拥有阻断内容正确性判断的权力

核对后的明确局部问题最多进入一次集中模型修复，修复后只确认受影响范围与必要的确定性条件；`UNKNOWN` 可交付为 `Candidate`，明确 `FAIL` 不得标记为 `Verified`，生成失败也不得用原文冒充候选稿

## 验证证据

- 全量本地测试：`python -m pytest -q`，本轮收尾后全部通过；新旧任务硬边界和保存的 `fnmatch` 回归另有定点测试
- 离线原件与资源回归：20 项通过，覆盖旧任务版本隔离、原件字节、隐藏图片资源、导出读取
- 编译与改动检查：`python -m compileall -q sourceloom` 在第一轮主链切换时通过；本轮修改的已跟踪文件通过定向 `git diff --check`；工作树中既有的 `docs/ITERATION_8.md` 尾随空格仍使全仓 `git diff --check` 返回失败，本轮没有改动该无关文件
- 开发材料：[Python 官方 `fnmatch` 源文档](https://github.com/python/cpython/blob/main/Doc/library/fnmatch.rst)，从网页取得真实原件后建立新任务，原件 4,329 字节、15 个解析对象
- 首次 `fnmatch` 开发运行：`active_plan → active_write → active_integrity → active_deliver`，3 次模型调用，117.5 秒，交付 13 个正文块、4,461 个字符；当时误判为 `IntegrityVerdict=PASS`、`PublicationStatus=Verified`
- 第一轮收尾复核：不重新生成 `fnmatch`，只对保存的原件与候选稿调用现有 `active_integrity` 一次，53.9 秒；它对“按当前平台规则处理”被改写为“跨平台一致”给出两项有原文和候选引文的 I4 `FAIL`，证实上述首次通过判定无效
- 全新材料 `keyword.rst`：关闭 OpenCode Go 的思考输出占用后，从输入到交付完成；规划、写作、整稿审核各一次，共 3 次模型调用，54.1 秒，最终 `completed / PASS / Verified`；`module keyword` 和 `:synopsis:` 所在对象为 `preserve=true / present=false / explain=false`，正文没有搬入这些原始元数据，原件仍可按字节读回
- 同一份 `keyword.rst` 的首次尝试在规划阶段连续两次因模型输出达到上限而截断，耗时 252.1 秒且没有候选稿；这两次调用属于开发失败记录，不计入后一次成功运行的三次调用，也不能被隐去

这些材料只用于开发阶段验证，不是冻结版本的正式发布验收；供应商计费回执仍为未结算状态，因此本记录不把预估费用写成实际费用；生产部署另行核对和记录

## 封板结论

**Round 1 — Core Chain Simplification：CLOSED / PASS**

冻结版本为 `ad12313922d8ffba7978adc2cd9eff2e44d99cc2`，生产发布目录为 `/srv/aialra/apps/sourceloom/releases/core-chain-ad12313-20260924T041639Z`；新任务使用 `core_chain_version=1`，明确标记的存量任务继续使用旧路径

生产封板使用此前未处理的 Python 官方 `linecache.rst` 原件，任务 `31c2374a68594be393e15c2b00448c30` 完成了规划 1 次、写作 2 次、整稿核对 1 次、修复 0 次，均记录为夸父社 Responses 线路；最终状态为 `completed / PASS / Verified`，I1—I5 均为 `PASS`，原件散列值一致，正文与阅读包接口均返回 200

任务记录的执行时间约为 6 分 33 秒，从上传到完成约为 6 分 48 秒；现有记录没有逐次调用耗时，不能据此认定模型或备用线路各占多少时间，这一测量缺口转入第二轮 Provider 工作

最终稿的 `PEP 302`、`I/O` 未完整展开，以及开头 `linecache 模块（linecache）` 不够自然，列入第三轮写作质量工作；这些问题不改变本轮 I1—I5 与主链结构的验收结论

封板原件与结果保存在本机 `.local/production-seal-linecache/`，其中 `result.json` 记录任务状态、调用次数、原件校验与交付接口结果；本轮不再因写作风格或 Provider 耗时重开核心主链
