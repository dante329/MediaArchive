#!/usr/bin/env node
/* 验证灯箱图层几何恒定：找一条多图的条目，开灯箱切几张，dump 每层 rect */
const cdp = require('./cdp');
const sleep = cdp.sleep;

(async () => {
  await cdp.withTab('9222', async (c) => {
    await cdp.navigate(c, 'http://127.0.0.1:8787/');
    await sleep(1200);
    const found = await c.evaluate(`(async () => {
      const list = await fetch('/api/items?limit=200').then(x => x.json());
      const arr = Array.isArray(list) ? list : (list.items || []);
      for (const it of arr.slice(0, 60)) {
        const d = await fetch('/api/item?id=' + it.id).then(x => x.json());
        const imgs = (d.files || []).filter(f => f.kind === 'image');
        if (imgs.length >= 3) {
          window.__dbgItem = d;
          return { id: d.id, title: (d.title || '').slice(0, 30), n: imgs.length };
        }
      }
      return null;
    })()`);
    console.log('ITEM:', JSON.stringify(found));
    if (!found) return;

    await c.evaluate(`(() => {
      const it = window.__dbgItem;
      const imgs = (it.files || []).filter(f => f.kind === 'image');
      openLightbox(imgs.map(f => ({ img: f.url, vid: '', isLive: false })), 0);
      return true;
    })()`);
    await sleep(700);
    const dump = async (tag) => {
      const d = await c.evaluate(`(() => {
        const r = sel => {
          const el = document.querySelector(sel);
          const b = el.getBoundingClientRect();
          return sel + ' ' + [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)].join(',')
                 + ' hidden=' + el.hidden + ' src=' + (el.getAttribute('src') || '').slice(-24);
        };
        return { idx: window.lbIdx, a: r('#lbimgA'), b: r('#lbimgB'), vw: innerWidth, vh: innerHeight };
      })()`);
      console.log(tag, JSON.stringify(d));
    };
    await dump('slide1');
    for (let i = 0; i < 3; i++) {
      await c.evaluate(`lbGo(1); true`);
      await sleep(500);
      await dump('after-go-' + (i + 1));
    }
  });
  cdp.finish(0);
})().catch(e => { console.error('ERR', e.message); process.exit(1); });
