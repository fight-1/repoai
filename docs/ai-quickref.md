# repo AI 辅助层 · 一页速查（打印贴工位）

## 失败了怎么办（最常见的 3 步）

| 步骤 | 命令 | 说明 |
| --- | --- | --- |
| 1 | `repo sync`（照常跑） | 失败后终端会自动追加"诊断建议" |
| 2 | `repo diagnose` | 完整复盘：规则 + 排查步骤 + 相关 FAQ |
| 3 | `repo report` | 生成脱敏诊断包 zip，提工单直接附 |

## 常用命令速查

```
repo diagnose          复盘最近一次失败（只读）
repo ask "问题"        自然语言提问；-c 追问，--reset 清空
repo doctor            工作区体检（磁盘/权限/反复失败）
repo faq <关键词>      离线知识库搜索
repo rules             浏览诊断规则库（40 类）
repo explain <R1001>   解释一条规则 / 一个命令 / 一段报错
repo report            生成提工单用诊断包（自动脱敏）
repo feedback          给刚才的诊断打 有用/没用
repo telemetry         看区域/上报状态（隐私情况）
repo models            查看/切换 AI 模型（--use 名字 验证并切换）
```

`repo help` 底部会列出以上全部命令（开启 AI 层后）。

## 典型问法（repo ask 实测好用）

- `repo ask "只想同步一个模块怎么做"`
- `repo ask "sync 到一半断网了怎么恢复"`
- `repo ask "upload 被 reject 说 missing change-id"`

## 开关与隐私（一行）

```
git config --file ~/.repoconfig/config ai.enabled false       # 完全关闭
git config --file ~/.repoconfig/config ai.autoHint false      # 只关自动提示
git config --file ~/.repoconfig/config ai.telemetry.enabled false  # 关统计
```

## 铁律（放心用）

- 原命令行为/输出/退出码 **零改动**，脚本 CI 不受影响
- 所有新命令**只读**，不会动你的工作区
- AI 建议仅供参考：AI 生成的步骤可能含删除/清理类命令（如 `rm -rf`、
  `git clean`），执行前自行判断；拿不准就 `[忽略]`，走 `repo explain`
  的人工步骤，并用 `repo feedback` 告诉我们哪里不对
- 统计只含结构化元数据，**无日志原文/代码/路径/用户名**
- 断网/隔离区自动切换本地规则库，功能照常并如实标注来源

## 安装后自检（只读，一条命令）

```bash
python3 .repo/repo/contrib/pilot_check.py   # [OK]/[WARN]/[FAIL] 逐项
```

## 记住两个就够

**失败了 → `repo diagnose`；要提单 → `repo report`。**
