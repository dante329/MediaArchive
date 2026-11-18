#!/usr/bin/env node
/**
 * 检查小红书是否真的登录（不能只看 web_session：游客也有这个 cookie）。
 * 输出: logged_in | not_logged_in | no_cdp
 * 退出码: 0=已登录 1=未登录 2=连不上 Chrome
 *
 * CDP 的超时/断连处理见 tools/cdp.js。这里额外把命令超时压到 15s：
 * 登录态探测是同步卡在 UI 上的，宁可快点回 no_cdp 也不要吊着。
 */
const cdp = require('./cdp');

const PORT = process.argv[2] || '9222';
const CMD_TIMEOUT = 15000;

const CHECK_EXPR = `(() => {
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
})()`;

async function main() {
  let list;
  try {
    list = await cdp.listTabs(PORT);
  } catch (e) {
    console.log('no_cdp'); cdp.finish(2); return;
  }

  // 已经有小红书的页面就直接借用，别多开标签页（也避免打扰正在跑的任务）
  let page = (list || []).find(t => t.type === 'page' && /xiaohongshu\.com/.test(t.url || ''));
  let created = false;
  if (!page) {
    try {
      page = await cdp.openTab(PORT, encodeURIComponent('https://www.xiaohongshu.com/'), { tries: 5, delay: 300 });
      created = true;
    } catch (e) {
      console.log('no_cdp'); cdp.finish(2); return;
    }
  }
  if (!page || !page.webSocketDebuggerUrl) { console.log('no_cdp'); cdp.finish(2); return; }

  let client = null;
  let logged = false;
  try {
    client = await cdp.connect(page.webSocketDebuggerUrl, { cmdTimeout: CMD_TIMEOUT, openTimeout: CMD_TIMEOUT });
    if (!/xiaohongshu\.com/.test(page.url || '')) {
      await client.send('Page.enable', {}, CMD_TIMEOUT);
      await client.send('Page.navigate', { url: 'https://www.xiaohongshu.com/' }, CMD_TIMEOUT);
      await cdp.sleep(3500);
    }
    await cdp.sleep(1200);
    logged = !!(await client.evaluate(CHECK_EXPR, { timeout: CMD_TIMEOUT }));
  } catch (e) {
    // 连不上 / 超时：明确报 no_cdp，别把「浏览器没了」说成「没登录」
    console.log('no_cdp');
    if (client) { try { client.close(); } catch (err) {} }
    if (created) await cdp.closeTab(PORT, page.id);
    cdp.finish(2);
    return;
  }

  if (client) { try { client.close(); } catch (e) {} }
  if (created) await cdp.closeTab(PORT, page.id);
  console.log(logged ? 'logged_in' : 'not_logged_in');
  cdp.finish(logged ? 0 : 1);
}

main().catch(() => { console.log('no_cdp'); cdp.finish(2); });
