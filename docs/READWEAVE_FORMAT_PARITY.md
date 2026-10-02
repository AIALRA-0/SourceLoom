# ReadWeave v20 格式规则对照

对照的是本机 ReadWeave 候选仓库 `apps/server/src/services/readweave_format.ts` 与 `readweave_active_pipeline.ts`，格式版本 `format-2026-09-v20`，仓库 HEAD `ab668ff0983d97228c75a296daf73fa768050590`

| ReadWeave 规则族 | SourceLoom 实际流程 | 决策依据 |
| --- | --- | --- |
| `mapReadWeaveProse`、`repairReadWeaveDuplicateNames`、`repairReadWeaveInternalContextMarkers` | `repair_readweave_duplicate_names` 与 `RW-FMT-124` 候选 | 精确重复专名只在可写正文删除；内部标识进候选，原始对象与代码区不改 |
| `repairReadWeaveProseSpacing`、`repairReadWeaveInlineCodeSpacing` | `repair_readweave_prose_spacing`、`repair_readweave_adjacent_inline_code_spacing` | 已在 `normalize_authored_periods` 接入；只补中文边界空格，不改代码本体 |
| v20 `repairReadWeaveBilingualNameLineBreaks` | `repair_readweave_bilingual_name_line_breaks` | 已在 `normalize_authored_periods` 接入；只合并明确的双语名称软换行，保护围栏、引用、表格、链接、HTML 和歧义括号 |
| `repairReadWeaveMixedUnitGloss` | `repair_readweave_mixed_unit_gloss` | 仅重排原文中符号、中文单位名、英文单位名均齐全且精确匹配的三元组；记录了来源核对限制，词元集合保持不变 |
| `repairReadWeaveMixedBilingualExplanation` | `repair_readweave_mixed_bilingual_explanation` | 本次补入现有规范化步骤；只把英文名称后的中文说明移出全角括号并调整逗号位置，词项不变；行内代码、链接、公式、引用、表格、标题、HTML 标签及属性不改 |
| `repairReadWeaveVerifiedMixedBilingualExplanation`、`repairReadWeaveExistingAcronyms`、`repairReadWeaveDefaultNameCase`、`repairReadWeaveVerifiedNameCase`、`repairReadWeaveDefinitionLabelsFromExistingNames`、`repairReadWeaveBilingualNameCodeMarkers` | 明确术语行用 `RW-FMT-136` 候选；其他名称问题由已安装技能和 source-bound `active_format` / `active_revision` 复核 | ReadWeave 依赖 outline 中已核实的 `canonical`、缩写及大小写策略；不在缺少对应证据的通用规范化函数中移动名称、改大小写或删反引号 |
| `formatReadWeaveNameParentheses`、`formatReadWeavePersonNameOrder`、`formatReadWeaveCanonicalEntities`、`formatReadWeaveTermReferences`、`repairReadWeaveOptionalQualifiers`、`repairReadWeaveConventionalTerms` | SourceLoom 已有原文证据、术语核验和内容修订链；未新增格式候选或搜索调用 | 这些规则依赖人物/实体语境、用户问题、来源上下文或异步 resolver；从排版文本推断可能改写名称事实或增加调用成本 |
| `formatReadWeaveAnswerHeadings`、标题编号/层级函数、`formatReadWeaveDefinitionBlock`、`formatReadWeaveCodeCopies`、列表和枚举修复函数 | 安装写作技能的结构检查、SourceLoom 已有编号设置及局部补丁 | 不迁移自动标题改造、定义升格或枚举拆分；需要读取作者结构和用户设置，顿号本身不足以证明应拆分 |
| `readWeaveFormatIssues`、公式检查、`applyReadWeaveFormatPatches`、`repairReadWeaveFormatBatch` | 安装技能的确定性扫描、SourceLoom 现有补丁器与 `active_format` 流程 | SourceLoom 已把规则候选传到现有 source-bound 审核；未复制 ReadWeave 的服务端补丁器或增加修复阶段，格式问题仍走当前有限局部修订流程 |

SourceLoom 的实际顺序是：写作后由 `normalize_authored_periods` 运行无模型的安全排版修复；`writing.scan` 再运行安装技能检查并添加少量 ReadWeave 候选；`active_format` 判断是否确属问题，需要改字时进入现有 `active_revision` / `active_patch`，局部修订再核对受保护原件及事实落点。没有添加阶段或额外模型调用。

本次 v20 差异中，双语名称软换行与相邻行内代码空格已分别由 SourceLoom 的 `4b4d541`、`96cf7e0` 接入；本次新增的 mixed bilingual explanation 用 SourceLoom 自动化测试覆盖，包含词项不变、幂等与受保护区负例。缩写顺序、别名语义、官方大小写、人物/实体规范化、引述解释、标题编号和枚举重构仍由来源绑定流程处理，避免把格式形状当成名称证据或改动作者结构。
