# repo AI 辅助层：安装及使用手册

适用于 repo 工具的 AI 辅助层（纯标准库实现，随 repo 本体分发）。
设计理念：离线优先、零依赖、对原命令零侵入、可一键关闭。

本手册分三部分：[安装与启用](#一安装与启用)、[日常使用](#二日常使用)、
[管理员指南](#三管理员指南)，最后是[配置参考](#四配置参考)与
[常见问题](#五常见问题)。

> 老手速查请看一页纸 `docs/ai-quickref.md`（可打印贴工位）；
> 部署收集器见 `contrib/collector-deploy.md`。

---

## 一、安装与启用

### 1.1 你需要知道的两件事

1. **AI 辅助层是原 repo 命令之上的可选层。** `repo init / sync / upload`
   等所有原有命令的行为、输出、退出码完全不变。命令失败后，终端可能在
   原有报错之后**追加**一段"诊断建议"，仅此而已。
2. **零新增依赖，离线可用。** 不需要 pip、不需要外网。隔离区（无外网
   开发环境）功能完整：本地规则诊断、知识库搜索、诊断包生成全部可用。

### 1.2 环境要求

| 项目 | 要求 | 说明 |
| --- | --- | --- |
| Python | ≥ 3.6 | 与 repo 本体要求一致；使用系统 python3（或 launcher 选定的解释器） |
| Git | ≥ 1.9.1 | 与 repo 本体一致 |
| 第三方包 | 无 | 纯 Python 标准库实现 |
| 网络 | 可选 | 无网络时自动按隔离区工作 |

### 1.3 安装（更新 repo 工具）

AI 辅助层随 repo 工具本体分发，**不需要单独安装**。拿到带 AI 层的
repo 代码即完成安装，方式三选一：

**方式 A：repo init 指定分支（新初始化的工作区）**

```bash
repo init -u <manifest仓库地址> -b <manifest分支> \
    --repo-url=<repo工具仓库地址> --repo-rev=<AI层分支>
```

> repo 启动器默认拉上游 `main`（不带 AI 层），所以 `--repo-url` 与
> `--repo-rev` 两个参数都必须给。

**方式 B：repo selfupdate（AI 层已合入 repo 工具默认分支时，推荐）**

```bash
repo selfupdate
```

> `repo selfupdate` 拉的是 `repo init` 时记录的分支；只有该分支本身
> 带 AI 层时才有效，否则请用方式 A 或 C。

**方式 C：手动切换工具代码分支（试用/灰度推荐）**

```bash
cd <工作区>/.repo/repo
git fetch origin <AI 层分支>
git checkout <AI 层分支>
```

> 无外网的环境由管理员把该分支同步到内网 git 镜像，再按上述方式更新。
> 注意 repo 启动器默认拉上游 `main`，因此 `repo init` 必须带
> `--repo-url`/`--repo-rev` 指向本仓库与 AI 层分支。

### 1.4 验证安装

```bash
repo rules            # 应列出 R1001…R1040 内置诊断规则（共 40 条）
repo faq 同步         # 应返回知识库搜索结果
```

两条命令都有输出即安装成功。**默认开箱即用**，无需任何配置。

一键全面自检（只读，不改任何文件；装完/报障前推荐先跑）：

```bash
python3 .repo/repo/contrib/pilot_check.py        # 或加 --offline
```

输出逐项 `[OK]/[WARN]/[FAIL]`：FAIL 需处理，WARN 逐条确认原因即可。
脚本检查 Python 版本、配置优先级、规则/知识库加载、网关与收集器连通、
队列与隐私抽查。

### 1.5 关闭 / 再开启 AI 层

> 以下命令要求 `~/.repoconfig` 目录存在（运行过 `repo init` 即已创建；
> 没有则先执行 `mkdir -p ~/.repoconfig`）。

一键全局关闭（关闭后与未改造版本行为完全一致）：

```bash
git config --file ~/.repoconfig/config ai.enabled false
```

恢复：

```bash
git config --file ~/.repoconfig/config --unset ai.enabled
```

只关闭"失败后自动追加诊断建议"，保留 AI 命令可用：

```bash
git config --file ~/.repoconfig/config ai.autoHint false
```

> 管理员策略可以强制关闭（用户配置无法覆盖），见 [3.2 节](#32-策略锁管理员策略文件)。

---

## 二、日常使用

### 2.1 核心流程：失败 → 诊断 → 深入 → 反馈

```
repo sync 失败
   │  原有错误输出不变，之后自动追加：
   ▼
💡 诊断建议（本地规则库）
R1002：sync 失败 - manifest revision 冲突/不存在
根因：manifest 中某 project 的 revision 在远端不存在…
建议步骤：
  1. 检查 .repo/manifests/default.xml …
  2. 确认该分支/tag 已推送到远端：git ls-remote …
  3. 临时方案：repo sync -d 放弃本地分支重新对齐
运行 repo explain R1002 查看完整排查步骤
[忽略] [不再显示]
   │
   ├─ 想看更多 → repo explain R1002      （完整排查步骤）
   ├─ 想搜资料 → repo faq revision 冲突   （知识库）
   ├─ 要提工单 → repo report              （脱敏诊断包）
   └─ 评价效果 → repo feedback --resolved （有用）
                repo feedback --unresolved（没用）
```

### 2.2 repo explain —— 解释规则 / 命令 / 错误原文

```bash
repo explain R1002           # 按规则 ID 查看完整排查步骤
repo explain sync            # 查看某命令的用途与帮助入口
repo explain "error: Cannot checkout project foo/bar: revision conflict"
                             # 粘贴错误原文，自动匹配规则库
```

- 输入规则 ID：显示根因、完整排查步骤、相关规则与 FAQ。
- 输入错误原文：规则库未命中时自动回退知识库搜索。
- 配置了 AI 网关（`ai.gateway.url`）时，规则详情会由 AI 在线补充；
  网关不可用会明确提示"AI 服务暂不可用，使用本地规则库结果"，不会静默。

### 2.3 repo faq —— 离线知识库搜索

```bash
repo faq 部分同步            # 关键词搜索（支持中文、英文）
repo faq no branches ready   # 英文同样可搜
repo faq --show 1002         # 显示指定条目全文
repo faq 冲突 -n 10          # 最多显示 10 条结果
```

管理员可通过离线包补充条目（`kb.d/`），普通用户无需维护。

### 2.4 repo report —— 生成提工单用的诊断包

```bash
repo report                  # 生成诊断包（zip）
repo report --preview        # 先看包含什么，不落盘
repo report -o ~/tmp         # 输出到指定目录
repo report --submit         # 提交工单系统（配置了端点时）
```

诊断包内容（全部自动脱敏）：

| 文件 | 内容 |
| --- | --- |
| meta.json | 版本、区域、平台、python 版本、client_id |
| failure.json | 最近一次失败（命令、异常、错误文本，已脱敏） |
| manifest_summary.json | project 数量、主机名等统计（不含仓库清单原文） |
| git_status.json | 各 project 是否脏、所在分支（路径已脱敏） |
| events_summary.json | 本地事件队列概况 |
| PREVIEW.txt | 人读预览 |

脱敏规则：URL 中的用户名 → `<user>`；`/home`、`/Users`、`/data` 下的
个人路径 → `/<home>/…`；password/token 样式字符串 → `<redacted>`。
**诊断包不包含源码、日志原文、个人绝对路径。**

### 2.5 repo feedback —— 评价诊断效果

```bash
repo feedback --resolved     # 建议解决了问题
repo feedback --unresolved   # 没解决
repo feedback --partial      # 部分有用
repo feedback --inaccurate   # 建议方向不对
repo feedback --rule R1002   # 指定评价哪条规则（默认取最近一次提示）
```

终端环境下直接运行 `repo feedback` 会交互式询问。打开
`ai.feedback.hint=true` 后，错误诊断建议之后还会追加一行一键反馈
（`[1]已解决 [2]未解决 [回车]跳过`）；仅在交互终端出现，管道/CI 输出
不受影响。反馈先写入本地队列，再按区域通道上报（在线区实时、隔离区随
离线报表），管理员据此优化规则库。

### 2.6 repo rules —— 查看/导入/导出离线规则库

```bash
repo rules                   # 列出全部规则（内置 + 管理员离线包）
repo rules --import 规则包.json   # 导入管理员下发的规则包
repo rules --export 规则包.json   # 导出当前规则（管理员维护用）
repo rules --lint 规则包.json     # 校验规则包（正则/引用/子命令），退出码非
                                 # 0 即有问题；不填文件则校验当前生效规则库
```

规则包为 JSON 文件，导入前校验格式，导入后存放在本地状态目录，按规则
ID 覆盖同名内置规则。管理员用法见 [3.3 节](#33-离线规则与知识库包)。

### 2.7 repo telemetry —— 查看与上报本地事件

```bash
repo telemetry                 # 查看区域/通道/待上报事件数
repo telemetry --flush         # 按当前区域通道上报一批
repo telemetry --flush --all   # 在线通道：持续上报直到队列清空
repo telemetry --export 文件    # 导出 NDJSON 摆渡文件（R2/R3 回收用）
```

事件先入本地队列再按通道出网：**在线区**默认自动上报（诊断产生事件后
顺带后台推送，15 分钟节流、3 秒超时，不影响命令体验；可用
`ai.telemetry.auto-flush=false` 关闭，改回手动 `--flush`）。上报目的地
就是 `ai.telemetry.endpoint` 那一个收集器——**你的 endpoint 配你网络
够得到的那一台**（例如 110 网段配 110，60 侧的用户配 60，两拨数据互
不掺和）。主端点不可达时事件留在本地队列，恢复后自动补报，不丢。（
管理员可按需开启 `ai.telemetry.fallback-endpoint` 做宕机转存：备用端
收下后事件仍留队列、主端点恢复后照常补报，服务端按事件 ID 去重不丢
不重；默认关闭。）也可以再配 `ai.telemetry.fallback-dir`（本地已挂载
的共享目录）优先落盘摆渡。**R2** 写摆渡目录（须已存在/已挂载）；**R3**
保留本地、用 `--export` 人工带出。导出与重传均安全——服务端按事件 ID
去重。事件内容是结构化元数据（命令名、异常类型、命中规则 ID 与置信度
分数、耗时等），不含代码、日志原文与清单内容。

### 2.8 repo ask —— 自然语言问答

```bash
repo ask "只想同步一个模块怎么操作"
repo ask -c "那只影响某一个项目可以吗"   # 追问（多轮）
repo ask --reset                        # 清空会话
```

- **多轮对话**：`-c` 在上一轮基础上追问（本地记住最近 4 轮问答，存在
  状态目录 `ask_session.json`，永不进入上报）。回答末尾提示轮次，如
  "第 2 轮，-c 可继续追问"。
- **在线区**：走 AI 网关，并附带**已脱敏的工作区概况**（project 数、
  repo 版本、本地规则库/知识库命中条目、最近一次失败摘要），回答更贴合
  你的环境；回答末尾始终标注来源。
- **隔离区 / 网关不可用**：自动改用本地规则库 + 离线知识库回答，并明确
  标注"离线来源"——命令格式完全一致，只是引擎不同，绝不静默切换。
- **隐私**：问答内容用于生成回答时会发给网关（这是问答本身的必然），
  但**不会**进入上报队列——事件里只记录"问过、来源是 AI 还是本地"，
  不含问题文本。本命令只读，不会执行任何变更。会话文件也只在本地，
  `repo ask --reset` 即可清除。

### 2.9 repo doctor —— 工作区体检

```bash
repo doctor          # 只读体检，不修改任何文件
repo doctor --json   # 机器可读输出
```

检查磁盘空间、`.repo` 可写性、manifest/本地片段 XML 合法性、git 版本、
近期反复失败模式、事件队列积压与区域通道。全部离线、只读、无副作用，
**工作区坏了也能跑**（这正是它最该用的时候）。退出码：有警告或失败为 1，
否则 0。适合在提工单前先跑一遍，很多"网络问题"其实是磁盘满或权限错。

可选的**命令前预检**（默认关闭）：打开 `ai.preflight=true` 后，在执行
`sync/init/upload/download` 前若发现磁盘将满或同类失败反复出现，会在
命令开始前打印一行提示（指向 `repo doctor`）。它只提示、绝不拦截或修改
命令；每小时最多提示一次；管道/脚本输出不受影响。

### 2.10 repo diagnose —— 深度分析最近一次失败

```bash
repo diagnose
```

自动追加的诊断建议是精简版；`repo diagnose` 对**最近一次失败**（已脱敏
存储）给出完整版：每条命中规则含完整排查步骤与相关规则、相关 FAQ 条目，
在线区再附一段 AI 服务分析。刚处理完故障想复盘、或失败时错过了提示，
都用它。只读，不重新执行任何命令、不产生新记录。

### 2.11 repo models —— 查看/切换 AI 模型

```bash
repo models                    # 列出网关上实际可用的模型（含 当前/回退 标记）
repo models --probe            # 逐个发一条测试请求，验证真能回答（较慢）
repo models --use <模型名>              # 验证可用后设为默认
```

- 模型清单来自网关 `GET /v1/models` 实时探测，**不是写死的**——网关
  新上/下线模型，这里自动跟着变。本工具不内置任何模型名。
- **列表里有 ≠ 你的账户能调**：`--probe`/`--use` 会真实发一条最小请求
  验证，探活失败（如 404/503，多为账户未开通）会**拒绝切换**并说明原因，
  不会把你切到一个用不了的模型上。
- 切换写入 `~/.repoconfig/config` 的 `ai.gateway.model`（与手动配置同一
  个键）；探活成功才写。未配置模型时在线增强会明确报错并回退本地结果。
- 运行期若配置的模型突然不可用，工具自动回退 `ai.gateway.model-fallback`
  并提示用 `repo models` 检查——不会静默降级。

### 2.12 数据存哪里

本地状态目录默认 `<工作区>/.repo/ai/`（可用 `ai.state-dir` 改）：

```
.repo/ai/
├── events.sqlite          # 使用事件队列（上报前的暂存）
├── client_id              # 匿名客户端标识（随机生成，无个人信息）
├── last_failure.json      # 最近一次失败（已脱敏）
├── failure_history.jsonl  # 最近 200 次失败历史（已脱敏）
├── pending_outcome.json   # 待判定的建议效果（本地短态，判定即删；不入诊断包）
├── .preflight_last        # 预检提示节流标记（存在 ai.preflight 时）
├── rules.d/               # 管理员导入的规则离线包
├── kb.d/                  # 管理员导入的知识库离线包
├── ask_session.json       # repo ask 多轮会话（--reset 清除；永不上报）
└── diagnostics/           # repo report 生成的诊断包
```

删除整个目录即可清除全部本地 AI 数据，不影响任何 repo 工作区数据。

---

## 三、管理员指南

### 3.1 配置下发（用户级）

用户配置在 `~/.repoconfig/config`（git-config 格式，`REPO_CONFIG_DIR`
可改位置）；工作区级覆盖在 `.repo/config`。用 `git config` 写即可（首次
需 `mkdir -p ~/.repoconfig`），git 会自动生成正确的嵌套小节：

```bash
git config --file ~/.repoconfig/config ai.gateway.url     http://gateway.example.com:8000/v1
git config --file ~/.repoconfig/config ai.gateway.model   <模型名>   # repo models 里选
git config --file ~/.repoconfig/config ai.gateway.token   sk-xxxxxxxx
git config --file ~/.repoconfig/config ai.telemetry.endpoint http://collector.example.com:8080
# 数据只去你配置的那一台收集器。
# ai.telemetry.fallback-endpoint 默认关闭（跨机转存仅适合两边都
# 够得到的管理员机器，显式配置才生效）。
git config --file ~/.repoconfig/config ai.telemetry.auto-flush false   # 默认 true
```

> 模型不用凭记忆填：配好 url/token 后 `repo models` 列出真实清单，
> `repo models --use <名字>` 验证可用并自动写 `ai.gateway.model`。

> **密钥安全**：`ai.gateway.token` 是**你个人的**网关 key（每人一把，在网
> 关平台自行申请；与全体用户共用一把的 `ai.telemetry.token` 上报口令不
> 是一回事），等同密码——不要提交进任何 git 仓库、不要写进 `.repo/config`
> （工作区级文件会随仓库扩散）。放在 `~/.repoconfig/config` 并保证
> `chmod 600`（多人共用机器必做），或用环境变量 `REPO_AI_GATEWAY_TOKEN`
> 在登录时注入。key 泄露请自行在网关平台吊销重申请。

区域探测优先级：显式 `ai.region` > 自动探测（探测顺序：配置了
`ai.gateway.url` → `online`；配置了 `ai.telemetry.endpoint` →
`isolated-r1`；配置了可写的 `ai.telemetry.exchange-dir` → `isolated-r2`；
都不满足 → `isolated-r3`）。

### 3.2 策略锁（管理员策略文件）

路径按顺序取第一个存在的文件：`/etc/repo/ai.conf`（推荐，经配置管理
统一下发）、`~/.repoai/policy.conf`。

```ini
# /etc/repo/ai.conf —— 全网禁用 AI 层
[ai]
        enabled = false
```

策略锁语义：`ai.enabled`、`ai.telemetry.enabled` 两个键**只能被策略强制
关闭**——策略写 `false` 时用户无论如何设置都无效；策略未写或写 `true`
时用户可自行开关。其余 `ai.*` 键策略仅提供默认值，用户可覆盖。

策略文件生效后，`repo rules` 等 AI 命令会打印一行提示并退出码 1，
原 repo 命令不受任何影响。

### 3.3 离线规则与知识库包

规则库管理闭环（推荐流程）：在线区导出 → 管理员修订 → 隔离区导入。

```bash
repo rules --export rules.json                 # 导出当前生效规则
repo rules --export --local-only dept.json     # 只导管理员规则
repo rules --import dept-rules.json            # 安装规则包（隔离区可用）
```

手工编写的规则包同样合法，字段名注意与导出格式一致（根因是 `cause`）：

```json
{
  "rules": [
    {
      "id": "R1002",
      "title": "sync 失败 - manifest revision 冲突（部门补充）",
      "patterns": ["revision conflict"],
      "commands": ["sync"],
      "confidence": 0.9,
      "cause": "…本地修订说明…",
      "steps": ["第一步…", "第二步…"]
    }
  ]
}
```

规则包按规则 ID 覆盖内置规则，可增量只写需要修订/新增的条目；分发给
隔离区用户（U 盘/内网文件服务器均可）。知识库离线包为同机制的 JSON
（`{"entries": [{"id","title","body","tags"}]}`），放入 `<state>/kb.d/`
即生效。

### 3.4 离线数据回收（R2/R3 隔离区）

隔离区用户的使用事件、失败记录都在 `events.sqlite`。回收通道：

- **R2（可摆渡文件）**：配置 `ai.telemetry.exchange-dir` 指向摆渡目录
  （目录须已存在/已挂载，不存在时 flush 会明确报错而不是本地空建）。
  `repo telemetry --flush` 写出 NDJSON 批次文件，随摆渡带出。
- **R3（完全隔离）**：`repo telemetry --export <文件>` 人工导出，或随
  `repo report` 诊断包回收（`events_summary.json` 含队列概况）。

> 事件内容仅为结构化元数据（命令名、异常类型、命中规则 ID 与置信度
> 分数、耗时、区域等），不含日志原文与代码。批次文件导出**不会**清空
> 本地队列，重复导入由服务端按事件 ID 去重。

管理员回收侧（在线区任意机器，`repo ai-admin`）：

```bash
repo ai-admin import /path/to/batches/   # 导入 .ndjson 批次（目录或文件）
repo ai-admin summary                    # Top 高频问题/区域分布/解决率
```

`summary` 的"高频问题"下按规则列出两个口径，务必对照着看：

- **解决率（显式）**：来自 `repo feedback` / 一键反馈的判决明细（如
  "R1027 反馈 3 条（resolved 1、unresolved 2）：解决率 33% ⚠ 建议复核"）。
  反馈 ≥3 且半数以上判定 unresolved/inaccurate 的规则自动标注——这是每周
  规则迭代该先改谁的直接依据。
- **建议后成功率（隐式）**：看到建议后**同一条命令下一次执行**是否成功
  （如"建议后重试 6 次：建议后成功率 67%"）。样本量大得多，但它是**相关性
  而非因果**——网络抖动自愈同样记为成功，因此绝不能当解决率引用。两口径
  背离（隐式高、显式差）时报表提示"疑似环境自愈，先别归功于该规则"；
  显式反馈样本不足（<3 条）而隐式重试连续失败时，也会标注建议复核。

隐式判定：建议展示后在本地记下待判定项，窗口内（默认 30 分钟，
`ai.outcome.window` 可调）同命令再次执行才判定——成功记
`success_after_hint`、失败记 `retry_failed`、没再跑则**不产生任何事件**
（宁可缺数据也不猜）。上报内容仅命令名、规则 ID、间隔秒数、"参数是否
改动"一位布尔，不含参数值或任何原文。

小团队样本少时以趋势为主、别看绝对值。

事件存储默认 `~/.repoai/store.sqlite`（`ai.admin.store` 可改）。重复导入
安全（按事件 ID 去重）。这是离线最小看板；在线区的实时看板由上报端
（`ai.telemetry.endpoint` 服务端）承接。

### 3.5 在线上报接收端（S6 最小服务）

在线区可以先跑内置的 stdlib 收集器（管理员任意机器，无需安装任何东西）：

```bash
REPO_TELEMETRY_TOKEN=<一个随机长串> \
python3 -m repoai.collector --host 0.0.0.0 --port 8080 \
    --store ~/.repoai/store.sqlite --rotate-days 7
```

- 接收 `POST /v1/events`（NDJSON），写入与 `repo ai-admin summary` 同一个
  sqlite 存储，按事件 ID 去重——客户端至少一次投递，重复上报安全。
- `--token`（或环境变量 `REPO_TELEMETRY_TOKEN`）设置后，客户端须配置一致
  的 `ai.telemetry.token`；不设 token 会启动时大声警告"开放接收"。
- `GET /healthz` 给运维探活；`GET /v1/stats`（需 token）返回总量/区域
  分布/按日计数等 JSON，Grafana 可直接取数；请求体上限 8 MB；坏行计数
  不入库不投毒。
- **按天轮转**：活库只保留最近 `--rotate-days` 天（默认 7），更早事件
  自动归档为 `<store目录>/archive/events-YYYYMMDD.ndjson` 并从活库清理；
  归档就是标准 NDJSON，随时可 `repo ai-admin import` 再灌回。轮转惰性
  执行（每天首次写入时），无定时器无线程，失败只警告不影响接收。
- 这是过渡形态；正式环境可换任意语言的 `/v1/events` 实现 + Grafana，
  或把落库目录交给 `ai-admin import` 同格式离线导入。
- systemd 部署模板与完整 runbook（token 生成/验证/备份/升级/停用）见
  工具检出目录 `contrib/collector-deploy.md`。

客户端对应配置：

```bash
git config --file ~/.repoconfig/config ai.telemetry.endpoint http://收集器:8080
git config --file ~/.repoconfig/config ai.telemetry.token    <同一串>
```

### 3.6 灰度与回退

- 灰度发布：按部门/工作区下发 `ai.autoHint=false` 或策略锁控制范围。
- 快速回退：策略文件写入 `enabled = false` 下发后全网即时回到原版行为，
  无需回滚代码版本。

---

## 四、配置参考

配置键（git-config 键名大小写不敏感；**键名不能含下划线**，请用连字符）。
读取优先级：环境变量 > `.repo/config`（工作区级）> `~/.repoconfig/config`
（用户级）> 管理员策略 > 内置默认。环境变量命名：`REPO_` + 键名大写、
点换下划线，如 `ai.gateway.url` → `REPO_AI_GATEWAY_URL`。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `ai.enabled` | `true` | 总开关；false 时全部 AI 功能禁用（策略可强制关） |
| `ai.autoHint` | `true` | 失败后自动追加诊断建议（受总开关约束） |
| `ai.hintOff.<规则ID>` | — | 按规则静默，如 `ai.hintOff.R1002 = true` |
| `ai.region` | `auto` | `auto`/`online`/`isolated-r1`/`isolated-r2`/`isolated-r3` |
| `ai.gateway.url` | 空 | 网关地址；填主机根或 OpenAI 风格 `/v1` 基址均可（配置即视为在线区） |
| `ai.gateway.model` | 空 | 网关模型名；**必须与网关返回的 ID 完全一致**（`foo-plus` 与 `fooplus` 是两个不同模型）。本工具不内置模型名；`repo models` 列清单，`repo models --use <名>` 探活后切换。未配置时在线增强会明确报错并回退本地结果 |
| `ai.gateway.model-fallback` | 空 | 主模型不可用（5xx/404）时自动重试的备胎模型（回退时会大声提示）；置空则不重试 |
| `ai.gateway.token` | 空 | 网关 Bearer 令牌（`sk-…` 之类）。多用户共享网关时建议**每人一把 key**，便于配额与审计；见下方安全提示 |
| `ai.gateway.max-tokens` | `2500` | 单次回答预算（推理类模型的隐藏思维链会占用额度，过小会出现 200 但内容为空） |
| `ai.gateway.timeout` | `30` | 网关请求超时（秒）；实测端到端约 3–8 秒 |
| `ai.telemetry.enabled` | `false` | 使用数据上报开关（默认关闭；策略可强制关，需显式开启才会采集） |
| `ai.telemetry.channel` | `auto` | `auto`/`online`/`file-exchange`/`offline` |
| `ai.telemetry.endpoint` | 空 | 在线上报端点（配置即探测为 isolated-r1） |
| `ai.telemetry.token` | 空 | 上报 Bearer 令牌（与收集器 --token 一致） |
| `ai.telemetry.exchange-dir` | 空 | R2 摆渡目录（须已存在/已挂载） |
| `ai.telemetry.batch-size` | `200` | 单次上报/导出的事件数上限 |
| `ai.telemetry.timeout` | `10` | 上报请求超时（秒） |
| `ai.feedback.hint` | `false` | 错误提示后一键反馈（仅交互终端生效） |
| `ai.outcome.window` | `1800` | 隐式判定窗口（秒）：建议展示后同命令在此时长内再次执行才判定成败；超时不判定、不上报 |
| `ai.preflight` | `false` | sync/init/upload/download 前轻量预检提示 |
| `ai.ticket.endpoint` | 空 | 诊断包工单提交端点 |
| `ai.admin.store` | `~/.repoai/store.sqlite` | 管理员事件存储（ai-admin） |
| `ai.state-dir` | `.repo/ai` | 本地状态目录 |
| `ai.rules.path` / `ai.kb.path` | 空 | 额外规则/知识库目录（管理员扩展） |

策略锁键：`ai.enabled`、`ai.telemetry.enabled`（仅可强制关闭）。

## 五、常见问题

**Q：AI 层会拖慢原来的命令吗？**
不会。成功路径上 AI 层不做任何网络或重 IO 动作：`ai.enabled=false` 时
完全 no-op；开启时也只读几层本地配置。失败时才追加纯本地文本诊断。
（`ai.preflight=true` 会在 sync/init/upload/download 前做几步只读检查，
均为本地 stat/读文件，毫秒级，且每小时最多提示一次。）

**Q：诊断建议是错的/打扰我了怎么办？**
按规则静默：`git config --file ~/.repoconfig/config ai.hintOff.R1002 true`；
或 `ai.autoHint=false` 只关自动提示；并用 `repo feedback --inaccurate`
反馈，管理员会据此修订规则。

**Q：会上报我的代码或日志吗？**
不会。上报的是结构化元数据（命令名、异常类型、命中规则 ID 与置信度
分数、耗时、区域、匿名 client_id）。诊断包含错误文本但已脱敏（去用户
名/家目录/凭据样式串），且是否外发由你发起（`repo report`）、经预览确认。

**Q：AI 给的建议可以直接照做吗？**
分两段看：规则库部分（`R10xx` + 建议步骤）是人工整理的排查流程，可
放心参考；"AI 分析"段是内部模型针对你的报错实时生成的，质量不错但
不保证——历史上出现过它建议 `rm -rf .repo/projects/…` 这类破坏性
命令的情况。执行删除/清理类命令前请自行判断，不确定就 `[忽略]` 并
走 `repo explain`/`repo faq` 的人工流程，或 `repo feedback` 打
"不准确"，管理员会复核规则与提示词。

**Q：隔离区和在线环境用法有区别吗？**
命令完全相同。差别只在数据来源：在线区由 AI 网关增强、建议可
`[采纳]`；隔离区走本地规则库，来源会如实标注"本地规则库"。

**Q：`repo help` 里能看到这几个命令吗？**
开启 AI 层（默认开启）后，`repo help` 底部会多出一节"AI assistant
commands"列出全部新命令；关闭后主视图与原版完全一致。
`repo help --all` 与 `man repo-ask` 等 man 页随时可查（man 页位于
`.repo/repo/man/`，可用 `MANPATH=.repo/repo/man man repo-ask` 查看）。

**Q：网关报"模型 X 暂不可用；回退到 Y"是什么意思？**
主模型返回 5xx/404，工具改用 `ai.gateway.model-fallback` 重试并明确告知
（不会静默换模型）。最常见的原因是**模型名写错**——不少网关对不存在的
名字回 503 而不是 404，且形近的名字往往是两个不同模型（如 `foo-plus`
与 `fooplus`）。跑 `repo models` 看准确清单，或 `repo models --use <名字>`
一步探活并切换；也可能确实是账户未开通或网关故障。

**Q：怎么确认当前区域与上报状态？**
`repo telemetry` 一行一个：区域、通道、待上报事件数。
`repo report --preview` 首行也显示当前区域。

**Q：报错 "AI features are disabled"？**
说明本地或管理员策略关闭了 AI 层。本地自行开启见 [1.5 节](#15-关闭--再开启-ai-层)；
若为策略强制关闭，联系管理员。

---

*能力范围：规则/知识库/诊断/报表/反馈 + 本地队列与多通道上报 +
OpenAI 兼容网关接入 + 纯标准库收集器与看板。*
