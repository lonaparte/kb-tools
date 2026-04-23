<!--
This file is the AI Summary prompt template used by kb-importer.

When an LLM agent generates a summary for a paper to be fed into
`kb-importer set-summary`, it should follow the structure and style
rules below.

### Context notes (specific to kb-importer)

- When calling `kb-importer set-summary PAPER_KEY`, the output goes
  INTO an existing paper md, not a new standalone file. So the
  instruction "md 文件名必须等于 PDF 文件名" below does NOT apply —
  kb-importer decides the filename (it's `{paper_key}.md`).
- The first line MUST still be `AI Summary YYYY-MM-DD` (today's date).
  kb-importer uses this as a structural marker and preserves it.
- Everything after the first line is rendered into the paper's md
  `<!-- kb-fulltext-start -->` region as-is.

### Customizing the template

This file ships with kb-importer but is read from a writable location
at runtime (see `kb-importer show-template --help`). Edit to adjust
the style, section order, language, etc. kb-importer does not parse
the template itself; it only hands the text to LLM agents.
-->

# AI Summary Config
生成的 md 文件名称必须严格等于 PDF 文件名。
标题必须且只能是如下格式：
AI Summary 2026-02-03
日期必须使用生成总结当天的真实日期，格式为 YYYY-MM-DD。
禁止输出“YYYY-MM-DD”这类占位符，必须替换为实际日期。
---
## 1. 论文的主要内容（重点部分）
用较充分篇幅概括核心研究内容，包括：
- 研究目标与整体技术思路
- 关键方法机制与实现逻辑
- 重要模型、算法或理论框架
- 至少给出一个关键公式，并解释符号含义
本部分应为全文篇幅最长、技术细节最充实的部分。
---
## 2. 论文试图解决的核心问题
说明：
- 核心科学或工程问题
- 发表当时的现实背景或应用场景
- 为什么该问题在当时具有研究价值
---
## 3. 论文发表当年的研究现状（基于当时年代）
围绕论文发表时间点说明：
- 主流研究方法或技术路线
- 已知关键瓶颈或理论限制
- 尚未有效解决的重要空白
必须采用历史视角，不能以后来的技术水平评价。
---
## 4. 论文提出的核心方法与技术思路
说明：
- 方法的核心设计思想
- 基本原理或理论基础
- 与以往方法的本质区别
- 新思路可能更有效的原因
---
## 5. 作者如何验证方法有效
介绍：
- 实验设计或理论证明方式
- 使用的数据集、任务场景或实验环境
- 评价指标与对比方法
- 结果的说服力及可能不足
---
## 6. 该工作的潜在缺陷与局限
在理解论文基础上做合理学术推测，例如：
- 假设条件是否过强
- 泛化能力可能的限制
- 规模、计算或数据方面约束
- 对噪声或分布变化的鲁棒性
- 实验覆盖是否充分
---
## 7. 论文发表之后的后续发展
简要概述：
- 后续重要技术进展
- 研究范式变化
- 是否出现更主流或更有效的替代思路
强调学术脉络延续，而非简单罗列名词。
---
## 行文风格要求
- 以分析性段落为主，不以条目堆砌为主体
- 强调理解、归纳与解释
- 不照抄论文摘要
- 不使用宣传或营销语气
---
## 输出格式（必须严格满足）
1. md 文件名称 = PDF 文件名
2. 正文第一行 = AI Summary
3. 从下一行开始按上述 7 部分顺序撰写，每部分带分标题
4. 每篇文献笔记需生成以 PDF 文件名命名的 Markdown 文件
5. 每一个自然段之间必须空一行（强制要求）
6. 公式要使用 `$公式$`，不要使用 `$$`

---

## 实际使用中发现的改进点（v2 补充）

基于一次 200+ 篇批量总结的反馈，以下问题需特别注意：

### 关于 LaTeX 公式
- 下划线必须转义：`$H\_k$`、`$R\_{ij}$`、`$\phi\_{sd}$`，否则某些 Markdown 渲染器会把下划线识别为斜体
- 优先使用 `\dot{x}`、`\ddot{x}` 而不是 `x'`、`x''`
- 矩阵和向量用 LaTeX 严格表示

### 关于第 6 节（局限性）
- 禁止硬套"当前热门方向"的批评。先找论文本身真实存在的局限（假设、尺度、鲁棒性、泛化、实验覆盖），只有当本文自然和某个方向挂得上钩时才谈该方向
- 宁可不写也不硬凑。一段扎实的 paper-specific 批评胜过三段空洞的"未来工作"

### 关于第 7 节（后续发展）
- **严禁 speculate**。对不熟悉的子领域、不熟悉的时间段（尤其老论文），不要编造"后来 XX 成为主流"——这经不起查证
- 不确定就简短收尾。比如"该方向后续发展详见领域综述，本文年代较早暂不深入"好过瞎编
- 可以写的：和本文同组作者的直接后续工作；本文直接引用过的、明确说"will extend to ..."的方向
