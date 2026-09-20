# SourceLoom v2 真实材料评测工具

`scripts/v2_real_eval.py` 用于对少量真实材料做一次性端到端评测。它只消费已经准备好的私有 manifest，不自动搜集材料，也不自动接受或发布项目。

默认模式是 `dry-run`，不会连接 SourceLoom，也不会调用模型。`offline` 只读取 manifest、历史报告、生产数据库和 checkpoint，同样不连接网络。真实执行必须显式使用 `--mode live --allow-paid`，因为 `/api/projects/{id}/produce` 可能产生供应商费用。

生产环境若使用受信代理认证，可把允许的身份放入 `SOURCELOOM_EVAL_SUBJECT` 环境变量。评测器只在内存中把它加入请求头，不会把值写入报告、checkpoint 或命令行参数。

manifest 可以是数组，也可以是包含 `materials` 数组的对象。每项至少包含以下字段：

```json
{
  "kind": "markdown",
  "name": "唯一的材料名称",
  "local_path": "private-materials/example.md",
  "sha256": "64 位小写 SHA-256"
}
```

网页材料使用 `url` 替代 `local_path`：

```json
{
  "kind": "web",
  "name": "唯一的网页名称",
  "url": "https://example.com/article",
  "sha256": "该来源冻结版本的 64 位小写 SHA-256"
}
```

执行前要提供历史报告或生产数据库路径，工具会按规范化 URL、SHA-256 和来源摘要检查重复材料；命中后直接拒绝创建项目。重复检查只输出文件、表和列的身份，不输出匹配正文。

```powershell
python scripts/v2_real_eval.py private-manifest.json `
  --mode dry-run `
  --history-report reports `
  --production-db .local/source-loom.sqlite `
  --output .local/v2-real-eval
```

真实模式的并发默认是 2，硬上限是 4：

```powershell
python scripts/v2_real_eval.py private-manifest.json `
  --mode live --allow-paid --workers 2 `
  --history-report reports/previous.json `
  --production-db .local/source-loom.sqlite `
  --checkpoint .local/v2-real-eval/checkpoint.json `
  --output .local/v2-real-eval
```

每个来源有独立 checkpoint。工具会按以下顺序推进：创建项目、后台上传或 URL 导入、轮询 intake、提交一次生成、轮询原 production 任务、导出结果。`project_id`、`intake_run_id`、`run_id` 和请求尝试标记会在每个外部写操作前后保存。重启时只轮询已经记录的任务；如果进程在请求返回前中断，工具会进入 `manual_review`，不会猜测请求是否送达，也不会自动再次创建或生成。

每篇完成材料会输出：

- `project.json`：项目详情、状态、调用次数和服务端成本明细。
- `output.md`：最终 Markdown 正文。
- `readweave-package.zip`：阅读包候选。
- `cost.json`：`estimated`、`reserved`、`unsettled`、`provider_actual` 四种费用字段和逐调用状态。

总报告和 artifacts 目录都使用 UTC 时间戳加随机后缀，已有报告不会被覆盖。工具不会调用接受接口，也不会调用发布接口。

离线测试：

```powershell
pytest -q tests/test_v2_real_eval.py
```

测试使用临时 manifest、临时 SQLite、假 HTTP 客户端和本地 checkpoint，不联网、不创建真实项目、不产生模型费用。
