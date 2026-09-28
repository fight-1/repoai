# repo + repoai

> `repo`（Android 多仓库管理工具）的一个分支，在原生 `repo` 之上加了一层可选的 AI 辅助（`repoai/`）。

![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)
![Python](https://img.shields.io/badge/python-3.6%2B-blue.svg)

- 仓库：<https://github.com/fight-1/repoai>
- 协议：Apache 2.0（见 [LICENSE](./LICENSE)、[NOTICE](./NOTICE)）
- 贡献：GitHub Pull Request + DCO（见 [CONTRIBUTING.md](./CONTRIBUTING.md)）

## 这是什么

`repo` 是构建在 Git 之上的仓库管理工具，用来按 manifest 一次性初始化、同步、上传成百上千个 Git 仓库。这个仓库在它的基础上加了 `repoai/`：一个本地运行、零新增依赖的 AI 辅助层。

AI 层不会改动任何原有命令的行为——输出和退出码都不变。命令失败后，它最多在原来的报错后面追加一小段诊断提示。不需要时可以整个关掉：`ai.enabled=false`。

### 为什么加这一层

`repo` 的报错往往不好直接下手：要么是底层 Git 错误，要么是 manifest 解析失败，要么是网络/锁问题。而且在隔离（无外网）研发环境里，也没法随手开浏览器查资料。`repoai` 的做法是用一套本地规则引擎和知识库来兜底——离线就能用；接了网关之后，还能用自然语言提问。

## repoai 提供了什么

`repo help` 里能看到 "AI assistant" 命令分组，新增的子命令：

| 命令 | 作用 |
| --- | --- |
| `repo rules` | 列出 / 导入 / 导出 / 校验离线诊断规则（内置 R1001–R1040 共 40 条） |
| `repo faq <关键词>` | 在离线知识库里检索 |
| `repo explain <规则/报错/命令>` | 解释一条诊断规则、一段报错，或一个 repo 命令 |
| `repo diagnose` | 分析最近一次失败的命令 |
| `repo doctor` | 只读式工作区自检（离线可用） |
| `repo ask <问题>` | 向助手提问；没配网关时自动降级到离线规则/知识库 |
| `repo models` | 列出 / 探测 / 切换网关模型 |
| `repo ai-admin` | 管理员用：导入隔离区批次并汇总 |

几点事实：

- 零新增依赖，只用 Python 标准库，Python 3.6+ 即可。
- 默认离线。规则、知识库、诊断都不需要网络。
- 在线部分走开放协议。网关兼容 OpenAI `/v1`，所以 vLLM、Ollama、LiteLLM 或任何兼容端点都能接。模型名不写死——`repo models` 会探测网关实际提供的模型。
- 遥测默认关闭。除非显式配置了 endpoint，否则不上传任何数据。

## 快速开始

### 安装 / 更新

AI 层跟着 `repo` 本体一起发，没有单独的安装步骤。要用这个分支，把 `repo` 指向它即可：

```bash
# 方式 A：repo init 时指定本仓库（新工作区）
repo init -u <manifest地址> -b <manifest分支> \
    --repo-url=https://github.com/fight-1/repoai --repo-rev=<分支>

# 方式 B：repo selfupdate（AI 层已合入默认分支时）
repo selfupdate

# 方式 C：手动切工具代码（试用 / 灰度）
cd <工作区>/.repo/repo
git fetch origin <分支> && git checkout <分支>
```

注意：上游启动器默认拉 `main`（不含 AI 层），所以得带上 `--repo-url`/`--repo-rev`，或用方式 B/C。

### 验证

```bash
repo rules    # 列出 40 条内置规则
repo faq 同步  # 返回知识库结果
repo doctor   # 只读自检
```

### 可选：接一个网关

```bash
git config --global ai.enabled true
git config --global ai.gateway.url  https://your-gateway/v1
git config --global ai.gateway.model your-model
repo models
```

更多：

- `docs/ai-user-guide.md`——完整手册
- `docs/ai-quickref.md`——一页速查
- `contrib/collector-deploy.md`——遥测收集器部署

## 架构（简述）

```
main.py
  └─ 注入 ai_hook（仅当 ai.enabled 且未被策略锁定时）
        │
repoai/
  ├─ config.py     ai.* 配置解析 + region 探针
  ├─ rules.py      本地规则引擎（rules.json）
  ├─ kb.py         离线知识库（kb.json）
  ├─ gateway.py    OpenAI 兼容 /v1 调用 + 模型探测
  ├─ diagnose.py   失败分析
  ├─ events.py     事件队列
  ├─ doctor.py     只读环境检查
  ├─ collector.py  诊断包生成
  ├─ telemetry.py  用量收集（默认关）
  ├─ report.py     上报
  ├─ hook.py       注入 main.py 的钩子
  └─ command_base.py / importer.py / dashboard.py
```

新子命令在 `subcmds/`，继承 `AiCommand`。

## 文档

- [repo Manifest 格式](./docs/manifest-format.md)
- [repo Hooks](./docs/repo-hooks.md)
- [在 Windows 上运行 Repo](./docs/windows.md)
- AI 层：[用户手册](./docs/ai-user-guide.md) · [速查](./docs/ai-quickref.md) · [管理员手册](./docs/ai-handbook.md) · [文件系统布局](./docs/internal-fs-layout.md) · [收集器部署](./contrib/collector-deploy.md)
- [贡献指南](./CONTRIBUTING.md) · [行为准则](./CODE_OF_CONDUCT.md) · [安全策略](./SECURITY.md) · [更新日志](./CHANGELOG.md)

## 上游

本仓库基于 AOSP 的 [`git-repo`](https://gerrit.googlesource.com/git-repo/)（Apache 2.0）。原有的 README、manpage、测试用例版权归 The Android Open Source Project；`repoai/` 新增部分版权归 **fight-1**。见 [NOTICE](./NOTICE)。

上游资源（供参考）：

- 主页：<https://gerrit.googlesource.com/git-repo/>
- 文档：<https://source.android.com/source/developing.html>
- GitHub 镜像：<https://github.com/GerritCodeReview/git-repo>

## 许可证

Apache 2.0，见 [LICENSE](./LICENSE)。
