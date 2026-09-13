<div align="center">

<h1>AIALRA SourceLoom · 源织</h1>

<p><strong>把原始资料，织成有据可循的教材</strong></p>

<p>面向 ReadWeave 的内容预处理工作台 · 自动生产试验中</p>

[English](README.en.md) · [实际输入与输出](reports/AUTOMATIC_TRIAL.md) · [完整计划](docs/MASTER_PLAN.md) · [部署说明](deploy/README.md)

</div>

## 1 先看当前结果

源织保存原件，按独立角色清点、规划、生成、审核和局部修复，再把学习材料交给 ReadWeave 阅读

当前已更新教学规划和文件库，收集并接入 24 份真实材料，正式自动成品仍为 0 份；已有自动草稿尚未通过全部要求，详见 [本轮输入、输出和未完成项](reports/TEACHING_ITERATION.md)

旧版人工修订样章保留为 [历史案例](docs/REAL_CASES.md)，不作为当前无人修订的自动生成证明

<div align="center">

<img src="docs/assets/readme/library-desktop.png" width="1120" alt="源织实际文件树与原文对照页面，画面仅使用合成材料，演示保存和阅读功能">

图 1.1 实际材料库页面，合成材料用于展示文件管理与对象显示，不代表模型改写质量

</div>

## 2 怎样使用

1. 点击「导入材料」选择论文、网页、文稿或包含附件的压缩包
2. 保存到左侧文件夹，打开材料查看原件
3. 配置生成通道后点击「生成学习正文」，后台保存任务并继续处理，页面可以关闭
4. 再次打开材料，选择「并排对照」查看输入和已有输出，停止的任务仍保留已经产生的草稿
5. 下载正文，或将具备导出条件的候选包送入 ReadWeave

可以重命名、移动、创建副本、移到回收站或恢复材料，手动编辑正文会保存新版本并撤销旧的通过状态

文件树支持右键、键盘、多选和整层文件夹恢复，正文编辑自动保存，详见 [连贯讲解与材料库迭代](docs/TEACHING_AND_LIBRARY.md)

## 3 在本机启动

需要本机能够运行 `python` 命令，版本至少为 3.11

1. 在仓库目录创建独立运行环境

```bash
# 创建项目自己的运行环境
python -m venv .venv
```

2. 激活环境，Windows 使用 `.venv/Scripts/Activate.ps1`，其他系统使用 `source .venv/bin/activate`
3. 安装程序

```bash
# 安装本仓库的程序与依赖
python -m pip install .
```

4. 启动网页服务

```bash
# 启动材料库页面，默认仅供本机访问
python -m sourceloom.cli serve
```

5. 浏览器打开命令输出的本地地址，默认端口为 `8765`
6. 在使用相同配置与材料目录的第二个终端启动后台处理

```bash
# 后台读取已保存的任务，独立于浏览器执行
python -m sourceloom.cli worker
```

未配置模型时可以保存和管理材料，自动生成会说明缺少通道

## 4 完整写作技能与模型配置

1. 把 [配置示例](config.example.json) 放在私有位置
2. 通过 `SOURCELOOM_CONFIG` 指定配置文件
3. 通过 `SOURCELOOM_DATA` 指定材料目录
4. 将完整的 [中文技术写作技能](https://github.com/AIALRA-0/agent-human-readable-technical-writing) 目录填入 `writing_skill_dir`

每个角色收到全部有效规则正文，完整技能包同时冻结保存，详见 [实际部署方式](docs/SKILL_RUNTIME.md)

模型收到规则不等于已经遵守，正式结果仍要求教学审核、写作审核与独立原文对照均覆盖当前正文；没有完成核对的内容保持草稿状态

自动生产使用以下单篇限制

- 最多 24 次调用
- 默认不设整篇或项目总时限，单次网络请求仍有限时处理
- 最多两轮局部修复
- 默认材料预算为 0.20 美元

完整试验预算与未知订阅成本见 [试验记录](reports/AUTOMATIC_TRIAL.md)

## 5 已验证范围与限制

- 文件夹、材料管理、版本保存、回收站及后台任务恢复有程序测试覆盖
- 网页保存原始内容与限量获取的图片，缺失资源保留为缺口，动态页面和所有图形类型尚未全面支持
- 普通文稿可提取文字、表格、链接与资源，复杂公式、修订痕迹和无法可靠转换的对象仍显式保留缺口
- 专用混合文档完成 ReadWeave 实际导入、保存和重开，重复图片、表格、代码、公式、段落身份与脚注目标均通过该例核对
- 24 份多领域真实材料的完整测试尚未完成，全流程平均每篇 0.10 美元也尚未实测达标

运行不调用付费模型的程序检查

```bash
# 安装测试依赖
python -m pip install ".[test]"

# 执行最多等待 120 秒的确定性检查
python scripts/verify.py
```

程序测试通过不能代替真实成文效果和用户实际阅读验收

## 6 继续了解

- [初衷与总计划](docs/MASTER_PLAN.md)
- [需求逐项对照](docs/REQUIREMENTS.md)
- [完整技能如何交付](docs/SKILL_RUNTIME.md)
- [真实输入、输出与失败](reports/AUTOMATIC_TRIAL.md)
- [已有项目的经验](docs/PROJECT_LESSONS.md)
- [部署与回滚](deploy/README.md)
- [贡献说明](CONTRIBUTING.md)
- [安全说明](SECURITY.md)

公开仓库不包含私人材料、模型凭据、生产配置、登录记录或本轮原始模型请求
