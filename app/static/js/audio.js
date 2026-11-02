// A short two-note chime synthesised with WebAudio, so no audio asset and no network.
// Browsers keep the context suspended until a user gesture, so playing before then is a quiet no-op.

const KEY = "sentinelwatch.muted";
let context = null;
let unlocked = false;

function ensureContext() {
  if (context) return context;
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) return null;
  try {
    context = new Ctor();
  } catch {
    context = null;
  }
  return context;
}

function unlock() {
  const ctx = ensureContext();
  if (ctx && ctx.state === "suspended") ctx.resume().catch(() => {});
  unlocked = true;
  window.removeEventListener("pointerdown", unlock);
  window.removeEventListener("keydown", unlock);
}

window.addEventListener("pointerdown", unlock);
window.addEventListener("keydown", unlock);

export function isMuted() {
  try {
    return localStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

export function setMuted(muted) {
  try {
    localStorage.setItem(KEY, muted ? "1" : "0");
  } catch {
    // Private mode: the toggle still works for this session through the caller's state.
  }
}

function note(ctx, frequency, start, duration, level) {
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = "sine";
  osc.frequency.value = frequency;
  gain.gain.setValueAtTime(0.0001, start);
  gain.gain.exponentialRampToValueAtTime(level, start + 0.012);
  gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
  osc.connect(gain).connect(ctx.destination);
  osc.start(start);
  osc.stop(start + duration + 0.02);
}

export function chime(severity = "medium") {
  if (isMuted() || !unlocked) return;
  const ctx = ensureContext();
  if (!ctx || ctx.state !== "running") return;
  try {
    const now = ctx.currentTime + 0.01;
    const urgent = severity === "critical" || severity === "high";
    note(ctx, 659.25, now, 0.42, 0.09);
    note(ctx, urgent ? 987.77 : 830.61, now + 0.14, 0.6, 0.075);
    if (severity === "critical") note(ctx, 987.77, now + 0.46, 0.6, 0.07);
  } catch {
    // Audio is a courtesy. It must never break the alert path.
  }
}
