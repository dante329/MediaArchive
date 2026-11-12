#!/usr/bin/env node
/**
 * 从抖音/小红书的收藏、喜欢页抓作品列表（复用常驻 Chrome 的登录态）。
 * 用法: node fetch_list.js <douyin|xhs> <fav|like> <out.json> [port] [targetUrl]
 * 输出: {platform, kind, page, items:[{id,url,cover,title,type}]}
 */
const fs = require('fs');
const [platform, kind, outPath, portArg, targetUrl] = process.argv.slice(2);
const PORT = portArg || '9222';
const HTTP = `http://127.0.0.1:${PORT}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const URL_MAP = {
  douyin: {
    fav: 'https://www.douyin.com/user/self?showTab=favorite_collection',
    like: 'https://www.douyin.com/user/self?showTab=like',
  },
};

let CURRENT = null;
async function closeTab() {
  if (!CURRENT) return;
  try { await fetch(`${HTTP}/json/close/${CURRENT.id}`); } catch (e) {}
  CURRENT = null;
}

async function newTab() {
  for (let i = 0; i < 20; i++) {
    try {
      const r = await fetch(`${HTTP}/json/new?about:blank`, { method: 'PUT' });
      const t = await r.json();
      if (t && t.webSocketDebuggerUrl) { CURRENT = t; return t; }
    } catch (e) {}
    await sleep(300);
  }
  throw new Error('连不上 Chrome 调试端口 ' + PORT);
}

function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let nextId = 1;
  const pending = new Map();
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { resolve, reject } = pending.get(m.id);
      pending.delete(m.id);
      m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
    }
  });
  const ready = new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve);
    ws.addEventListener('error', () => reject(new Error('WebSocket 连接失败')));
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
  return { ws, ready, send };
}

(async () => {
  const target = await newTab();
  const cdp = connect(target.webSocketDebuggerUrl);
  await cdp.ready;
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  await cdp.send('Network.enable', { maxTotalBufferSize: 64 * 1024 * 1024 });

  const evalJs = async (expression) => {
    try {
      const r = await cdp.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
      return r.result?.value;
    } catch (e) { return null; }
  };
  const navigate = async (url) => {
    await cdp.send('Page.navigate', { url });
    for (let i = 0; i < 50; i++) {
      await sleep(500);
      const st = await evalJs('document.readyState');
      if (st === 'complete') break;
    }
    await sleep(3000);
  };

  let pageUrl = targetUrl || URL_MAP[platform]?.[kind] || (platform === 'xhs' ? 'https://www.xiaohongshu.com/' : '');
  if (!pageUrl) throw new Error('没有可用的列表地址');

  await navigate(pageUrl);

  // 小红书：从首页找到“我/个人主页”，再点收藏/赞过
  if (platform === 'xhs' && !targetUrl) {
    const prof = await evalJs(`(() => {
      const as=[...document.querySelectorAll('a[href*="/user/profile/"]')];
      const mine=as.find(a=>{ const t=(a.textContent||'').trim(); const l=(a.getAttribute('aria-label')||''); return t==='我' || t.includes('我的') || l.includes('我'); });
      if(mine) return mine.href;
      const vis=as.filter(a=>a.offsetParent!==null);
      return vis.length ? vis[0].href : '';
    })()`);
    if (prof) {
      await navigate(prof);
    } else {
      await evalJs(`(() => {
        const els=[...document.querySelectorAll('a,div,span,button')].filter(e=>e.children.length===0 && ['我','我的'].includes(e.textContent.trim()));
        if(els[0]) els[0].click();
        return els.length;
      })()`);
      await sleep(3500);
    }
    const tab = kind === 'like' ? '赞过' : '收藏';
    await evalJs(`(() => {
      const want=${JSON.stringify(tab)};
      const els=[...document.querySelectorAll('a,div,span,button')].filter(e=>{
        const t=(e.textContent||'').trim();
        return t===want || t.startsWith(want + ' ') || t.startsWith(want + '\\n') || t.startsWith(want + '·');
      });
      if(els[0]) els[0].click();
      return els.length;
    })()`);
    await sleep(2500);
    pageUrl = (await evalJs('location.href')) || pageUrl;
  }

  const PROFILES = {
    x: { sel: 'a[href*="/status/"]', id: /\/status\/(\d+)/ },
    instagram: { sel: 'a[href*="/p/"],a[href*="/reel/"]', id: /\/(?:p|reel)\/([0-9A-Za-z_-]+)/ },
    weibo: { sel: 'a[href*="/detail/"],a[href*="/status/"]', id: /\/(?:detail|status)\/([0-9A-Za-z_]+)/ },
  };
  const profile = PROFILES[platform];
  const isProfile = Boolean(profile);
  const itemSel = profile ? profile.sel : (platform === 'douyin'
    ? 'a[href*="/video/"],a[href*="/note/"]'
    : 'a[href*="/explore/"],a[href*="/discovery/item/"]');
  const colSel = 'a[href*="/collection/"]';
  const idPat = profile ? profile.id : /\/(?:video|note|explore|discovery\/item)\/([0-9A-Za-z]+)/;

  const collectExpr = (sel, col) => `(() => {
    const out=[]; const seen=new Set();
    const links=[...document.querySelectorAll(${JSON.stringify(sel)})];
    for(const a of links){
      const href=a.href||'';
      const m=href.match(${JSON.stringify(idPat.source)});
      if(!m||seen.has(m[1])) continue; seen.add(m[1]);
      let card=a, img=a.querySelector('img');
      for(let i=0;i<5&&card&&!img;i++){ card=card.parentElement; if(card) img=card.querySelector('img'); }
      let cover=img?(img.currentSrc||img.src||img.getAttribute('data-src')||''):'';
      if(cover.startsWith('blob:')||cover.startsWith('data:')) cover='';
      let title=(img&&img.alt)?img.alt.trim():'';
      if(!title&&card){ const t=(card.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean); title=t[0]||''; }
      const isVid = !!a.querySelector('video') || /\/reel\//.test(href);
      out.push({id:m[1],url:href.split('?')[0],cover,title,type:${col ? '"collection"' : '"item"'},videohint:isVid});
    }
    return out;
  })()`;

  const colExpr = `(() => {
    const out=[]; const seen=new Set();
    for(const a of [...document.querySelectorAll(${JSON.stringify(colSel)})]){
      const href=a.href||'';
      const m=href.match(/\\/collection\\/([0-9A-Za-z]+)/);
      if(!m||seen.has(m[1])) continue; seen.add(m[1]);
      let card=a, img=a.querySelector('img');
      for(let i=0;i<5&&card&&!img;i++){ card=card.parentElement; if(card) img=card.querySelector('img'); }
      let cover=img?(img.currentSrc||img.src||img.getAttribute('data-src')||''):'';
      if(cover.startsWith('blob:')||cover.startsWith('data:')) cover='';
      let title=(img&&img.alt)?img.alt.trim():'';
      if(!title&&card){ const t=(card.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean); title=t[1]||t[0]||''; }
      out.push({id:m[1],url:href.split('?')[0],cover,title,type:'collection'});
    }
    return out;
  })()`;

  const map = new Map();
  let stable = 0;
  const maxRounds = isProfile ? 14 : 60;
  for (let round = 0; round < maxRounds; round++) {
    let batch = (await evalJs(collectExpr(itemSel, false))) || [];
    if (isProfile) {
      batch = batch.map(it => { const typ = it.videohint ? '视频' : '图文'; delete it.videohint; return { ...it, type: typ }; });
    }
    if (!batch.length) {
      const cols = (await evalJs(colExpr)) || [];
      batch = cols;
    }
    let added = 0;
    for (const it of batch) {
      if (!map.has(it.id)) { map.set(it.id, it); added++; }
    }
    if (added === 0) stable++; else stable = 0;
    if (stable >= 6 && map.size > 0) break;
    await evalJs(`(() => {
      window.scrollTo(0, document.body.scrollHeight);
      const sc=document.scrollingElement||document.documentElement;
      sc.scrollTop=sc.scrollHeight;
      const c=[...document.querySelectorAll('div')].find(d=>d.scrollHeight>d.clientHeight+300 && ['auto','scroll'].includes(getComputedStyle(d).overflowY));
      if(c) c.scrollTop=c.scrollHeight;
      return document.body.scrollHeight;
    })()`);
    await sleep(850);
  }

  pageUrl = (await evalJs('location.href')) || pageUrl;
  const items = [...map.values()];
  if (!items.length && isProfile) {
    throw new Error('没有抓到作品：' + platform + ' 可能需要登录，或当前页面不可见');
  }
  fs.writeFileSync(outPath, JSON.stringify({ platform, kind, page: pageUrl, items }, null, 2));
  console.log(`[list] ${platform}/${kind} 共 ${items.length} 条`);
  cdp.ws.close();
  await closeTab();
  process.exit(0);
})().catch(async (e) => {
  console.error('抓列表失败:', e.message);
  await closeTab();
  process.exit(1);
});
