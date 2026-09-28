# Copyright (C) 2026 fight-1
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Built-in read-only dashboard for the telemetry collector.

Single self-contained HTML page (no CDN, no build step, works offline on
the intranet) served by `repoai.collector` at `/`.  It only calls the
collector's own JSON endpoints, so it exposes nothing new: the page is a
view over GET /v1/stats and GET /v1/summary, both still bearer-token
protected.  The token is kept in browser localStorage -- never in the
page source, never sent to any other origin.
"""

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>repo AI 遥测看板</title>
<style>
:root { color-scheme: light dark; }
body { font: 14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",
       "Noto Sans CJK SC",sans-serif; margin: 0; padding: 24px;
       background: #f6f7f9; color: #1c1e21; }
h1 { font-size: 19px; margin: 0 0 4px; }
.sub { color: #656d76; font-size: 12px; margin-bottom: 20px; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
.card { background: #fff; border: 1px solid #d9dde3; border-radius: 8px;
        padding: 14px 18px; min-width: 110px; }
.card .n { font-size: 26px; font-weight: 600; }
.card .l { color: #656d76; font-size: 12px; }
.panel { background: #fff; border: 1px solid #d9dde3; border-radius: 8px;
         padding: 16px 18px; margin-bottom: 16px; }
.panel h2 { font-size: 14px; margin: 0 0 10px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th,td { text-align: left; padding: 5px 8px;
        border-bottom: 1px solid #eceff2; }
th { color: #656d76; font-weight: 500; font-size: 12px; }
td.n { text-align: right; font-variant-numeric: tabular-nums; }
.bar { height: 9px; background: #2f6feb; border-radius: 3px;
       min-width: 2px; }
.row { display: flex; align-items: center; gap: 10px; }
.row .nm { flex: 0 0 90px; }
.row .bx { flex: 1; background: #eef1f5; border-radius: 3px; }
.row .vl { flex: 0 0 46px; text-align: right;
           font-variant-numeric: tabular-nums; }
input[type=password] { padding: 6px 9px; border: 1px solid #c8ccd2;
        border-radius: 6px; width: 320px; font-size: 13px; }
button { padding: 6px 14px; border: 1px solid #c8ccd2; border-radius: 6px;
        background: #fff; cursor: pointer; font-size: 13px; }
button:hover { background: #f0f2f5; }
.err { color: #b3261e; font-size: 13px; margin-top: 8px; }
.hint { color: #656d76; font-size: 12px; }
#login,#dash { display: none; }
.pulse { width: 8px; height: 8px; border-radius: 50%;
         background: #2da44e; display: inline-block; }
.pulse.bad { background: #da3633; }
.badge { font-size: 11px; font-weight: 400; vertical-align: 2px;
         border: 1px solid #82071e; color: #cf222e; border-radius: 10px;
         padding: 1px 8px; display: none; }
.badge.primary { border-color: #1a7f37; color: #1a7f37; }
@media (prefers-color-scheme: dark) {
  body { background: #0d1117; color: #e6edf3; }
  .card,.panel { background: #161b22; border-color: #30363d; }
  th,td { border-bottom-color: #21262d; }
  th,.sub,.hint,.card .l { color: #8b949e; }
  .row .bx { background: #21262d; }
  input[type=password],button { background: #0d1117; color: #e6edf3;
    border-color: #30363d; }
  button:hover { background: #21262d; }
  .err { color: #ff7b72; }
  .badge { border-color: #da3633; color: #ff7b72; }
  .badge.primary { border-color: #238636; color: #3fb950; }
}
</style>
</head>
<body>
<h1><span class="pulse" id="dot"></span> repo AI 遥测看板
  <span class="badge" id="role"></span></h1>
<div class="sub" id="meta">加载中…</div>

<div id="login">
  <div class="panel">
    <h2>访问令牌</h2>
    <div class="hint">收集器启用了 Bearer 鉴权，请输入与
      ai.telemetry.token 相同的令牌（仅存在本浏览器，不会上传到别处）。</div>
    <p>
      <input type="password" id="tok" placeholder="Bearer token"
             autocomplete="off">
      <button id="go">查看看板</button>
    </p>
    <div class="err" id="err"></div>
  </div>
</div>

<div id="dash">
  <div class="cards" id="cards"></div>
  <div class="panel"><h2>高频问题（规则命中排行）</h2>
    <div id="rules"></div></div>
  <div class="panel"><h2>未命中缺口（无规则覆盖，建议补库）</h2>
    <div id="gaps"></div></div>
  <div class="panel"><h2>事件类型 / 区域分布</h2>
    <div style="display:flex;gap:28px;flex-wrap:wrap">
      <div style="flex:1;min-width:220px"><div id="types"></div></div>
      <div style="flex:1;min-width:180px"><div id="regions"></div></div>
    </div>
  </div>
  <div class="panel"><h2>每日事件量</h2><table id="daily"></table></div>
  <div class="sub">
    <button id="reload">刷新</button>
    <button id="logout">更换令牌</button>
    <span class="hint" id="updated"></span>
  </div>
</div>

<script>
var tok = localStorage.getItem('repoai_token') || '';

function hdrs() { return tok ? { 'Authorization': 'Bearer ' + tok } : {}; }

function get(path) {
  return fetch(path, { headers: hdrs() }).then(function (r) {
    if (r.status === 401) { var e = new Error('unauthorized');
      e.auth = true; throw e; }
    if (!r.ok) { throw new Error(path + ' -> HTTP ' + r.status); }
    return r.json();
  });
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
  });
}

function rows(list) {
  if (!list.length) { return '<div class="hint">暂无数据</div>'; }
  var max = Math.max.apply(null, list.map(function (r) { return r[1]; }));
  return list.map(function (r) {
    var w = max ? Math.round(100 * r[1] / max) : 0;
    return '<div class="row"><div class="nm">' + esc(r[0]) + '</div>' +
      '<div class="bx"><div class="bar" style="width:' + w + '%"></div>' +
      '</div><div class="vl">' + r[1] + '</div></div>';
  }).join('');
}

function pairs(o) {
  return Object.keys(o).map(function (k) { return [k, o[k]]; });
}

function card(n, l) {
  return '<div class="card"><div class="n">' + esc(n) +
    '</div><div class="l">' + esc(l) + '</div></div>';
}

function show(err) {
  if (err && err.auth) {
    document.getElementById('login').style.display = 'block';
    document.getElementById('dash').style.display = 'none';
    document.getElementById('dot').className = 'pulse bad';
    document.getElementById('err').textContent = tok ?
      '令牌不正确，请重试。' : '该看板需要令牌。';
    document.getElementById('meta').textContent = '需要令牌';
    return;
  }
  document.getElementById('login').style.display = 'none';
  document.getElementById('dash').style.display = 'block';
  if (err) {
    document.getElementById('dot').className = 'pulse bad';
    document.getElementById('meta').textContent = '加载失败：' + err;
  }
}

function render(stats, summary, titles) {
  document.getElementById('dot').className = 'pulse';
  document.getElementById('login').style.display = 'none';
  document.getElementById('dash').style.display = 'block';
  document.getElementById('cards').innerHTML =
    card(stats.total || 0, '事件总数') +
    card(stats.clients || 0, '客户端数') +
    card(summary.feedback_total || 0, '反馈条数') +
    card(summary.resolved_rate === null ? '—' :
         summary.resolved_rate + '%', '问题解决率') +
    card((stats.process && stats.process.rejected) || 0, '拒绝请求') +
    card(stats.rotate_days || '—', '保留天数');
  var badge = document.getElementById('role');
  if (stats.role === 'backup') {
    badge.textContent = '备用收集器 · 主收集器恢复后由管理员并库';
    badge.className = 'badge';
    badge.style.display = 'inline-block';
  } else if (stats.role) {
    badge.textContent = '主收集器';
    badge.className = 'badge primary';
    badge.style.display = 'inline-block';
  } else {
    badge.style.display = 'none';
  }
  var rfb = summary.rule_feedback || {};
  var roc = summary.rule_outcome || {};
  var tl = (summary.top_rules || []).map(function (r) {
    var per = rfb[r[0]] || {};
    var tot = 0, res = 0;
    for (var k in per) { tot += per[k]; if (k === 'resolved') res = per[k]; }
    var rate = tot ? Math.round(100 * res / tot) + '% (' + tot + ')' : '—';
    var o = roc[r[0]] || {};
    var okn = o.success_after_hint || 0;
    var tries = okn + (o.retry_failed || 0);
    var orate = tries ?
      Math.round(100 * okn / tries) + '% (' + tries + ')' : '—';
    return [r[0] + (titles[r[0]] ? '  ' + titles[r[0]] : ''),
            r[1], rate, orate];
  });
  document.getElementById('rules').innerHTML = tl.length ?
    '<table><tr><th>规则</th><th class="n">命中</th>' +
    '<th class="n">反馈解决率</th>' +
    '<th class="n">建议后成功率*</th></tr>' +
    tl.map(function (r) {
      return '<tr><td>' + esc(r[0]) + '</td><td class="n">' + r[1] +
        '</td><td class="n">' + esc(r[2]) + '</td><td class="n">' + esc(r[3]) +
        '</td></tr>';
    }).join('') + '</table>' +
    '<div class="hint">* 建议后成功率：看到建议后同命令下一次重试的成功率，' +
    '属隐式相关口径（网络自愈也算成功），不等于解决率。</div>' :
    '<div class="hint">暂无命中记录</div>';
  var gp = summary.top_unmatched || [];
  document.getElementById('gaps').innerHTML = gp.length ?
    '<table><tr><th>命令</th><th>异常</th><th class="n">次数</th></tr>' +
    gp.map(function (r) {
      return '<tr><td>' + esc(r[0]) + '</td><td>' + esc(r[1]) +
        '</td><td class="n">' + r[2] + '</td></tr>';
    }).join('') + '</table>' :
    '<div class="hint">暂无——失败都被现有规则覆盖了</div>';
  document.getElementById('types').innerHTML =
    '<div class="hint" style="margin-bottom:6px">事件类型</div>' +
    rows(pairs(stats.by_type || {}));
  document.getElementById('regions').innerHTML =
    '<div class="hint" style="margin-bottom:6px">区域</div>' +
    rows(pairs(stats.by_region || {}));
  var d = stats.daily || [];
  document.getElementById('daily').innerHTML = d.length ?
    '<tr><th>日期</th><th class="n">事件数</th></tr>' + d.map(function (r) {
      return '<tr><td>' + esc(r[0]) + '</td><td class="n">' + r[1] +
        '</td></tr>';
    }).join('') : '';
  document.getElementById('meta').textContent =
    '事件库 ' + esc(stats.store || '') + ' · 本进程已接收 ' +
    ((stats.process && stats.process.received) || 0) + ' 条';
  document.getElementById('updated').textContent =
    '更新于 ' + new Date().toLocaleTimeString();
}

function load() {
  Promise.all([get('/v1/stats'), get('/v1/summary'),
               get('/v1/rules').catch(function () { return {}; })])
    .then(function (res) {
      document.getElementById('meta').className = 'sub';
      render(res[0], res[1], res[2] || {});
    })
    .catch(show);
}

document.getElementById('go').onclick = function () {
  tok = document.getElementById('tok').value.trim();
  localStorage.setItem('repoai_token', tok);
  document.getElementById('err').textContent = '';
  load();
};
document.getElementById('tok').onkeydown = function (e) {
  if (e.key === 'Enter') { document.getElementById('go').click(); }
};
document.getElementById('reload').onclick = load;
document.getElementById('logout').onclick = function () {
  localStorage.removeItem('repoai_token'); tok = '';
  document.getElementById('tok').value = ''; show();
};
if (tok) { load(); } else { show(); }
setInterval(function () {
  if (document.getElementById('dash').style.display === 'block') { load(); }
}, 60000);
</script>
</body>
</html>
"""


def page():
    """Return the dashboard HTML body (utf-8 bytes)."""
    return PAGE.encode("utf-8")
