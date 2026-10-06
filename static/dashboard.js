'use strict';
const money = value => '$' + Number(value).toFixed(2);
const el = id => document.getElementById(id);
function cell(row, text, css) {
  const td = document.createElement('td');
  td.textContent = text;
  if (css) td.className = css;
  row.appendChild(td);
}
function curve(values) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 600 220');
  svg.setAttribute('role', 'img');
  const title = document.createElementNS(ns, 'title');
  title.textContent = 'Synthetic equity: ' + values.map(money).join(', ');
  svg.appendChild(title);
  const lo = Math.min(...values) - 0.15, hi = Math.max(...values) + 0.15;
  const points = values.map((v, i) => [45 + i * 520 / Math.max(1, values.length - 1), 180 - (v - lo) / (hi - lo) * 150]);
  const line = document.createElementNS(ns, 'polyline');
  line.setAttribute('points', points.map(p => p.join(',')).join(' '));
  line.setAttribute('fill', 'none'); line.setAttribute('stroke', '#72f4ff');
  line.setAttribute('stroke-width', '3'); svg.appendChild(line);
  for (let i = 0; i < points.length; i++) {
    const dot = document.createElementNS(ns, 'circle');
    dot.setAttribute('cx', points[i][0]); dot.setAttribute('cy', points[i][1]);
    dot.setAttribute('r', '4'); dot.setAttribute('fill', '#5ef2c2'); svg.appendChild(dot);
  }
  for (const [value, y] of [[hi, 22], [lo, 198]]) {
    const label = document.createElementNS(ns, 'text');
    label.setAttribute('x', '8'); label.setAttribute('y', y);
    label.setAttribute('fill', '#87a1c3'); label.setAttribute('font-size', '12');
    label.textContent = money(value); svg.appendChild(label);
  }
  el('curve').replaceChildren(svg);
}
async function render() {
  try {
    const response = await fetch('/api/demo');
    if (!response.ok) throw new Error('Replay data could not be loaded.');
    const data = await response.json();
    el('win-rate').textContent = (data.win_rate * 100).toFixed(0) + '%';
    el('win-count').textContent = data.wins + ' wins · ' + data.losses + ' loss';
    el('pnl').textContent = (data.pnl < 0 ? '−' : '+') + money(Math.abs(data.pnl));
    el('pnl').classList.add(data.pnl < 0 ? 'neg' : 'pos');
    el('bankroll').textContent = money(data.bankroll);
    el('unexecuted').textContent = data.events.filter(e => e.status !== 'settled').length;
    for (const message of data.limitations) {
      const li = document.createElement('li'); li.textContent = message;
      el('limitations').appendChild(li);
    }
    for (const event of data.events) {
      const row = document.createElement('tr');
      cell(row, event.question || event.market);
      cell(row, event.status === 'settled' ? (event.pnl > 0 ? 'WIN' : 'LOSS') : event.status.replaceAll('_', ' '));
      cell(row, event.entry_price == null ? '—' : money(event.entry_price));
      cell(row, event.cost == null ? '—' : money(event.cost));
      cell(row, event.payout == null ? '—' : money(event.payout));
      cell(row, event.pnl == null ? '—' : (event.pnl < 0 ? '−' : '+') + money(Math.abs(event.pnl)), event.pnl < 0 ? 'neg' : 'pos');
      el('events').appendChild(row);
    }
    curve(data.curve);
  } catch (error) {
    el('error').textContent = error.message;
    el('error').hidden = false;
  }
}
render();
