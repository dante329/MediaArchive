#!/usr/bin/env node
/**
 * 从抖音/小红书的收藏、喜欢页抓作品列表（复用常驻 Chrome 的登录态）。
 * 用法: node fetch_list.js <douyin|xhs> <fav|like|list> <out.json> [port] [targetUrl]
 * 输出: {platform, kind, page, items:[{id,url,cover,title,type}]}
 *
 * CDP 的超时/断连处理见 tools/cdp.js：以前 send() 不会 reject，常驻 Chrome 被
 * 重启时脚本会一声不响地以 0 退出，上层只看到「列表是空的」。
 */
const fs = require('fs');
const cdp = require('./cdp');

const [platform, kind, outPath, portArg, targetUrl] = process.argv.slice(2);
const PORT = portArg || '9222';
const sleep = cdp.sleep;

const URL_MAP = {
  douyin: {
    fav: 'https://www.douyin.com/user/self?showTab=favorite_collection',
    like: 'https://www.douyin.com/user/self?showTab=like',
  },
};

(async () => {
  if (!platform || !kind || !outPath) {
    throw new Error('usage: fetch_list.js <douyin|xhs> <fav|like|list> <out.json> [port] [targetUrl]');
  }
  let pageUrl = targetUrl || (URL_MAP[platform] || {})[kind] ||
    (platform === 'xhs' ? 'https://www.xiaohongshu.com/' : '');
  if (!pageUrl) throw new Error('没有可用的列表地址');

  const result = await cdp.withTab(PORT, async (c) => {
    await c.send('Page.enable');
    await c.send('Runtime.enable');
    await c.send('Network.enable', { maxTotalBufferSize: 64 * 1024 * 1024 });

    const evalJs = expr => c.evaluate(expr).catch(() => null);
    const navigate = url => cdp.navigate(c, url, { tries: 2, settleMs: 3000 });

    let href = await navigate(pageUrl);

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
        href = await navigate(prof);
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
      href = (await evalJs('location.href')) || href;
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
        if(a.closest('[class*="footer"]')) continue;   // 页脚推荐位不是列表本体
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
    let complete = true;   // 滚到滚不动为止才算 complete；撞 maxRounds 上限就是没滚完
    const maxRounds = isProfile ? 14 : 60;
    for (let round = 0; round < maxRounds; round++) {
      let batchItems = (await evalJs(collectExpr(itemSel, false))) || [];
      if (isProfile) {
        batchItems = batchItems.map(it => {
          const typ = it.videohint ? '视频' : '图文';
          delete it.videohint;
          return { ...it, type: typ };
        });
      }
      if (!batchItems.length) {
        batchItems = (await evalJs(colExpr)) || [];
      }
      let added = 0;
      for (const it of batchItems) {
        if (!map.has(it.id)) { map.set(it.id, it); added++; }
      }
      if (added === 0) stable++; else stable = 0;
      if (stable >= 6 && map.size > 0) break;
      // 一直什么都没有就别白滚 60 轮：八成是没登录或页面结构变了，早失败早说清楚
      if (map.size === 0 && round >= 7) break;
      await evalJs(`(() => {
        window.scrollTo(0, document.body.scrollHeight);
        const sc=document.scrollingElement||document.documentElement;
        sc.scrollTop=sc.scrollHeight;
        for (const d of document.querySelectorAll('div,main,section')) {
          if (d.scrollHeight>d.clientHeight+100 && ['auto','scroll'].includes(getComputedStyle(d).overflowY)) d.scrollTop=d.scrollHeight;
        }
        return document.body.scrollHeight;
      })()`);
      await sleep(850);
    }

    if (map.size > 0 && stable < 6) complete = false;   // 撞了轮数上限，没滚完
    const finalHref = (await evalJs('location.href')) || href;
    const items = [...map.values()];
    if (!items.length) {
      throw new Error('没有抓到作品：' + platform + ' 可能需要登录，或当前页面不可见（' +
        String(finalHref).slice(0, 120) + '）');
    }
    return { payload: { platform, kind, page: finalHref, items, complete }, log: `[list] ${platform}/${kind} 共 ${items.length} 条 (complete=${complete})` };
  });

  fs.writeFileSync(outPath, JSON.stringify(result.payload, null, 2));
  console.log(result.log);
  cdp.finish(0);
})().catch(e => {
  console.error('抓列表失败:', (e && e.message) || String(e));
  cdp.finish(1);
});
