/* Screenshot the HMI through Chrome DevTools Protocol. No dependencies: node's global
   WebSocket talks to the browser directly.

     node tools/shot.js metro-tottenham /tmp/hmi-metro.png
     node tools/shot.js metro-tottenham /tmp/hmi-mimic.png '#mimic'

   CHROME=/path/to/chrome if it is not in the usual macОS place. This exists because a mimic
   with twelve bays is not something to eyeball by hand every time, and because a page that
   renders nothing looks exactly like a page whose data has not arrived yet.
*/
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');

const site = process.argv[2] || 'metro-tottenham';
const out = process.argv[3] || '/tmp/hmi.png';
const clipSel = process.argv[4] || null;   // e.g. '#mimic' to shoot one element at full size
const URL = 'http://127.0.0.1:8091';
const PORT = 9222;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const CHROME = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${PORT}`, '--window-size=1500,1050',
  '--no-first-run', '--no-default-browser-check', '--hide-scrollbars',
  '--user-data-dir=/tmp/vecn-shot', URL,
], { stdio: 'ignore' });

function targets() {
  return new Promise((resolve, reject) => {
    http.get(`http://127.0.0.1:${PORT}/json`, (res) => {
      let body = '';
      res.on('data', (c) => { body += c; });
      res.on('end', () => resolve(JSON.parse(body)));
    }).on('error', reject);
  });
}

(async () => {
  let page;
  for (let i = 0; i < 40 && !page; i++) {
    try {
      page = (await targets()).find((t) => t.type === 'page' && t.url.startsWith(URL));
    } catch { /* not up yet */ }
    if (!page) await sleep(250);
  }
  if (!page) { chrome.kill(); throw new Error('no page target'); }

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) pending.get(msg.id)(msg);
  });
  await new Promise((r) => ws.addEventListener('open', r));
  const send = (method, params = {}) => new Promise((resolve) => {
    const n = ++id;
    pending.set(n, resolve);
    ws.send(JSON.stringify({ id: n, method, params }));
  });

  await send('Page.enable');
  await send('Runtime.enable');
  await sleep(4000);                       // first refresh() against Prometheus
  const res = await send('Runtime.evaluate', {
    expression: `openSite(${JSON.stringify(site)}); 'ok'`,
    awaitPromise: true, returnByValue: true,
  });
  if (res.result && res.result.exceptionDetails) {
    console.error(JSON.stringify(res.result.exceptionDetails).slice(0, 400));
  }
  await sleep(5000);                       // trend + one more poll
  let clip;
  if (clipSel) {
    const r = await send('Runtime.evaluate', {
      expression: `(() => { const b = document.querySelector(${JSON.stringify(clipSel)}).getBoundingClientRect();
                   return JSON.stringify({x: b.x, y: b.y, width: b.width, height: b.height}); })()`,
      returnByValue: true,
    });
    const b = JSON.parse(r.result.result.value);
    clip = { ...b, scale: 2 };
  }
  const shot = await send('Page.captureScreenshot',
    clip ? { format: 'png', clip } : { format: 'png', captureBeyondViewport: true });
  fs.writeFileSync(out, Buffer.from(shot.result.data, 'base64'));
  const errs = await send('Runtime.evaluate', {
    expression: `JSON.stringify({pill: document.getElementById('link-state').textContent,
                                 err: window.stateError || null})`,
    returnByValue: true,
  });
  console.log(out, errs.result.result.value);
  ws.close();
  chrome.kill();
  process.exit(0);
})().catch((e) => { console.error(e); chrome.kill(); process.exit(1); });
