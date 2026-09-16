<!-- Language switch -->
[English](README.md) · **中文**

# 📚 文献监测机器人 (Literature Monitor Bot)

一个 **由智能体部署**、全自动的文献雷达。它每天两次扫描 **bioRxiv**，每周通过 NCBI 扫描
**一组可配置的期刊**，并可选地通过 OAI-PMH 抓取 **不在 PubMed 中的期刊**；每篇论文都经过一个
三级过滤漏斗，最后把一张精炼的中文摘要卡片推送到 **Discord webhook**（也支持 Telegram）。
推送之后，你可以直接在聊天频道里追问 —— 展开某一条、深度精读全文 PDF、或者调取某张图。

> 📡 **我们为每一种数据源都提供了成文的抓取策略** —— bioRxiv API、PubMed 按 ISSN、
> 面向非 PubMed 期刊的 OAI-PMH，以及 RSS 方案 —— 并记录了每一种各自需要的坑与硬化。
> 详见 **[docs/feed-sources.md](docs/feed-sources.md)**。

### 它跑在哪里：一台永不关机的电脑 → 你的手机

它被设计成运行在一台**永远不会关机**的电脑上 —— 一个 **HPC 登录节点、一台实验室工作站，
或者一个 NAS**。这台机器全天候安静地跑定时扫描，而真正到达 **你** 面前的，只是一条推送到
**Discord 或 Telegram** 的整洁消息。它的意义在于，把一台你本来就一直开着的机器变成一个
**由 AI 精选的信息流**：与其自己去刷 bioRxiv 和十几本期刊的目录，不如让这台常开的主机替你
阅读、替你筛选，只把少数几篇值得你花时间的论文直接送到你手机上的 app 里 —— 在那里你还可以
让它继续深挖。

参考部署方式是在 **HPC 上通过 `cron → sbatch`** 运行，因此无论你有没有开着会话，扫描都照常
工作。核心流水线只是纯 Python + `claude` CLI，所以在工作站或 NAS 上，你可以直接用 `cron`
（或 `systemd` timer）来驱动 `run_biorxiv.sh` / `run_journals.sh`，完全跳过 SLURM 那一层。

---

## ⭐ 它的不同之处：它是 *智能体驱动* 的

大多数文献提醒工具都要 **你自己** 手写一份脆弱的关键词列表，然后碰运气。而这个工具从设计上
就是 **交给一个智能体（coding agent，例如 [Claude Code](https://claude.com/claude-code)）来部署** 的，
这彻底改变了配置这一步。

> **研究方向在代码里是完全留空的，这是故意的。**
> 代码里任何地方都没有硬编码任何研究主题 —— 每一个 LLM prompt 都在运行时从配置文件读取你的方向。

**推荐的部署方式就是直接跟智能体对话。** 你不用手工去编辑配置文件，而是：

1. **向智能体描述你的研究** —— 用一两段话说清楚你关心的方法/技术、生物学系统或科学问题，
   以及什么算命中、什么算噪音。
2. **给它 3–5 篇「必须抓到」的示例论文** —— 那些你绝对希望被标记出来的文章。
3. **让智能体替你写配置** —— 它会起草：
   - `config/research_focus.txt` —— 注入到每个 LLM prompt 里的研究方向陈述；
   - `config/keywords.txt` —— 免费的第一级预筛关键词（为了召回率，保持宽泛）；
   - `config/journals.json` + `config/categories.txt` —— 要监测哪些期刊、哪些 bioRxiv 分类。
4. **和智能体一起迭代** —— 它会在最近的论文上空跑一遍漏斗，你看看哪些通过了、哪些没通过，
   它再调整方向/关键词，直到 **针对你的领域** 的精确率和召回率都合适 —— 而不是别人的领域。

最终你得到的是一个**通过一次对话、按你自己的研究调好**的监测器，而不是靠你去背一套配置格式。
这种「智能体在环」的调优，正是本工具的核心特性。

完整的分步部署指南见 **[SKILL.md](SKILL.md)**（它同时也是一个
[Claude Code Skill](https://claude.com/claude-code) —— 把这个文件夹放进 `~/.claude/skills/`，
智能体就能替你把整套东西部署起来）。

---

## 工作原理 —— 三级漏斗

```
数据源 (bioRxiv API / NCBI E-utils / OAI-PMH)
  │  抓取 + 去重 (cache/seen.sqlite，按 doi+version)
  ▼
第 1 级  分类白名单 + 关键词预筛                    ← 免费，不用 LLM
  │
  ▼
第 2 级  标题初筛：所有标题放进一次 LLM 调用 → yes/maybe/no
  │        (yes + maybe 通过)
  ▼
第 3 级  摘要评审：每次 LLM 调用 8 篇 → 打分 0-5 + 中文摘要 + 点评
  │        (相关 = 得分 ≥ 3)
  ▼
推送     得分 ≥ 3 → Discord 卡片 (+ 归档 + 写入 state/latest_digest.json)
```

漏斗就是精髓所在：两个廉价的前置阶段先扔掉约 95% 不相关的论文，因此只有少数几篇会真正消耗
LLM token。典型开销约为 **每次运行 10–25K token** —— 比直接逐篇评审所有摘要 **便宜约 15 倍**。

### 两条通道，刻意为之

- **对外推送 = 一个 Discord incoming webhook。** 就是 cron 发出的一次普通 HTTPS POST ——
  没有常驻进程、没有网关、没有需要刷新的 token。定时推送在没有任何会话时也照常工作。
- **追问问答 = 一个绑定到项目的聊天机器人。** 它从磁盘读取摘要，而不是从聊天记录里读，
  所以即使推送落在了另一个频道也照样能用，webhook 也无需做成双向的。

---

## 💬 追问 / 问答系统

摘要推送到之后，摘要本身只是个开始。一个绑定到项目的聊天机器人通过从磁盘读取
`state/latest_digest.json`（以及 `digests/runs/` 下的每次运行归档）来回答追问 ——
因此不用重新抓取、也不用重新扫描：

- **展开某一条** —— 「展开第 3 条」/「expand #3」 —— 从上一次摘要里调出那篇论文的完整摘要和
  元数据。不调用 LLM、不重新抓取。支持按序号 **或** 按标题子串查找。
- **深度精读** —— 「深度追问」/「deep-dive」 —— 对该论文运行 `deep_analyze.py`：它会抓取
  **全文**（JATS → cookie 预热的 PDF → Firecrawl → 摘要兜底），把整篇论文喂给 LLM，
  返回一份多段落的精读。是全文 PDF，而不只是摘要。
- **给我看某张图** —— 「给我看 Fig.1」/「show me Fig. 1」 —— 运行 `get_figure.py`：它按图注
  定位图，渲染 **整页**（这样多面板的图不会被拆碎），把图片发回聊天频道。
- **Journal Club 准备** —— `jc_prep.py` 把一篇 PDF 变成完整的讲解材料：逐图解读、方法学
  深挖、以及一份幻灯片大纲。

这些都设计成从聊天频道里驱动 —— 你读完摘要，然后直接问就行。抓取、渲染、总结都由机器人来做。

问答机器人是一个绑定到项目的 Claude Code 机器人，按通道分别配置 —— 见
**[templates/discord/SETUP.md](templates/discord/SETUP.md)**
（→ [claude-code-discord-multibot](https://github.com/Lihan-Zhong/claude-code-discord-multibot)）
或 **[templates/telegram/SETUP.md](templates/telegram/SETUP.md)**
（→ [claude-code-telegram-multibot](https://github.com/Lihan-Zhong/claude-code-telegram-multibot)）。

> 🧩 **想换别的聊天平台**（比如微信 / WeiXin），**或者用 Codex 智能体**而不是 Claude Code？
> 这两种桥接方式都可以参考 **[codex-chat-bridge](https://github.com/Lihan-Zhong/codex-chat-bridge)**。

---

## 快速开始

简版（完整细节见 **[SKILL.md](SKILL.md)**）：

1. **复制模板** —— 把共享的 `templates/scripts/` 加上**一个通道文件夹**（`discord/` 默认，
   或 `telegram/`）复制到一个你拥有的项目根目录，填好那些一目了然的占位符（路径、SLURM
   分区/账户、给 NCBI/Crossref 礼貌池用的联系邮箱）。
2. **加上你的 Discord webhook** —— 写进 `state/discord_webhook.txt`（chmod 600）。用
   `discord/push_discord.py --healthcheck` 验证（它只 GET webhook，不发帖）。
3. **设定你的研究方向** —— 复制 `.example` 配置文件，并且最好 **让 agent 替你来写**
   （见上面的 *agentic* 一节）。
4. **接上 cron** —— `cron_submit_*.sh` 会向计算节点提交一个 `sbatch` 作业。
5. **在信任 cron 之前，先手动逐级冒烟测试：**
   ```bash
   scripts/fetch_biorxiv.py --days 2 | tee cand.jsonl
   scripts/triage_titles.py --keep yes,maybe < cand.jsonl | tee tri.jsonl
   scripts/llm_judge.py --batch 8 --keep-all < tri.jsonl | tee jud.jsonl
   discord/push_discord.py --dry-run < jud.jsonl        # 只渲染，不发送
   ```

---

## 可靠性：失败是「响亮的」，绝不伪装成空结果

对一个文献机器人来说，最危险的失败是那种**看起来像平静的一天**的失败。如果某次运行推送了
**「今日无相关文献」**，它可能意味着两种完全不同的情况：

1. **真的没有** —— LLM 跑了、评审了，只是没有一篇得分 ≥ 3；或者
2. **一次静默失败** —— 某个阶段崩了，产出了 0 个候选，然后推了一张伪装成空的摘要，
   看起来和第 1 种一模一样。

本仓库里的每一个阶段失败都被做成 **响亮的**：它会推送一条 ⚠️ 横幅，而不是伪装成空的摘要；
**不缓存**那些没推成功的论文（它们会在下一次运行时重试 —— 什么都不会丢）；并记录一个你能
grep 到的独特标记。已经处理好的情况 —— 额度耗尽、临时过载、认证过期、API 抖动超时、SLURM
作业卡住 —— 都记录在 [SKILL.md](SKILL.md) 的 *Operational resilience* 一节里。这些都是在
生产环境中踩坑踩出来的。

---

## 环境要求

- **带 SLURM 的 HPC**，且你在某个队列上有优先级（实验室的 condo / 配额）。作业本身很小 ——
  1 CPU、2 GB、1 小时。
- **Python 3.9+** —— 核心流水线只用标准库。`pypdf` + **poppler**（`pdftoppm` / `pdftotext`）
  只有深读 / 取图 / JC 工具才需要。
- **一个智能体 CLI** 在 `PATH` 上 —— 即 LLM 引擎。开箱即用接了
  **[Claude Code](https://claude.com/claude-code)**（`claude -p --tools ""`，prompt 通过
  **stdin** 传入）；也可以换用 **Codex 智能体** —— 参考
  **[codex-chat-bridge](https://github.com/Lihan-Zhong/codex-chat-bridge)**。
- **一个 Discord incoming webhook**（主通道）。Telegram bot token 可选。
- 可选：[Firecrawl](https://github.com/firecrawl/firecrawl) 作为深度追问的兜底抓取。

---

## 🔒 安全

**本仓库里没有任何 secret。** webhook URL、bot token、chat ID 只存在于你在部署时 **自己创建**
的配置文件里 —— 这些文件全部被 [`.gitignore`](.gitignore) 排除。随包发布的 `*.example` 文件
只包含占位符。

- **Discord webhook URL 是一个凭证** —— 任何拿到它的人都能往你的频道发帖。绝不要提交它。
- `.pyc` 文件会内嵌它编译时所在机器的绝对源码路径 —— `.gitignore` 已排除 `__pycache__/` 和
  `*.pyc`，这样你的路径不会泄漏进 git 历史。

---

## 📖 文档

- **[SKILL.md](SKILL.md)** —— 完整的分步部署指南（同时也是一个 Claude Code Skill）。
- **[docs/feed-sources.md](docs/feed-sources.md)** —— 数据源与抓取策略：bioRxiv / PubMed-按-ISSN /
  OAI-PMH / RSS 各自的坑与硬化，以及如何添加一个新数据源。

## 许可证

[MIT](LICENSE) © 2026 Lihan Zhong
