# SourceLoom 后续真实端到端素材候选

核对日期：2026-09-22，清单为后续真实流程验证准备，未触发 SourceLoom 生成或付费模型调用

我对照了 `scripts/v2_smoke_campaign.py`、`evals/real-corpus.json`、`docs/REAL_CASES.md` 和已记录的历史试验，下列 URL 未在这些已知记录中出现，也排除了 MDN Null/Function、W3C Contrast、RFC 9775、Astral uv、PLOS、Rust、Git SECURITY、RFC 2119/3629、USGS 水循环和 SERC 教学材料

网页、Markdown、纯文本、PDF 和 DOCX 均来自公开的官方来源，十项清单由 `scripts/v2_real_eval.py` 直接执行，旧的 `v2_smoke_campaign.py run` 只接受各一份网页、Markdown 和 PDF

## 建议执行顺序

每份素材创建独立项目，依次走完 `create → intake → produce → poll → export`，再开始下一份

正式运行设置单篇 `--budget-cny` 上限，另行监控批次总账，并显式提供 `--allow-paid`，预检阶段只下载材料并计算摘要

每步记录项目与运行 ID、材料 SHA-256、对象和资源清单、模型调用、费用、终态与导出路径，只有任务进入可交付终态、导出文件非空且并非原文逐字节回退，才进入内容核对

网页和文件里的图表、图片、视频若未能捕获，在结果里明确标记缺失，不能把“导出成功”当作对象完整

| 顺序 | 材料和原始来源 | 格式、预估篇幅 | 关键对象与本项验收观察点 |
| --- | --- | --- | --- |
| 1 | [Flask 官方 README](https://raw.githubusercontent.com/pallets/flask/main/README.md) | Markdown，短，约 48 行 | 顶部 SVG 标志、参考式链接、Python 与 shell 围栏代码，检查图片目标与参考链接可定位，代码块次序和正文没有并入段落 |
| 2 | [NOAA《Making Waves: Ocean Currents》](https://oceanservice.noaa.gov/podcast/apr14/mw123-currents.html) | HTML，短，约 3 分钟视频 | 3 分钟视频及逐字稿，含潮汐、风和温盐环流三个部分，逐字稿还提到 NASA 的全球洋流动画，检查正文、视频/动画链接和逐字稿均进入对象清单，若媒体二进制未保存，原始目标链接仍须保留 |
| 3 | [NASA《Mountain Rain or Snow》](https://science.nasa.gov/citizen-science/mountain-rain-or-snow/) | HTML，短至中，约 3–5 分钟阅读 | 当前抓取的页面含说明漫画、雪花照片、手机应用截图和项目链接，但正文提到的地区关键词表不在所获 HTML 中，检查图片与正文说明对应，项目步骤和电话号码无遗漏，结果须准确说明表格缺失，不能补造关键词 |
| 4 | [NASA《Entry Points to NASA Science Data》](https://science.nasa.gov/learn/entry-points-to-nasa-data/) | HTML，中，约 5–8 分钟阅读 | 多个学科的数据资源表，列含资源名、说明、学科、所需技能，并有页首图像和多个外站链接，检查表格行列关系完整，链接锚文本仍对应原目标，不把 NASA 导航混入文章正文 |
| 5 | [NASA/JPL GRACE-FO 两页事实说明 PDF](https://gracefo.jpl.nasa.gov/rails/active_storage/blobs/redirect/eyJfcmFpbHMiOnsibWVzc2FnZSI6IkJBaHBBdXNCIiwiZXhwIjpudWxsLCJwdXIiOiJibG9iX2lkIn19--c38594fb14e558e925e6f6bd6c6ed03bc4a36529/GRACE_FO_Fact_Sheet_REV4-6-18_508.pdf?disposition=inline)（[来源页](https://gracefo.jpl.nasa.gov/resources/38/grace-fo-fact-sheet/)） | PDF，短，2 页 | 航天器主图及密集技术说明，当前预检未见独立测量示意图，检查页序、实际图注和单位，这是描述 2018 年前后任务计划的历史资料，改写需保留原资料时点，不得把计划措辞写成当前状态 |
| 6 | [HM Treasury《Checklist for Assessment of Project and Programme Business Cases》PDF](https://assets.publishing.service.gov.uk/media/626a9a508fa8f57a3b41bc04/Business_Case_Reviewers_Checklist.pdf)（[来源页](https://www.gov.uk/government/publications/the-green-book-templates-and-support-material)） | PDF，短，2 页，密集清单 | 五个商业案例维度、多层检查问题和项目阶段标题，检查两页内容完整、子问题仍隶属正确维度，保留 `SOC/OBC/FBC/PBC` 等缩写和资格条件 |
| 7 | [Office of the Advocate General for Scotland 公开回函 DOCX](https://assets.publishing.service.gov.uk/media/5a80d7e240f0b62305b8d766/REDACTED_09.02.16_Style__Guidence_for_documents.docx)（[来源页](https://www.gov.uk/government/publications/style-guidance-for-documents)） | DOCX，很短，文件 52 KB，实测约 250 词 | 文件含 35 个段落、1 个表格和 1 个图片对象，实际内容为删节的信息公开回函，提到曾发出指南，并非指南正文，检查 Word 段落、表格和图片都被识别，改写不得将回函改造成指南，这是小型结构样本，不能单独代表长 DOCX |
| 8 | [Natural England《Ancient woodland assessment guide》DOCX](https://assets.publishing.service.gov.uk/media/61d5c3238fa8f54c18a64185/Ancient_woodland_assessment_guide.docx)（[引用来源页](https://www.gov.uk/guidance/ancient-woodland-ancient-trees-and-veteran-trees-advice-for-making-planning-decisions)） | DOCX，中，文件 82 KB，实测约 1,500 词 | 144 个段落、2 个图片对象，包含对开发影响的评估内容，检查标题层级、列表顺序、图文位置和评估步骤，不把表单提示误作已填写的事实 |
| 9 | [RFC 20《ASCII format for Network Interchange》](https://www.rfc-editor.org/rfc/rfc20.txt) | TXT，中，9 个页标 | 固定宽度纯文本，含 ASCII 位表、字符表、定义和章节层级，检查列对齐、控制字符名、`[Page n]` 页界及标题不被吞并，不要将表中保留字符改写成普通标点 |
| 10 | [HTTPX 官方 README](https://raw.githubusercontent.com/encode/httpx/master/README.md) | Markdown，中，当前快照 147 行 | 标志与状态徽章、HTML 标签、截图、围栏示例、功能列表和众多文档链接，检查相对图片 `docs/img/...` 能否随原始仓库基址解析，链接与代码块保留，README 末尾许可信息不遗漏 |

DOCX 的段落、表格、媒体数来自公开文件的预检，原件不进入仓库，NOAA 页面逐字稿明确了视频分段及 NASA 动画链接，PDF 页数经原件核对

每批正式运行前重新下载公开源、计算 SHA-256，并用生产数据库和历史报告确认去重，网页和 `main`/`master` 分支上的文件可能随时间变化
