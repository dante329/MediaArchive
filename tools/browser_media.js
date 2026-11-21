#!/usr/bin/env node
/**
 * 浏览器兜底：在常驻 Chrome 里打开页面，抓视频/图片直链和元信息（Threads 等 yt-dlp 没有 extractor 的站点）。
 * 用法: node browser_media.js <url> <out.json> [port]
 *
 * 约定同 douyin_api.js：要么写出 out.json 并以 0 退出，要么非 0 退出并说清原因。
 * CDP 的超时/断连处理见 tools/cdp.js。
 */
const fs = require('fs');
const cdp = require('./cdp');

const URL_ARG = process.argv[2];
const OUT = process.argv[3];
const PORT = process.argv[4] || '9222';
const sleep = cdp.sleep;

(async () => {
  if (!URL_ARG || !OUT) throw new Error('usage: browser_media.js <url> <out.json> [port]');

  const result = await cdp.withTab(PORT, async (c) => {
    // 边加载边收集网络里的媒体直链（很多站点把真实地址只放在分片请求里）
    const media = new Map();
    c.ws.addEventListener('message', ev => {
      let m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (!m || m.method !== 'Network.responseReceived') return;
      const r = m.params.response || {};
      const u = r.url || '';
      const mime = (r.mimeType || '').toLowerCase();
      if ((mime.startsWith('video/') || mime.startsWith('audio/') ||
           /\.(mp4|m3u8|webm|mov)(\?|$)/i.test(u)) && !u.startsWith('blob:')) {
        media.set(u, { url: u, mime, kind: mime.startsWith('audio/') ? 'audio' : 'video' });
      }
    });

    await c.send('Page.enable');
    await c.send('Runtime.enable');
    await c.send('Network.enable', { maxTotalBufferSize: 128 * 1024 * 1024 });

    // 打不开页面就直接失败：兜底解析拿不到 DOM 也没有意义
    const href = await cdp.navigate(c, URL_ARG, { tries: 2, settleMs: 2500 });
    await c.evaluate(`(() => { window.scrollTo(0, document.body.scrollHeight); const v=document.querySelector('video'); if(v){v.muted=true; v.play&&v.play().catch(()=>{});} return 1; })()`);
    await sleep(2500);

    const dom = await c.evaluate(`(() => {
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
    if (!dom) throw new Error('页面里没有取到任何 DOM 信息（' + href.slice(0, 120) + '）');

    const title = dom.title || '';
    const handle = (() => {
      try {
        const p = new URL(URL_ARG).pathname;
        // Threads: /@username/post/...
        let m = p.match(/\/@([^/]+)/);
        if (m) return m[1];
        // X/Twitter: /username/status/...
        m = p.match(/^\/([^/?#]+)/);
        if (m && !['status','i','home','explore','search','hashtag','settings','notifications','messages'].includes(m[1])) return m[1];
        return '';
      } catch (e) { return ''; }
    })();
    const videos = [...new Set([...(dom.videos || []), ...media.keys()])];
    const images = [...new Set((dom.images || []).filter(u =>
      /scontent|cdninstagram|fbcdn|twimg|mmbiz\.qpic|mmbiz\.qlogo|cdn/i.test(u) ||
      /\.(jpg|jpeg|png|webp)/i.test(u)))];
    return {
      payload: {
        url: URL_ARG, title, description: dom.description || '', ogImage: dom.ogImage || '',
        ogVideo: dom.ogVideo || '', canonical: dom.canonical || '', handle,
        videos, images, text: dom.text || '',
      },
      log: `browser media: videos=${videos.length} images=${images.length} title=${title.slice(0, 40)}`,
    };
  });

  fs.writeFileSync(OUT, JSON.stringify(result.payload, null, 2));
  console.log(result.log);
  cdp.finish(0);
})().catch(e => {
  console.error('browser_media failed:', (e && e.message) || String(e));
  cdp.finish(1);
});
