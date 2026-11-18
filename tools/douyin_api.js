#!/usr/bin/env node
/**
 * 抖音页面内 API：借页面 SDK 的签名 fetch，直接拿 JSON。
 * 用法:
 *   node douyin_api.js detail <分享链接或作品URL> <out.json> [port]
 *   node douyin_api.js list <like|post> <out.json> [port]
 *   node douyin_api.js fav <out.json> [port]
 *   node douyin_api.js user <主页URL> <out.json> [port]
 *
 * 约定（很重要）：这个脚本要么写出 out.json 并以 0 退出，要么以非 0 退出并在
 * stderr 给出人话原因。绝不允许「什么都不写、安安静静 exit 0」——
 * 上层 engine.py 是靠 out.json 是否存在判断成败的，静默退出会变成一条
 * 没有任何信息的「抖音解析失败：」。CDP 的超时/断连处理见 tools/cdp.js。
 */
const fs = require('fs');
const cdp = require('./cdp');

const MODE = process.argv[2];
const ARG = process.argv[3];
const OUT = MODE === 'fav' ? process.argv[3] : process.argv[4];
const PORT = (MODE === 'fav' ? process.argv[4] : process.argv[5]) || '9222';
// 第 6/7 个参数：limit（0=不限）
const LIMIT_ARG = MODE === 'fav' ? process.argv[5] : process.argv[6];
const LIMIT = parseInt(LIMIT_ARG, 10) || 0; // 0 means unlimited
const HOST = 'https://www.douyin.com';
const CLIENT = 'device_platform=webapp&aid=6383&channel=channel_pc_web&pc_client_type=1&version_code=290100&version_name=29.1.0&cookie_enabled=true&platform=PC';
const sleep = cdp.sleep;

/** 音频直链特征：抖音图文作品里的 video.play_addr 装的其实是背景音乐 mp3 */
function isAudioUrl(u) {
  return /\.(mp3|m4a|aac|flac)(\?|#|$)/i.test(u || '') || /music/i.test(u || '');
}

/** 取一个「真的是视频」的播放地址；拿不到就返回空串，绝不把 mp3 当视频 */
function pickPlayUrl(video) {
  if (!video || typeof video !== 'object') return '';
  const rates = Array.isArray(video.bit_rate) ? [...video.bit_rate] : [];
  rates.sort((a, b) => (b.bit_rate || 0) - (a.bit_rate || 0));
  const lists = rates
    .map(r => r && r.play_addr && r.play_addr.url_list)
    .filter(l => Array.isArray(l) && l.length);
  const cands = [];
  if (lists.length) cands.push(lists[0][lists[0].length - 1]);
  const plain = video.play_addr && video.play_addr.url_list;
  if (Array.isArray(plain) && plain.length) cands.push(plain[plain.length - 1]);
  for (const u of cands) {
    if (u && !isAudioUrl(u)) return u;
  }
  return '';
}

function mapPost(p) {
  const id = String(p.aweme_id || '');
  const images = Array.isArray(p.images) ? p.images : [];
  const isImg = images.length > 0;
  const rates = Array.isArray(p.video && p.video.bit_rate) ? [...p.video.bit_rate] : [];
  rates.sort((a, b) => (b.bit_rate || 0) - (a.bit_rate || 0));
  const play = pickPlayUrl(p.video);
  const cover = (p.video && p.video.cover && p.video.cover.url_list && p.video.cover.url_list[0]) ||
    (images[0] && images[0].url_list && images[0].url_list[0]) || '';
  const imgs = images.map(i => (i.url_list && i.url_list[i.url_list.length - 1]) || '').filter(Boolean);
  // 每张图各自的实况伴生视频（真·实况图文才有；普通图文这里是空的）
  const imgLive = images.map(i => pickPlayUrl(i && i.video));
  // 作品级的伴生视频：只有「真的是视频」才算。
  // 抖音图文的 video.play_addr 装的是背景音乐 mp3（duration=0、bit_rate=[]），
  // 以前直接把它当 live_video，结果每张图都下载一份 mp3 存成 NN_live.mp4，
  // 前端就把它当成实况照片播放 —— 实况功能看起来「坏了」就是这么来的。
  const duration = (p.video && p.video.duration) || 0;
  const postLive = (!isImg || (duration > 0 && play)) ? play : '';
  const au = p.author || {};
  return {
    id, url: `${HOST}/${isImg ? 'note' : 'video'}/${id}`,
    title: (p.desc || '').trim(), type: isImg ? '图文' : '视频',
    author: au.nickname || '', author_id: au.sec_uid || '', unique_id: au.unique_id || '',
    author_url: au.sec_uid ? `${HOST}/user/${au.sec_uid}` : '',
    cover, video: isImg ? '' : play, images: imgs,
    // 图文作品的实况伴生视频，供下载端合成实况照片；没有就是空串
    live_video: isImg ? (imgLive.some(Boolean) ? '' : postLive) : '',
    image_live: isImg ? imgLive : [],
    created: p.create_time || 0, duration,
    data_size: (rates[0] && rates[0].play_addr && rates[0].play_addr.data_size) ||
      (p.video && p.video.play_addr && p.video.play_addr.data_size) || 0,
    stats: p.statistics || {},
  };
}

/** 页面内 fetch 的统一封装：把 HTTP 状态 / 网络异常都变成可判断的字段 */
function pageFetch(apiUrl) {
  return `(async () => { try { const r = await fetch(${JSON.stringify(apiUrl)}, { credentials: 'include' }); if (!r.ok) return { __status: r.status }; return await r.json(); } catch (e) { return { __err: String(e) }; } })()`;
}

function apiError(d) {
  if (!d) return '没有返回';
  if (d.__err) return '页面内请求异常：' + d.__err;
  if (d.__status) return 'HTTP ' + d.__status;
  if (d.status_code) return 'status_code=' + d.status_code + ' ' + (d.status_msg || '');
  return '';
}

function collect(list, items) {
  for (const p of list || []) {
    const it = mapPost(p);
    if (it.id && !items.some(x => x.id === it.id)) items.push(it);
  }
}

/** 需要登录态的模式先自检一次，把「没登录」和「接口坏了」区分开 */
async function requireLogin(c) {
  const me = await c.evaluate(pageFetch(`${HOST}/aweme/v1/web/user/profile/self/?${CLIENT}`));
  const sec = me && me.user && me.user.sec_uid;
  if (!sec) {
    throw new Error('需要登录抖音（页面里没有登录态）：请在 设置 → 数据源 → 抖音 里重新扫码登录');
  }
  return sec;
}

async function modeDetail(c, nav) {
  const href = await nav(ARG);
  const m = href.match(/\/(?:video|note)\/(\d+)/) || href.match(/[?&]modal_id=(\d+)/);
  if (!m) throw new Error('没能解析出作品 ID（页面可能是登录/验证页）: ' + href.slice(0, 120));
  const id = m[1];
  const apiUrl = `${HOST}/aweme/v1/web/aweme/detail/?aweme_id=${id}&${CLIENT}`;
  let data = null, lastErr = '';
  for (let attempt = 1; attempt <= 3; attempt++) {
    const d = await c.evaluate(pageFetch(apiUrl));
    lastErr = apiError(d) || (d && d.aweme_detail ? '' : '返回里没有 aweme_detail');
    if (!lastErr) { data = d; break; }
    if (attempt < 3) {
      await sleep(1200 * attempt);
      // 重新加载一次页面：签名/风控参数过期时刷新就能恢复
      await nav(href, { tries: 1 }).catch(() => {});
    }
  }
  if (!data) throw new Error(`详情接口失败（aweme_id=${id}，重试 3 次）：${lastErr}`);
  const item = mapPost(data.aweme_detail);
  if (!item.id) throw new Error('详情为空（可能需要登录或过验证）');
  return { item, log: `detail ${id} type=${item.type} author=${item.author} images=${(item.images || []).length} live=${(item.image_live || []).filter(Boolean).length + (item.live_video ? 1 : 0)}` };
}

async function modeFav(c, nav) {
  await nav(`${HOST}/user/self?showTab=favorite_collection`);
  await requireLogin(c);
  const urlBase = 'https://www-hj.douyin.com/aweme/v1/web/aweme/listcollection/?device_platform=webapp&aid=6383&channel=channel_pc_web&pc_client_type=1&version_code=170400&version_name=17.4.0&cookie_enabled=true&platform=PC';
  const perPage = 20;
  const maxPages = LIMIT > 0 ? Math.ceil(LIMIT / perPage) + 2 : 50;
  const want = LIMIT > 0 ? LIMIT : Infinity;
  let cursor = 0, items = [], pages = 0, hasMore = true;
  let emptyStreak = 0, reason = 'cap';
  while (pages < maxPages && hasMore) {
    if (items.length >= want) { reason = 'want'; break; }
    const res = await c.evaluate(`new Promise(res => { try { const x = new XMLHttpRequest(); x.open('POST', ${JSON.stringify(urlBase)}, true); x.withCredentials = true; x.timeout = 20000; x.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded'); x.onload = () => res({status: x.status, body: x.responseText || ''}); x.ontimeout = () => res({status: 0, body: '', err: 'timeout'}); x.onerror = () => res({status: 0, body: '', err: 'network'}); x.send('count=${perPage}&cursor=${cursor}'); } catch (e) { res({status: 0, body: '', err: String(e)}); } })`);
    if (!res || !res.status) {
      if (pages === 0) throw new Error('收藏接口请求失败：' + ((res && res.err) || '没有响应') +
        '（cursor=' + cursor + '，通常是登录态失效或被风控）');
      reason = 'error'; break;
    }
    let d = null;
    try { d = JSON.parse(res.body); } catch (e) {
      if (pages === 0) throw new Error('收藏接口返回非 JSON（HTTP ' + res.status + '，可能被重定向到登录页）');
      reason = 'error'; break;
    }
    if (d.status_code) {
      if (pages === 0) throw new Error('收藏接口 status_code=' + d.status_code + ' ' + (d.status_msg || ''));
      reason = 'error'; break;
    }
    const list = d.aweme_list || [];
    if (!list.length) {
      emptyStreak++;
      if (pages === 0) throw new Error('收藏列表为空（可能需要重新登录，或收藏确实是空的）');
      if (emptyStreak >= 2) { reason = 'empty2'; break; }
      hasMore = Boolean(d.has_more);
      const next = d.cursor || d.max_cursor || 0;
      if (next !== cursor) cursor = next; else { hasMore = false; reason = 'cursor'; }
      pages += 1;
      continue;
    }
    emptyStreak = 0;
    const before = items.length;
    collect(list, items);
    if (items.length === before) { reason = 'dup'; break; } // 全是重复
    hasMore = Boolean(d.has_more);
    const next = d.cursor || d.max_cursor || 0;
    if (next === cursor) { hasMore = false; reason = 'cursor'; }
    cursor = next;
    pages += 1;
    if (!hasMore) reason = 'hasmore';
  }
  const complete = items.length < want && reason !== 'cap';
  if (LIMIT > 0 && items.length > LIMIT) items.length = LIMIT;
  return { payload: { kind: 'fav', items, complete }, log: `fav ${items.length} items (${pages} pages, complete=${complete})` };
}

/** 页面 DOM 滚动收集：像真人一样往下滚，把网格里渲染出来的作品链接全捡走。
 *  API 分页被风控截断时（has_more 提前变 false / 游标卡死），这是兜底来源。 */
async function domScrollCollect(c, want, haveIds) {
  const seen = new Set(haveIds);
  const map = new Map();
  let stable = 0;
  const maxRounds = 80;
  const collectExpr = `(() => {
    const out=[]; const s=new Set();
    for (const a of document.querySelectorAll('a[href*="/video/"],a[href*="/note/"]')) {
      // 页脚推荐位里的链接不是本博主的作品，排除
      if (a.closest('[class*="footer"]')) continue;
      const href=a.href||'';
      const m=href.match(/\\/(?:video|note)\\/([0-9A-Za-z]+)/);
      if(!m||s.has(m[1])) continue; s.add(m[1]);
      let card=a, img=a.querySelector('img');
      for(let i=0;i<5&&card&&!img;i++){ card=card.parentElement; if(card) img=card.querySelector('img'); }
      let cover=img?(img.currentSrc||img.src||img.getAttribute('data-src')||''):'';
      if(cover.startsWith('blob:')||cover.startsWith('data:')) cover='';
      let title=(img&&img.alt)?img.alt.trim():'';
      if(!title&&card){ const t=(card.innerText||'').split('\\n').map(x=>x.trim()).filter(Boolean); title=t[0]||''; }
      out.push({id:m[1], url:href.split('?')[0], cover, title,
                type:/\\/note\\//.test(href)?'图文':'视频', author:'', author_id:'', author_url:'',
                video:'', images:[], live_video:'', image_live:[], created:0, duration:0, data_size:0, stats:{}});
    }
    return out;
  })()`;
  // 抖音主页真正的滚动容器是路由容器 div（window 不动），把所有可滚动容器都滚到底
  const scrollExpr = `(() => {
    const links=[...document.querySelectorAll('a[href*="/video/"],a[href*="/note/"]')];
    const last=links[links.length-1];
    if(last) last.scrollIntoView({block:'end'});
    window.scrollTo(0, document.body.scrollHeight);
    const sc=document.scrollingElement||document.documentElement;
    sc.scrollTop=sc.scrollHeight;
    for (const d of document.querySelectorAll('div,main,section')) {
      if (d.scrollHeight>d.clientHeight+100 && ['auto','scroll'].includes(getComputedStyle(d).overflowY)) d.scrollTop=d.scrollHeight;
    }
    return document.body.scrollHeight;
  })()`;
  for (let round = 0; round < maxRounds; round++) {
    const batch = (await c.evaluate(collectExpr).catch(() => null)) || [];
    let added = 0;
    for (const it of batch) {
      if (!seen.has(it.id)) { seen.add(it.id); map.set(it.id, it); added++; }
    }
    if (Number.isFinite(want) && seen.size >= want) break;
    if (added === 0) { stable++; if (stable >= 6 && map.size > 0) break; } else stable = 0;
    if (map.size === 0 && round >= 7) break;
    await c.evaluate(scrollExpr).catch(() => null);
    await sleep(850);
  }
  return [...map.values()];
}

/** 读页面上声明的作品/喜欢总数（「作品 80」这种 tab 标签），用来判断 API 有没有撒谎 */
function totalExpr(kind) {
  const label = kind === 'like' ? '喜欢' : '作品';
  return `(() => { const t=document.body.innerText||''; const m=t.match(new RegExp('${label}\\\\s*([0-9]+)')); return m?parseInt(m[1],10):0; })()`;
}

async function modeUser(c, nav) {
  const href = await nav(ARG);
  const m = href.match(/\/user\/([^/?#]+)/);
  if (!m) throw new Error('没能解析出博主 sec_uid（页面可能不是主页）: ' + href.slice(0, 120));
  const sec = m[1];
  const want = LIMIT > 0 ? LIMIT : Infinity;   // 目标条数
  const total = (await c.evaluate(totalExpr('post')).catch(() => 0)) || 0;  // 页面声明的作品总数
  // ---- 阶段 1：API 分页（快、字段全） ----
  const perPage = 20;
  const maxPages = LIMIT > 0 ? Math.ceil(LIMIT / perPage) + 2 : 60;
  let cursor = 0, items = [], pages = 0, hasMore = true;
  let emptyStreak = 0, apiErr = '', reason = 'cap';
  while (pages < maxPages && hasMore) {
    if (items.length >= want) { reason = 'want'; break; }
    const apiUrl = `${HOST}/aweme/v1/web/aweme/post/?${CLIENT}&sec_user_id=${sec}&count=${perPage}&max_cursor=${cursor}`;
    const d = await c.evaluate(pageFetch(apiUrl));
    const err = apiError(d);
    if (err) {
      if (pages === 0) { apiErr = err; reason = 'error0'; break; }
      reason = 'error'; break;   // 中途被风控：保留已拿到的，交给阶段 2 兜底
    }
    const list = d.aweme_list || [];
    if (!list.length) {
      emptyStreak++;
      if (pages === 0) { apiErr = 'empty'; reason = 'empty0'; break; }
      if (emptyStreak >= 2) { reason = 'empty2'; break; }
      hasMore = Boolean(d.has_more);
      const next = d.max_cursor || 0;
      if (next !== cursor) cursor = next; else { hasMore = false; reason = 'cursor'; }
      pages += 1;
      continue;
    }
    emptyStreak = 0;
    const before = items.length;
    collect(list, items);
    if (items.length === before) { reason = 'dup'; break; }   // 全是重复，到头了
    hasMore = Boolean(d.has_more);
    const next = d.max_cursor || 0;
    if (next === cursor) { hasMore = false; reason = 'cursor'; }
    cursor = next;
    pages += 1;
    if (!hasMore) reason = 'hasmore';
  }
  // ---- 阶段 2：API 拿得比页面声明的总数少就滚页面兜底（API 说完了不算，页面滚完才算） ----
  // reason=want：已经拿够；cap：大列表撞页数上限（滚页面也救不回多少）→ 不滚
  // reason=error：中途被风控 → 无论几页都滚
  // total>0 且 items<total：抖音把每页 count 砍到个位数再提前 has_more=false 的典型截断 → 滚
  const needDom = items.length < want &&
    (reason === 'error' ||
     (total > 0 && items.length < total) ||
     (reason !== 'want' && reason !== 'cap' && pages < 6));
  let domCount = 0;
  if (needDom) {
    const wantDom = total > 0 ? Math.min(want, total) : want;
    const domItems = await domScrollCollect(c, wantDom, items.map(x => x.id));
    domCount = domItems.length;
    if (domItems.length) {
      // 以 DOM 顺序（真实时间序）为骨架，API 拿到的富字段覆盖上去
      const byId = new Map(items.map(x => [x.id, x]));
      const merged = domItems.map(d => byId.get(d.id) || d);
      const dset = new Set(domItems.map(d => d.id));
      for (const x of items) if (!dset.has(x.id)) merged.push(x);
      items = merged;
    }
  }
  if (!items.length) {
    throw new Error(apiErr === 'empty' || !apiErr
      ? '主页作品为空：可能需要登录，或这个博主没有公开作品'
      : '主页作品接口' + apiErr);
  }
  // 没到目标条数就自然结束 = 源到头了（complete）；到目标条数停下 / 撞页数上限 = 可能还有更多
  // 页面声明的总数拿满了也算 complete
  const complete = (items.length < want && reason !== 'cap') || (total > 0 && items.length >= total);
  if (LIMIT > 0 && items.length > LIMIT) items.length = LIMIT;
  return { payload: { kind: 'user', sec_uid: sec, items, complete },
           log: `user ${sec.slice(0, 20)} ${items.length}/${total || '?'} items (api ${pages} pages + dom ${domCount}, complete=${complete})` };
}

async function modeList(c, nav) {
  const kind = ARG === 'post' ? 'post' : 'like';
  await nav(`${HOST}/user/self?showTab=${kind === 'like' ? 'like' : 'post'}`);
  const sec = await requireLogin(c);
  const endpoint = kind === 'like' ? 'aweme/v1/web/aweme/favorite/' : 'aweme/v1/web/aweme/post/';
  const perPage = 20;
  const maxPages = LIMIT > 0 ? Math.ceil(LIMIT / perPage) + 2 : 50;
  const want = LIMIT > 0 ? LIMIT : Infinity;
  const total = (await c.evaluate(totalExpr(kind)).catch(() => 0)) || 0;
  let cursor = 0, items = [], pages = 0, hasMore = true;
  let emptyStreak = 0, reason = 'cap';
  while (pages < maxPages && hasMore) {
    if (items.length >= want) { reason = 'want'; break; }
    const apiUrl = `${HOST}/${endpoint}?${CLIENT}&sec_user_id=${sec}&count=${perPage}&max_cursor=${cursor}`;
    const d = await c.evaluate(pageFetch(apiUrl));
    const err = apiError(d);
    if (err) {
      if (pages === 0) throw new Error('列表接口' + err);
      reason = 'error'; break;
    }
    const list = d.aweme_list || [];
    if (!list.length) {
      emptyStreak++;
      if (pages === 0) throw new Error('列表为空：可能需要登录，或这个列表对当前账号不可见');
      if (emptyStreak >= 2) { reason = 'empty2'; break; }
      hasMore = Boolean(d.has_more);
      const next = d.max_cursor || 0;
      if (next !== cursor) cursor = next; else { hasMore = false; reason = 'cursor'; }
      pages += 1;
      continue;
    }
    emptyStreak = 0;
    const before = items.length;
    collect(list, items);
    if (items.length === before) { reason = 'dup'; break; }
    hasMore = Boolean(d.has_more);
    const next = d.max_cursor || 0;
    if (next === cursor) { hasMore = false; reason = 'cursor'; }
    cursor = next;
    pages += 1;
    if (!hasMore) reason = 'hasmore';
  }
  // 不够且停得可疑就滚页面兜底（与 modeUser 同一套逻辑）
  const needDom = items.length < want &&
    (reason === 'error' ||
     (total > 0 && items.length < total) ||
     (reason !== 'want' && reason !== 'cap' && pages < 6));
  let domCount = 0;
  if (needDom) {
    const wantDom = total > 0 ? Math.min(want, total) : want;
    const domItems = await domScrollCollect(c, wantDom, items.map(x => x.id));
    domCount = domItems.length;
    if (domItems.length) {
      const byId = new Map(items.map(x => [x.id, x]));
      const merged = domItems.map(d => byId.get(d.id) || d);
      const dset = new Set(domItems.map(d => d.id));
      for (const x of items) if (!dset.has(x.id)) merged.push(x);
      items = merged;
    }
  }
  const complete = (items.length < want && reason !== 'cap') || (total > 0 && items.length >= total);
  if (LIMIT > 0 && items.length > LIMIT) items.length = LIMIT;
  return { payload: { kind, sec_uid: sec, items, complete }, log: `list ${kind} ${items.length}/${total || '?'} items (${pages} pages + dom ${domCount}, complete=${complete})` };
}

(async () => {
  if (!MODE || !OUT || (MODE !== 'fav' && !ARG)) {
    throw new Error('usage: douyin_api.js detail <url> <out> [port] | list <like|post> <out> [port] | fav <out> [port] | user <url> <out> [port]');
  }
  const HANDLERS = { detail: modeDetail, fav: modeFav, user: modeUser, list: modeList };
  const handler = HANDLERS[MODE];
  if (!handler) throw new Error('未知模式: ' + MODE);

  const result = await cdp.withTab(PORT, async (c) => {
    await c.send('Page.enable');
    await c.send('Runtime.enable');
    await c.send('Network.enable');
    const nav = (url, opts) => cdp.navigate(c, url, opts);
    return handler(c, nav);
  });

  if (result.item) {
    fs.writeFileSync(OUT, JSON.stringify(result.item, null, 2));
  } else {
    fs.writeFileSync(OUT, JSON.stringify(result.payload, null, 2));
  }
  console.log(result.log);
  cdp.finish(0);
})().catch(e => {
  console.error('douyin_api failed:', (e && e.message) || String(e));
  cdp.finish(1);
});
