// Скриншоты для руководства (public/guide/*.png → конвертировать в .webp).
// Headless Chromium через DevTools Protocol, мобильная ширина, демо-данные в отдельном временном профиле браузера.
// Запуск: node tools/guide-shots.mjs http://localhost:8080/ /tmp/liv-shots /tmp/liv-chrome-profile
// Блокировки/исключения не нажимаются — серверные списки не меняются. Продукты, дела и т. п. — только в этом профиле.
import { spawn } from 'node:child_process';
import { writeFileSync, mkdirSync, rmSync } from 'node:fs';

const [,, URL, OUT, PROFILE] = process.argv;
mkdirSync(OUT, { recursive: true });
rmSync(PROFILE, { recursive: true, force: true });
const chrome = spawn('chromium', ['--headless=new', '--disable-gpu', '--no-sandbox', '--remote-debugging-port=9333',
  `--user-data-dir=${PROFILE}`, '--hide-scrollbars', URL], { stdio: 'ignore' });
process.on('exit', () => { try { chrome.kill('SIGKILL'); } catch {} });
process.on('uncaughtException', e => { console.log('FATAL', e.message); process.exit(1); });
process.on('unhandledRejection', e => { console.log('FATAL', e.message); process.exit(1); });
const sleep = ms => new Promise(r => setTimeout(r, ms));

let targets;
for (let i = 0; i < 300; i++) {
  try { targets = await (await fetch('http://127.0.0.1:9333/json/list')).json(); if (targets.some(t => t.type === 'page' && t.url.startsWith(URL))) break; } catch {}
  await sleep(200);
}
const ws = new WebSocket(targets.find(t => t.type === 'page' && t.url.startsWith(URL)).webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener('open', r));
let id = 0; const pending = new Map();
ws.addEventListener('message', e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } });
const cdp = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, m => m.error ? rej(new Error(method + ': ' + m.error.message)) : res(m.result)); ws.send(JSON.stringify({ id: i, method, params })); });
const js = async expr => {
  const r = await cdp('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) console.log('JS ERROR', r.exceptionDetails.exception?.description?.slice(0, 300) || r.exceptionDetails.text);
  return r.result.value;
};
async function until(expr, timeout = 60000) {
  const t = Date.now();
  while (Date.now() - t < timeout) { if (await js(expr)) return; await sleep(300); }
  console.log('STATE', JSON.stringify(await js(`({url:location.href,status:document.getElementById('recipeStatus')?.textContent,slots:document.querySelectorAll('#meals .menu-slot').length,meals:document.getElementById('meals')?.textContent.slice(0,150),pantry:localStorage.getItem('liv_v3_pantry'),dis:document.getElementById('regenerateMeals')?.disabled})`)));
  throw new Error('timeout: ' + expr);
}
async function shot(name, selector, pad = 8) {
  await sleep(400);
  const r = await js(`(()=>{const e=${selector};if(!e)return null;e.scrollIntoView({block:'start'});const b=e.getBoundingClientRect();return {x:b.left+scrollX,y:b.top+scrollY,w:b.width,h:b.height}})()`);
  if (!r) throw new Error('no element for ' + name);
  await sleep(300);
  const clip = { x: Math.max(0, r.x - pad), y: Math.max(0, r.y - pad), width: r.w + pad * 2, height: r.h + pad * 2, scale: 1 };
  const { data } = await cdp('Page.captureScreenshot', { format: 'png', clip, captureBeyondViewport: true });
  writeFileSync(`${OUT}/${name}.png`, Buffer.from(data, 'base64'));
  console.log('✓', name, Math.round(clip.width) + 'x' + Math.round(clip.height));
}
const menuReady = `document.querySelectorAll('#meals .menu-slot').length===4 && !document.getElementById('regenerateMeals').disabled`;
const slot = n => `[...document.querySelectorAll('#meals .menu-slot')].find(s=>s.querySelector('h3').textContent.includes('${n}'))`;

await cdp('Page.enable'); await cdp('Runtime.enable');
await cdp('Emulation.setDeviceMetricsOverride', { width: 420, height: 900, deviceScaleFactor: 2, mobile: true });
await until(`location.protocol.startsWith('http') && document.readyState==='complete'`, 60000);

// Демо-данные — только в этом профиле браузера
const day = d => { const x = new Date(); x.setDate(x.getDate() + d); return x.getFullYear() + '-' + String(x.getMonth() + 1).padStart(2, '0') + '-' + String(x.getDate()).padStart(2, '0'); };
const seed = {
  pantry: ['Курица', 'Рис', 'Гречка', 'Макароны', 'Картофель', 'Молоко', 'Сливочное масло', 'Огурцы', 'Хлебцы', 'Джем', 'Арахисовая паста', 'Мороженое', 'Клубника', 'Кетчуп'],
  shopping: ['Сыр', 'Сметана'], mealMode: 'home', allowAlmost: true, menuRound: 0, foodDiary: {},
};
const tasks = { version: 1, deleted: [], rewards: ['t2'], tasks: [
  { id: 't1', text: 'Купить продукты на неделю', done: false, date: day(0) },
  { id: 't2', text: 'Записаться на маникюр', done: true, date: null },
  { id: 't3', text: 'Тренировка в зале', done: false, date: day(2) }] };
await js(`(()=>{const s=${JSON.stringify(seed)};for(const k in s)localStorage.setItem('liv_v3_'+k,JSON.stringify(s[k]));
  localStorage.setItem('liv_v3_tasks',${JSON.stringify(JSON.stringify(tasks))});
  localStorage.setItem('liv_v3_workouts',${JSON.stringify(JSON.stringify([day(-6), day(-4), day(-1)]))});
  localStorage.setItem('liv_v3_calendar_notes',${JSON.stringify(JSON.stringify({ [day(0)]: 'Попробовать новый рецепт ✧' }))});
  localStorage.setItem('liv_v3_stars','2');return true})()`);
await js(`setTimeout(()=>location.reload(),50);true`);
await sleep(1500);
await until(`location.protocol.startsWith('http') && document.readyState==='complete'`, 60000);
await until(menuReady, 40000);
await js(`document.head.insertAdjacentHTML('beforeend','<style id="shotcss">.nav{display:none!important}</style>')`);

// 1. Меню целиком
await shot('menu', `document.querySelector('#food .glass')`);
// Ужин с гарниром: листаем ↻, пока не появится гарнир
for (let i = 0; i < 8 && !(await js(`!!${slot('Ужин')}.querySelector('.side-label')`)); i++) {
  await js(`${slot('Ужин')}.querySelector('.reroll').click()`); await sleep(500); await until(menuReady);
}
await shot('dinner', slot('Ужин'));
// Блюдо с пометкой «докупить»
for (let i = 0; i < 10 && !(await js(`!!${slot('Обед')}.querySelector('.buy-note')`)); i++) {
  await js(`${slot('Обед')}.querySelector('.reroll').click()`); await sleep(500); await until(menuReady);
}
await shot('almost', slot('Обед'));
// Раскрытая карточка
await js(`${slot('Ужин')}.querySelector('details').open=true`);
await shot('recipe', `${slot('Ужин')}.querySelector('.dish-row')`);
await js(`${slot('Ужин')}.querySelector('details').open=false`);
// Полдник-перекус
for (let i = 0; i < 6 && !(await js(`/\\+/.test(${slot('Полдник')}.querySelector('strong').textContent)`)); i++) {
  await js(`${slot('Полдник')}.querySelector('.reroll').click()`); await sleep(500); await until(menuReady);
}
await js(`${slot('Полдник')}.querySelector('details').open=true`);
await shot('snack', slot('Полдник'));
await js(`${slot('Полдник')}.querySelector('details').open=false`);
// Идеи
await js(`(()=>{const d=document.querySelector('#meals>details');if(d)d.open=true})()`);
await shot('ideas', `document.querySelector('#meals>details')`).catch(e => console.log('ideas skipped', e.message));
await js(`(()=>{const d=document.querySelector('#meals>details');if(d)d.open=false})()`);
// Продукты, покупки, исключения
await shot('pantry', `document.querySelectorAll('#food .glass')[1]`);
await shot('shopping', `document.querySelectorAll('#food .glass')[2]`);
await js(`document.querySelector('#excludeFood').closest('.glass').querySelector('details').open=true`);
await shot('exclude', `document.querySelector('#excludeFood').closest('.glass')`);
// Дневник: отмечаем съеденное
await js(`${slot('Завтрак')}.querySelector('.eat').click()`);
await js(`${slot('Обед')}.querySelector('.eat').click()`);
await shot('eaten', `document.querySelector('#eaten').closest('.glass')`);
// Режим «Готова докупить»
await js(`(()=>{const s=document.getElementById('mealMode');s.value='shop';s.dispatchEvent(new Event('change'));return true})()`);
await sleep(800); await until(menuReady, 90000);
await js(`(()=>{const s=document.getElementById('mealMode');s.value='home';s.dispatchEvent(new Event('change'));return true})()`);
await sleep(800); await until(menuReady, 90000);
// Сохраняем пару рецептов в коллекцию
await js(`${slot('Завтрак')}.querySelector('.fav').click();${slot('Обед')}.querySelector('.fav').click();[...${slot('Ужин')}.querySelectorAll('.fav')].forEach(b=>b.click());true`);
await shot('saved', slot('Ужин'));
// Остальные разделы
const tab = p => js(`document.querySelector('#nav button[data-page="${p}"]').click()`);
await tab('tasks'); await shot('tasks', `document.getElementById('tasks')`);
await tab('calendar'); await shot('calendar', `document.getElementById('calendar')`);
await tab('stars'); await shot('stars', `document.querySelectorAll('#stars .glass')[0]`);
await js(`document.getElementById('savedToggle').click();true`);
await js(`[...document.querySelectorAll('#savedRecipes .saved-group')].filter(d=>/Ужин/.test(d.querySelector('summary').textContent)).forEach(d=>d.open=true);true`);
await shot('collection', `document.querySelectorAll('#stars .glass')[1]`);
await tab('settings'); await shot('settings', `document.getElementById('settings')`);
await tab('food');
// Нижняя панель и шапка
await js(`document.getElementById('shotcss').remove();scrollTo(0,0)`);
await shot('header', `document.querySelector('header.top')`, 6);
// Нижняя панель полупрозрачная — снимаем её без содержимого страницы под ней
await js(`document.querySelector('.wrap').style.visibility='hidden';scrollTo(0,0);true`);
await shot('nav', `document.getElementById('nav')`, 4);

ws.close(); chrome.kill();
