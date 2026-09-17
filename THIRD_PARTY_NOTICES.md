# 第三方与集成说明

## 1 运行依赖

依赖版本在 `pyproject.toml` 中声明，使用时保留各依赖包自带的许可证和版权声明

- FastAPI、Uvicorn、Pydantic 和 HTTPX 提供服务、数据合同与请求能力
- Beautiful Soup、markdown-it-py、docutils 和 defusedxml 提供文本及结构解析
- pypdf、pypdfium2 和 Pillow 提供 PDF 接入、页面渲染与图像处理
- CairoSVG 以 LGPL-3.0-or-later 许可提供 SVG 栅格化，源代码和许可证见 [CairoSVG 项目](https://github.com/Kozea/CairoSVG)
- pytest、Playwright 和 ReportLab 仅用于相应验证与合成材料生成

## 2 外部系统

ReadWeave 基于 TriliumNext，SourceLoom 使用其公开格式与接口进行集成，没有把阅读器源码复制进本仓库

中文技术写作技能单独维护和加载，生产运行时保存所用规则摘要；本仓库不复制它作为另一份写作技能发布

模型服务使用各自的调用与计费规则，项目许可证不授予第三方账号、模型或原始文档的权利

## 3 演示与报告材料

本仓库截图、领域种子、图像探针和调用示例均来自本项目创建的合成材料，真实私人附件与部署记录不随仓库公开
