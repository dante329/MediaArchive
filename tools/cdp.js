'use strict';
/**
 * tools/cdp.js —— 所有借常驻 Chrome 抓数据的脚本共用的 CDP 小客户端。
 *
 * 为什么必须有这一个共享实现：
 * 以前每个脚本各写一份 WebSocket 客户端，而且 send() 只有 resolve、没有 reject。
 * 常驻 Chrome 是所有抖音/小红书任务共用的，它一旦被重启（服务重启、登录、
 * 某个任务超时兜底），已建立的 WebSocket 就断了，pending 的 Promise 永远不落地，
 * Node 的事件循环空掉之后会以 **0** 退出 —— 不写输出文件、不打任何错误。
 * 上层 engine.py 靠「输出文件在不在」判断成败，于是用户只看到一条空的
 * 「抖音解析失败：」，重试也没用。这就是抖音爬取失败率居高不下的直接原因之一。
 *
 * 这里的约定：
 *   - send() 有超时，超时就 reject；
 *   - WebSocket close/error 会把所有 pending 一次性 reject；
 *   - 脚本永远「要么写出结果并以 0 退出，要么以非 0 退出并说清楚原因」；
 *   - 打开的标签页无论成功失败都要关掉（withTab 已经保证）。
 */

const DEFAULT_CMD_TIMEOUT = Number(process.env.MA_CDP_TIMEOUT || 90000);
const DEFAULT_OPEN_TIMEOUT = 20000;

const sleep = ms => new Promise(r => setTimeout(r, ms));

function httpBase(port) {
  return `http://127.0.0.1:${port || 9222}`;
}

/** 列出当前所有 CDP target */
async function listTabs(port) {
  const r = await fetch(`${httpBase(port)}/json/list`);
  if (!r.ok) throw new Error(`Chrome 调试端口 ${port} 返回 HTTP ${r.status}`);
  return r.json();
}

/**
 * 开一个新标签页。Chrome 刚启动/刚被重启时 /json/new 会短暂失败，所以带重试。
 * 返回 target 对象（含 id 和 webSocketDebuggerUrl）。
 */
async function openTab(port, url = 'about:blank', { tries = 15, delay = 400 } = {}) {
  const base = httpBase(port);
  let lastErr = '';
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(`${base}/json/new?${url}`, { method: 'PUT' });
      if (r.ok) {
        const t = await r.json();
        if (t && t.webSocketDebuggerUrl) return t;
        lastErr = '返回里没有 webSocketDebuggerUrl';
      } else {
        lastErr = `HTTP ${r.status}`;
      }
    } catch (e) {
      lastErr = e.message;
    }
    await sleep(delay);
  }
  throw new Error(`连不上 Chrome 调试端口 ${port}（常驻 Chrome 没起来或已被关掉）：${lastErr}`);
}

/** 关掉标签页；失败不抛（清理路径上不能再炸一次） */
async function closeTab(port, id) {
  if (!id) return;
  try { await fetch(`${httpBase(port)}/json/close/${id}`); } catch (e) {}
}

/**
 * 连接一个 target 的 WebSocket。
 * resolve 出 client：{ ws, send, evaluate, close, closed }
 *   - send(method, params, timeout?) → resolve 整条应答消息（含 result / error）
 *   - evaluate(expr, opts?)          → 页面里执行 JS，返回值；opts.throwOnError 时把页面异常抛出
 */
function connect(wsUrl, { cmdTimeout = DEFAULT_CMD_TIMEOUT, openTimeout = DEFAULT_OPEN_TIMEOUT } = {}) {
  return new Promise((resolveReady, rejectReady) => {
    let ws;
    try {
      ws = new WebSocket(wsUrl);
    } catch (e) {
      rejectReady(new Error('CDP WebSocket 地址无效（Chrome 可能刚被重启）: ' + e.message));
      return;
    }
    let id = 1;
    const pending = new Map();
    let settled = false;
    let closed = false;

    const failAll = err => {
      for (const [, p] of pending) { clearTimeout(p.timer); p.reject(err); }
      pending.clear();
    };

    const openTimer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try { ws.close(); } catch (e) {}
      rejectReady(new Error(`连接 Chrome 调试端口超时（${Math.round(openTimeout / 1000)}s）`));
    }, openTimeout);

    ws.addEventListener('open', () => {
      if (settled) return;
      settled = true;
      clearTimeout(openTimer);
      resolveReady(client);
    });
    ws.addEventListener('error', () => {
      if (!settled) {
        settled = true;
        clearTimeout(openTimer);
        rejectReady(new Error('连不上 Chrome 的调试 WebSocket（常驻 Chrome 没起来？）'));
        return;
      }
      failAll(new Error('CDP 连接出错（Chrome 可能被关闭或重启）'));
    });
    ws.addEventListener('close', ev => {
      closed = true;
      failAll(new Error(`CDP 连接被关闭（code=${ev && ev.code}）：常驻 Chrome 被重启或标签页已销毁`));
    });
    ws.addEventListener('message', ev => {
      let m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m && m.id && pending.has(m.id)) {
        const p = pending.get(m.id);
        pending.delete(m.id);
        clearTimeout(p.timer);
        p.resolve(m);
      }
    });

    const client = {
      ws,
      get closed() { return closed; },
      send(method, params = {}, timeout = cmdTimeout) {
        if (closed) return Promise.reject(new Error('CDP 连接已关闭，无法执行 ' + method));
        return new Promise((resolve, reject) => {
          const i = id++;
          const timer = setTimeout(() => {
            if (!pending.has(i)) return;
            pending.delete(i);
            reject(new Error(`CDP 命令超时（${Math.round(timeout / 1000)}s）：${method}`));
          }, timeout);
          pending.set(i, { resolve, reject, timer });
          try {
            ws.send(JSON.stringify({ id: i, method, params }));
          } catch (e) {
            clearTimeout(timer);
            pending.delete(i);
            reject(new Error(`发送 CDP 命令失败（${method}）：${e.message}`));
          }
        });
      },
      /**
       * 在页面里执行 JS 并取回值。
       * 导航过程中执行上下文会被销毁，这类协议错误按「暂时拿不到值」处理返回 null，
       * 免得调用方把一次正常的跳转当成致命错误。
       */
      async evaluate(expression, { timeout = cmdTimeout, throwOnError = false } = {}) {
        const m = await this.send('Runtime.evaluate', {
          expression, returnByValue: true, awaitPromise: true,
        }, timeout);
        if (m.error) return null;
        const res = m.result || {};
        if (res.exceptionDetails) {
          const msg = (res.exceptionDetails.exception && res.exceptionDetails.exception.description) ||
            res.exceptionDetails.text || '页面执行失败';
          if (throwOnError) throw new Error(msg);
          return null;
        }
        return res.result ? res.result.value : null;
      },
      close() {
        failAll(new Error('CDP 客户端已关闭'));
        try { ws.close(); } catch (e) {}
      },
    };
  });
}

/**
 * 打开标签页 → 连接 → 跑 fn(client, target) → 无论成败都关标签页。
 * fn 抛出的异常原样往外抛，由调用方决定退出码。
 */
async function withTab(port, fn, { url = 'about:blank', cmdTimeout, openTimeout } = {}) {
  const target = await openTab(port, url);
  let client = null;
  try {
    client = await connect(target.webSocketDebuggerUrl, { cmdTimeout, openTimeout });
    return await fn(client, target);
  } finally {
    if (client) { try { client.close(); } catch (e) {} }
    await closeTab(port, target && target.id);
  }
}

/**
 * 导航并等页面加载完成，返回最终 location.href。
 * chrome-error://（网络抖动 / 风控 / Chrome 刚重启）会自动重试。
 */
async function navigate(client, url, { tries = 3, settleMs = 2500, pollMs = 500, maxPolls = 40 } = {}) {
  let lastHref = '';
  for (let attempt = 1; attempt <= tries; attempt++) {
    await client.send('Page.navigate', { url });
    for (let i = 0; i < maxPolls; i++) {
      await sleep(pollMs);
      let st = null;
      try { st = await client.evaluate('document.readyState'); } catch (e) { st = null; }
      if (st === 'complete') break;
    }
    await sleep(settleMs);
    lastHref = '';
    try { lastHref = (await client.evaluate('location.href')) || ''; } catch (e) {}
    if (lastHref && !lastHref.startsWith('chrome-error:') && lastHref !== 'about:blank') {
      return lastHref;
    }
    if (attempt < tries) await sleep(1500 * attempt);
  }
  throw new Error(`页面打不开（重试 ${tries} 次）：${lastHref || '无响应'} → ${String(url).slice(0, 120)}`);
}

/**
 * 收尾退出：不要用 process.exit()，它会在 stdout 是管道时把还没刷出去的日志截断，
 * 上层就只剩一条空的错误信息。设好 exitCode，让事件循环自然收尾；
 * 再挂一个 unref 的兜底定时器，防止某个句柄没关干净导致进程吊着。
 */
function finish(code) {
  process.exitCode = code;
  const guard = setTimeout(() => process.exit(code), 3000);
  if (guard.unref) guard.unref();
}

module.exports = {
  sleep, httpBase, listTabs, openTab, closeTab, connect, withTab, navigate, finish,
  DEFAULT_CMD_TIMEOUT,
};
