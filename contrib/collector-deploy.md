# 收集器部署 runbook（管理员）

在线区遥测接收端 `repoai/collector.py` 的部署、验证、升级与数据运维。
客户端侧说明见 `docs/ai-user-guide.md` 3.5 节。

## 1. 准备（一次性）

```bash
# 1) 代码：收集器只依赖 repoai/ 包（可独立运行，不需要工作区）
sudo mkdir -p /opt/repo-ai && sudo cp -r \
    <repo工具检出目录>/repoai /opt/repo-ai/repoai
# （整仓 clone 更便于 git pull 升级）

# 2) 服务账号 + 数据目录
sudo useradd -r -s /usr/sbin/nologin repoai
sudo mkdir -p /var/lib/repo-telemetry
sudo chown repoai:repoai /var/lib/repo-telemetry

# 3) token（客户端要拿同一个值）
sudo sh -c 'umask 077; printf "REPO_TELEMETRY_TOKEN=%s\n" \
    "$(python3 -c \"import secrets;print(secrets.token_urlsafe(32))\")" \
    > /etc/repo-telemetry-collector.env'
sudo cat /etc/repo-telemetry-collector.env   # 抄下发给客户端管理员

# 4) systemd 单元（模板见同目录 repo-telemetry-collector.service）
sudo cp contrib/repo-telemetry-collector.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now \
    repo-telemetry-collector
```

无 systemd 的老机器用 nohup 过渡（重启不自愈，尽快补 unit）：

```bash
cd /opt/repo-ai && REPO_TELEMETRY_TOKEN=<同串> nohup python3 \
  -m repoai.collector --host 0.0.0.0 --port 8080 \
  --store /var/lib/repo-telemetry/store.sqlite --rotate-days 30 \
  >> /var/log/repo-telemetry.log 2>&1 &
```

无 sudo 时加 crontab 开机自启（`crontab -e` 追加一行，路径按实际改）：

```cron
@reboot cd /opt/repo-ai && REPO_TELEMETRY_TOKEN=<同串> nohup python3 \
  -m repoai.collector --host 0.0.0.0 --port 8080 \
  --store /var/lib/repo-telemetry/store.sqlite --rotate-days 30 \
  >> /var/log/repo-telemetry.log 2>&1 &
```

## 2. 部署后验证

```bash
# 探活（应回 {"ok":true,...}）
curl -s http://<收集器>:8080/healthz

# 统计接口：无 token 必须 401（安全默认生效）
curl -s -o /dev/null -w '%{http_code}\n' http://<收集器>:8080/v1/stats   # 401

# 带 token 的统计（看板数据源）
curl -s -H "Authorization: Bearer $TOKEN" http://<收集器>:8080/v1/stats

# 端到端：任一台配好 ai.telemetry.endpoint/token 的机器
repo telemetry --flush        # 已上报 N 条，剩余 0 条
repo ai-admin summary         # 或 ai-admin 指向同一 store 路径查看
```

防火墙：`8080/tcp` 仅对内网开发网段开放；`/v1/stats` 与 `/v1/events`
共用 token，**不要把 token 写进任何 git 仓库**。

## 3. 日常运维

- **日志**：journalctl -u repo-telemetry-collector（nohup 模式看
  /var/log/repo-telemetry.log）。关注：401 激增（客户端没配 token）、
  轮转记录、"轮转失败"警告。
- **轮转**：活库保留 `--rotate-days` 天（默认 7，模板给了 30），过期
  自动归档 `/var/lib/repo-telemetry/archive/events-YYYYMMDD.ndjson`。
- **备份**：归档目录就是标准 NDJSON，直接 tar/备份软件收；活库是
  普通 sqlite，停机 cp 即可（或用 `sqlite3 store.sqlite ".backup f"`）。
- **重灌/迁移**：归档文件在任何机器 `repo ai-admin import <文件...>`
  即可重建分析库——归档即备份格式。

## 4. 升级

```bash
# 整仓 clone 部署时：git pull 后拷最新的 repoai/ 过去
cd <检出目录> && git pull && \
    sudo cp -r repoai /opt/repo-ai/ && sudo rm -rf /opt/repo-ai/repoai
sudo systemctl restart repo-telemetry-collector
curl -s http://<收集器>:8080/healthz
```

数据格式（sqlite schema / NDJSON）向后兼容；如将来有破坏性变更，
release notes 会标注，先手工备份再升级。

## 5. 停用/回退

```bash
sudo systemctl disable --now repo-telemetry-collector
```

客户端不受影响：事件继续留在各自本地队列（sqlite），收集器恢复后
`repo telemetry --flush` 自动补报，按事件 ID 去重，不丢不重。

## 6. 无 root / 无 systemd 的最小形态

团队规模小、或者部署机没有 root 时，收集器可以完全跑在普通用户目录里
（它只需要 Python 3 标准库 + 一个可写目录）：

```bash
mkdir -p ~/repo-ai && cd ~/repo-ai
cp -r <repo工具检出目录>/repoai ./repoai
umask 077 && python3 -c 'import secrets;print(secrets.token_urlsafe(32))' > token
REPO_TELEMETRY_TOKEN=$(cat token) nohup python3 -m repoai.collector \
    --host 0.0.0.0 --port 8080 \
    --store ~/.repoai/store.sqlite --rotate-days 30 >> collector.log 2>&1 &
crontab -e   # 追加：@reboot cd ~/repo-ai && REPO_TELEMETRY_TOKEN=$(cat token) \
             #   nohup python3 -m repoai.collector --host 0.0.0.0 --port 8080 \
             #   --store ~/.repoai/store.sqlite --rotate-days 30 >> collector.log 2>&1 &
```

要点：token 文件保持 0600、**不入库不入 git**；`--rotate-days` 按合规
要求设；`--host 0.0.0.0` 务必配合防火墙只开放给开发网段。

客户端配置：

```bash
git config --file ~/.repoconfig/config ai.telemetry.endpoint http://collector.example.com:8080
git config --file ~/.repoconfig/config ai.telemetry.token '<向管理员索取>'
```

默认**自动上报**（诊断产生事件后顺带推送，15 分钟节流；
`ai.telemetry.auto-flush=false` 关）。主端点宕机期间事件留本地队列，
恢复后补报，不丢不重（按事件 ID 去重）。

## 6.1 网络分区下的多收集器部署

若开发网被切成互不可达的片段（A 段够得到 collector-a，B 段只够得到
collector-b），**每个片段跑一台完全独立的收集器**（同协议、可不同
token、独立 store），各收各的：

```bash
# B 段用户的客户端配置
git config --file ~/.repoconfig/config ai.telemetry.endpoint http://collector-b.example.com:18081
git config --file ~/.repoconfig/config ai.telemetry.token '<B 收集器的 token>'
```

- **不要让两拨数据自动掺到一起**：`ai.telemetry.fallback-endpoint`
  默认**关闭**，主端点故障时事件留本地队列等补报。要全局汇总，由
  管理员从能同时够到两边的机器 `repo ai-admin pull <对方 /v1/export>`
  合并（按事件 ID 去重，幂等，可 crontab 定时）。
- 确实**两边都够得到**的机器（跳板机/管理员机）才建议显式开启宕机转存：

```bash
git config --file ~/.repoconfig/config ai.telemetry.fallback-endpoint http://collector-b.example.com:18081
```

要点：

- **回退是逐机显式开启的**（配置主端点才是上报授权；备用端默认关，
  开了也只在主端点故障时生效）；`ai.enabled=false` /
  `ai.telemetry.enabled=false` / 隔离区判定全部优先。
- **不丢不重的对账方式**：备用端收下后事件**仍留在本地队列**——只有
  主端点的 2xx 才标记已发送。主端点恢复后客户端照常补报，服务端按
  事件 ID 去重；备库里多出的副本用 `repo ai-admin pull` 并回主库。
- **关闭/搬迁**：`ai.telemetry.fallback-endpoint` 置空即关（默认即空），
  改值即换机（策略文件 `/etc/repo/ai.conf` 可统一下发）。

备收集器启动与主收集器完全同构，只是加 `--role backup`（看板页眉红字
标注"备用收集器"防看错库，收数据行为一致）：

```bash
REPO_TELEMETRY_TOKEN=$(cat token) nohup python3 -m repoai.collector \
    --host 0.0.0.0 --port 18081 --role backup \
    --store ~/.repoai/store-backup.sqlite --rotate-days 30 \
    >> collector.log 2>&1 &
curl -s http://127.0.0.1:18081/healthz    # {"ok":true,"role":"backup",...}
```

## 6.2 共享目录摆渡（可选，离线区用）

完全无法走 HTTP 的机器（如离线摆渡区）可配 `ai.telemetry.fallback-dir`
指向本地已挂载的共享目录：回退时优先落盘（覆盖式快照文件
`repo-events-<client前12位>.ndjson`，不堆积），收集器用
`--ingest-dir <同一挂载点>` 每分钟自动收编，或
`repo ai-admin import <挂载点>` 手动收编。普通用户**不需要**配这个。

## 6.3 Web 看板（收集器内置）

浏览器打开 `http://<收集器地址>:8080/`，输入与 `ai.telemetry.token`
相同的令牌即可查看：事件总数/客户端数/反馈与解决率/规则命中排行（含
每条规则的反馈解决率与"建议后成功率"两列；后者是隐式相关口径——看到
建议后同命令下一次重试的成功率，自愈也算成功，不能当解决率读。低解决
率或重试高失败规则是每周复核的首要对象）/
区域与每日趋势，60 秒自动刷新。页眉标注本库身份（"主收集器"/红字
"备用收集器"，防把备库看成主库）。**"未命中缺口"栏**列出没有任何规
则覆盖的失败（命令+异常类型+次数），是每周补规则库的直接输入——这
些正是下一条该写的 Rxxxx。页面为单文件内嵌 HTML（无 CDN、无构建、
断网内网可用），只调用收集器自身已鉴权的 JSON 端点，页面源码不含任
何密钥。旧进程升级：更新代码副本后重启进程即可，数据不丢。

## 7. 过渡形态说明

本服务是 stdlib 单文件实现（无依赖、无 root、可 U 盘拷贝），定位是
最小可用后端；上规模后可无缝替换：任何实现 `POST /v1/events`
（NDJSON，Bearer 可选）与 `GET /v1/stats` 语义的服务 + Grafana 即可，
客户端零改动。
