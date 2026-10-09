export interface ReactorController {
  setIntensity(value: number): void;
  setProgress(value: number): void;
  destroy(): void;
}

interface ReactorOptions {
  color?: string;
  coreColor?: string;
  quality?: "auto" | "low" | "high";
}

interface Particle {
  x: number;
  y: number;
  z: number;
  size: number;
  phase: number;
  core: number;
  drift: number;
  alpha: number;
}

const TAU = Math.PI * 2;
const MAX_DPR = 2;
const MAX_TILT = Math.PI / 30;
const AXIAL_TILT = 0.16;
const DEFAULT_COLOR = "#35c6ff";
const DEFAULT_CORE_COLOR = "#eaf7ff";

export function createReactor(canvas: HTMLCanvasElement, options: ReactorOptions = {}): ReactorController {
  const renderingContext = canvas.getContext("2d", { alpha: true });
  if (!renderingContext) {
    canvas.dataset.reactorFallback = "1";
    return {
      setIntensity: () => undefined,
      setProgress: () => undefined,
      destroy: () => undefined,
    };
  }

  const ctx = renderingContext;
  const color = options.color ?? DEFAULT_COLOR;
  const coreColor = options.coreColor ?? DEFAULT_CORE_COLOR;
  const particles = buildParticles(pickParticleCount(canvas, options.quality ?? "auto"));
  const pointerEvent = window.PointerEvent ? "pointermove" : "mousemove";
  let width = 1;
  let height = 1;
  let dpr = 1;
  let raf = 0;
  let running = false;
  let destroyed = false;
  let elapsed = 0;
  let lastTime = 0;
  let spinY = 0;
  let tiltX = 0;
  let tiltY = 0;
  let targetTiltX = 0;
  let targetTiltY = 0;
  let intensity = 0.64;
  let targetIntensity = 0.64;
  let progress = 0.6;
  let targetProgress = 0.6;
  let resizeObserver: ResizeObserver | null = null;

  const controller: ReactorController = {
    setIntensity(value) {
      targetIntensity = clamp(Number.isFinite(value) ? value : 0.64, 0.08, 1);
    },
    setProgress(value) {
      targetProgress = clamp(Number.isFinite(value) ? value : 0, 0, 1);
    },
    destroy() {
      destroyed = true;
      stop();
      canvas.removeEventListener(pointerEvent, onPointerMove);
      canvas.removeEventListener("pointerleave", resetTilt);
      canvas.removeEventListener("mouseleave", resetTilt);
      document.removeEventListener("visibilitychange", onVisibility);
      resizeObserver?.disconnect();
      window.removeEventListener("resize", resize);
    },
  };

  resize();
  attach();
  start();
  return controller;

  function attach(): void {
    if (window.ResizeObserver) {
      resizeObserver = new ResizeObserver(resize);
      resizeObserver.observe(canvas);
    } else {
      window.addEventListener("resize", resize, { passive: true });
    }
    canvas.addEventListener(pointerEvent, onPointerMove, { passive: true });
    canvas.addEventListener("pointerleave", resetTilt);
    canvas.addEventListener("mouseleave", resetTilt);
    document.addEventListener("visibilitychange", onVisibility);
  }

  function onVisibility(): void {
    if (document.hidden) {
      stop();
    } else {
      start();
    }
  }

  function start(): void {
    if (running || destroyed || document.hidden) {
      return;
    }
    running = true;
    lastTime = 0;
    raf = window.requestAnimationFrame(render);
  }

  function stop(): void {
    if (raf) {
      window.cancelAnimationFrame(raf);
      raf = 0;
    }
    running = false;
    lastTime = 0;
  }

  function resize(): void {
    const rect = canvas.getBoundingClientRect();
    dpr = Math.min(MAX_DPR, Math.max(1, window.devicePixelRatio || 1));
    width = Math.max(1, Math.round((rect.width || canvas.clientWidth || 1) * dpr));
    height = Math.max(1, Math.round((rect.height || canvas.clientHeight || 1) * dpr));
    if (canvas.width !== width) {
      canvas.width = width;
    }
    if (canvas.height !== height) {
      canvas.height = height;
    }
  }

  function onPointerMove(event: Event): void {
    const pointer = event as PointerEvent;
    if ("pointerType" in pointer && pointer.pointerType === "touch") {
      return;
    }

    const rect = canvas.getBoundingClientRect();
    const nx = ((pointer.clientX - rect.left) / Math.max(1, rect.width)) * 2 - 1;
    const ny = ((pointer.clientY - rect.top) / Math.max(1, rect.height)) * 2 - 1;
    targetTiltY = clamp(nx, -1, 1) * MAX_TILT;
    targetTiltX = clamp(-ny, -1, 1) * MAX_TILT;
  }

  function resetTilt(): void {
    targetTiltX = 0;
    targetTiltY = 0;
  }

  function render(nowMs: number): void {
    if (!running || destroyed) {
      return;
    }

    const dt = lastTime ? Math.min(0.1, Math.max(0, (nowMs - lastTime) * 0.001)) : 0;
    lastTime = nowMs;
    elapsed += dt;
    spinY += dt * 0.08;

    const frameEase = 1 - Math.pow(0.94, dt * 60);
    tiltX += (targetTiltX - tiltX) * frameEase;
    tiltY += (targetTiltY - tiltY) * frameEase;
    intensity += (targetIntensity - intensity) * (1 - Math.exp(-dt * 4));
    progress += (targetProgress - progress) * (1 - Math.exp(-dt * 3.3));

    draw();
    raf = window.requestAnimationFrame(render);
  }

  function draw(): void {
    const cx = width / 2;
    const cy = height / 2;
    const radius = Math.min(width, height) * 0.31;
    const glow = 0.35 + intensity * 0.65;
    ctx.clearRect(0, 0, width, height);
    ctx.save();
    ctx.globalCompositeOperation = "lighter";

    drawCore(cx, cy, radius, glow);
    drawParticleCloud(cx, cy, radius);
    drawRings(cx, cy, radius, glow);

    ctx.restore();
  }

  function drawCore(cx: number, cy: number, radius: number, glow: number): void {
    const pulse = 0.9 + Math.sin(elapsed * 2.2) * 0.08;
    const coreRadius = radius * (0.19 + progress * 0.025) * pulse;
    const gradient = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius * 0.72);
    gradient.addColorStop(0, withAlpha(coreColor, 0.86 * glow));
    gradient.addColorStop(0.11, withAlpha(color, 0.54 * glow));
    gradient.addColorStop(0.36, withAlpha(color, 0.18 * glow));
    gradient.addColorStop(1, "rgba(53, 198, 255, 0)");
    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.arc(cx, cy, radius * 0.74, 0, TAU);
    ctx.fill();

    ctx.fillStyle = withAlpha(coreColor, 0.72 + progress * 0.2);
    ctx.shadowColor = color;
    ctx.shadowBlur = 28 * dpr * glow;
    ctx.beginPath();
    ctx.arc(cx, cy, coreRadius, 0, TAU);
    ctx.fill();
    ctx.shadowBlur = 0;
  }

  function drawParticleCloud(cx: number, cy: number, radius: number): void {
    const projected = particles.map((particle) => projectParticle(particle, cx, cy, radius));
    projected.sort((a, b) => a.depth - b.depth);

    for (const particle of projected) {
      if (particle.alpha <= 0.003 || particle.size <= 0.2) {
        continue;
      }
      const particleColor = particle.core > 0.45 ? coreColor : color;
      ctx.fillStyle = withAlpha(particleColor, particle.alpha);
      ctx.beginPath();
      ctx.arc(particle.x, particle.y, particle.size, 0, TAU);
      ctx.fill();
    }
  }

  function projectParticle(particle: Particle, cx: number, cy: number, radius: number): {
    x: number;
    y: number;
    size: number;
    alpha: number;
    depth: number;
    core: number;
  } {
    const wave = Math.sin(elapsed * (0.72 + particle.drift * 0.35) + particle.phase);
    let x = particle.x * (1 + wave * (0.008 + particle.drift * 0.018));
    let y = particle.y * (1 + wave * (0.008 + particle.drift * 0.018));
    let z = particle.z * (1 + wave * (0.008 + particle.drift * 0.018));

    [x, y] = rotate2(x, y, AXIAL_TILT);
    [x, z] = rotate2(x, z, spinY);
    [y, z] = rotate2(y, z, tiltX);
    [x, z] = rotate2(x, z, tiltY);

    const camera = 3.8;
    const scale = camera / Math.max(1.5, camera - z);
    const pulse = 0.84 + 0.16 * Math.sin(elapsed * (1.15 + particle.drift * 0.65) + particle.phase);
    const edgeFade = clamp((z + 1.35) / 2.7, 0.3, 1);
    const hot = particle.core > 1.5 ? 1.85 : 1 + particle.core * 0.35;

    return {
      x: cx + x * radius * scale,
      y: cy + y * radius * scale,
      size: Math.max(0.55, particle.size * dpr * scale * (0.72 + intensity * 0.35) * hot),
      alpha: particle.alpha * pulse * intensity * edgeFade * hot,
      depth: z,
      core: particle.core,
    };
  }

  function drawRings(cx: number, cy: number, radius: number, glow: number): void {
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(elapsed * 0.045);
    drawCircle(radius * 1.05, withAlpha(color, 0.18 * glow), 1.1 * dpr);
    drawCircle(radius * 0.82, withAlpha(color, 0.12 * glow), 0.8 * dpr);
    drawTicks(radius * 1.13, radius * 1.2, 72, glow);
    ctx.restore();

    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(-elapsed * 0.07);
    drawCircle(radius * 1.31, withAlpha(color, 0.10 * glow), 0.8 * dpr);
    drawTicks(radius * 1.31, radius * 1.39, 48, glow * 0.7);
    ctx.restore();

    ctx.save();
    ctx.translate(cx, cy);
    ctx.lineCap = "round";
    ctx.lineWidth = 3 * dpr;
    ctx.strokeStyle = progress > 0.82 ? withAlpha("#ffb64d", 0.9) : withAlpha(color, 0.9);
    ctx.shadowColor = progress > 0.82 ? "#ffb64d" : color;
    ctx.shadowBlur = 18 * dpr * glow;
    ctx.beginPath();
    ctx.arc(0, 0, radius * 1.24, -Math.PI / 2, -Math.PI / 2 + TAU * progress);
    ctx.stroke();
    ctx.shadowBlur = 0;
    ctx.restore();
  }

  function drawCircle(radius: number, stroke: string, lineWidth: number): void {
    ctx.strokeStyle = stroke;
    ctx.lineWidth = lineWidth;
    ctx.beginPath();
    ctx.arc(0, 0, radius, 0, TAU);
    ctx.stroke();
  }

  function drawTicks(inner: number, outer: number, count: number, glow: number): void {
    ctx.strokeStyle = withAlpha(color, 0.26 * glow);
    ctx.lineWidth = 0.8 * dpr;
    ctx.beginPath();
    for (let i = 0; i < count; i += 1) {
      const a = (i / count) * TAU;
      const major = i % 12 === 0;
      const medium = i % 4 === 0;
      const from = major ? inner * 0.965 : medium ? inner * 0.985 : inner;
      const to = major ? outer * 1.035 : medium ? outer : inner + (outer - inner) * 0.62;
      ctx.moveTo(Math.cos(a) * from, Math.sin(a) * from);
      ctx.lineTo(Math.cos(a) * to, Math.sin(a) * to);
    }
    ctx.stroke();
  }
}

function pickParticleCount(canvas: HTMLCanvasElement, quality: ReactorOptions["quality"]): number {
  if (quality === "high") {
    return 2_600;
  }
  if (quality === "low") {
    return 1_300;
  }
  const rect = canvas.getBoundingClientRect();
  const cores = window.navigator.hardwareConcurrency || 8;
  return cores <= 4 || rect.width < 520 ? 1_300 : 2_200;
}

function buildParticles(count: number): Particle[] {
  const coreCount = Math.round(count * 0.26);
  const bodyCount = Math.round(count * 0.52);
  const shellCount = count - coreCount - bodyCount;
  const particles: Particle[] = [];
  for (let i = 0; i < shellCount; i += 1) {
    particles.push(writeParticle("shell"));
  }
  for (let i = 0; i < bodyCount; i += 1) {
    particles.push(writeParticle("body"));
  }
  for (let i = 0; i < coreCount; i += 1) {
    particles.push(writeParticle("core"));
  }
  particles.push({ x: 0, y: 0, z: 0, size: 34, phase: 0.1, core: 2, drift: 0, alpha: 0.22 });
  particles.push({ x: 0.028, y: -0.012, z: 0.018, size: 26, phase: 1.7, core: 2, drift: 0, alpha: 0.18 });
  particles.push({ x: -0.022, y: 0.018, z: -0.006, size: 21, phase: 3.1, core: 2, drift: 0, alpha: 0.15 });
  return particles;
}

function writeParticle(zone: "shell" | "body" | "core"): Particle {
  const [dx, dy, dz] = randomUnitVector();
  let radius: number;
  let core: number;
  let size: number;
  let alpha: number;

  if (zone === "core") {
    radius = Math.pow(Math.random(), 1.6) * 0.42;
    core = 0.5 + 0.5 * (1 - radius / 0.42);
    size = 1.5 + Math.random() * 3.8;
    alpha = 0.30 + Math.random() * 0.34;
  } else if (zone === "body") {
    const t = Math.pow(Math.random(), 0.72);
    radius = 0.4 + t * 0.56;
    core = 0.3 * (1 - t);
    size = 1.1 + Math.random() * 2.8;
    alpha = 0.18 + Math.random() * 0.24;
  } else {
    radius = clamp(0.98 + gaussianish() * 0.18, 0.58, 1.3);
    core = 0;
    size = 0.8 + Math.random() * 2.4 + Math.random() * Math.random() * 0.9;
    alpha = 0.12 + Math.random() * 0.22;
  }

  return {
    x: dx * radius,
    y: dy * radius,
    z: dz * radius,
    size,
    phase: Math.random() * TAU,
    core,
    drift: Math.random(),
    alpha,
  };
}

function randomUnitVector(): [number, number, number] {
  const z = Math.random() * 2 - 1;
  const angle = Math.random() * TAU;
  const radius = Math.sqrt(Math.max(0, 1 - z * z));
  return [Math.cos(angle) * radius, Math.sin(angle) * radius, z];
}

function gaussianish(): number {
  return Math.random() + Math.random() + Math.random() - 1.5;
}

function rotate2(a: number, b: number, angle: number): [number, number] {
  const s = Math.sin(angle);
  const c = Math.cos(angle);
  return [a * c - b * s, a * s + b * c];
}

function withAlpha(hex: string, alpha: number): string {
  const [r, g, b] = parseHex(hex);
  return `rgba(${r}, ${g}, ${b}, ${clamp(alpha, 0, 1).toFixed(4)})`;
}

function parseHex(value: string): [number, number, number] {
  const fallback: [number, number, number] = [53, 198, 255];
  const raw = value.startsWith("#") ? value.slice(1) : value;
  if (!/^[0-9a-fA-F]{3}$|^[0-9a-fA-F]{6}$/.test(raw)) {
    return fallback;
  }
  if (raw.length === 3) {
    return [
      Number.parseInt(`${raw[0]}${raw[0]}`, 16),
      Number.parseInt(`${raw[1]}${raw[1]}`, 16),
      Number.parseInt(`${raw[2]}${raw[2]}`, 16),
    ];
  }
  return [
    Number.parseInt(raw.slice(0, 2), 16),
    Number.parseInt(raw.slice(2, 4), 16),
    Number.parseInt(raw.slice(4, 6), 16),
  ];
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}
