# 三份真实材料的输入与最终输出

以下为经过人工修订的历史样章，未通过本轮完整自动生产验收；最新自动草稿与实际失败见 [真实试验记录](../reports/AUTOMATIC_TRIAL.md)

直接打开 [完整案例入口](https://sourceloom.aialra.online/examples/real/)，登录后点击任何一份材料，即可阅读改写正文，无须先理解工作台的内部步骤

## 1 三份材料

| 实际输入 | 最终输出 |
| --- | --- |
| PLOS 的 4 页学术方法文章《Ten Simple Rules for Reproducible Computational Research》PDF，保留全部十条规则、出版声明及 25 项参考文献 | [阅读论文改写](https://sourceloom.aialra.online/examples/real/paper.html) |
| MDN 的完整盒模型网页正文，包含原图、代码、表格与运行示例 | [阅读网页改写](https://sourceloom.aialra.online/examples/real/web.html) |
| Rust 官方教程第 4.1 节完整 Markdown，展开同一版本中的代码引用并保留五幅原图 | [阅读 Markdown 改写](https://sourceloom.aialra.online/examples/real/rust.html) |

每份提供连续阅读、原文阅读、原文与成稿对照、最终 PDF、改写 Markdown，以及包含原件与资源的压缩包

压缩包是本次案例的离线阅读包，不冒充已经验证过的 ReadWeave 原生导入包

## 2 实际怎样完成

先下载完整原件，提取并核对源章节，取得引用的图片和代码，再分别调用规划与分节生成角色，保存完整返回和失败返回

当前助手逐段对照原文，修正具体遗漏和错误解释，再生成阅读页面和 PDF

本次共调用模型 37 次，按保存用量与配置的保守费率折算约 0.2993 美元，包含截断、无正文和无法直接使用的返回；这是估算，不是供应商账单，预设总上限为 1.50 美元

程序行为测试不能代替这三份成稿的阅读判断，模型审核也没有获得可当作完整通过证据的返回，本次最终修订由当前助手直接完成

## 3 阅读时需要知道的范围

- 论文以原始 PDF 为输入，并借助出版社 HTML 核对三栏阅读顺序、断行与引用，不能据此声称任意 PDF 已能无人干预处理
- 网页保留文章正文，排除网站导航与页脚，运行示例由原始代码重建；离线页面没有复制原站的在线代码编辑器
- Markdown 的代码引用需要先展开，不能把原始占位语法或空代码块交给写作者后仍声称无损
- 本次保留的是完整材料，并补充教学解释，因此成品明显长于原文；篇幅本身不是质量合格的证据
- 这三份是经过当前助手修订的案例成品，尚未证明生产工作台能够对任意材料一键自动交付，也没有代替用户接受

## 4 来源与许可

- [论文及作者声明](https://journals.plos.org/ploscompbiol/article?id=10.1371/journal.pcbi.1003285)：按原文注明的知识共享署名许可使用
- [MDN 盒模型](https://developer.mozilla.org/en-US/docs/Learn_web_development/Core/Styling_basics/Box_model)：文字及改编遵循 CC BY-SA 2.5，代码按 [MDN 许可说明](https://developer.mozilla.org/en-US/docs/MDN/Writing_guidelines/Attrib_copyright_license) 保留
- [Rust 原始 Markdown](https://github.com/rust-lang/book/blob/1500248d8f230566e4ec9f27fcbb8fe9e2898ab1/src/ch04-01-what-is-ownership.md)：固定到同一仓库版本，采用 MIT 许可并在阅读包中附完整声明

## 5 自行部署案例文件

工作台从配置的数据目录下 `examples/` 子目录提供阅读文件，该目录沿用工作台的登录校验

将经过核对的案例页面及其资源放进 `examples/real/`，其中首页为 `index.html`，即可从 `/examples/real/` 打开

这些案例内容属于运行数据，不会把模型配置、原始调用记录、密钥或私人材料自动复制进公开仓库
