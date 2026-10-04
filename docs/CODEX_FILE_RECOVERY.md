# 历史 Codex 文件实验：不属于当前产品通道

当前 SourceLoom 新文章生成和压测只允许 **Web Chat 普通 chat 模式**，使用用户指定的 Web-only 业务凭据。不得使用 Codex、CLI、Runner、其他供应商、管理员 Key 或另一账号补齐能力。当前运行方式见 [README](../README.md) 和 [生产部署说明](../deploy/README.md)。

## 统计更正

2026-10-03 保存的历史实验共 40 个 Codex 任务，39 个取得要求的文件，其中包含 38 个案例和 2 个原生 UI 请求。该批 **Web Chat 任务数为 0**，无法计算 Web Chat 成功率。历史文件回收和阅读判断不计入 Web Chat 产品验收。

已保存的脚本明确从服务器管理密钥文件读取实际 Bearer 凭据，并覆盖业务通道为 Codex。此行为不符合用户的调用授权。所有历史任务的 callerId 相同，但尚缺历史 Key 权限记录的关联，因此不能据此声称用户的 Web-only Key 绕过了 API 鉴权。管理员具备技术权限也不代表获得产品生成授权。

原脚本、原请求、终态回执、费用记录和旧说明作为私人审计证据保留，不随公开仓库分发；不删除已接受稿件，也不把旧实验改名为 Web Chat 验收。本文不再提供 Codex 任务派发或管理员凭据配置步骤。

## 可保留的通道无关机制

已有文件的严格字节核验仍可用于离线处理：核对原任务身份、唯一预期文件、安全相对路径、UTF-8/Base64 编码、字节长度和 SHA-256。校验成功不证明模型读过原图，不升级语义状态，也不授权重新生成。

可复用的资源编译、页面交互和导出结果应明确记录其来源。新 Web Chat 验收必须使用独立台账，并记录实际通道、普通 chat 模式、获准 Key 身份、支持的输入和真实终态；不匹配回执不能保存为成功 Web 结果。

## 离线来源包

上传已经冻结的网页/Markdown 及其资产时，普通 ZIP 可以附带根目录唯一 `SOURCE_MANIFEST.json`：

```json
{
  "schema": "sourceloom-source-manifest/1",
  "source": {
    "path": "source.md",
    "sha256": "<ORIGINAL_FILE_SHA256>",
    "url": "https://example.org/source.md"
  },
  "assets": [
    {
      "url": "https://example.org/figure.png",
      "path": "assets/figure.png",
      "sha256": "<ASSET_FILE_SHA256>"
    }
  ]
}
```

程序校验包内路径和已收到文件的 SHA 后复用原接入逻辑，不抓在线页面，不根据文件 hash 猜 URL。清单保存为 provenance，不进入文章或正文对象编号。清单有错误则拒绝，不暗中修正；无清单的原上传行为保持。这个机制恢复已有来源身份，不能给 Reference 目标增加解释权限。
