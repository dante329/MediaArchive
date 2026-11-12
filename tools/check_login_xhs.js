#!/usr/bin/env node
/**
 * 检查小红书是否真的登录（不能只看 web_session：游客也有这个 cookie）。
 * 输出: logged_in | not_logged_in | no_cdp
 */
const PORT = process.argv[2] || '9222';
const HTTP = `http://127.0.0.1:${PORT}`;

async function main() {
  let list;
  try {
    list = await (await fetch(`${HTTP}/json/list`)).json();
  } catch (e) {
    console.log('no_cdp'); process.exit(2);
  }
  let page = (list || []).find((t) => t.type === 'page' && /xiaohongshu\.com/.test(t.url || ''));
  let created = false;
  if (!page) {
    try {
      const r = await fetch(`${HTTP}/json/new?url=${encodeURIComponent('https://www.xiaohongshu.com/')}`, { method: 'PUT' });
      page = await r.json();
      created = true;
    } catch (e) {
      console.log('no_cdp'); process.exit(2);
    }
  }
  if (!page || !page.webSocketDebuggerUrl) { console.log('no_cdp'); process.exit(2); }

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 1; const pending = new Map();
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j); });
  const send = (method, params = {}) => new Promise((res) => {
    const i = id++; pending.set(i, res);
    ws.send(JSON.stringify({ id: i, method, params }));
  });

  if (!/xiaohongshu\.com/.test(page.url || '')) {
    await send('Page.enable');
    await send('Page.navigate', { url: 'https://www.xiaohongshu.com/' });
    await new Promise((r) => setTimeout(r, 3500));
  }
  await new Promise((r) => setTimeout(r, 1200));

  let logged = false;
  try {
    const r = await send('Runtime.evaluate', {
      returnByValue: true,
      expression: `(() => {
        try {
          if (location.pathname.startsWith('/login')) return false;
          const s = window.__INITIAL_STATE__ || {};
          const u = s.user || {};
          if (u.loggedIn === true) return true;
          if (u.loggedIn === false) return false;
          const txt = document.body ? (document.body.innerText || '') : '';
          if (txt.includes('登录后推荐') || txt.includes('扫码登录') || txt.includes('手机号登录')) return false;
          const btn = document.querySelector('.login-container, .login-btn, [class*="login-container"], [class*="login-btn"]');
          if (btn && btn.offsetParent !== null) return false;
          const av = document.querySelector('a[href*="/user/profile/"] img, .side-bar img');
          return !!av;
        } catch (e) { return false; }
      })()`,
    });
    logged = !!(r && r.result && r.result.result && r.result.result.value);
  } catch (e) {
    logged = false;
  }
  ws.close();
  if (created && page.id) {
    try { await fetch(`${HTTP}/json/close/${page.id}`); } catch (e) {}
  }
  console.log(logged ? 'logged_in' : 'not_logged_in');
  process.exit(logged ? 0 : 1);
}
main().catch(() => { console.log('no_cdp'); process.exit(2); });
