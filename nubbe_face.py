#!/usr/bin/env python3
"""nubbe_face: NUBB-E's animated face, fully offline.

Run it once and leave it open:
    python nubbe_face.py
It opens http://127.0.0.1:8765 in your browser (press F11 for fullscreen).
Two eyes of different sizes, spring physics at 240 Hz, drawn every frame at
your monitor's refresh rate. The eyes follow your mouse, wander and glance
around on their own, and get sleepy if you leave them alone for a while.
Click the face and it giggles.

States (lasting):
    idle      blinks, glances around, follows your cursor
    listening eyes widen and look at you, ring pulses
    thinking  eyes ponder up-left / up-right, one squints, dots bounce
    talking   eyes bounce with the speech rhythm, reply shows as a caption
    happy     ^ ^ eyes
    sleep     eyes closed, little z's float up

Reactions (play for a moment on top of the current state):
    surprised curious excited sad confused wink giggle lookaround

From any script:
    from nubbe_face import set_face
    set_face("thinking")
    set_face("talking", "Found the bug on line 42!")
    set_face("excited")      # a reaction

No installs needed, standard library only. Later, a small screen on the
robot body can open the same page over local Wi-Fi.
"""
import argparse
import json
import queue
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from nubbe_config import CFG
    PORT = CFG["app"]["port"]
except ImportError:
    PORT = 8765
BASE_STATES = {"idle", "listening", "thinking", "talking", "happy", "sleep"}
REACTIONS = {"surprised", "curious", "excited", "sad", "confused", "wink", "giggle", "lookaround"}
STATES = BASE_STATES | REACTIONS
FACE_STATES = STATES  # used by nubbe_app.py


def set_face(state: str, text: str | None = None, who: str = "nubbe", port: int = PORT) -> None:
    """Update the face. Silently does nothing if the face isn't running."""
    payload = {"state": state, "who": who}
    if text is not None:
        payload["text"] = text
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/state",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=0.3).close()
    except Exception:
        pass  # the face is optional, never crash the chat over it


FACE_JS = r"""// NUBB-E face engine: two eyes of different sizes, spring physics at 240 Hz,
// drawn every frame at your monitor's refresh rate. Shared by nubbe_face.py and nubbe_app.py.
(function () {
'use strict';
const GLOW = '#6ff3ff';
const TAU = Math.PI * 2;
const STEP = 1 / 240;   // physics steps per second (smooth even on 60 Hz screens)

// a spring pulls a value toward its target; low damping = bouncy overshoot
class Spring {
  constructor(v, k, zeta) { this.v = v; this.t = v; this.vel = 0; this.k = k; this.c = 2 * zeta * Math.sqrt(k); }
  step(dt) { this.vel += (this.k * (this.t - this.v) - this.c * this.vel) * dt; this.v += this.vel * dt; }
}

// sizes in vmin. sizeR < sizeL: the right eye is smaller on purpose
const DEFAULT = { eyeW: 16, eyeH: 22, sizeL: 1, sizeR: 0.8, h: 1, lidT: 0, lidB: 0, slope: 0, dy: 0, arc: 0 };

// lasting states
const POSES = {
  idle: {},
  listening: { eyeH: 24, sizeL: 1.06, sizeR: 0.92, listen: 1, gaze: { x: 0, y: 0 } },
  thinking: { eyeH: 19, sizeR: 0.62, lidTR: 0.18, think: 1 },
  talking: { talk: 1 },
  happy: { arc: 1, sizeR: 0.9, glow: 1.2 },
  sleep: { eyeH: 1.8, alpha: 0.45, glow: 0.6 },
  sleepy: { lidTL: 0.5, lidTR: 0.58, slopeL: -0.08, slopeR: 0.08, glow: 0.75, gaze: { x: 0, y: 2.2 } },
};
// short reactions that play on top of the current state, then fade back
const REACTIONS = {
  surprised:  { ms: 1000, eyeW: 18.5, eyeH: 27, sizeR: 0.9, glow: 1.5, gaze: { x: 0, y: -1.2 } },
  curious:    { ms: 1500, tilt: 0.13, dyL: -2.6, dyR: 1.6, sizeL: 1.1, sizeR: 0.72, gaze: { x: 2.8, y: -1 } },
  excited:    { ms: 1700, eyeH: 24, arc: 1, sizeR: 0.9, glow: 1.45 },
  sad:        { ms: 1800, eyeH: 20, lidT: 0.36, slopeL: -0.38, slopeR: 0.38, glow: 0.7, gaze: { x: 0, y: 2.8 } },
  confused:   { ms: 1600, tilt: -0.09, sizeL: 1.12, sizeR: 0.78, lidTR: 0.45, slopeR: 0.22, dyL: -1.4,
                gaze: { x: -1.5, y: -0.5 } },
  wink:       { ms: 520, hL: 0.07, sizeR: 0.95, glow: 1.15, gaze: { x: 0, y: 0 } },
  giggle:     { ms: 1200, arc: 1, sizeR: 0.9, glow: 1.3 },
  lookaround: { ms: 2300 },
};

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const rnd = (a, b) => a + Math.random() * (b - a);
const smooth = x => x * x * (3 - 2 * x);
const get = (p, key, s) => p[key + s] ?? p[key] ?? DEFAULT[key + s] ?? DEFAULT[key];

function NubbeFace(canvas, opts = {}) {
  const box = opts.box || canvas.parentElement || document.body;
  const fullscreen = !!opts.fullscreen;
  const sleepyAfter = opts.sleepyAfter || 90;     // seconds of no activity before it gets sleepy
  const ctx = canvas.getContext('2d');

  // ---------- sizing ----------
  let W = 1, H = 1, S = 1, dpr = 1, rect = canvas.getBoundingClientRect();
  function resize() {
    dpr = window.devicePixelRatio || 1;
    W = fullscreen ? innerWidth : box.clientWidth;
    H = fullscreen ? innerHeight : box.clientHeight;
    S = Math.min(W, H * 1.15) / 100;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
    rect = canvas.getBoundingClientRect();
  }
  addEventListener('resize', resize);
  if (window.ResizeObserver) new ResizeObserver(resize).observe(box);
  addEventListener('scroll', () => { rect = canvas.getBoundingClientRect(); }, true);
  resize();

  // ---------- springs ----------
  const sp = (v, k, z) => new Spring(v, k, z);
  const G = {
    gx: sp(0, 420, 0.72), gy: sp(0, 420, 0.72),     // gaze: fast saccades with a tiny overshoot
    hx: sp(0, 90, 0.85), hy: sp(0, 90, 0.85),       // the whole face follows the gaze a little
    tilt: sp(0, 120, 0.7), glow: sp(0.6, 60, 1), alpha: sp(0.45, 40, 1),
    listen: sp(0, 50, 1), think: sp(0, 60, 1), talk: sp(0, 90, 1),
  };
  const eye = () => ({
    w: sp(16, 230, 0.62), h: sp(2, 230, 0.62),      // bouncy squash and stretch
    lidT: sp(0, 260, 0.9), lidB: sp(0, 260, 0.9), slope: sp(0, 200, 0.85),
    dy: sp(0, 200, 0.75), arc: sp(0, 110, 0.9),
  });
  const E = { L: eye(), R: eye() };
  const ALL = [...Object.values(G), ...Object.values(E.L), ...Object.values(E.R)];

  // ---------- state ----------
  let base = 'sleep', reaction = null, t = 0, last = performance.now(), acc = 0;
  let lastActivity = 0, sleepy = false, zTimer = 0;
  const gaze = { x: 0, y: 0, next: 0, seq: [] };
  const pointer = { x: 0, y: 0, t: -99 };
  const look = { x: 0, y: 0, until: -1 };
  const blink = { start: -1, next: 1.5, dur: 0.2, double: false };
  const parts = [];

  function toGaze(clientX, clientY) {
    const fx = rect.left + W / 2, fy = rect.top + H / 2, r = Math.min(W, H) * 0.55;
    return { x: clamp((clientX - fx) / r * 6, -7, 7), y: clamp((clientY - fy) / r * 4, -4.5, 5.5) };
  }
  function wake() {
    lastActivity = t;
    if (sleepy) { sleepy = false; if (base === 'idle') react('surprised', 700); }
  }
  addEventListener('mousemove', e => { pointer.x = e.clientX; pointer.y = e.clientY; pointer.t = t; wake(); });
  addEventListener('keydown', wake);
  canvas.addEventListener('click', () => react('giggle'));
  canvas.addEventListener('dblclick', () => react('surprised'));

  // ---------- public controls ----------
  function react(name, ms, auto) {
    const R = REACTIONS[name];
    if (!R) return;
    reaction = { name, start: t, until: t + (ms || R.ms) / 1000 };
    if (!auto) lastActivity = t;
    if (name === 'surprised') { E.L.h.vel += 70; E.R.h.vel += 60; E.L.w.vel += 25; E.R.w.vel += 20; emit('bang', 1); }
    if (name === 'excited') emit('spark', 7);
    if (name === 'giggle') emit('spark', 4);
    if (name === 'wink') emit('spark', 2);
    if (name === 'confused') emit('q', 1);
    if (name === 'lookaround') gaze.seq = [[-6, -0.5], [6, -1], [-3, 1.5], [0, 0]];
  }
  function set(s) {
    if (REACTIONS[s]) return react(s);
    if (!POSES[s] || s === 'sleepy') return;
    if (s !== base) { lastActivity = t; if (s !== 'sleep') sleepy = false; }
    base = s;
  }
  function lookAt(clientX, clientY, holdMs = 1200) {
    const g = toGaze(clientX, clientY);
    look.x = g.x; look.y = g.y; look.until = t + holdMs / 1000;
  }

  // ---------- behaviour ----------
  function pose() {
    let p = POSES[base] || {};
    if (base === 'idle' && sleepy) p = { ...p, ...POSES.sleepy };
    if (reaction) {
      if (t < reaction.until) p = { ...p, ...REACTIONS[reaction.name] };
      else reaction = null;
    }
    return p;
  }
  function chooseWander() {
    let x, y, wait;
    if (base === 'thinking') {               // pondering: glance up-left, then up-right
      const right = gaze.x < 0;
      x = right ? rnd(3.5, 5.5) : rnd(-5, -3); y = rnd(-4.5, -2.8); wait = rnd(1.0, 2.2);
    } else if (base === 'talking' || base === 'listening') {
      x = rnd(-2.2, 2.2); y = rnd(-1.2, 1); wait = rnd(0.7, 1.8);     // mostly looking at you
    } else if (sleepy) {
      x = rnd(-1.5, 1.5); y = rnd(1.5, 2.8); wait = rnd(2, 4);
    } else {
      const r = Math.random();
      if (r < 0.12 && base === 'idle') { react('lookaround', 0, true); x = 0; y = 0; wait = 2.4; }
      else if (r < 0.5) { x = rnd(-2, 2); y = rnd(-1.2, 1.2); wait = rnd(0.6, 1.6); }
      else { x = rnd(-6.5, 6.5); y = rnd(-3.5, 3.5); wait = rnd(0.9, 2.6); }
    }
    if (Math.hypot(x - gaze.x, y - gaze.y) > 5 && Math.random() < 0.3) startBlink();  // big glances often blink
    gaze.x = x; gaze.y = y; gaze.next = t + wait;
  }
  function gazeTarget(p) {
    const drift = { x: Math.sin(t * 0.7) * 0.25 + Math.sin(t * 2.3) * 0.12, y: Math.sin(t * 0.9 + 1) * 0.18 };
    const pointerOK = (base === 'idle' || base === 'happy') && !sleepy;
    let g;
    if (reaction && reaction.name === 'lookaround' && gaze.seq.length) {
      const i = Math.min(gaze.seq.length - 1, Math.floor((t - reaction.start) / 0.55));
      g = { x: gaze.seq[i][0], y: gaze.seq[i][1] };
    } else if (p.gaze) g = p.gaze;
    else if (t < look.until) g = look;
    else if (pointerOK && t - pointer.t < 2.5) g = toGaze(pointer.x, pointer.y);
    else { if (t > gaze.next) chooseWander(); g = gaze; }
    return { x: g.x + drift.x, y: g.y + drift.y };
  }
  function startBlink() {
    if (blink.start >= 0) return;
    blink.start = t; blink.double = Math.random() < 0.18; blink.dur = sleepy ? 0.5 : 0.2;
  }
  function blinkAmount() {
    const busy = reaction && ['wink', 'surprised', 'giggle', 'excited'].includes(reaction.name);
    if (base !== 'sleep' && !busy && blink.start < 0 && t > blink.next) startBlink();
    if (blink.start < 0) return 0;
    const p = (t - blink.start) / blink.dur;
    if (p >= 1) {
      if (blink.double) { blink.double = false; blink.start = t + 0.07; }
      else { blink.start = -1; blink.next = t + (sleepy ? rnd(1.2, 2.5) : rnd(2.2, 5.5)); }
      return 0;
    }
    if (p < 0) return 0;
    return p < 0.4 ? smooth(p / 0.4) : 1 - smooth((p - 0.4) / 0.6);   // shut fast, open slower
  }
  function speech() {   // smooth fake speech rhythm with little pauses between "words"
    const v = 0.5 + 0.35 * Math.sin(t * 11.3) * Math.sin(t * 4.7 + 0.6) + 0.2 * Math.sin(t * 17.9);
    return clamp(v, 0, 1) * (Math.sin(t * 1.9) > -0.75 ? 1 : 0.15);
  }
  function updateTargets() {
    if (base === 'idle' && !sleepy && t - lastActivity > sleepyAfter) sleepy = true;
    const p = pose();
    const g = gazeTarget(p);
    G.gx.t = g.x; G.gy.t = g.y; G.hx.t = g.x * 0.4; G.hy.t = g.y * 0.3;
    const talk = G.talk.v, say = talk * speech();
    G.tilt.t = (p.tilt ?? 0) + Math.sin(t * 1.3) * 0.03 * talk;
    G.glow.t = p.glow ?? 1; G.alpha.t = p.alpha ?? 1;
    G.listen.t = p.listen ?? 0; G.think.t = p.think ?? 0; G.talk.t = p.talk ?? 0;
    const eyeW = p.eyeW ?? DEFAULT.eyeW, eyeH = p.eyeH ?? DEFAULT.eyeH;
    for (const s of ['L', 'R']) {
      const e = E[s], side = s === 'L' ? -1 : 1;
      const size = get(p, 'size', s) * (1 + side * G.gx.v * 0.012);   // eye on the side you look at grows a touch
      e.w.t = eyeW * size * (1 - 0.04 * say);
      e.h.t = eyeH * size * get(p, 'h', s) * (1 + 0.14 * say);
      e.lidT.t = get(p, 'lidT', s); e.lidB.t = get(p, 'lidB', s);
      e.slope.t = get(p, 'slope', s); e.dy.t = get(p, 'dy', s) - 1.3 * say;
      e.arc.t = get(p, 'arc', s);
    }
    if ((sleepy || base === 'sleep') && t > zTimer) { emit('z', 1); zTimer = t + rnd(1.0, 1.6); }
  }

  // ---------- particles: sparkles, ?, !, z ----------
  function emit(kind, n) {
    for (let i = 0; i < n; i++) {
      const p = { kind, life: 0, x: 0, y: 0, vx: 0, vy: 0, size: 1, spin: rnd(-2, 2), max: 1 };
      if (kind === 'spark') {
        const a = rnd(0, TAU), r = rnd(20, 30);
        p.x = Math.cos(a) * r; p.y = Math.sin(a) * r * 0.6 - 4;
        p.vx = Math.cos(a) * 6; p.vy = Math.sin(a) * 4 - 4; p.max = rnd(0.6, 1.0); p.size = rnd(1.2, 2.2);
      } else if (kind === 'q') { p.x = 24; p.y = -18; p.vy = -4; p.max = 1.3; p.size = 7; }
      else if (kind === 'bang') { p.y = -24; p.vy = -2; p.max = 0.9; p.size = 8; }
      else if (kind === 'z') { p.x = 20; p.y = -10; p.vx = 3.5; p.vy = -5; p.max = 2.4; p.size = 3.5; }
      parts.push(p);
    }
  }
  function stepParts(dt) {
    for (let i = parts.length - 1; i >= 0; i--) {
      const p = parts[i];
      p.life += dt; p.x += p.vx * dt; p.y += p.vy * dt;
      if (p.life >= p.max) parts.splice(i, 1);
    }
  }

  // ---------- drawing ----------
  function roundRect(x, y, w, h, r) {
    r = Math.max(0, Math.min(r, w / 2, h / 2));
    ctx.beginPath(); ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }
  function star(x, y, r, rot) {
    ctx.beginPath();
    for (let i = 0; i < 8; i++) {
      const a = rot + i * Math.PI / 4, rr = i % 2 ? r * 0.35 : r;
      ctx.lineTo(x + Math.cos(a) * rr, y + Math.sin(a) * rr);
    }
    ctx.closePath(); ctx.fill();
  }
  function render(bl) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = ctx.strokeStyle = GLOW;
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    const alpha = clamp(G.alpha.v, 0, 1), glow = Math.max(0, G.glow.v);

    const breathe = Math.sin(t * TAU / (base === 'sleep' ? 7 : 4)) * S;
    let hop = 0, shake = 0;
    if (reaction && (reaction.name === 'excited' || reaction.name === 'giggle')) {
      const fade = 1 - (t - reaction.start) / (reaction.until - reaction.start);
      if (reaction.name === 'excited') hop = Math.abs(Math.sin((t - reaction.start) * 9)) * 2.4 * S * fade;
      else { shake = Math.sin(t * 38) * 0.9 * S * fade; hop = Math.abs(Math.sin(t * 12)) * 0.8 * S * fade; }
    }
    const cx = W / 2 + G.hx.v * S + shake, cy = H / 2 + breathe + G.hy.v * S - hop;

    if (G.listen.v > 0.01) {                // listening rings
      ctx.shadowBlur = 0; ctx.lineWidth = 0.5 * S;
      for (let i = 0; i < 2; i++) {
        const ph = (t / 1.6 + i * 0.5) % 1;
        ctx.globalAlpha = 0.5 * (1 - ph) * G.listen.v;
        ctx.beginPath(); ctx.arc(cx, cy, (0.7 + 0.45 * ph) * 35 * S, 0, TAU); ctx.stroke();
      }
    }

    ctx.save();
    ctx.translate(cx, cy); ctx.rotate(G.tilt.v); ctx.translate(-cx, -cy);
    ctx.shadowColor = GLOW;
    for (const s of ['L', 'R']) {
      const e = E[s], side = s === 'L' ? -1 : 1;
      const ex = cx + side * 16 * S + G.gx.v * 0.75 * S;
      const ey = cy + G.gy.v * 0.8 * S + e.dy.v * S;
      const ws = Math.max(0.5, e.w.v) / 16;
      const w = Math.max(0.5, e.w.v) * S, full = Math.max(0.5, e.h.v) * S;
      const arc = clamp(e.arc.v, 0, 1);
      const h = Math.max(1.3 * S, full * (1 - 0.6 * arc) * (1 - 0.94 * bl));
      ctx.shadowBlur = 2.6 * S * dpr * glow;

      const lt = clamp(e.lidT.v, 0, 1), lb = clamp(e.lidB.v, 0, 1);
      ctx.save();
      if (lt > 0.005 || lb > 0.005) {        // eyelids: cut the eye with a (slanted) line
        const top = ey - h / 2, bot = ey + h / 2, tn = Math.tan(e.slope.v), R = w;
        const y0 = lt > 0.005 ? top + lt * h : top - 4 * S;
        const y1 = lb > 0.005 ? bot - lb * h : bot + 4 * S;
        ctx.beginPath();
        ctx.moveTo(ex - R, y0 - tn * R); ctx.lineTo(ex + R, y0 + tn * R);
        ctx.lineTo(ex + R, y1); ctx.lineTo(ex - R, y1); ctx.closePath(); ctx.clip();
      }
      ctx.globalAlpha = alpha * (1 - arc);
      roundRect(ex - w / 2, ey - h / 2, w, h, 7 * S * ws);
      ctx.fill();
      ctx.restore();

      if (arc > 0.01) {                      // happy ^ ^
        ctx.globalAlpha = alpha * arc; ctx.lineWidth = 2.8 * S * ws;
        ctx.beginPath();
        ctx.arc(ex, ey + 3 * S * (e.h.v / 22), (5 + 1.6 * arc) * S * ws, Math.PI, TAU);
        ctx.stroke();
      }
    }
    ctx.restore();

    if (G.think.v > 0.01) {                  // thinking dots
      ctx.shadowBlur = 0;
      for (let j = 0; j < 3; j++) {
        const lift = Math.max(0, Math.sin(t * TAU - j * 0.9));
        ctx.globalAlpha = G.think.v * (0.35 + 0.65 * lift);
        ctx.beginPath(); ctx.arc(cx + (23 + j * 4.4) * S, cy - 20 * S - lift * 1.6 * S, 1.2 * S, 0, TAU); ctx.fill();
      }
    }

    ctx.shadowBlur = 1.5 * S * dpr;
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    for (const p of parts) {
      const k = p.life / p.max;
      ctx.globalAlpha = (k < 0.15 ? k / 0.15 : 1 - smooth((k - 0.15) / 0.85)) * Math.max(alpha, 0.6);
      const x = cx + p.x * S, y = cy + p.y * S;
      if (p.kind === 'spark') star(x, y, p.size * S * (1 - 0.5 * k), p.spin * p.life);
      else {
        const scale = p.kind === 'z' ? 0.6 + k : p.kind === 'bang' ? (k < 0.2 ? 0.6 + 2 * k : 1) : 1;
        ctx.font = `700 ${p.size * scale * S}px system-ui, sans-serif`;
        ctx.fillText(p.kind === 'z' ? 'z' : p.kind === 'q' ? '?' : '!', x, y);
      }
    }
    ctx.globalAlpha = 1; ctx.shadowBlur = 0;
  }

  function frame(now) {
    const dt = Math.min(0.05, (now - last) / 1000); last = now;
    updateTargets();
    acc += dt;
    while (acc >= STEP) { for (const s of ALL) s.step(STEP); acc -= STEP; t += STEP; }
    stepParts(dt);
    render(blinkAmount());
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  return { set, react, lookAt, isReaction: n => !!REACTIONS[n], get state() { return base; } };
}

window.NubbeFace = NubbeFace;
window.NUBBE_REACTIONS = Object.keys(REACTIONS);
})();
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NUBB-E</title>
<style>
  html, body { margin: 0; height: 100%; background: #0b0f14; overflow: hidden;
               font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  canvas { position: absolute; inset: 0; display: block; cursor: pointer; }
  #status { position: absolute; top: 3vh; left: 0; right: 0; text-align: center; pointer-events: none;
            color: #6ff3ff; opacity: .6; letter-spacing: .35em; text-transform: uppercase;
            font-size: clamp(11px, 1.8vmin, 18px); }
  #caption { position: absolute; bottom: 6vh; left: 50%; transform: translateX(-50%); pointer-events: none;
             width: min(84vw, 900px); text-align: center; color: #d9faff;
             font-size: clamp(16px, 3.2vmin, 32px); line-height: 1.4; min-height: 2.8em;
             transition: opacity .6s; }
  #caption.you { opacity: .55; font-style: italic; }
  #caption.faded { opacity: 0; }
</style>
</head>
<body>
<canvas id="c"></canvas>
<div id="status">zzz</div>
<div id="caption"></div>
<script src="/face.js"></script>
<script>
  const face = NubbeFace(document.getElementById('c'), { fullscreen: true });
  const caption = document.getElementById('caption'), statusEl = document.getElementById('status');
  const LABELS = { listening: 'listening', thinking: 'thinking', sleep: 'zzz' };
  let fadeTimer = null;

  function setState(s) {
    if (face.isReaction(s)) { face.react(s); return; }   // reactions play on top, then fade back
    face.set(s);
    statusEl.textContent = LABELS[s] || '';
    clearTimeout(fadeTimer);
    caption.classList.remove('faded');
    if (s === 'idle') fadeTimer = setTimeout(() => caption.classList.add('faded'), 8000);
    if (s === 'listening') caption.textContent = '';
  }
  function setCaption(text, who) {
    caption.textContent = text;
    caption.className = who === 'you' ? 'you' : '';
  }

  const es = new EventSource('/events');
  es.onmessage = (e) => {
    const d = JSON.parse(e.data);
    if (d.state) setState(d.state);
    if (d.text !== undefined) setCaption(d.text, d.who);
  };
  es.onerror = () => setState('sleep');
</script>
</body>
</html>
"""

_clients: list[queue.Queue] = []
_lock = threading.Lock()
_current = {"state": "idle", "text": "", "who": "nubbe"}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/face.js":
            body = FACE_JS.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q: queue.Queue = queue.Queue()
            with _lock:
                _clients.append(q)
                q.put(dict(_current))
            try:
                while True:
                    try:
                        msg = q.get(timeout=15)
                        self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")  # keeps the connection alive
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with _lock:
                    if q in _clients:
                        _clients.remove(q)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/state":
            self.send_error(404)
            return
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except (ValueError, json.JSONDecodeError):
            self.send_error(400)
            return
        if data.get("state") not in STATES:
            self.send_error(400, "unknown state")
            return
        with _lock:
            if data["state"] in BASE_STATES:
                _current.update(data)
            for q in _clients:
                q.put(data)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass  # keep the terminal quiet


def main() -> None:
    p = argparse.ArgumentParser(description="NUBB-E's offline animated face.")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--host", default="127.0.0.1",
                   help="use 0.0.0.0 later so the robot's screen can open it over local Wi-Fi")
    p.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = p.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}"
    print(f"NUBB-E's face is at {url}  (F11 for fullscreen, Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nface off. *sleepy beep*")


if __name__ == "__main__":
    main()
