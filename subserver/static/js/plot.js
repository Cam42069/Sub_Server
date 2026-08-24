/* Sub_Server plotting engine.
 *
 * A small canvas renderer, deliberately dependency-free: the server is meant to
 * run on a local network that may have no route to the internet, so pulling a
 * charting library off a CDN would leave every page broken.
 *
 * Supported series kinds: line, scatter, step, area, bar.
 */
'use strict';

const PlotTheme = {
  background: '#252525',
  grid: '#3a3a3a',
  gridMinor: '#303030',
  axis: '#5c5c5c',
  text: '#b4b4b4',
  textStrong: '#f0f0f0',
  crosshair: '#6a6a6a',
  font: '11px "Segoe UI", Roboto, system-ui, sans-serif',
  labelFont: '12px "Segoe UI", Roboto, system-ui, sans-serif'
};

const PAD = { top: 12, right: 16, bottom: 30, left: 62 };
const RIGHT_AXIS_PAD = 58;  // room for a second set of tick labels
const MAX_CLIENT_POINTS = 20000;

/* ---------- axis helpers ---------- */

/** A "nice" round number at or above `range / count`. */
function niceStep(range, count) {
  if (!(range > 0)) return 1;
  const rough = range / Math.max(1, count);
  const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
  const normalised = rough / magnitude;
  let step;
  if (normalised <= 1) step = 1;
  else if (normalised <= 2) step = 2;
  else if (normalised <= 2.5) step = 2.5;
  else if (normalised <= 5) step = 5;
  else step = 10;
  return step * magnitude;
}

function linearTicks(min, max, count) {
  const step = niceStep(max - min, count);
  const first = Math.ceil(min / step) * step;
  const ticks = [];
  for (let value = first; value <= max + step * 1e-9; value += step) {
    ticks.push(Math.abs(value) < step * 1e-9 ? 0 : value);
  }
  return ticks;
}

/* Time steps that read naturally on a clock: seconds, minutes, hours, days. */
const TIME_STEPS = [
  1, 2, 5, 10, 15, 30,
  60, 120, 300, 600, 900, 1800,
  3600, 7200, 10800, 21600, 43200,
  86400, 172800, 604800, 2592000
];

function timeTicks(minSec, maxSec, count) {
  const span = maxSec - minSec;
  if (!(span > 0)) return [minSec];
  const rough = span / Math.max(1, count);
  let step = TIME_STEPS[TIME_STEPS.length - 1];
  for (const candidate of TIME_STEPS) {
    if (candidate >= rough) { step = candidate; break; }
  }
  const ticks = [];
  if (step >= 86400) {
    // Align on local midnight so day labels land on real day boundaries.
    const start = new Date(minSec * 1000);
    start.setHours(0, 0, 0, 0);
    const days = step / 86400;
    for (let t = start.getTime() / 1000; t <= maxSec; t += days * 86400) {
      if (t >= minSec) ticks.push(t);
    }
  } else {
    // Align on the local hour, so :00/:15/:30 fall where a clock would put them.
    const anchor = new Date(minSec * 1000);
    anchor.setMinutes(0, 0, 0);
    let t = anchor.getTime() / 1000;
    while (t < minSec) t += step;
    for (; t <= maxSec; t += step) ticks.push(t);
  }
  return ticks.length ? ticks : [minSec, maxSec];
}

function pad2(value) { return value < 10 ? '0' + value : String(value); }

function formatTime(seconds, span) {
  const d = new Date(seconds * 1000);
  if (span >= 3 * 86400) return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  if (span >= 86400) return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  if (span >= 600) return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

function formatFullTime(seconds) {
  const d = new Date(seconds * 1000);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
         `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

/** Format an axis value with only as many decimals as the tick step needs. */
function formatNumber(value, step) {
  if (value === 0) return '0';
  const magnitude = Math.abs(value);
  if (magnitude >= 1e6 || magnitude < 1e-4) return value.toExponential(2);
  const decimals = Math.max(0, Math.min(6, Math.ceil(-Math.log10(step || 1)) + 1));
  const text = value.toFixed(decimals);
  // Trim trailing zeros only in the fractional part -- stripping them from an
  // integer would turn 540 into 54.
  return text.indexOf('.') < 0 ? text : text.replace(/0+$/, '').replace(/\.$/, '');
}

/** Index of the last element of `values` that is <= `target`. */
function lowerBound(values, target) {
  let lo = 0;
  let hi = values.length - 1;
  if (hi < 0) return -1;
  if (target <= values[0]) return 0;
  if (target >= values[hi]) return hi;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (values[mid] <= target) lo = mid; else hi = mid - 1;
  }
  return lo;
}

/* ---------- the plot ---------- */

class Plot {
  constructor(canvas, tooltipElement) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.tooltip = tooltipElement || null;
    this.series = [];
    this.lines = [];
    this.spec = { showGrid: true, yLabel: '', yMin: null, yMax: null };
    this.xRange = null;
    this.hover = null;
    this.emptyMessage = 'Waiting for data…';
    this._overflow = false;

    this._onMove = (event) => this._handleMove(event);
    this._onLeave = () => { this.hover = null; this._hideTooltip(); this.draw(); };
    canvas.addEventListener('mousemove', this._onMove);
    canvas.addEventListener('mouseleave', this._onLeave);

    this._resizeObserver = new ResizeObserver(() => this.draw());
    this._resizeObserver.observe(canvas.parentElement || canvas);
  }

  destroy() {
    this.canvas.removeEventListener('mousemove', this._onMove);
    this.canvas.removeEventListener('mouseleave', this._onLeave);
    if (this._resizeObserver) this._resizeObserver.disconnect();
  }

  /** True once a live series has grown large enough to be worth re-decimating. */
  needsRefresh() { return this._overflow; }

  setSpec(spec) {
    this.spec = Object.assign({ showGrid: true, yLabel: '', yMin: null, yMax: null }, spec || {});
  }

  setSeries(list) {
    this._overflow = false;
    this.series = (list || []).map((s) => ({
      label: s.label,
      color: s.color || '#4fc3f7',
      kind: s.kind || 'line',
      width: s.width == null ? 1.8 : s.width,
      pointSize: s.point_size == null ? 2.5 : s.point_size,
      axis: s.y_axis || 'left',
      x: (s.x || []).slice(),
      y: (s.y || []).slice()
    }));
  }

  /** Append newly-arrived points to the matching existing series. */
  appendSeries(list) {
    (list || []).forEach((incoming) => {
      const target = this.series.find((s) => s.label === incoming.label);
      if (!target) return;
      const xs = incoming.x || [];
      const ys = incoming.y || [];
      for (let i = 0; i < xs.length; i += 1) {
        target.x.push(xs[i]);
        target.y.push(ys[i]);
      }
      if (target.x.length > MAX_CLIENT_POINTS) this._overflow = true;
    });
  }

  /** Drop points older than `startSeconds`; live windows otherwise grow forever. */
  trimBefore(startSeconds) {
    this.series.forEach((s) => {
      if (!s.x.length || s.x[0] >= startSeconds) return;
      const cut = lowerBound(s.x, startSeconds);
      if (cut > 0) { s.x = s.x.slice(cut); s.y = s.y.slice(cut); }
    });
  }

  setLines(lines) { this.lines = (lines || []).slice(); }
  setXRange(from, to) { this.xRange = (from == null || to == null) ? null : [from, to]; }

  /* ---------- ranges ---------- */
  _xExtent() {
    if (this.xRange) return this.xRange;
    let min = Infinity;
    let max = -Infinity;
    this.series.forEach((s) => {
      if (!s.x.length) return;
      if (s.x[0] < min) min = s.x[0];
      if (s.x[s.x.length - 1] > max) max = s.x[s.x.length - 1];
    });
    if (!isFinite(min) || !isFinite(max)) return null;
    if (min === max) { min -= 1; max += 1; }
    return [min, max];
  }

  hasRightAxis() { return this.series.some((s) => s.axis === 'right' && s.x.length); }

  /** Value range for one axis. Explicit y_min/y_max apply to the left axis. */
  _yExtent(xFrom, xTo, axis) {
    let min = Infinity;
    let max = -Infinity;
    this.series.forEach((s) => {
      if ((s.axis || 'left') !== axis) return;
      for (let i = 0; i < s.y.length; i += 1) {
        if (s.x[i] < xFrom || s.x[i] > xTo) continue;
        const value = s.y[i];
        if (value < min) min = value;
        if (value > max) max = value;
      }
    });
    if (axis === 'left') {
      this.lines.forEach((line) => {
        if (line.value < min) min = line.value;
        if (line.value > max) max = line.value;
      });
      if (this.spec.yMin != null) min = this.spec.yMin;
      if (this.spec.yMax != null) max = this.spec.yMax;
    }
    if (!isFinite(min) || !isFinite(max)) return null;
    if (min === max) {
      const bump = Math.abs(min) > 1 ? Math.abs(min) * 0.05 : 1;
      min -= bump; max += bump;
    } else if (axis === 'right' || (this.spec.yMin == null && this.spec.yMax == null)) {
      const headroom = (max - min) * 0.06;   // keep traces off the frame edges
      min -= headroom; max += headroom;
    }
    return [min, max];
  }

  /* ---------- drawing ---------- */
  draw() {
    const canvas = this.canvas;
    const parent = canvas.parentElement;
    const cssWidth = Math.max(80, (parent ? parent.clientWidth : canvas.clientWidth) || 300);
    const cssHeight = Math.max(80, (parent ? parent.clientHeight : canvas.clientHeight) || 200);
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(cssWidth * dpr) || canvas.height !== Math.round(cssHeight * dpr)) {
      canvas.width = Math.round(cssWidth * dpr);
      canvas.height = Math.round(cssHeight * dpr);
    }
    const ctx = this.ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssWidth, cssHeight);
    ctx.fillStyle = PlotTheme.background;
    ctx.fillRect(0, 0, cssWidth, cssHeight);

    const rightAxis = this.hasRightAxis();
    const left = PAD.left;
    const top = PAD.top;
    const width = cssWidth - PAD.left - (rightAxis ? RIGHT_AXIS_PAD : PAD.right);
    const height = cssHeight - PAD.top - PAD.bottom;
    if (width <= 10 || height <= 10) return;

    const xExtent = this._xExtent();
    const hasPoints = this.series.some((s) => s.x.length > 0);
    if (!xExtent || (!hasPoints && !this.lines.length)) {
      ctx.fillStyle = PlotTheme.text;
      ctx.font = PlotTheme.labelFont;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(this.emptyMessage, cssWidth / 2, cssHeight / 2);
      return;
    }
    const yExtent = this._yExtent(xExtent[0], xExtent[1], 'left') || [0, 1];
    const yExtentRight = rightAxis ? (this._yExtent(xExtent[0], xExtent[1], 'right') || [0, 1]) : null;

    const xScale = (value) => left + ((value - xExtent[0]) / (xExtent[1] - xExtent[0])) * width;
    const scaleFor = (extent) => (value) =>
      top + height - ((value - extent[0]) / (extent[1] - extent[0])) * height;
    const yScale = scaleFor(yExtent);
    const yScaleRight = yExtentRight ? scaleFor(yExtentRight) : yScale;
    const scaleOf = (s) => ((s.axis || 'left') === 'right' ? yScaleRight : yScale);
    const extentOf = (s) => ((s.axis || 'left') === 'right' ? yExtentRight : yExtent);
    this._geometry = {
      left, top, width, height, xExtent, yExtent, yExtentRight,
      xScale, yScale, yScaleRight, scaleOf
    };

    this._drawGrid(ctx, xExtent, yExtent, yExtentRight);
    ctx.save();
    ctx.beginPath();
    ctx.rect(left, top, width, height);
    ctx.clip();
    this.series.forEach((s) => this._drawSeries(ctx, s, xScale, scaleOf(s), extentOf(s)));
    this.lines.forEach((line) => this._drawReferenceLine(ctx, line, xScale, yScale));
    if (this.hover) this._drawCrosshair(ctx);
    ctx.restore();

    ctx.strokeStyle = PlotTheme.axis;
    ctx.lineWidth = 1;
    ctx.strokeRect(left + 0.5, top + 0.5, width - 1, height - 1);
  }

  _drawGrid(ctx, xExtent, yExtent, yExtentRight) {
    const { left, top, width, height, xScale, yScale, yScaleRight } = this._geometry;
    const span = xExtent[1] - xExtent[0];
    const xTickCount = Math.max(2, Math.min(9, Math.floor(width / 90)));
    const yTickCount = Math.max(2, Math.min(8, Math.floor(height / 42)));
    const xt = timeTicks(xExtent[0], xExtent[1], xTickCount);
    const yStep = niceStep(yExtent[1] - yExtent[0], yTickCount);
    const yt = linearTicks(yExtent[0], yExtent[1], yTickCount);

    ctx.font = PlotTheme.font;
    ctx.fillStyle = PlotTheme.text;
    ctx.lineWidth = 1;

    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    xt.forEach((tick) => {
      const x = Math.round(xScale(tick)) + 0.5;
      if (x < left || x > left + width) return;
      if (this.spec.showGrid) {
        ctx.strokeStyle = PlotTheme.grid;
        ctx.beginPath();
        ctx.moveTo(x, top);
        ctx.lineTo(x, top + height);
        ctx.stroke();
      }
      ctx.fillText(formatTime(tick, span), x, top + height + 6);
    });

    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    yt.forEach((tick) => {
      const y = Math.round(yScale(tick)) + 0.5;
      if (y < top || y > top + height) return;
      if (this.spec.showGrid) {
        ctx.strokeStyle = PlotTheme.grid;
        ctx.beginPath();
        ctx.moveTo(left, y);
        ctx.lineTo(left + width, y);
        ctx.stroke();
      }
      ctx.fillText(formatNumber(tick, yStep), left - 7, y);
    });

    if (yExtentRight) {
      const rightStep = niceStep(yExtentRight[1] - yExtentRight[0], yTickCount);
      ctx.textAlign = 'left';
      ctx.fillStyle = PlotTheme.text;
      linearTicks(yExtentRight[0], yExtentRight[1], yTickCount).forEach((tick) => {
        const y = Math.round(yScaleRight(tick)) + 0.5;
        if (y < top || y > top + height) return;
        ctx.fillText(formatNumber(tick, rightStep), left + width + 7, y);
      });
    }

    if (this.spec.yLabel) {
      ctx.save();
      ctx.translate(12, top + height / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = PlotTheme.textStrong;
      ctx.font = PlotTheme.labelFont;
      ctx.fillText(this.spec.yLabel, 0, 0);
      ctx.restore();
    }
  }

  _drawSeries(ctx, s, xScale, yScale, yExtent) {
    if (!s.x.length) return;
    ctx.strokeStyle = s.color;
    ctx.fillStyle = s.color;
    ctx.lineWidth = s.width;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';

    if (s.kind === 'scatter') {
      const radius = Math.max(0.8, s.pointSize);
      for (let i = 0; i < s.x.length; i += 1) {
        ctx.beginPath();
        ctx.arc(xScale(s.x[i]), yScale(s.y[i]), radius, 0, Math.PI * 2);
        ctx.fill();
      }
      return;
    }

    if (s.kind === 'bar') {
      // Bars sit on the axis baseline (or the visible floor if 0 is off-screen).
      const baseline = yScale(Math.max(yExtent[0], Math.min(0, yExtent[1])));
      const barWidth = Math.max(1, Math.min(14, (this._geometry.width / s.x.length) * 0.7));
      for (let i = 0; i < s.x.length; i += 1) {
        const x = xScale(s.x[i]);
        const y = yScale(s.y[i]);
        ctx.fillRect(x - barWidth / 2, Math.min(y, baseline), barWidth, Math.abs(baseline - y));
      }
      return;
    }

    ctx.beginPath();
    for (let i = 0; i < s.x.length; i += 1) {
      const x = xScale(s.x[i]);
      const y = yScale(s.y[i]);
      if (i === 0) ctx.moveTo(x, y);
      else if (s.kind === 'step') { ctx.lineTo(x, yScale(s.y[i - 1])); ctx.lineTo(x, y); }
      else ctx.lineTo(x, y);
    }

    if (s.kind === 'area') {
      const baseline = yScale(Math.max(yExtent[0], Math.min(0, yExtent[1])));
      ctx.save();
      ctx.lineTo(xScale(s.x[s.x.length - 1]), baseline);
      ctx.lineTo(xScale(s.x[0]), baseline);
      ctx.closePath();
      ctx.globalAlpha = 0.22;
      ctx.fill();
      ctx.restore();
      ctx.beginPath();
      for (let i = 0; i < s.x.length; i += 1) {
        const x = xScale(s.x[i]);
        const y = yScale(s.y[i]);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
    }
    ctx.stroke();

    if (s.pointSize > 0 && s.x.length <= 400 && s.kind !== 'area') {
      for (let i = 0; i < s.x.length; i += 1) {
        ctx.beginPath();
        ctx.arc(xScale(s.x[i]), yScale(s.y[i]), Math.max(1, s.pointSize * 0.7), 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }

  _drawReferenceLine(ctx, line, xScale, yScale) {
    const { left, width } = this._geometry;
    const y = yScale(line.value);
    ctx.save();
    ctx.strokeStyle = line.color || '#ffb74d';
    ctx.lineWidth = 1.4;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(left + width, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = line.color || '#ffb74d';
    ctx.font = PlotTheme.font;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'bottom';
    ctx.fillText(`${line.label} ${formatNumber(line.value, Math.abs(line.value) / 100 || 0.01)}`,
                 left + width - 4, y - 3);
    ctx.restore();
  }

  _drawCrosshair(ctx) {
    const { top, height, xScale } = this._geometry;
    const x = Math.round(xScale(this.hover.time)) + 0.5;
    ctx.save();
    ctx.strokeStyle = PlotTheme.crosshair;
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, top + height);
    ctx.stroke();
    ctx.restore();
    this.hover.points.forEach((point) => {
      const scale = point.axis === 'right' ? this._geometry.yScaleRight : this._geometry.yScale;
      ctx.beginPath();
      ctx.fillStyle = point.color;
      ctx.arc(xScale(point.time), scale(point.value), 3.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = '#1a1a1a';
      ctx.lineWidth = 1;
      ctx.stroke();
    });
  }

  /* ---------- interaction ---------- */
  _handleMove(event) {
    if (!this._geometry) return;
    const rect = this.canvas.getBoundingClientRect();
    const px = event.clientX - rect.left;
    const py = event.clientY - rect.top;
    const { left, top, width, height, xExtent } = this._geometry;
    if (px < left || px > left + width || py < top || py > top + height) {
      if (this.hover) { this.hover = null; this._hideTooltip(); this.draw(); }
      return;
    }
    const time = xExtent[0] + ((px - left) / width) * (xExtent[1] - xExtent[0]);
    const points = [];
    this.series.forEach((s) => {
      if (!s.x.length) return;
      const index = lowerBound(s.x, time);
      // lowerBound lands on or before `time`; the next point may be closer.
      let best = index;
      if (index + 1 < s.x.length &&
          Math.abs(s.x[index + 1] - time) < Math.abs(s.x[index] - time)) best = index + 1;
      points.push({ label: s.label, color: s.color, axis: s.axis || 'left',
                    time: s.x[best], value: s.y[best] });
    });
    if (!points.length) return;
    this.hover = { time, points };
    this._showTooltip(px, py, points);
    this.draw();
  }

  _showTooltip(px, py, points) {
    if (!this.tooltip) return;
    const stamp = points.reduce((closest, p) => (closest == null ? p.time : closest), null);
    const rows = points.map((p) =>
      `<div class="row"><span class="sw" style="background:${p.color}"></span>` +
      `<span>${escapeHtml(p.label)}</span><strong style="margin-left:auto">` +
      `${formatNumber(p.value, Math.abs(p.value) / 1000 || 0.001)}</strong></div>`).join('');
    this.tooltip.innerHTML =
      `<div style="color:#8a8a8a;margin-bottom:.25rem">${formatFullTime(stamp)}</div>${rows}`;
    this.tooltip.style.display = 'block';
    const wrap = this.tooltip.parentElement;
    const maxLeft = wrap.clientWidth - this.tooltip.offsetWidth - 8;
    this.tooltip.style.left = Math.max(4, Math.min(px + 14, maxLeft)) + 'px';
    this.tooltip.style.top = Math.max(4, py - this.tooltip.offsetHeight - 10) + 'px';
  }

  _hideTooltip() {
    if (this.tooltip) this.tooltip.style.display = 'none';
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

window.SubPlot = { Plot, escapeHtml, formatFullTime, formatNumber };
