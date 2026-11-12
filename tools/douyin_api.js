#!/usr/bin/env node
/**
 * 抖音页面内 API：借页面 SDK 的签名 fetch，直接拿 JSON。
 * 用法:
 *   node douyin_api.js detail <分享链接或作品URL> <out.json> [port]
 *   node douyin_api.js list <like|post> <out.json> [port]
 */
const fs = require('fs');
const MODE = process.argv[2];
const ARG = process.argv[3];
const OUT = MODE === 'fav' ? process.argv[3] : process.argv[4];
const PORT = (MODE === 'fav' ? process.argv[4] : process.argv[5]) || '9222';
const HOST = 'https://www.douyin.com';
const HTTP = `http://127.0.0.1:${PORT}`;
const CLIENT = 'device_platform=webapp&aid=6383&channel=channel_pc_web&pc_client_type=1&version_code=290100&version_name=29.1.0&cookie_enabled=true&platform=PC';
const sleep = ms => new Promise(r => setTimeout(r, ms));

function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 1; const pending = new Map();
  ws.addEventListener('message', ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  const ready = new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', () => rej(new Error('WebSocket 失败'))); });
  const send = (method, params = {}) => new Promise(res => {
    const i = id++; pending.set(i, res);
    ws.send(JSON.stringify({ id: i, method, params }));
  });
  return { ws, ready, send };
}

function mapPost(p) {
  const id = String(p.aweme_id || '');
  const images = Array.isArray(p.images) ? p.images : [];
  const isImg = images.length > 0;
  const rates = Array.isArray(p.video && p.video.bit_rate) ? [...p.video.bit_rate] : [];
  rates.sort((a, b) => (b.bit_rate || 0) - (a.bit_rate || 0));
  const pick = rates.map(r => r && r.play_addr && r.play_addr.url_list).find(Boolean) || [];
  const play = (pick && pick[pick.length - 1]) ||
    (p.video && p.video.play_addr && p.video.play_addr.url_list && p.video.play_addr.url_list.slice(-1)[0]) || '';
  const cover = (p.video && p.video.cover && p.video.cover.url_list && p.video.cover.url_list[0]) ||
    (images[0] && images[0].url_list && images[0].url_list[0]) || '';
  const imgs = images.map(i => (i.url_list && i.url_list[i.url_list.length - 1]) || '').filter(Boolean);
  const au = p.author || {};
  return {
    id, url: `${HOST}/${isImg ? 'note' : 'video'}/${id}`,
    title: (p.desc || '').trim(), type: isImg ? '图文' : '视频',
    author: au.nickname || '', author_id: au.sec_uid || '', unique_id: au.unique_id || '',
    author_url: au.sec_uid ? `${HOST}/user/${au.sec_uid}` : '',
    cover, video: isImg ? '' : play, images: imgs,
    created: p.create_time || 0, duration: (p.video && p.video.duration) || 0,
    data_size: (rates[0] && rates[0].play_addr && rates[0].play_addr.data_size) || (p.video && p.video.play_addr && p.video.play_addr.data_size) || 0,
    stats: p.statistics || {},
  };
}

(async () => {
  if (!MODE || !ARG || !OUT) throw new Error('usage: douyin_api.js detail <url> <out> [port] | list <like|post> <out> [port]');
  const t = await (await fetch(`${HTTP}/json/new?about:blank`, { method: 'PUT' })).json();
  const cdp = connect(t.webSocketDebuggerUrl);
  await cdp.ready;
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  await cdp.send('Network.enable');
  const evalJs = async expr => {
    const r = await cdp.send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.result && r.result.exceptionDetails) throw new Error((r.result.exceptionDetails.text || '页面执行失败'));
    return r.result && r.result.result ? r.result.result.value : null;
  };
  const nav = async url => {
    await cdp.send('Page.navigate', { url });
    for (let i = 0; i < 30; i++) { await sleep(500); if ((await evalJs('document.readyState')) === 'complete') break; }
    await sleep(2500);
  };

  if (MODE === 'detail') {
    await nav(ARG);
    const href = (await evalJs('location.href')) || '';
    const m = href.match(/\/(?:video|note)\/(\d+)/) || href.match(/[?&]modal_id=(\d+)/);
    if (!m) throw new Error('没能解析出作品 ID（页面可能是登录/验证页）: ' + href.slice(0, 120));
    const id = m[1];
    const data = await evalJs(`(async () => { const r = await fetch('${HOST}/aweme/v1/web/aweme/detail/?aweme_id=${id}&${CLIENT}', { credentials: 'include' }); if (!r.ok) return { __status: r.status }; return await r.json(); })()`);
    if (!data || data.__status) throw new Error(`详情接口 HTTP ${data && data.__status}`);
    if (data.status_code) throw new Error('详情接口 status_code=' + data.status_code + ' ' + (data.status_msg || ''));
    const a = data.aweme_detail;
    if (!a) throw new Error('详情为空（可能需要登录或过验证）');
    const item = mapPost(a);
    fs.writeFileSync(OUT, JSON.stringify(item, null, 2));
    console.log(`detail ${id} type=${item.type} author=${item.author}`);
  } else if (MODE === 'fav') {
    await nav(`${HOST}/user/self?showTab=favorite_collection`);
    const urlBase = 'https://www-hj.douyin.com/aweme/v1/web/aweme/listcollection/?device_platform=webapp&aid=6383&channel=channel_pc_web&pc_client_type=1&version_code=170400&version_name=17.4.0&cookie_enabled=true&platform=PC';
    let cursor = 0, items = [], pages = 0, hasMore = true;
    while (pages < 15 && hasMore) {
      const res = await evalJs(`new Promise(res => { try { const x = new XMLHttpRequest(); x.open('POST', ${JSON.stringify(urlBase)}, true); x.withCredentials = true; x.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded'); x.onload = () => res({status: x.status, body: x.responseText || ''}); x.onerror = () => res({status: 0, body: ''}); x.send('count=18&cursor=${cursor}'); } catch (e) { res({status: 0, body: String(e)}); } })`);
      if (!res || !res.status) throw new Error('收藏接口请求失败');
      let d = null;
      try { d = JSON.parse(res.body); } catch (e) { throw new Error('收藏接口返回非 JSON'); }
      if (d.status_code) throw new Error('收藏接口 status_code=' + d.status_code + ' ' + (d.status_msg || ''));
      const list = d.aweme_list || [];
      if (!list.length) {
        if (pages === 0) throw new Error('收藏列表为空（可能需要重新登录）');
        break;
      }
      for (const p of list) {
        const it = mapPost(p);
        if (it.id && !items.some(x => x.id === it.id)) items.push(it);
      }
      hasMore = Boolean(d.has_more);
      cursor = d.cursor || d.max_cursor || 0;
      pages += 1;
    }
    fs.writeFileSync(OUT, JSON.stringify({ kind: 'fav', items }, null, 2));
    console.log(`fav ${items.length} items`);
  } else if (MODE === 'user') {
    await nav(ARG);
    const href = (await evalJs('location.href')) || '';
    const m = href.match(/\/user\/([^/?#]+)/);
    if (!m) throw new Error('没能解析出博主 sec_uid（页面可能不是主页）');
    const sec = m[1];
    let cursor = 0, items = [], pages = 0, hasMore = true;
    while (pages < 15 && hasMore) {
      const d = await evalJs(`(async () => { const r = await fetch('${HOST}/aweme/v1/web/aweme/post/?${CLIENT}&sec_user_id=${sec}&count=18&max_cursor=${cursor}', { credentials: 'include' }); if (!r.ok) return { __status: r.status }; return await r.json(); })()`);
      if (!d || d.__status) throw new Error(`主页作品接口 HTTP ${d && d.__status}`);
      if (d.status_code) throw new Error('主页作品接口 status_code=' + d.status_code + ' ' + (d.status_msg || ''));
      const list = d.aweme_list || [];
      if (!list.length) {
        if (pages === 0) throw new Error('主页作品为空：可能需要登录，或这个博主没有公开作品');
        break;
      }
      for (const p of list) {
        const it = mapPost(p);
        if (it.id && !items.some(x => x.id === it.id)) items.push(it);
      }
      hasMore = Boolean(d.has_more && d.max_cursor);
      cursor = d.max_cursor || 0;
      pages += 1;
    }
    fs.writeFileSync(OUT, JSON.stringify({ kind: 'user', sec_uid: sec, items }, null, 2));
    console.log(`user ${sec.slice(0, 20)} ${items.length} items`);
  } else if (MODE === 'list') {
    const kind = ARG === 'post' ? 'post' : 'like';
    await nav(`${HOST}/user/self?showTab=${kind === 'like' ? 'like' : 'post'}`);
    const me = await evalJs(`(async () => { const r = await fetch('${HOST}/aweme/v1/web/user/profile/self/?${CLIENT}', { credentials: 'include' }); return await r.json().catch(() => null); })()`);
    const sec = me && me.user && me.user.sec_uid;
    if (!sec) throw new Error('需要登录抖音（页面里没有登录态）');
    const endpoint = kind === 'like' ? 'aweme/v1/web/aweme/favorite/' : 'aweme/v1/web/aweme/post/';
    let cursor = 0, items = [], pages = 0, hasMore = true;
    while (pages < 12 && hasMore) {
      const d = await evalJs(`(async () => { const r = await fetch('${HOST}/${endpoint}?${CLIENT}&sec_user_id=${sec}&count=18&max_cursor=${cursor}', { credentials: 'include' }); if (!r.ok) return { __status: r.status }; return await r.json(); })()`);
      if (!d || d.__status) throw new Error(`列表接口 HTTP ${d && d.__status}`);
      if (d.status_code) throw new Error('列表接口 status_code=' + d.status_code + ' ' + (d.status_msg || ''));
      const list = d.aweme_list || [];
      if (!list.length) {
        if (pages === 0) throw new Error('列表为空：可能需要登录，或这个列表对当前账号不可见');
        break;
      }
      for (const p of list) {
        const it = mapPost(p);
        if (it.id && !items.some(x => x.id === it.id)) items.push(it);
      }
      hasMore = Boolean(d.has_more && d.max_cursor);
      cursor = d.max_cursor || 0;
      pages += 1;
    }
    fs.writeFileSync(OUT, JSON.stringify({ kind, sec_uid: sec, items }, null, 2));
    console.log(`list ${kind} ${items.length} items`);
  } else {
    throw new Error('未知模式: ' + MODE);
  }
  cdp.ws.close();
  try { await fetch(`${HTTP}/json/close/${t.id}`); } catch (e) {}
  process.exit(0);
})().catch(e => { console.error('douyin_api failed:', e.message); process.exit(1); });
