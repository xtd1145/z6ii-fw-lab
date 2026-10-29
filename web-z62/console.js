// SPDX-License-Identifier: GPL-2.0-or-later
'use strict';
const $ = id => document.getElementById(id);
const held = new Set();
const shortcuts = {KeyM: 9, Enter: 3, KeyX: 0, ArrowUp: 5, ArrowDown: 4, ArrowLeft: 7, ArrowRight: 6};
const names = ['待机', '菜单', '播放', '拍摄应用', '提示'];
let queue = Promise.resolve(), available = false, running = false, busy = false, lastUrl = null, stopped = false;
function notice(text) { $('notice').textContent = text; }
async function request(path, body = {}) {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body), keepalive: true, signal: AbortSignal.timeout(15000)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '操作失败');
}
function send(path, body) {
  queue = queue.then(() => request(path, body)).catch(error => notice(error.message));
  return queue;
}
function paintKeys() {
  document.querySelectorAll('[data-pin]').forEach(button => {
    button.classList.toggle('held', held.has(Number(button.dataset.pin)));
    button.setAttribute('aria-pressed', String(held.has(Number(button.dataset.pin))));
  });
}
function press(pin, down) {
  if ((down && !available) || held.has(pin) === down) return;
  if (down) held.add(pin); else held.delete(pin);
  paintKeys(); send('/key', {pin, level: down ? 0 : 1});
}
function releaseAll() { held.clear(); paintKeys(); return send('/release', {}); }
document.querySelectorAll('[data-pin]').forEach(button => {
  const pin = Number(button.dataset.pin);
  button.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    event.preventDefault(); button.setPointerCapture(event.pointerId); press(pin, true);
  });
  for (const event of ['pointerup', 'pointercancel', 'lostpointercapture'])
    button.addEventListener(event, () => press(pin, false));
  button.addEventListener('contextmenu', event => event.preventDefault());
  button.addEventListener('keydown', event => {
    if (event.key === ' ' || event.key === 'Enter') { event.preventDefault(); if (!event.repeat) press(pin, true); }
  });
  button.addEventListener('keyup', event => {
    if (event.key === ' ' || event.key === 'Enter') { event.preventDefault(); press(pin, false); }
  });
});
for (const type of ['keydown', 'keyup']) document.addEventListener(type, event => {
  if (event.ctrlKey || event.altKey || event.metaKey) return;
  if (event.code === 'Escape' && type === 'keydown') { releaseAll(); return; }
  if (event.code === 'Enter' && event.target.closest('button')) return;
  if (shortcuts[event.code] !== undefined) {
    event.preventDefault(); if (!event.repeat) press(shortcuts[event.code], type === 'keydown');
  }
});
window.addEventListener('blur', releaseAll);
window.addEventListener('pagehide', () => { if (!stopped) releaseAll(); });
document.addEventListener('visibilitychange', () => { if (document.hidden && !stopped) releaseAll(); });
$('release').onclick = releaseAll;
$('auto-menu').onchange = () => send('/auto-menu', {enabled: $('auto-menu').checked});
$('restart').onclick = async () => {
  available = running = false; held.clear(); paintKeys();
  $('frame').hidden = true; $('placeholder').hidden = false;
  notice('正在重新启动…'); await send('/restart', {}); await status();
};
$('stop').onclick = async () => {
  await releaseAll(); await send('/stop', {}); stopped = true; running = available = false;
  $('connection').textContent = '已关闭'; notice('模拟器已关闭。再次运行启动文件即可重新打开。');
  document.querySelectorAll('button,input').forEach(element => element.disabled = true);
};
async function status() {
  if (stopped) return;
  try {
    const response = await fetch('/status', {signal: AbortSignal.timeout(15000)});
    const state = await response.json(); if (!response.ok) throw new Error(state.error || '读取失败');
    running = state.phase === 'running' && state.execution === 'running';
    available = running && state.input_ready;
    $('connection').textContent = state.error ? '运行异常' : state.index === 1 ? '菜单可操作' : state.menu_pending ? '菜单切换中' : state.main_screen_experiment && state.display_enabled ? '主屏实验' : '启动中';
    $('connection').className = 'pill ' + (state.error ? 'failed' : state.phase);
    $('application').textContent = '前台 · ' + (state.ready ? names[state.index] || '切换中' : '初始化');
    $('uptime').textContent = `已运行 ${state.uptime || 0} 秒 · 相机内 ${state.virtual_seconds || 0} 秒`;
    $('auto-menu').checked = state.auto_menu !== 'off';
    $('restart').disabled = state.phase === 'starting';
    document.querySelectorAll('[data-pin]').forEach(button => button.disabled = !available);
    $('release').disabled = !available;
    if (state.error) notice(state.error);
    else if (state.index === 1) notice('原厂菜单已打开，可以选择项目和切换分类。');
    else if (state.menu_pending) notice('菜单请求已收到，正在等待相机完成切换。请松开按键并继续等待，相机内时钟走得较慢。');
    else if (state.main_screen_experiment) notice(state.display_enabled
      ? (state.sd_card_attached ? '原厂主屏已显示，已连接实验虚拟卡；绿色背景不是实时取景。' : '原厂主屏已显示，当前未插卡；绿色背景不是实时取景。')
      : `主屏实验启动中，已回放 ${state.experimental_boundaries || 0}/2 次外部完成信号，请等待相机内时间推进至约 20 秒。`);
    else if (state.auto_menu === 'armed') notice('正在等待原厂启动完成，随后会自动打开菜单；暂时黑屏属于当前版本的已知状态。');
    else if (available) notice('拍摄主界面尚未完成。可轻按 MENU；如无法进入，请重启模拟器。');
    else notice('正在初始化，请稍候…');
  } catch (error) { notice('连接暂不可用：' + error.message); }
}
async function frame() {
  if (busy || stopped || !running || !$('live').checked || document.hidden) return;
  busy = true;
  try {
    const response = await fetch('/frame', {signal: AbortSignal.timeout(15000)});
    if (!response.ok) return;
    const blob = await response.blob(), url = URL.createObjectURL(blob);
    $('frame').src = url; $('frame').hidden = false; $('placeholder').hidden = true;
    if (lastUrl) URL.revokeObjectURL(lastUrl); lastUrl = url;
    $('frame-state').textContent = '实时画面 · 1024 × 682';
  } finally { busy = false; }
}
async function refresh() { try { await frame(); } catch {} setTimeout(refresh, 250); }
setInterval(() => { if (held.size) send('/heartbeat', {}); }, 1000);
setInterval(status, 1500); status(); refresh();
