# SourceLoom 材料处理器 v1

这是早期 v1 的历史说明。当前入口、单包交接和阅读器操作以 [v1.1 使用说明](PROCESSOR_V11_USE.md) 为准；下文旧工作台和分组附件说明不属于当前默认产品。

默认入口现在是材料工作台。主线为：原件接入 → 任务包 → 手动或 API 模型交接 → Markdown 编译 → 图文预览 → ReadWeave。新项目不经过旧 Planner、Concept Ledger、Writer 批次或强制 Integrity；旧材料与证据在 `/legacy`，旧高级工作台在 `/workbench`。

## 本地启动

```powershell
python -m pip install -e ".[test]"
$env:SOURCELOOM_DATA = ".local/processor/data"
$env:SOURCELOOM_CONFIG = ".local/processor/config.private.json"
python -m sourceloom.cli serve --port 8765
```

打开 `http://127.0.0.1:8765`。不配置模型也可以完成手动闭环。私有配置文件不要提交 Git。已有实例可继续使用其配置；新默认首页不会自动启动或迁移历史任务。

## 日常使用

1. 导入 PDF，或一起上传正文与配图。PDF 原件、逐页截图与能可靠提取的内嵌图分别保存。网页保留快照和直接配图，导航不进入模型正文；DOCX、Markdown、文本与图片共用任务包。
2. 设置阅读偏好，复制完整任务说明，分别取得模型能实际读取的原件和图像附件。任务 ZIP 用于保存，不假定模型能够打开 ZIP。
3. 模型返回 Markdown，并在需要原件的位置放独占一行的 `{{source:src-00004}}`。索引中的 ID 是当前材料的实际 ID，不能从别的任务复制。
4. 粘贴或上传 Markdown。保存产生新版本，程序恢复原图、代码、表格和公式，列出未知标记、缺失对象及非法图片路径。原文句号不会被改成换段。
5. 图文预览后下载 ReadWeave 原生 ZIP，或直接导入已配置的 ReadWeave。原始文件、图片、Markdown、来源映射和文件检查回执一起进入阅读包。

页标记生成可展开的原页回查。资源标记生成实际原件；程序不让模型重画图片或重写受保护代码。复杂扫描件可以直接交给支持视觉输入的模型，提取文字不被当成完整 OCR 证明。

## 自动模型通道

自动与手动使用同一个任务包和结果编译器。API 一次提交完整文字与实际图像／PDF，返回纯 Markdown，没有独立 Plan，也没有模型 JSON correction。超过单次输入能力会明确拒绝，不截断原文或拆成几十条消息。

可在私有配置中指定 `processor_provider`，它覆盖已有模型配置。示例（占位值，不能直接用于生产）：

```json
{
  "provider": "manual",
  "auth_mode": "local",
  "processor_provider": {
    "provider": "openai-compatible",
    "provider_id": "my-vision-api",
    "base_url": "https://your-api.example/v1",
    "api_key": "YOUR_PRIVATE_KEY",
    "model": "YOUR_VERIFIED_VISION_MODEL",
    "protocol": "chat_completions",
    "processor_supports_images": true,
    "processor_supports_pdf": false,
    "call_timeout": 240,
    "max_output_tokens": 12000
  },
  "readweave_url": "http://127.0.0.1:11802",
  "readweave_token": "YOUR_PRIVATE_ETAPI_TOKEN",
  "readweave_parent": "YOUR_CANDIDATE_PARENT_NOTE"
}
```

Responses 通道可以明确配置 `processor_supports_pdf`；仅配置模型名字不证明文件能力。已部署 Router 的任务合同目前是文本子集，没有真正的附件输入，图文任务在该通道明确不支持。纯文本任务可经 `processor_router` 使用持久任务；只查询原 task ID，不建立浏览器自动化或账号恢复系统。

## 状态与边界

- **文件与资源检查通过**：资源已能恢复，原件摘要核对通过，安全 HTML 与原生包可编译。它不证明语义绝对忠实。
- **语义未核对**：可以保存、阅读、导出、导入候选；不会标成 Verified。v1 默认不自动调用语义 Reviewer。
- **ReadWeave 读回通过**：远端正文和附件分别读回比较。每次导入回执中的编辑器保存重开仍独立记录，不能用 ETAPI 读回代替编辑器验收。
- **UNKNOWN**：原请求可能已执行，保留身份和费用；不会自动重投。手动导回是新版本，原请求不被清除。Router 可以查询原任务；API 无可靠查询能力时保留 UNKNOWN。

版本编辑使用基线身份避免覆盖另一份编辑。异步结果绑定原任务包；进程重启从持久回执找回成功结果，未取得终态的请求保持未知。新材料的冻结原件不能从旧主链入口修改。

## 验收工具

`tests/test_processor.py` 使用真实 PDF 解析和原生包编译检查材料闭环；`tests/test_processor_channels.py` 验证真实请求形状、单次投递及 UNKNOWN 边界。

`scripts/processor_readweave_verify.py` 在独立测试笔记上执行实际编辑器保存、关闭、重开及图像／代码核对。它只修改新建的测试笔记，不应用于已有用户文章。

第一版不把“通道成功”冒充文章质量保证，也不声称优于直接 Pro。复杂矢量图或无法可靠提取的部分保留原页与原文件；PPTX、批量处理和无限长材料不是已验证能力。
