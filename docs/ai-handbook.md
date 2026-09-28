# repo AI 辅助层 · 用户使用手册

> 面向对象：全体使用 `repo` 的研发同学。
> 看完这一篇就能上手；命令细节可再查 `docs/ai-user-guide.md`，
> 一页速查可打印 `docs/ai-quickref.md`。

---

## 0. 它是什么，对你意味着什么

repo 命令行的 AI 辅助层，在你**命令失败的时候**帮你把"为什么失败、
下一步做什么"说清楚。它不是聊天机器人，核心场景就一句话：

> **sync/upload 报错了 → 终端会自己多出一段诊断建议；不够用再问一句。**

三条铁律，先吃定心丸：

1. **原命令一切照旧**。`repo sync` 的输出、退出码、行为没有任何改动，
   脚本和 CI 完全不受影响。AI 段是失败后**追加**在 stderr 的提示。
2. **新命令全部只读**。`diagnose` / `ask` / `report` 这些只看不改，
   不会动你的代码和工作区。
3. **不用也能过**。不想用就把开关关掉（§5），关了之后你和以前没有
   任何区别。

一句话风险提示：**AI 生成的建议步骤里可能含删除/清理类命令
（如 `rm -rf`、`git clean`）。执行前自己判断；拿不准就点 `[忽略]`，
用 `repo explain` 看人工排查步骤。**

---

## 1. 获取（你大概率什么都不用做）

AI 层随 `repo` 工具本体一起分发（同一个仓库、同一份代码，**零额外依赖**：
只用 Python 标准库，装好 repo 就有）。**大多数人不需要任何安装动作**：
平时 `repo sync` 工作区、或重新获取 `repo` 启动器后自然升级。

需要确认自己在带 AI 层的版本上时（比如手上是旧启动器/上游 repo）：

```bash
repo init --repo-url <本仓库的 git 地址> --repo-rev <AI 层分支>
```

已 init 过的工作区重跑一次 `repo init` 即可切换启动源，不用重建工作区。
判断是否生效：`repo diagnose --help` 能打开即已就位。

安装后自检（只读，一条命令，全 [OK] 即可）：

```bash
cd <你的工作区>
python3 .repo/repo/contrib/pilot_check.py --offline
```

---

## 2. 配置（共 4 行，1 分钟）

配置写在你自己的 `~/.repoconfig/config`，用 `git config` 写即可：

```bash
mkdir -p ~/.repoconfig

# ① AI 网关（可跳过：不配也能用全部本地功能，只是没有 AI 问答/追问）
#    任何 OpenAI 兼容 /v1 端点都行：自建 vLLM / Ollama / LiteLLM /
#    one-api 网关 / 各家云端 API 的兼容端点。
git config --file ~/.repoconfig/config ai.gateway.url   http://gateway.example.com:8000/v1
git config --file ~/.repoconfig/config ai.gateway.token <你的网关 key>
# 选一个实际可用的模型名（列表由你的网关给出；本工具不内置模型名）
repo models                 # 列出 + 探活
repo models --use <模型名>   # 探活通过才写入配置

# ② 使用统计上报（可选，团队自建收集器：contrib/collector-deploy.md）
git config --file ~/.repoconfig/config ai.telemetry.endpoint http://collector.example.com:8080
git config --file ~/.repoconfig/config ai.telemetry.token    <收集器token>
```

**多模型/多端点**：`ai.gateway.url` 决定协议（OpenAI 兼容即可），
`ai.gateway.model` 决定模型，`ai.gateway.model-fallback` 配一个备胎；
主模型 5xx/404 时**大声提示并回退**，绝不静默换模型。
Anthropic / Gemini 原生协议经 LiteLLM 一类转换网关接入。

> key/口令都是**凭据，等同密码**：只写进 `~/.repoconfig/config`（建议
> `chmod 600 ~/.repoconfig/config`，多人共用机器必做），不要提交到任何
> git 仓库、不要写进 `.repo/config`（会随仓库扩散）。两类值别混：
> `ai.gateway.token` 是**你个人的**网关 key；`ai.telemetry.token` 是
> 全收集器**统一的**上报口令。

配好后自检（只读）：

```bash
repo doctor            # 工作区体检 + AI 层状态
repo models            # 网关通了会列出可用模型
repo telemetry         # 显示当前上报区域/通道
```

不配 ② 也完全能用：事件留在本地队列，只是管理员看不到统计。
不配 ① 也能用：自动退回本地规则库（离线也准，只是少了 AI 追问）。

---

## 3. 日常怎么用（三个场景）

### 场景 A：命令失败了（最高频）

```bash
repo sync            # 照常敲，失败了……
```

终端会自动多出一段：

```
----------------------------------------
💡 诊断建议（本地规则库 + AI 服务）
R1021：项目不存在/当前目录无项目
根因：路径不属于任何 manifest project，或 groups 不匹配。
建议步骤：
  1. repo list | grep <关键字> 确认 project 名/路径
  ...
AI 分析（AI 服务提供）： ...
[采纳] [忽略] [不再显示]
```

- 按提示的步骤走即可；**含删除动作的步骤先自行判断**（见 §0 铁律）。
- `[采纳]/[忽略]` 回车即选，帮我们把诊断做得更准；对某条规则烦了
  选 `[不再显示]`，或直接关它：
  `git config --file ~/.repoconfig/config ai.hintOff.R1021 true`。
- 建议还不够？`repo diagnose` 完整复盘（规则 + 排查步骤 + 相关 FAQ）。
- 要提工单：`repo report` 生成**自动脱敏**的诊断包 zip，直接附到工单。

### 场景 B：想问 repo 怎么用

```bash
repo ask "只想同步一个模块怎么做"
repo ask -c "那怎么只拉今天的增量"      # -c 继续追问
repo ask --reset "换个话题"             # 开新会话
```

### 场景 C：查知识，不联网也行

```bash
repo faq 断点          # 离线知识库搜关键词
repo explain R1021     # 解释一条规则
repo explain "missing change-id"   # 解释一段报错/一个命令
repo rules             # 浏览规则库全表
```

**记住两个就够：失败了 → `repo diagnose`；要提单 → `repo report`。**

---

## 4. 隐私：我们会看到什么，看不到什么

**会上报**（结构化元数据，用于统计哪类问题最常见）：

- 命令名（如 `sync`）、异常类型名、命中规则 ID 与匹配置信度分数
  （0~1 的小数）、耗时、大区域标签
- 你主动点的反馈（有用/没用）
- 建议给出后**同一条命令下一次**执行的成功/失败与间隔秒数（隐式效果
  判定，不含参数内容；没再跑就什么都不报。仅按规则聚合统计，不用于
  任何个人画像）

**绝不上报**：

- ❌ 代码内容、日志原文、文件路径、用户名、机器名
- ❌ `repo ask` / `repo faq` 的问题文本（问了什么只有你自己知道）
- ❌ 清单/manifest 内容、git 地址

技术上的保证，不是口头承诺：事件在你本机就是按"不含敏感字段"的
结构写入的，压根没有装这些内容的字段；上报前还有脱敏红线测试。
随时可查本机要上报什么：

```bash
repo telemetry --export /tmp/t.ndjson   # 导出看，一眼能核对
```

上报默认在产生诊断事件后顺带进行（15 分钟节流，3 秒超时，不拖慢
命令）。断网/主收集器不可达时事件留在本地，恢复后自动补报，不丢。

---

## 5. 开关（想关就关，一键）

```bash
# 完全关闭 AI 层（回到没有它的一切，逐字节无输出差异）
git config --file ~/.repoconfig/config ai.enabled false

# 只关"失败后自动追加的建议"，保留主动查询命令
git config --file ~/.repoconfig/config ai.autoHint false

# 只关统计上报（本地功能全保留，事件留在本地队列）
git config --file ~/.repoconfig/config ai.telemetry.enabled false

# 关某一条规则的提示（规则 ID 见提示头部）
git config --file ~/.repoconfig/config ai.hintOff.R1021 true
```

改回来把 `false` 换成 `true`（hintOff 反向，删掉该行或改 `false`
即恢复）。

---

## 6. 常见问题

**Q：AI 给的建议可以直接照做吗？**
不要盲从。AI 步骤可能包含删除/清理类命令（`rm -rf`、`git clean`、
强制 checkout 等）。**动手前自己判断，拿不准先问 owner 或走工单。**
规则库给出的人工步骤（`repo explain <规则号>`）是保守版。

**Q：我在隔离网/断网，功能还在吗？**
在。自动切本地规则库模式，提示会如实标注"本地规则库"，`ask`
等在线功能会明确告诉你暂时不可用，**不会假装成功**。

**Q：`repo doctor` 说网关不可达？**
检查 `ai.gateway.url` 是否带 `/v1`、key 是否过期、代理是否要配。
仍不通就找管理员——期间本地功能照常。

**Q：它会不会偷偷改我的代码/工作区？**
不会。所有 AI 命令只读；原命令的行为路径没有被改动过（有测试逐
字节钉死输出与退出码）。

**Q：提示太频繁/太吵？**
`[不再显示]`，或关某条规则：
`git config --file ~/.repoconfig/config ai.hintOff.<规则ID> true`；
再不行 `ai.autoHint false` 全关自动提示。

**Q：诊断不准怎么办？**
点 `[忽略]` 就是负反馈；或 `repo feedback` 选 `[4] 不准确`。管理员每周按
看板上的"未命中缺口"与低解决率规则迭代规则库，你的反馈直接进迭代输入。

**Q：同事没配网关/统计，能用吗？**
能。那些只是锦上添花；不配也享受本地规则库诊断。

---

## 7. 支持

- 遇到问题：工单/群里附 `repo report` 生成的 zip（内含脱敏后的现场信息）
- 管理员看板：浏览器打开收集器地址，输入 token
- 你的反馈入口：`repo feedback`（已解决/未解决/部分有用/不准确）

---

*本手册对应能力：rules / faq / explain /
report / feedback / telemetry / doctor / ask / diagnose / models。*
