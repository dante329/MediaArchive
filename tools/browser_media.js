#!/usr/bin/env node
/**
 * 浏览器兜底：在常驻 Chrome 里打开页面，抓视频/图片直链和元信息（Threads 等 yt-dlp 没有 extractor 的站点）。
 * 用法: node browser_media.js <url> <out.json> [port]
 */
const fs = require('fs');
const PORT = process.argv[4] || '9222';
const URL_ARG = process.argv[2];
const OUT = process.argv[3];
const HTTP = `http://127.0.0.1:${PORT}`;
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  if (!URL_ARG || !OUT) throw new Error('usage: browser_media.js <url> <out.json> [port]');
  const t = await (await fetch(`${HTTP}/json/new?about:blank`, { method: 'PUT' })).json();
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  let id = 1;
  const pending = new Map();
  const media = new Map();
  ws.addEventListener('message', ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
    if (m.method === 'Network.responseReceived') {
      const r = m.params.response || {};
      const u = r.url || '';
      const mime = (r.mimeType || '').toLowerCase();
      if ((mime.startsWith('video/') || mime.startsWith('audio/') || /\.(mp4|m3u8|webm|mov)(\?|$)/i.test(u)) && !u.startsWith('blob:')) {
        media.set(u, { url: u, mime, kind: mime.startsWith('audio/') ? 'audio' : 'video' });
      }
    }
  });
  await new Promise(r => ws.addEventListener('open', r));
  const send = (method, params = {}) => new Promise(res => {
    const i = id++;
    pending.set(i, res);
    ws.send(JSON.stringify({ id: i, method, params }));
  });
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Network.enable', { maxTotalBufferSize: 128 * 1024 * 1024 });
  const evalJs = async expr => {
    try {
      const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
      return r.result && r.result.result ? r.result.result.value : null;
    } catch (e) { return null; }
  };
  await send('Page.navigate', { url: URL_ARG });
  for (let i = 0; i < 24; i++) {
    await sleep(500);
    if ((await evalJs('document.readyState')) === 'complete') break;
  }
  await sleep(2500);
  await evalJs(`(() => { window.scrollTo(0, document.body.scrollHeight); const v=document.querySelector('video'); if(v){v.muted=true; v.play&&v.play().catch(()=>{});} return 1; })()`);
  await sleep(2500);

  const dom = await evalJs(`(() => {
    const meta = n => { const e=document.querySelector('meta[property="'+n+'"],meta[name="'+n+'"]'); return e?e.getAttribute('content')||'':''; };
    const vids=[...document.querySelectorAll('video')].map(v=>v.currentSrc||v.src||'').filter(u=>u&&!u.startsWith('blob:'));
    const srcs=[...document.querySelectorAll('video source')].map(v=>v.src||'').filter(Boolean);
    const imgs=[...document.querySelectorAll('img')].map(v=>v.getAttribute('data-src')||v.getAttribute('data-original')||v.currentSrc||v.src||'').filter(u=>/^https?:/.test(u));
    const perf=(performance.getEntriesByType('resource')||[]).map(e=>e.name).filter(u=>/\\.(mp4|m3u8|webm)(\\?|$)/i.test(u));
    return {
      title: (meta('og:title')||document.title||'').trim(),
      description: (meta('og:description')||meta('description')||'').trim(),
      ogImage: meta('og:image'),
      ogVideo: meta('og:video')||meta('og:video:url'),
      canonical: (document.querySelector('link[rel="canonical"]')||{}).href||'',
      videos: [...new Set([...vids,...srcs,...perf])],
      images: imgs.slice(0,40),
      text: (document.body.innerText||'').slice(0,600)
    };
  })()`);
  const title = (dom && dom.title) || '';
  const handle = (() => { try { return (new URL(URL_ARG).pathname.match(/\/@([^/]+)/)||[])[1]||''; } catch(e){ return ''; } })();
  const videos = [...new Set([...(dom&&dom.videos||[]), ...media.keys()])];
  const images = [...new Set((dom&&dom.images||[]).filter(u=>/scontent|cdninstagram|fbcdn|twimg|mmbiz\.qpic|mmbiz\.qlogo|cdn/i.test(u) || /\.(jpg|jpeg|png|webp)/i.test(u)))];
  fs.writeFileSync(OUT, JSON.stringify({
    url: URL_ARG, title, description: dom&&dom.description||'', ogImage: dom&&dom.ogImage||'',
    ogVideo: dom&&dom.ogVideo||'', canonical: dom&&dom.canonical||'', handle,
    videos, images, text: dom&&dom.text||''
  }, null, 2));
  console.log(`browser media: videos=${videos.length} images=${images.length} title=${title.slice(0,40)}`);
  ws.close();
  try { await fetch(`${HTTP}/json/close/${t.id}`); } catch(e) {}
  process.exit(0);
})().catch(e => { console.error('browser_media failed:', e.message); process.exit(1); });
