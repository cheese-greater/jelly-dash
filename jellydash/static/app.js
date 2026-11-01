'use strict';

const COLORS = {
	bg: '#282a36', bgDeep: '#21222c', current: '#44475a', fg: '#f8f8f2', comment: '#6272a4',
	cyan: '#8be9fd', green: '#50fa7b', orange: '#ffb86c', pink: '#ff79c6', purple: '#bd93f9',
	red: '#ff5555', yellow: '#f1fa8c'
};

const state = { data: null, lastSuccess: 0, failures: 0, intervalMs: 2000, inflight: false, timer: null, clockOffset: 0 };

/* Server time in seconds, using the offset measured at the last poll, so durations survive clock skew. */
function serverNow() {
	return Date.now() / 1000 + state.clockOffset;
}

const byId = (id) => document.getElementById(id);

/* ---------- formatting ---------- */

function esc(value) {
	return String(value ?? '').replace(/[&<>"']/g, (character) => ({
		'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
	})[character]);
}

/* Dotted quad with an optional /prefix or :port; IPv6 entries and bracketed endpoints fail this. */
const IPV4_PATTERN = /^\d{1,3}(\.\d{1,3}){3}(\/\d{1,2})?(:\d{1,5})?$/;

function isIpv4(address) {
	return typeof address === 'string' && IPV4_PATTERN.test(address);
}

function ipv4Only(addresses) {
	return (addresses || []).filter(isIpv4);
}

function fmtBytes(bytes, decimals = 1) {
	if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return '--';
	const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
	let value = Math.max(bytes, 0);
	let index = 0;
	while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
	return `${value.toFixed(index === 0 ? 0 : decimals)} ${units[index]}`;
}

function fmtRate(bytesPerSecond) {
	if (bytesPerSecond === null || bytesPerSecond === undefined) return '--';
	return `${fmtBytes(bytesPerSecond)}/s`;
}

function fmtDuration(seconds) {
	if (seconds === null || seconds === undefined) return '--';
	const total = Math.floor(seconds);
	const days = Math.floor(total / 86400);
	const hours = Math.floor((total % 86400) / 3600);
	const minutes = Math.floor((total % 3600) / 60);
	const secs = total % 60;
	const pad = (number) => String(number).padStart(2, '0');
	if (days > 0) return `${days}d ${pad(hours)}:${pad(minutes)}`;
	return `${pad(hours)}:${pad(minutes)}:${pad(secs)}`;
}

function fmtPct(value, decimals = 0) {
	if (value === null || value === undefined) return '--';
	return `${Number(value).toFixed(decimals)}%`;
}

function fmtNumber(value, decimals = 2, suffix = '') {
	if (value === null || value === undefined) return '--';
	return `${Number(value).toFixed(decimals)}${suffix}`;
}

function fmtAgo(isoString) {
	if (!isoString || isoString.startsWith('0001-')) return 'never';
	const then = Date.parse(isoString);
	if (Number.isNaN(then)) return '--';
	const seconds = Math.max(0, (Date.now() - then) / 1000);
	if (seconds < 90) return 'just now';
	if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
	if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
	return `${Math.round(seconds / 86400)}d ago`;
}

function levelClass(percent, warn = 70, bad = 90) {
	if (percent === null || percent === undefined) return 'c-comment';
	if (percent >= bad) return 'c-red';
	if (percent >= warn) return 'c-yellow';
	return 'c-green';
}

function barHtml(percent, variant = '') {
	const clamped = Math.max(0, Math.min(100, Number(percent) || 0));
	return `<span class="bar ${variant}"><i style="clip-path: inset(0 ${100 - clamped}% 0 0)"></i></span>`;
}

/* A taller bar with text at each end: the filled amount on the left, the remainder on the right. */
function labeledBarHtml(percent, leftText, rightText, variant = '') {
	const clamped = Math.max(0, Math.min(100, Number(percent) || 0));
	return `<span class="bar labeled ${variant}"><i style="clip-path: inset(0 ${100 - clamped}% 0 0)"></i>` +
		`<span class="l">${esc(leftText)}</span><span class="r">${esc(rightText)}</span></span>`;
}

function kvHtml(rows) {
	return rows.filter((row) => row).map(([key, value, extraClass]) =>
		`<span class="k">${esc(key)}</span><span class="v ${extraClass || ''}">${value}</span>`
	).join('');
}

function tag(text, variant = '') {
	return `<span class="tag ${variant}">${esc(text)}</span>`;
}

function statusDot(variant) {
	const symbol = { ok: '●', bad: '●', warn: '●', dim: '○' }[variant] || '○';
	const color = { ok: 'c-green', bad: 'c-red', warn: 'c-yellow', dim: 'c-comment' }[variant] || 'c-comment';
	return `<span class="dot ${color}">${symbol}</span>`;
}

/* ---------- graphs ---------- */

class Graph {
	constructor(canvasId, labelId, options) {
		this.canvas = byId(canvasId);
		this.label = byId(labelId);
		this.context = this.canvas.getContext('2d');
		this.series = options.series;
		this.fixedMax = options.max ?? null;
		this.format = options.format || ((value) => fmtPct(value));
		this.points = options.points || 180;
		this.last = null;
		this.hoverIndex = null;
		this.canvas.addEventListener('mousemove', (event) => this.onHover(event));
		this.canvas.addEventListener('mouseleave', () => { this.hoverIndex = null; this.redraw(); });
		if (window.ResizeObserver) {
			new ResizeObserver(() => this.redraw()).observe(this.canvas.parentElement);
		}
	}

	onHover(event) {
		if (!this.last) return;
		const rect = this.canvas.getBoundingClientRect();
		const step = rect.width / Math.max(this.points - 1, 1);
		const length = this.last.length;
		const index = Math.round((rect.width - (event.clientX - rect.left)) / step);
		this.hoverIndex = Math.max(0, Math.min(length - 1, length - 1 - index));
		this.redraw();
	}

	update(histories, intervalSeconds) {
		this.histories = histories;
		this.last = histories[0] || [];
		this.intervalSeconds = intervalSeconds || 2;
		this.redraw();
	}

	redraw() {
		if (!this.histories) return;
		const canvas = this.canvas;
		const ratio = window.devicePixelRatio || 1;
		const width = canvas.clientWidth;
		const height = canvas.clientHeight;
		if (width === 0 || height === 0) return;
		if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
			canvas.width = Math.round(width * ratio);
			canvas.height = Math.round(height * ratio);
		}
		const context = this.context;
		context.setTransform(ratio, 0, 0, ratio, 0, 0);
		context.clearRect(0, 0, width, height);

		let max = this.fixedMax;
		if (max === null) {
			max = 0;
			for (const history of this.histories) for (const value of history) if (value !== null && value > max) max = value;
			max = max > 0 ? max * 1.15 : 1;
		}

		context.strokeStyle = 'rgba(98, 114, 164, 0.25)';
		context.lineWidth = 1;
		for (const fraction of [0.25, 0.5, 0.75]) {
			const y = Math.round(height - fraction * height) + 0.5;
			context.beginPath(); context.moveTo(0, y); context.lineTo(width, y); context.stroke();
		}

		const step = width / Math.max(this.points - 1, 1);
		const top = 18;
		const usable = height - top - 2;
		const xFor = (index, length) => width - (length - 1 - index) * step;
		const yFor = (value) => height - 2 - Math.max(0, Math.min(1, value / max)) * usable;

		this.histories.forEach((history, seriesIndex) => {
			const color = this.series[seriesIndex].color;
			const length = history.length;
			if (length === 0) return;
			const gradient = context.createLinearGradient(0, top, 0, height);
			gradient.addColorStop(0, hexToRgba(color, 0.55));
			gradient.addColorStop(1, hexToRgba(color, 0.03));
			let segmentStart = null;
			const flush = (end) => {
				if (segmentStart === null) return;
				context.beginPath();
				for (let index = segmentStart; index <= end; index += 1) {
					const x = xFor(index, length);
					const y = yFor(history[index]);
					if (index === segmentStart) context.moveTo(x, y); else context.lineTo(x, y);
				}
				context.lineTo(xFor(end, length), height);
				context.lineTo(xFor(segmentStart, length), height);
				context.closePath();
				context.fillStyle = gradient;
				context.fill();
				context.beginPath();
				for (let index = segmentStart; index <= end; index += 1) {
					const x = xFor(index, length);
					const y = yFor(history[index]);
					if (index === segmentStart) context.moveTo(x, y); else context.lineTo(x, y);
				}
				context.strokeStyle = color;
				context.lineWidth = 1.5;
				context.lineJoin = 'round';
				context.stroke();
				segmentStart = null;
			};
			for (let index = 0; index < length; index += 1) {
				if (history[index] === null || history[index] === undefined) { flush(index - 1); continue; }
				if (segmentStart === null) segmentStart = index;
			}
			flush(length - 1);
		});

		const hover = this.hoverIndex;
		const length = this.last.length;
		if (hover !== null && length > 0) {
			const x = Math.round(xFor(hover, length)) + 0.5;
			context.strokeStyle = COLORS.fg;
			context.lineWidth = 1;
			context.setLineDash([3, 3]);
			context.beginPath(); context.moveTo(x, top); context.lineTo(x, height); context.stroke();
			context.setLineDash([]);
			this.histories.forEach((history, seriesIndex) => {
				const value = history[hover];
				if (value === null || value === undefined) return;
				context.fillStyle = this.series[seriesIndex].color;
				context.beginPath(); context.arc(xFor(hover, length), yFor(value), 3, 0, Math.PI * 2); context.fill();
			});
		}
		this.renderLabel(hover);
	}

	renderLabel(hoverIndex) {
		const parts = this.series.map((series, index) => {
			const history = this.histories[index] || [];
			const sample = hoverIndex === null ? history[history.length - 1] : history[hoverIndex];
			return `<span style="color:${series.color}">${esc(series.label)} <b style="color:${COLORS.fg}">${esc(this.format(sample ?? null))}</b></span>`;
		});
		if (hoverIndex !== null) {
			const secondsAgo = (this.last.length - 1 - hoverIndex) * this.intervalSeconds;
			parts.push(`<span class="muted">${secondsAgo === 0 ? 'now' : `-${fmtDuration(secondsAgo)}`}</span>`);
		} else {
			const span = Math.max(this.last.length - 1, 0) * this.intervalSeconds;
			parts.push(`<span class="muted">${span > 0 ? `last ${fmtDuration(span)}` : ''}</span>`);
		}
		this.label.innerHTML = parts.join('');
	}
}

function hexToRgba(hex, alpha) {
	const value = parseInt(hex.slice(1), 16);
	return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${alpha})`;
}

const graphs = {};

function initGraphs(points) {
	graphs.cpu = new Graph('cpu-graph', 'cpu-graph-label', { series: [{ label: 'cpu', color: COLORS.purple }], max: 100, points });
	graphs.mem = new Graph('mem-graph', 'mem-graph-label', { series: [{ label: 'used', color: COLORS.green }], max: 100, points });
	graphs.net = new Graph('net-graph', 'net-graph-label', {
		series: [{ label: '▼ rx', color: COLORS.cyan }, { label: '▲ tx', color: COLORS.pink }],
		format: fmtRate, points
	});
	graphs.disk = new Graph('disk-graph', 'disk-graph-label', {
		series: [{ label: 'read', color: COLORS.purple }, { label: 'write', color: COLORS.green }],
		format: fmtRate, points
	});
	graphs.power = new Graph('power-graph', 'power-graph-label', { series: [{ label: 'battery', color: COLORS.orange }], max: 100, points });
}

/* ---------- renderers ---------- */

function renderHeader(data) {
	const host = data.host || {};
	byId('hostline').innerHTML = [
		`<b>${esc(host.hostname || '?')}</b>`,
		host.model ? esc(host.model) : null,
		host.os ? esc(host.os) : null,
		host.kernel ? `<em>${esc(host.kernel)}</em>` : null,
		host.uptime_s !== undefined ? `up <b>${fmtDuration(host.uptime_s)}</b>` : null
	].filter(Boolean).join(' <span class="c-current">·</span> ');
}

function renderCpu(data) {
	const cpu = data.cpu || {};
	const temp = cpu.temp_c;
	byId('cpu-right').innerHTML = `<span class="${levelClass(cpu.total, 70, 90)}">${fmtPct(cpu.total)}</span>` +
		(temp !== null && temp !== undefined ? ` <span class="c-comment">│</span> <span class="${levelClass(temp, 65, 80)}">${temp.toFixed(0)}°C</span>` : '');
	graphs.cpu.update([data.history.cpu], data.meta.interval);
	byId('cpu-cores').innerHTML = (cpu.cores || []).map((value, index) =>
		`<div class="core"><span class="label">C${index}</span>${barHtml(value)}<span class="pct ${levelClass(value, 70, 90)}">${fmtPct(value)}</span></div>`
	).join('');
	const freq = cpu.freq_mhz ? `${cpu.freq_mhz} MHz${cpu.freq_max_mhz ? ` <span class="c-comment">/ ${cpu.freq_max_mhz}</span>` : ''}` : '--';
	byId('cpu-kv').innerHTML = kvHtml([
		['freq', freq],
		['temp', temp !== null && temp !== undefined ? `<span class="${levelClass(temp, 65, 80)}">${temp.toFixed(1)} °C</span>` : '--'],
		cpu.fan_rpm !== null && cpu.fan_rpm !== undefined ? ['fan', `${Math.round(cpu.fan_rpm)} rpm`] : null
	]);
}

function meterHtml(label, value, total, variant) {
	const percent = total ? (value / total) * 100 : 0;
	return `<div class="meter"><span class="label">${esc(label)}</span>${barHtml(percent, variant)}<span class="value">${fmtBytes(value)}</span><span class="pct">${fmtPct(percent)}</span></div>`;
}

function renderMem(data) {
	const mem = data.mem || {};
	byId('mem-right').innerHTML = `<span class="${levelClass(mem.percent, 75, 90)}">${fmtPct(mem.percent)}</span> <span class="c-comment">of ${fmtBytes(mem.total, 1)}</span>`;
	graphs.mem.update([data.history.mem], data.meta.interval);
	byId('mem-meters').innerHTML = [
		meterHtml('used', mem.used, mem.total, 'green'),
		meterHtml('cached', mem.cached, mem.total, 'cyan'),
		meterHtml('free', mem.free, mem.total, 'purple'),
		mem.swap_total ? meterHtml('swap', mem.swap_used, mem.swap_total, 'orange') : ''
	].join('');
	byId('mem-kv').innerHTML = kvHtml([
		['available', fmtBytes(mem.available)],
		['buffers', fmtBytes(mem.buffers)],
		['swap', mem.swap_total ? `${fmtBytes(mem.swap_used)} / ${fmtBytes(mem.swap_total)}` : 'none']
	]);
}

function renderPower(data) {
	const power = data.power || {};
	const ups = power.ups || {};
	const psu = power.psu || {};
	const throttled = power.throttled || {};
	const source = power.source || 'unknown';
	const sourceText = { battery: 'ON BATTERY', external: 'EXTERNAL POWER', unknown: 'POWER SOURCE UNKNOWN' }[source];
	const sourceVariant = { battery: 'bad', external: 'ok', unknown: 'warn' }[source];
	const viaNut = ups.available && ups.source === 'nut';
	const sourceNote = ups.available ? `${esc(ups.chip || ups.source || 'ups')}${ups.source === 'nut' ? ' via NUT' : ''}${ups.address ? ' @' + esc(ups.address) : ''}` : esc(ups.detail || 'no UPS detected');
	byId('power-source').className = `source-line ${source}`;
	byId('power-source').innerHTML = `${statusDot(sourceVariant)} ${sourceText}<small>${sourceNote}</small>`;

	const rightParts = [];
	if (psu.input_voltage) rightParts.push(`${psu.input_voltage.toFixed(2)} V`);
	if (psu.total_power_w !== null && psu.total_power_w !== undefined) rightParts.push(`${psu.total_power_w.toFixed(1)} W`);
	if (ups.available && ups.percent !== null && ups.percent !== undefined) rightParts.push(`🔋 ${fmtPct(ups.percent)}`);
	if (ups.available && ups.on_battery && ups.time_to_empty_s) rightParts.push(`${fmtDuration(ups.time_to_empty_s)} left`);
	byId('power-right').textContent = rightParts.join(' │ ') || '--';

	if (ups.available) {
		const percent = ups.percent;
		const statusText = String(ups.status || 'unknown');
		let statusVariant = 'dim';
		if (ups.on_battery === true) statusVariant = 'bad';
		else if (statusText.startsWith('charging')) statusVariant = 'info';
		else if (statusText === 'full' || statusText === 'online') statusVariant = 'ok';
		const meters = [`<div class="meter"><span class="label">battery</span>${barHtml(percent, 'reverse')}<span class="pct ${levelClass(100 - (percent ?? 0), 50, 80)}">${fmtPct(percent)}</span></div>`];
		if (ups.load_pct !== null && ups.load_pct !== undefined) {
			meters.push(`<div class="meter"><span class="label">load</span>${barHtml(ups.load_pct)}<span class="pct ${levelClass(ups.load_pct, 60, 85)}">${fmtPct(ups.load_pct)}</span></div>`);
		}
		const has = (value) => value !== null && value !== undefined;
		const rows = [
			has(ups.time_to_empty_s) ? ['runtime', `<span class="${ups.on_battery ? 'c-orange' : ''}">${fmtDuration(ups.time_to_empty_s)}</span>${ups.runtime_low_s ? ` <span class="c-comment">(shutdown below ${fmtDuration(ups.runtime_low_s)})</span>` : ''}`] : null,
			ups.time_to_full_s ? ['to full', fmtDuration(ups.time_to_full_s)] : null,
			has(ups.power) ? ['draw', `${fmtNumber(ups.power, 0, ' W')}${ups.nominal_power_w ? ` <span class="c-comment">of ${fmtNumber(ups.nominal_power_w, 0, ' W')}</span>` : ''}`] : null,
			has(ups.input_voltage) ? ['mains in', `<span class="${ups.on_battery ? 'c-red' : 'c-green'}">${fmtNumber(ups.input_voltage, 1, ' V')}</span>${ups.transfer_low && ups.transfer_high ? ` <span class="c-comment">(ok ${ups.transfer_low}-${ups.transfer_high} V)</span>` : ''}`] : null,
			has(ups.output_voltage) ? ['output', fmtNumber(ups.output_voltage, 1, ' V')] : null,
			has(ups.voltage) ? ['battery', `${fmtNumber(ups.voltage, 2, ' V')}${ups.voltage_nominal ? ` <span class="c-comment">/ ${fmtNumber(ups.voltage_nominal, 0, ' V')} nominal</span>` : ''}${has(ups.current) ? ` · ${ups.current >= 0 ? '+' : ''}${fmtNumber(ups.current, 2, ' A')}` : ''}`] : null,
			has(ups.rate_pct_per_hour) ? ['rate', fmtNumber(ups.rate_pct_per_hour, 1, ' %/h')] : null,
			ups.test_result ? ['self-test', esc(ups.test_result)] : null,
			ups.beeper ? ['beeper', esc(ups.beeper)] : null
		];
		const warnings = (ups.warnings || []).map((warning) => tag(warning, 'bad')).join(' ');
		byId('power-battery').innerHTML = `
			<div class="meters">${meters.join('')}</div>
			<div style="margin-top:6px;font-size:12px">${tag(statusText, statusVariant)} ${warnings}<span class="c-comment"> ${esc(ups.status_text || '')}${ups.detail ? ` · ${esc(ups.detail)}` : ''}</span></div>
			<div class="kv" style="margin-top:6px">${kvHtml(rows)}</div>`;
		byId('power-graph-wrap').classList.remove('hidden');
		graphs.power.update([data.history.battery], data.meta.interval);
	} else {
		byId('power-battery').innerHTML = '';
		byId('power-graph-wrap').classList.add('hidden');
	}

	const rails = psu.rails || {};
	const maxCurrent = psu.max_current_ma ? `${(psu.max_current_ma / 1000).toFixed(1)} A${psu.max_current_ma >= 5000 ? ' <span class="c-green">(PD 5A)</span>' : ' <span class="c-yellow">(no 5A PD)</span>'}` : null;
	const inputVoltage = psu.input_voltage ? `<span class="${psu.input_voltage < 4.8 ? 'c-red' : 'c-green'}">${psu.input_voltage.toFixed(3)} V</span>` : null;
	const coreVolt = rails.VDD_CORE_V ? `${rails.VDD_CORE_V.toFixed(3)} V · ${(rails.VDD_CORE_A || 0).toFixed(2)} A` : null;
	const boardRows = [
		inputVoltage ? ['input', inputVoltage] : null,
		maxCurrent ? ['psu limit', maxCurrent] : null,
		psu.total_power_w !== null && psu.total_power_w !== undefined ? ['board', `${psu.total_power_w.toFixed(2)} W`] : null
	];
	/* The SoC rail, USB current limit and throttle readouts are Pi board details; a UPS reporting via NUT hides them. */
	if (!viaNut) {
		boardRows.push(
			coreVolt ? ['vdd core', coreVolt] : null,
			psu.usb_max_current_enable !== null && psu.usb_max_current_enable !== undefined ? ['usb limit', psu.usb_max_current_enable ? 'raised (1.6 A)' : 'default (600 mA)'] : null,
			['throttle src', throttled.source ? esc(throttled.source) : `<span class="c-comment">${esc(throttled.error || 'unavailable')}</span>`]
		);
	}
	byId('power-kv').innerHTML = kvHtml(boardRows);

	const flags = throttled.flags || {};
	const flagNames = [['under_voltage', 'under-voltage'], ['throttled', 'throttled'], ['freq_capped', 'freq capped'], ['soft_temp_limit', 'temp limit']];
	byId('power-flags').classList.toggle('hidden', viaNut);
	byId('power-flags').innerHTML = viaNut ? '' : flagNames.map(([key, label]) => {
		const flag = flags[key];
		if (!flag) return `<div class="flag"><span class="k">${label}</span><span class="v c-comment">--</span></div>`;
		const now = flag.now ? '<span class="c-red">● now</span>' : '<span class="c-green">○ now</span>';
		const past = flag.past === null || flag.past === undefined ? '' : (flag.past ? ' <span class="c-yellow">● past</span>' : ' <span class="c-comment">○ past</span>');
		return `<div class="flag"><span class="k">${label}</span><span class="v">${now}${past}</span></div>`;
	}).join('');
}

function kindTag(kind) {
	const variant = { wifi: 'info', ethernet: 'ok', tailscale: 'dim', bridge: 'dim', virtual: 'dim' }[kind] || 'dim';
	return tag(kind, variant);
}

function renderNetwork(data) {
	const network = data.network || {};
	const interfaces = network.interfaces || [];
	const primary = interfaces.find((entry) => entry.name === network.primary) || interfaces[0];
	if (!primary) {
		byId('net-right').textContent = 'no interfaces';
		byId('net-primary').innerHTML = '';
		byId('net-list').innerHTML = '';
		return;
	}
	byId('net-right').innerHTML = `<span class="c-cyan">▼ ${fmtRate(primary.rx_rate)}</span> <span class="c-pink">▲ ${fmtRate(primary.tx_rate)}</span>`;
	graphs.net.update([data.history.net_rx, data.history.net_tx], data.meta.interval);

	let headline = `<div class="row"><span>${statusDot(primary.up ? 'ok' : 'bad')}</span><div class="main"><span class="name">${esc(primary.name)}</span>${kindTag(primary.kind)}`;
	if (primary.kind === 'wifi' && primary.wifi) {
		const wifi = primary.wifi;
		headline += wifi.ssid ? `<span>“${esc(wifi.ssid)}”</span>` : '';
		headline += wifi.freq_mhz ? `<span class="meta">${wifi.freq_mhz >= 5900 ? '6 GHz' : (wifi.freq_mhz >= 4900 ? '5 GHz' : '2.4 GHz')}</span>` : '';
		headline += `</div>`;
		headline += `<div class="usage"><span class="c-comment">signal</span>${barHtml(wifi.quality_pct, 'reverse')}<span class="pct ${levelClass(100 - (wifi.quality_pct ?? 0), 50, 70)}">${wifi.signal_dbm !== null && wifi.signal_dbm !== undefined ? `${wifi.signal_dbm} dBm` : '--'}</span></div>`;
		const bitrates = [wifi.rx_bitrate_mbps ? `rx ${wifi.rx_bitrate_mbps} Mb/s` : null, wifi.tx_bitrate_mbps ? `tx ${wifi.tx_bitrate_mbps} Mb/s` : null].filter(Boolean).join(' · ');
		headline += `<div class="sub">${fmtPct(wifi.quality_pct)} quality${bitrates ? ' · ' + bitrates : ''}</div>`;
	} else {
		headline += primary.speed_mbps ? `<span class="meta">${primary.speed_mbps >= 1000 ? `${primary.speed_mbps / 1000} Gb/s` : `${primary.speed_mbps} Mb/s`} link</span>` : '';
		headline += `</div>`;
	}
	headline += `<div class="sub">${esc(ipv4Only(primary.ips).join('  ') || 'no address')}</div></div>`;
	byId('net-primary').innerHTML = headline;

	byId('net-list').innerHTML = interfaces.filter((entry) => entry.name !== primary.name).map((entry) => `
		<div class="row">
			<span>${statusDot(entry.up ? 'ok' : 'dim')}</span>
			<div class="main"><span class="name">${esc(entry.name)}</span>${kindTag(entry.kind)}
				<span class="right"><span class="c-cyan">▼ ${fmtRate(entry.rx_rate)}</span> <span class="c-pink">▲ ${fmtRate(entry.tx_rate)}</span></span></div>
			<div class="sub">${esc(ipv4Only(entry.ips).join('  ') || (entry.up ? 'no address' : 'down'))}${entry.kind === 'wifi' && entry.wifi && entry.wifi.signal_dbm !== null ? ` · ${entry.wifi.signal_dbm} dBm` : ''}</div>
		</div>`).join('');
}

function renderTailscale(data) {
	const tailscale = data.tailscale || {};
	const viewer = data.viewer || {};
	if (!tailscale.available) {
		byId('ts-right').innerHTML = `<span class="c-red">${esc(tailscale.state || 'unavailable')}</span>`;
		byId('ts-kv').innerHTML = kvHtml([['error', `<span class="c-red">${esc(tailscale.error || 'tailscale status failed')}</span>`]]);
		byId('ts-health').innerHTML = '';
		byId('ts-peers').innerHTML = '';
		byId('ts-viewer').innerHTML = renderViewer(viewer);
		return;
	}
	const running = tailscale.state === 'Running';
	byId('ts-right').innerHTML = `<span class="${running ? 'c-green' : 'c-red'}">${esc(tailscale.state)}</span> <span class="c-comment">│ ${tailscale.peers_online}/${tailscale.peers_total} peers online</span>`;
	byId('ts-kv').innerHTML = kvHtml([
		['node', `<b>${esc(tailscale.dns_name || tailscale.hostname || '--')}</b>`],
		['ips', esc(ipv4Only(tailscale.ips).join('  '))],
		tailscale.tailnet ? ['tailnet', esc(tailscale.tailnet)] : null,
		['version', esc(tailscale.version || '--')]
	]);
	byId('ts-health').innerHTML = (tailscale.health || []).map((message) => `<div class="health">${esc(message)}</div>`).join('');
	byId('ts-viewer').innerHTML = renderViewer(viewer);
	byId('ts-peers').innerHTML = (tailscale.peers || []).map((peer) => {
		const path = peer.online ? (peer.direct ? `<span class="c-green">direct</span> ${esc(isIpv4(peer.cur_addr) ? peer.cur_addr : '')}` : `<span class="c-yellow">relay</span> ${esc(peer.relay || '')}`) : `<span class="c-comment">last seen ${fmtAgo(peer.last_seen)}</span>`;
		return `<div class="row">
			<span>${statusDot(peer.online ? 'ok' : 'dim')}</span>
			<div class="main"><span class="name">${esc(peer.hostname)}</span><span class="meta">${esc(peer.os || '')}</span>${peer.exit_node ? tag('exit node', 'warn') : ''}${peer.exit_node_option && !peer.exit_node ? tag('exit', 'dim') : ''}<span class="right">${path}</span></div>
			<div class="sub">${esc(ipv4Only(peer.ips)[0] || '')}${peer.online && (peer.rx_bytes || peer.tx_bytes) ? ` · ▼ ${fmtBytes(peer.rx_bytes)} ▲ ${fmtBytes(peer.tx_bytes)}` : ''}</div>
		</div>`;
	}).join('');
}

function renderViewer(viewer) {
	if (!viewer || !viewer.ip) return '';
	const who = viewer.name || viewer.login ? `<b>${esc(viewer.name || viewer.login)}</b> ` : '';
	if (viewer.peer) {
		const path = viewer.peer.direct ? `<span class="c-green">direct</span>${isIpv4(viewer.peer.cur_addr) ? ' via ' + esc(viewer.peer.cur_addr) : ''}` : `<span class="c-yellow">relayed</span>${viewer.peer.relay ? ' via DERP ' + esc(viewer.peer.relay) : ''}`;
		return `you: ${who}from <b>${esc(viewer.peer.hostname)}</b> (${esc(viewer.peer.os || '?')}) over tailscale, ${path}`;
	}
	const origin = isIpv4(viewer.ip) ? ` from ${esc(viewer.ip)}` : '';
	if (viewer.via === 'tailscale') return `you: ${who}over tailscale${origin}`;
	return `you: ${who}on the LAN${origin}`;
}

function renderServices(data) {
	const services = data.services || [];
	const upCount = services.filter((service) => service.status === 'up').length;
	byId('services-right').innerHTML = `<span class="${upCount === services.length ? 'c-green' : 'c-red'}">${upCount}/${services.length} up</span>`;
	byId('services-list').innerHTML = services.map((service) => {
		const variant = { up: 'ok', down: 'bad', degraded: 'warn', unknown: 'dim' }[service.status] || 'dim';
		const facts = [
			service.uptime_s !== null && service.uptime_s !== undefined ? `up ${fmtDuration(service.uptime_s)}` : null,
			service.latency_ms !== null && service.latency_ms !== undefined ? `${service.latency_ms} ms` : null,
			service.memory ? fmtBytes(service.memory) : null,
			service.restarts ? `${service.restarts} restarts` : null,
			service.pid ? `pid ${service.pid}` : null
		].filter(Boolean).join(' · ');
		return `<div class="row">
			<span>${statusDot(variant)}</span>
			<div class="main"><span class="name">${esc(service.name)}</span>${tag(service.status, variant)}${service.version ? `<span class="meta">v${esc(service.version)}</span>` : ''}<span class="right meta">${esc(service.runner || '')}</span></div>
			<div class="sub">${esc(facts)}${service.detail ? `${facts ? ' · ' : ''}<b>${esc(service.detail)}</b>` : ''}</div>
		</div>`;
	}).join('');
}

function renderDisks(data) {
	const disks = data.disks || {};
	const mounts = disks.mounts || [];
	const devices = disks.devices || {};
	const read = Object.values(devices).reduce((sum, device) => sum + (device.read_rate || 0), 0);
	const write = Object.values(devices).reduce((sum, device) => sum + (device.write_rate || 0), 0);
	byId('disks-right').innerHTML = `<span class="c-purple">r ${fmtRate(read)}</span> <span class="c-green">w ${fmtRate(write)}</span>`;
	graphs.disk.update([data.history.disk_read, data.history.disk_write], data.meta.interval);

	byId('disks-list').innerHTML = mounts.map((mount) => {
		const device = devices[mount.base_device] || {};
		const smart = device.smart;
		let smartHtml = '';
		if (smart && smart.available) {
			smartHtml = smart.passed === false ? tag('SMART FAIL', 'bad') : tag('SMART ok', 'ok');
			if (smart.warnings && smart.warnings.length) smartHtml += ` <span class="c-yellow">${esc(smart.warnings.join(', '))}</span>`;
		} else if (smart && smart.detail) {
			smartHtml = `<span class="c-comment" title="${esc(smart.detail)}">smart: ${esc(smart.detail)}</span>`;
		}
		const temp = device.temp_c ?? (smart && smart.temp_c);
		const facts = [
			device.model ? esc(device.model) : null,
			device.transport ? `${esc(device.transport)}${device.rotational ? ' hdd' : (device.transport === 'nvme' || device.transport === 'sd card' ? '' : ' ssd')}` : null,
			device.size ? fmtBytes(device.size, 0) : null,
			temp !== null && temp !== undefined ? `<span class="${levelClass(temp, 55, 70)}">${Number(temp).toFixed(0)} °C</span>` : null,
			device.utilization !== null && device.utilization !== undefined ? `busy ${fmtPct(device.utilization)}` : null,
			smart && smart.power_on_hours ? `${Math.round(smart.power_on_hours / 24)} d on` : null,
			smart && smart.percentage_used !== undefined && smart.percentage_used !== null ? `${smart.percentage_used}% worn` : null
		].filter(Boolean).join(' · ');
		const subLine = [facts, smartHtml].filter(Boolean).join(' · ');
		return `<div class="row">
			<span>${statusDot(mount.percent >= 90 ? 'bad' : (mount.percent >= 75 ? 'warn' : 'ok'))}</span>
			<div class="main"><span class="name">${esc(mount.mountpoint)}</span><span class="meta">${esc(mount.device)}</span>${tag(mount.fstype, 'dim')}
				<span class="right"><span class="c-purple">r ${fmtRate(device.read_rate)}</span> <span class="c-green">w ${fmtRate(device.write_rate)}</span></span></div>
			<div class="usage">${labeledBarHtml(mount.percent, `${fmtBytes(mount.used)} used`, `${fmtBytes(mount.free)} free`)}<span class="pct ${levelClass(mount.percent, 75, 90)}">${fmtPct(mount.percent)}</span></div>
			${subLine ? `<div class="sub">${subLine}</div>` : ''}
		</div>`;
	}).join('') || '<div class="c-comment">no mounted block devices</div>';
}

function renderProcesses(data) {
	const processes = data.processes || {};
	byId('proc-right').textContent = `${processes.count ?? '--'} total · top ${(processes.top || []).length} by cpu`;
	byId('proc-table').querySelector('tbody').innerHTML = (processes.top || []).map((process) => `
		<tr>
			<td class="num c-comment">${process.pid}</td>
			<td class="name">${esc(process.name)}</td>
			<td class="user">${esc(process.user)}</td>
			<td class="num c-comment">${process.threads}</td>
			<td class="num">${fmtBytes(process.mem)}</td>
			<td class="num ${levelClass(process.cpu, 25, 60)}">${process.cpu.toFixed(1)}</td>
			<td class="bar-col">${barHtml(process.cpu)}</td>
		</tr>`).join('');
}

function renderErrors(data) {
	const errors = data.errors || {};
	byId('errors').innerHTML = Object.entries(errors).map(([name, message]) =>
		`<div class="error-line"><b>${esc(name)}</b> collector failed: ${esc(String(message).split('\n')[0])}</div>`
	).join('');
	const sampledAgo = data.sampled_at ? Math.max(0, data.meta.now - data.sampled_at) : null;
	byId('footnote').textContent = `jellydash ${data.meta.version} · sampled ${sampledAgo !== null ? sampledAgo.toFixed(1) + 's' : '--'} ago · every ${data.meta.interval}s · history ${data.meta.history_points} points`;
}

/* ---------- actions: run a command on the Pi and stream its output ---------- */

const ANSI_COLORS = [
	'#21222c', '#ff5555', '#50fa7b', '#f1fa8c', '#bd93f9', '#ff79c6', '#8be9fd', '#f8f8f2',
	'#6272a4', '#ff6e6e', '#69ff94', '#ffffa5', '#d6acff', '#ff92df', '#a4ffff', '#ffffff'
];

function ansi256(index) {
	if (index < 16) return ANSI_COLORS[index];
	if (index >= 232) { const grey = 8 + (index - 232) * 10; return `rgb(${grey},${grey},${grey})`; }
	const cube = index - 16;
	const steps = [0, 95, 135, 175, 215, 255];
	return `rgb(${steps[Math.floor(cube / 36)]},${steps[Math.floor(cube / 6) % 6]},${steps[cube % 6]})`;
}

/* A very small terminal: enough of ANSI/VT100 for progress bars and colors. */
class Terminal {
	constructor(element) {
		this.element = element;
		this.lines = [];
		this.current = [];
		this.column = 0;
		this.style = {};
		this.pending = '';
		this.currentElement = null;
		this.follow = true;
		this.clear();
	}

	clear() {
		this.lines = [];
		this.current = [];
		this.column = 0;
		this.style = {};
		this.pending = '';
		this.element.innerHTML = '';
		this.currentElement = document.createElement('div');
		this.currentElement.className = 'line';
		this.element.appendChild(this.currentElement);
		this.renderCurrent(true);
	}

	notice(text) {
		const line = document.createElement('div');
		line.className = 'line notice';
		line.textContent = text;
		this.element.insertBefore(line, this.currentElement);
		this.scroll();
	}

	write(text) {
		let input = this.pending + text;
		this.pending = '';
		let index = 0;
		while (index < input.length) {
			const character = input[index];
			if (character === '\x1b') {
				const consumed = this.escape(input, index);
				if (consumed === 0) { this.pending = input.slice(index); break; }
				index += consumed;
				continue;
			}
			if (character === '\n') { this.newline(); index += 1; continue; }
			if (character === '\r') { this.column = 0; index += 1; continue; }
			if (character === '\b') { this.column = Math.max(0, this.column - 1); index += 1; continue; }
			if (character === '\t') { this.put(' '); while (this.column % 8 !== 0) this.put(' '); index += 1; continue; }
			if (character === '\x07') { index += 1; continue; }
			this.put(character);
			index += 1;
		}
		this.renderCurrent();
		this.scroll();
	}

	put(character) {
		this.current[this.column] = { character, style: this.style };
		this.column += 1;
	}

	newline() {
		this.lines.push(this.current);
		this.renderCurrent(true);
		this.currentElement.classList.remove('active');
		const finished = this.currentElement;
		finished.innerHTML = Terminal.renderCells(this.current);
		this.current = [];
		this.column = 0;
		this.currentElement = document.createElement('div');
		this.currentElement.className = 'line';
		this.element.appendChild(this.currentElement);
		if (this.lines.length > 4000) {
			this.lines.shift();
			if (this.element.firstChild && this.element.firstChild !== this.currentElement) this.element.firstChild.remove();
		}
	}

	escape(input, start) {
		const next = input[start + 1];
		if (next === undefined) return 0;
		if (next === '[') {
			let index = start + 2;
			while (index < input.length && !/[A-Za-z@`]/.test(input[index])) index += 1;
			if (index >= input.length) return 0;
			const parameters = input.slice(start + 2, index);
			const command = input[index];
			if (command === 'm') this.sgr(parameters);
			else if (command === 'K') { const mode = parseInt(parameters || '0', 10); if (mode === 0) this.current.length = this.column; else if (mode === 2) { this.current = []; } }
			else if (command === 'J') { if ((parameters || '0') === '2') { this.current = []; this.column = 0; } }
			else if (command === 'G') { this.column = Math.max(0, parseInt(parameters || '1', 10) - 1); }
			else if (command === 'C') { this.column += parseInt(parameters || '1', 10); }
			else if (command === 'D') { this.column = Math.max(0, this.column - parseInt(parameters || '1', 10)); }
			return index - start + 1;
		}
		if (next === ']') {
			let index = start + 2;
			while (index < input.length && input[index] !== '\x07' && !(input[index] === '\x1b' && input[index + 1] === '\\')) index += 1;
			if (index >= input.length) return 0;
			return (input[index] === '\x07' ? index + 1 : index + 2) - start;
		}
		if (next === '(' || next === ')') return 3;
		return 2;
	}

	sgr(parameters) {
		const codes = (parameters || '0').split(';').map((value) => parseInt(value || '0', 10));
		for (let index = 0; index < codes.length; index += 1) {
			const code = codes[index];
			if (code === 0) this.style = {};
			else if (code === 1) this.style = { ...this.style, bold: true };
			else if (code === 2) this.style = { ...this.style, dim: true };
			else if (code === 3) this.style = { ...this.style, italic: true };
			else if (code === 4) this.style = { ...this.style, underline: true };
			else if (code === 22) this.style = { ...this.style, bold: false, dim: false };
			else if (code === 23) this.style = { ...this.style, italic: false };
			else if (code === 24) this.style = { ...this.style, underline: false };
			else if (code >= 30 && code <= 37) this.style = { ...this.style, fg: ANSI_COLORS[code - 30] };
			else if (code >= 90 && code <= 97) this.style = { ...this.style, fg: ANSI_COLORS[code - 90 + 8] };
			else if (code === 39) this.style = { ...this.style, fg: undefined };
			else if (code >= 40 && code <= 47) this.style = { ...this.style, bg: ANSI_COLORS[code - 40] };
			else if (code >= 100 && code <= 107) this.style = { ...this.style, bg: ANSI_COLORS[code - 100 + 8] };
			else if (code === 49) this.style = { ...this.style, bg: undefined };
			else if (code === 38 || code === 48) {
				const key = code === 38 ? 'fg' : 'bg';
				if (codes[index + 1] === 5) { this.style = { ...this.style, [key]: ansi256(codes[index + 2] || 0) }; index += 2; }
				else if (codes[index + 1] === 2) { this.style = { ...this.style, [key]: `rgb(${codes[index + 2] || 0},${codes[index + 3] || 0},${codes[index + 4] || 0})` }; index += 4; }
			}
		}
	}

	static renderCells(cells) {
		let html = '';
		let open = null;
		let buffer = '';
		const flush = () => {
			if (!buffer) return;
			if (open) {
				const css = [];
				if (open.fg) css.push(`color:${open.fg}`);
				if (open.bg) css.push(`background:${open.bg}`);
				if (open.bold) css.push('font-weight:700');
				if (open.dim) css.push('opacity:0.6');
				if (open.italic) css.push('font-style:italic');
				if (open.underline) css.push('text-decoration:underline');
				html += css.length ? `<span style="${css.join(';')}">${esc(buffer)}</span>` : esc(buffer);
			} else {
				html += esc(buffer);
			}
			buffer = '';
		};
		for (const cell of cells) {
			const style = cell ? cell.style : {};
			if (open !== style) { flush(); open = style; }
			buffer += cell ? cell.character : ' ';
		}
		flush();
		return html;
	}

	renderCurrent(force) {
		if (!this.currentElement) return;
		this.currentElement.innerHTML = Terminal.renderCells(this.current) + (this.running ? '<span class="cursor"></span>' : '');
	}

	scroll() {
		if (this.follow) this.element.scrollTop = this.element.scrollHeight;
	}
}

const actionState = { current: null, source: null, terminal: null, timer: null, run: null, acknowledged: {} };

function renderActions(data) {
	const container = byId('actions');
	const actions = data.actions || [];
	container.innerHTML = actions.map((action) => {
		const run = action.run;
		const running = run && run.running;
		let dot = '';
		if (running) dot = '<span class="dot">●</span> running';
		else if (run && !actionState.acknowledged[action.name]) dot = run.exit_code === 0 ? '<span class="dot c-green">●</span> done' : (run.exit_code < 0 ? '<span class="dot c-yellow">●</span> stopped' : `<span class="dot c-red">●</span> exit ${run.exit_code}`);
		return `<button class="action-button ${running ? 'running' : ''}" type="button" data-action="${esc(action.name)}" title="${esc(action.description)}">▶ ${esc(action.label)}${dot ? ' · ' + dot : ''}</button>`;
	}).join('');
	for (const button of container.querySelectorAll('button')) {
		button.addEventListener('click', () => openAction(button.dataset.action));
	}
	if (actionState.current) {
		const action = actions.find((entry) => entry.name === actionState.current);
		if (action) updateActionStatus(action.run);
	}
}

function findAction(name) {
	return ((state.data && state.data.actions) || []).find((entry) => entry.name === name) || null;
}

function openAction(name) {
	const action = findAction(name);
	if (!action) return;
	const dialog = byId('action-dialog');
	actionState.current = name;
	actionState.acknowledged[name] = true;
	byId('action-title').textContent = action.label;
	byId('action-description').textContent = action.description || '';
	byId('action-command').textContent = action.command || '';
	if (!actionState.terminal) actionState.terminal = new Terminal(byId('action-terminal'));
	const run = action.run;
	if (run) {
		showActionOutput();
		attachActionStream(name, 0);
	} else {
		showActionConfirm();
	}
	if (!dialog.open) dialog.showModal();
}

function showActionConfirm() {
	closeActionStream();
	byId('action-confirm').hidden = false;
	byId('action-terminal').hidden = true;
	byId('action-input-row').hidden = true;
	byId('action-foot').hidden = true;
	byId('action-status').textContent = '';
	byId('action-run').focus();
}

function showActionOutput() {
	byId('action-confirm').hidden = true;
	byId('action-terminal').hidden = false;
	byId('action-foot').hidden = false;
}

async function actionControl(verb, body) {
	const name = actionState.current;
	if (!name) return;
	try {
		const response = await fetch(`api/actions/${encodeURIComponent(name)}/${verb}`, {
			method: 'POST', cache: 'no-store',
			headers: { 'X-Requested-With': 'jellydash', 'Content-Type': 'application/json' },
			body: JSON.stringify(body || {})
		});
		if (!response.ok) {
			const payload = await response.json().catch(() => ({}));
			actionState.terminal.notice(`${verb} failed: ${payload.error || response.status}`);
		}
	} catch (error) {
		actionState.terminal.notice(`${verb} failed: ${error.message}`);
	}
}

function setInputRowVisible(running) {
	byId('action-input-row').hidden = !running;
	if (running && byId('action-dialog').open) byId('action-input').focus();
}

async function startAction(name) {
	const action = findAction(name);
	if (!action) return;
	closeActionStream();
	actionState.terminal.clear();
	showActionOutput();
	byId('action-status').innerHTML = '<span class="c-yellow">starting…</span>';
	try {
		const response = await fetch(`api/actions/${encodeURIComponent(name)}/run`, {
			method: 'POST', headers: { 'X-Requested-With': 'jellydash' }, cache: 'no-store'
		});
		const payload = await response.json().catch(() => ({}));
		if (!response.ok) {
			if (response.status === 409) { attachActionStream(name, 0); return; }
			throw new Error(payload.error || `HTTP ${response.status}`);
		}
		action.run = payload.run;
		attachActionStream(name, 0);
		poll();
	} catch (error) {
		byId('action-status').innerHTML = `<span class="c-red">failed: ${esc(error.message)}</span>`;
		actionState.terminal.notice(`could not start: ${error.message}`);
	}
}

function attachActionStream(name, offset) {
	closeActionStream();
	const terminal = actionState.terminal;
	terminal.clear();
	terminal.running = true;
	const source = new EventSource(`api/actions/${encodeURIComponent(name)}/stream?from=${offset}`);
	actionState.source = source;
	source.addEventListener('output', (event) => { terminal.write(JSON.parse(event.data)); });
	source.addEventListener('exit', (event) => {
		const run = JSON.parse(event.data);
		terminal.running = false;
		terminal.renderCurrent(true);
		updateActionStatus(run);
		const action = findAction(name);
		if (action) action.run = run;
		closeActionStream();
		poll();
	});
	source.onerror = () => {
		if (source.readyState === EventSource.CLOSED) return;
		terminal.notice('connection lost, retrying… (if the update reboots the Pi this is expected)');
	};
	updateActionStatus((findAction(name) || {}).run);
	actionState.timer = setInterval(() => updateActionStatus((findAction(name) || {}).run), 1000);
}

function closeActionStream() {
	if (actionState.source) { actionState.source.close(); actionState.source = null; }
	if (actionState.timer) { clearInterval(actionState.timer); actionState.timer = null; }
}

function updateActionStatus(run) {
	const status = byId('action-status');
	const meta = byId('action-meta');
	const rerun = byId('action-rerun');
	if (!run) { status.textContent = ''; meta.textContent = ''; rerun.hidden = true; setInputRowVisible(false); return; }
	const now = serverNow();
	rerun.hidden = run.running;
	if (byId('action-input-row').hidden === !!run.running) setInputRowVisible(!!run.running);
	if (run.running) {
		status.innerHTML = `<span class="c-yellow">● running</span> <span class="c-comment">${fmtDuration(Math.max(0, now - run.started_at))}</span>`;
		meta.textContent = run.pid ? `pid ${run.pid}` : '';
	} else {
		const ok = run.exit_code === 0;
		const outcome = run.exit_code < 0 ? `stopped (signal ${-run.exit_code})` : `${ok ? 'finished' : 'failed'} (exit ${run.exit_code})`;
		status.innerHTML = `<span class="${ok ? 'c-green' : (run.exit_code < 0 ? 'c-yellow' : 'c-red')}">● ${outcome}</span> <span class="c-comment">in ${fmtDuration((run.finished_at || now) - run.started_at)}</span>`;
		meta.textContent = run.finished_at ? `finished ${new Date(run.finished_at * 1000).toLocaleTimeString()}` : '';
	}
}

function setupActions() {
	const dialog = byId('action-dialog');
	byId('action-run').addEventListener('click', () => startAction(actionState.current));
	byId('action-cancel').addEventListener('click', () => dialog.close());
	byId('action-rerun').addEventListener('click', () => showActionConfirm());
	byId('action-input-row').addEventListener('submit', (event) => {
		event.preventDefault();
		const input = byId('action-input');
		actionControl('input', { text: input.value, newline: true });
		input.value = '';
		input.focus();
	});
	byId('action-interrupt').addEventListener('click', () => actionControl('input', { text: '\u0003', newline: false }));
	byId('action-stop').addEventListener('click', () => actionControl('stop'));
	byId('action-close').addEventListener('click', () => dialog.close());
	byId('action-follow').addEventListener('change', (event) => {
		if (actionState.terminal) { actionState.terminal.follow = event.target.checked; actionState.terminal.scroll(); }
	});
	byId('action-terminal').addEventListener('scroll', () => {
		const element = byId('action-terminal');
		const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 8;
		if (actionState.terminal && !atBottom && actionState.terminal.follow) {
			actionState.terminal.follow = false;
			byId('action-follow').checked = false;
		}
	});
	dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
	dialog.addEventListener('close', () => { closeActionStream(); actionState.current = null; if (state.data) renderActions(state.data); });
}

function render(data) {
	renderHeader(data);
	renderCpu(data);
	renderMem(data);
	renderPower(data);
	renderNetwork(data);
	renderTailscale(data);
	renderServices(data);
	renderDisks(data);
	renderProcesses(data);
	renderErrors(data);
	renderActions(data);
}

/* ---------- polling ---------- */

function setBadge(text, variant, title) {
	const badge = byId('live-badge');
	badge.textContent = text;
	badge.className = `badge ${variant}`;
	badge.title = title || '';
	const reason = byId('badge-reason');
	reason.hidden = variant !== 'bad';
	reason.textContent = variant === 'bad' ? (title || '') : '';
}

async function poll() {
	if (state.inflight) return;
	if (document.visibilityState === 'hidden') return;
	state.inflight = true;
	const controller = new AbortController();
	const timeout = setTimeout(() => controller.abort(), 8000);
	try {
		const response = await fetch('api/status', { cache: 'no-store', signal: controller.signal });
		if (!response.ok) throw new Error(`HTTP ${response.status}`);
		const data = await response.json();
		if (!graphs.cpu) initGraphs(data.meta.history_points || 180);
		state.data = data;
		state.lastSuccess = Date.now();
		if (data.meta && data.meta.now) state.clockOffset = data.meta.now - Date.now() / 1000;
		state.failures = 0;
		state.intervalMs = Math.max(1000, (data.meta.interval || 2) * 1000);
		render(data);
		setBadge('LIVE', 'ok', `updated ${new Date().toLocaleTimeString()}`);
	} catch (error) {
		state.failures += 1;
		const age = state.lastSuccess ? fmtDuration((Date.now() - state.lastSuccess) / 1000) : 'never';
		const reason = error.name === 'AbortError' ? 'request timed out after 8s' : error.message;
		setBadge(state.failures > 2 ? 'DISCONNECTED' : 'STALE', 'bad', `${reason} (${state.failures}x); last update ${age} ago`);
		if (!state.data) byId('hostline').textContent = `cannot reach the server: ${error.message}`;
	} finally {
		clearTimeout(timeout);
		state.inflight = false;
	}
}

function schedule() {
	clearTimeout(state.timer);
	state.timer = setTimeout(async () => { await poll(); schedule(); }, state.intervalMs);
}

/* ---------- installable app ---------- */

function setupInstall() {
	const secure = window.isSecureContext;
	if (secure && 'serviceWorker' in navigator) {
		navigator.serviceWorker.register('sw.js')
			.then(() => { document.documentElement.dataset.serviceWorker = 'registered'; })
			.catch((error) => { document.documentElement.dataset.serviceWorker = `failed: ${error.message}`; });
	} else {
		document.documentElement.dataset.serviceWorker = secure ? 'unsupported' : 'insecure-context';
	}
	const button = byId('install-button');
	let deferredPrompt = null;
	window.addEventListener('beforeinstallprompt', (event) => {
		event.preventDefault();
		deferredPrompt = event;
		button.hidden = false;
	});
	button.addEventListener('click', async () => {
		if (!deferredPrompt) return;
		deferredPrompt.prompt();
		await deferredPrompt.userChoice;
		deferredPrompt = null;
		button.hidden = true;
	});
	window.addEventListener('appinstalled', () => { button.hidden = true; });
}

document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') { poll(); schedule(); } });
setupInstall();
setupActions();
poll().then(schedule);
