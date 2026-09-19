/* HoloCore: el núcleo holográfico de J.A.R.V.I.S.

   Componente autónomo. La página le pasa el estado de voz (idle, connecting,
   listening, speaking), el nivel de volumen y si hay una investigación en curso;
   él se ocupa de la esfera de partículas, de los anillos que reaccionan al sonido,
   de la red neuronal que sustituye a los anillos en modo investigación, de la red
   de fondo, de los circuitos que crecen desde las esquinas hacia el núcleo y del
   color de todo ello. No sabe nada de Vapi ni del backend: así se puede probar y
   cambiar por separado.

   Humor (mood): de voz + investigación sale uno de idle, listening, thinking o
   speaking. Se publica como data-mood en el escenario (la página entera) y en el
   núcleo. La paleta de cada humor vive en CSS (variables --hud-a/-b/-c por
   data-mood, con los valores de Tailwind); aquí se lee y se interpola para los
   canvas, que no saben de transiciones CSS.

   Rendimiento en móvil: un solo requestAnimationFrame, canvas con el DPR limitado
   a 2, sin shadowBlur ni filtros por frame, la red de fondo a media cadencia en
   reposo, y todo se detiene con la pestaña oculta. La rotación de los anillos va
   por CSS y el escalado por transform en un contenedor aparte, para que ninguna
   de las dos pise a la otra. */
(function (global) {
  "use strict";

  const TAU = Math.PI * 2;
  // En pantallas 3x el canvas triplica los píxeles sin ganar nitidez que se vea.
  const DPR_CAP = 2;
  const GLOBE_POINTS = 220;
  const GLOBE_NEIGHBOURS = 2;
  const NET_NODES = 40;
  const NET_NEIGHBOURS = 3;
  const FIELD_NODES = 34;
  const FIELD_NEIGHBOURS = 2;
  // Lo que dura el cruce entre anillos y red (debe coincidir con duration-700).
  const FADE_MS = 750;
  // Paleta de respaldo si el CSS no define las variables: la de reposo.
  const FALLBACK_PALETTE = { a: [8, 145, 178], b: [30, 58, 138], c: [103, 232, 249] };
  const REDUCED_MOTION = Boolean(
    global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches
  );

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function rgba(rgb, alpha) {
    return `rgba(${rgb[0] | 0}, ${rgb[1] | 0}, ${rgb[2] | 0}, ${alpha})`;
  }

  function mixRgb(from, to, k) {
    return [from[0] + (to[0] - from[0]) * k, from[1] + (to[1] - from[1]) * k, from[2] + (to[2] - from[2]) * k];
  }

  // "8 145 178" o "8, 145, 178" → [8, 145, 178]. Es lo que hay en --hud-a/-b/-c.
  function parseTriplet(raw, fallback) {
    const parts = String(raw || "")
      .split(/[\s,]+/)
      .filter(Boolean)
      .map(Number);
    if (parts.length !== 3 || parts.some((n) => !Number.isFinite(n))) return fallback.slice();
    return parts;
  }

  function readPalette(stage) {
    const style = global.getComputedStyle(stage);
    return {
      a: parseTriplet(style.getPropertyValue("--hud-a"), FALLBACK_PALETTE.a),
      b: parseTriplet(style.getPropertyValue("--hud-b"), FALLBACK_PALETTE.b),
      c: parseTriplet(style.getPropertyValue("--hud-c"), FALLBACK_PALETTE.c),
    };
  }

  function fitCanvas(canvas) {
    const dpr = Math.min(global.devicePixelRatio || 1, DPR_CAP);
    const w = canvas.clientWidth || 1;
    const h = canvas.clientHeight || 1;
    const pw = Math.round(w * dpr);
    const ph = Math.round(h * dpr);
    if (canvas.width !== pw || canvas.height !== ph) {
      canvas.width = pw;
      canvas.height = ph;
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h };
  }

  // Esfera de Fibonacci: puntos repartidos de forma uniforme, sin amontonarse en
  // los polos como pasaría con latitud/longitud regulares.
  function spherePoints(count) {
    const points = [];
    const golden = Math.PI * (3 - Math.sqrt(5));
    for (let i = 0; i < count; i++) {
      const y = 1 - (i / (count - 1)) * 2;
      const radius = Math.sqrt(1 - y * y);
      const theta = golden * i;
      points.push({ x: Math.cos(theta) * radius, y, z: Math.sin(theta) * radius });
    }
    return points;
  }

  // Pares de vecinos más cercanos, calculados una vez. La rotación conserva las
  // distancias, así que la topología no cambia y no hay que recalcular por frame.
  function nearestPairs(points, k) {
    const seen = new Set();
    const pairs = [];
    for (let i = 0; i < points.length; i++) {
      const a = points[i];
      const candidates = [];
      for (let j = 0; j < points.length; j++) {
        if (i === j) continue;
        const b = points[j];
        candidates.push({ j, d: (a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2 });
      }
      candidates.sort((p, q) => p.d - q.d);
      for (const { j } of candidates.slice(0, k)) {
        const key = i < j ? `${i}:${j}` : `${j}:${i}`;
        if (seen.has(key)) continue;
        seen.add(key);
        pairs.push([i, j]);
      }
    }
    return pairs;
  }

  function drawGlobe(ctx, w, h, points, links, angle, level, pal) {
    ctx.clearRect(0, 0, w, h);
    const cx = w / 2;
    const cy = h / 2;
    // La esfera desborda los anillos medios y roza el exterior, como en la referencia.
    const radius = Math.min(w, h) * (0.44 + level * 0.02);
    const tilt = 0.38;
    const ca = Math.cos(angle);
    const sa = Math.sin(angle);
    const ct = Math.cos(tilt);
    const st = Math.sin(tilt);
    const projected = new Array(points.length);
    for (let i = 0; i < points.length; i++) {
      const p = points[i];
      const x = p.x * ca + p.z * sa;
      const z1 = -p.x * sa + p.z * ca;
      const y = p.y * ct - z1 * st;
      const z = p.y * st + z1 * ct;
      // z va de -1 (detrás) a 1 (hacia quien mira).
      projected[i] = { x: cx + x * radius, y: cy + y * radius, z };
    }

    ctx.lineWidth = 0.7;
    const linkBoost = 0.7 + level * 0.6;
    for (let i = 0; i < links.length; i++) {
      const a = projected[links[i][0]];
      const b = projected[links[i][1]];
      const depth = (a.z + b.z + 2) / 4;
      ctx.strokeStyle = rgba(pal.a, ((0.06 + depth * 0.24) * linkBoost).toFixed(3));
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }

    for (let i = 0; i < projected.length; i++) {
      const p = projected[i];
      const front = (p.z + 1) / 2;
      ctx.fillStyle =
        front > 0.5 ? rgba(pal.c, (0.35 + front * 0.6).toFixed(3)) : rgba(pal.b, (0.2 + front * 0.45).toFixed(3));
      ctx.beginPath();
      ctx.arc(p.x, p.y, 0.8 + front * 1.6, 0, TAU);
      ctx.fill();
    }
  }

  // Red neuronal: nodos a la deriva, enlaces que titilan y pulsos de "electricidad"
  // que viajan de nodo a nodo. En forma de disco (dentro del núcleo, modo
  // investigación) o de rectángulo (el fondo de toda la pantalla). El tempo la
  // acelera o la frena: 1 es el ritmo de escucha, el reposo va a la mitad y el
  // procesamiento (thinking) al doble y pico.
  function createNet(canvas, options) {
    const disc = options.shape === "disc";
    const count = options.nodes;
    const neighbours = options.neighbours;
    const pulseRate = options.pulseRate;
    let nodes = [];
    let links = [];
    let pulses = [];

    function reset() {
      nodes = [];
      for (let i = 0; i < count; i++) {
        let x;
        let y;
        if (disc) {
          const r = 0.42 * Math.sqrt(Math.random());
          const a = Math.random() * TAU;
          x = Math.cos(a) * r;
          y = Math.sin(a) * r;
        } else {
          x = Math.random();
          y = Math.random();
        }
        nodes.push({
          x,
          y,
          vx: (Math.random() - 0.5) * 0.05,
          vy: (Math.random() - 0.5) * 0.05,
          flash: 0,
          z: 0,
        });
      }
      links = nearestPairs(nodes, neighbours);
      pulses = [];
    }

    function place(n, w, h) {
      if (disc) {
        const scale = Math.min(w, h);
        return [w / 2 + n.x * scale, h / 2 + n.y * scale];
      }
      return [n.x * w, n.y * h];
    }

    function draw(dt, t, pal, tempo) {
      const { ctx, w, h } = fitCanvas(canvas);
      ctx.clearRect(0, 0, w, h);
      const step = dt * tempo;

      for (const n of nodes) {
        n.x += n.vx * step;
        n.y += n.vy * step;
        if (disc) {
          if (Math.hypot(n.x, n.y) > 0.44) {
            n.vx *= -1;
            n.vy *= -1;
          }
        } else {
          if (n.x < 0 || n.x > 1) n.vx *= -1;
          if (n.y < 0 || n.y > 1) n.vy *= -1;
          n.x = clamp(n.x, 0, 1);
          n.y = clamp(n.y, 0, 1);
        }
        n.flash = Math.max(0, n.flash - step * 2.5);
      }

      const pos = nodes.map((n) => place(n, w, h));
      const dim = disc ? 1 : 0.55;

      ctx.lineWidth = 1;
      for (let i = 0; i < links.length; i++) {
        const [ax, ay] = pos[links[i][0]];
        const [bx, by] = pos[links[i][1]];
        const flicker = (0.18 + 0.12 * Math.sin(t * 3 * tempo + i * 1.7)) * dim;
        ctx.strokeStyle = rgba(pal.b, flicker.toFixed(3));
        ctx.beginPath();
        ctx.moveTo(ax, ay);
        ctx.lineTo(bx, by);
        ctx.stroke();
      }

      // La densidad de pulsos va ligada al tiempo real y al tempo, no a los fps.
      if (links.length && Math.random() < step * pulseRate) {
        const link = links[Math.floor(Math.random() * links.length)];
        const forward = Math.random() < 0.5;
        pulses.push({
          from: forward ? link[0] : link[1],
          to: forward ? link[1] : link[0],
          pos: 0,
          speed: 1.2 + Math.random() * 0.9,
        });
      }

      ctx.lineWidth = 1.6;
      ctx.lineCap = "round";
      for (let i = pulses.length - 1; i >= 0; i--) {
        const pulse = pulses[i];
        pulse.pos += pulse.speed * step;
        const [ax, ay] = pos[pulse.from];
        const [bx, by] = pos[pulse.to];
        if (pulse.pos >= 1) {
          nodes[pulse.to].flash = 1;
          pulses.splice(i, 1);
          continue;
        }
        const tail = Math.max(0, pulse.pos - 0.18);
        const x1 = ax + (bx - ax) * tail;
        const y1 = ay + (by - ay) * tail;
        const x2 = ax + (bx - ax) * pulse.pos;
        const y2 = ay + (by - ay) * pulse.pos;
        const glow = ctx.createLinearGradient(x1, y1, x2, y2);
        glow.addColorStop(0, rgba(pal.a, 0));
        glow.addColorStop(1, rgba(pal.c, 0.95 * dim));
        ctx.strokeStyle = glow;
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
        ctx.fillStyle = rgba(pal.c, dim);
        ctx.beginPath();
        ctx.arc(x2, y2, 1.8, 0, TAU);
        ctx.fill();
      }

      // El halo es un segundo círculo tenue: shadowBlur sería mucho más caro en móvil.
      for (let i = 0; i < nodes.length; i++) {
        const n = nodes[i];
        const [x, y] = pos[i];
        const r = (disc ? 2.2 : 1.6) + n.flash * 2.6;
        ctx.fillStyle = rgba(pal.a, ((0.12 + n.flash * 0.25) * dim).toFixed(3));
        ctx.beginPath();
        ctx.arc(x, y, r * 2.6, 0, TAU);
        ctx.fill();
        ctx.fillStyle = n.flash > 0.4 ? rgba(pal.c, dim) : rgba(pal.c, 0.95 * dim);
        ctx.beginPath();
        ctx.arc(x, y, r, 0, TAU);
        ctx.fill();
      }
    }

    reset();
    return { reset, draw };
  }

  // Conectores de circuito: una traza por esquina, del borde al filo del núcleo, con
  // un codo a 45° como una pista de PCB. La geometría se calcula en píxeles con
  // la posición real del núcleo, así que sigue siendo exacta al girar el móvil.
  function layoutConnectors(svg, stage, core) {
    if (!svg) return;
    const s = stage.getBoundingClientRect();
    const c = core.getBoundingClientRect();
    const W = Math.max(1, Math.round(s.width));
    const H = Math.max(1, Math.round(s.height));
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const center = { x: c.left - s.left + c.width / 2, y: c.top - s.top + c.height / 2 };
    const radius = Math.min(c.width, c.height) / 2 + 10;
    const pad = Math.min(W, H) * 0.16;
    const corners = { tl: [0, 0], tr: [W, 0], bl: [0, H], br: [W, H] };
    for (const path of svg.querySelectorAll("path[data-corner]")) {
      const [cxr, cyr] = corners[path.dataset.corner] || corners.tl;
      const sx = cxr === 0 ? 1 : -1;
      const sy = cyr === 0 ? 1 : -1;
      const S = { x: cxr + sx * pad, y: cyr + sy * pad };
      const dx = S.x - center.x;
      const dy = S.y - center.y;
      const dist = Math.hypot(dx, dy) || 1;
      const E = { x: center.x + (dx / dist) * radius, y: center.y + (dy / dist) * radius };
      // Primero en horizontal, luego 45° hasta el núcleo; si no cabe, al revés.
      const ex = Math.sign(E.x - S.x) || sx;
      const ey = Math.sign(E.y - S.y) || sy;
      let K = { x: E.x - ex * Math.abs(E.y - S.y), y: S.y };
      if ((K.x - S.x) * ex < 0) K = { x: S.x, y: E.y - ey * Math.abs(E.x - S.x) };
      const ok = (K.y - S.y) * ey >= 0 && (K.x - S.x) * ex >= 0;
      const d = ok
        ? `M${S.x.toFixed(1)} ${S.y.toFixed(1)} L${K.x.toFixed(1)} ${K.y.toFixed(1)} L${E.x.toFixed(1)} ${E.y.toFixed(1)}`
        : `M${S.x.toFixed(1)} ${S.y.toFixed(1)} L${E.x.toFixed(1)} ${E.y.toFixed(1)}`;
      path.setAttribute("d", d);
      const tip = path.nextElementSibling;
      if (tip && tip.tagName === "circle") {
        tip.setAttribute("cx", E.x.toFixed(1));
        tip.setAttribute("cy", E.y.toFixed(1));
      }
    }
  }

  function createHoloCore(root, options) {
    const stage = (options && options.stage) || root;
    const part = (name) => root.querySelector(`[data-hud="${name}"]`);
    const globeCanvas = part("globe");
    const netCanvas = part("net");
    const ringOuter = part("ring-outer");
    const ringMid = part("ring-mid");
    const label = part("label");
    const fieldCanvas = stage.querySelector('[data-hud="field"]');
    const connectors = stage.querySelector('[data-hud="connectors"]');
    const energy = Array.from(stage.querySelectorAll('[data-hud="energy"]'));

    const state = { voice: "idle", volume: 0, volumeAt: 0, researching: false, mood: "" };
    const points = spherePoints(GLOBE_POINTS);
    const links = nearestPairs(points, GLOBE_NEIGHBOURS);
    const net = createNet(netCanvas, { shape: "disc", nodes: NET_NODES, neighbours: NET_NEIGHBOURS, pulseRate: 6 });
    const field = fieldCanvas
      ? createNet(fieldCanvas, { shape: "rect", nodes: FIELD_NODES, neighbours: FIELD_NEIGHBOURS, pulseRate: 2.5 })
      : null;

    let level = 0;
    let reach = 0;
    let angle = 0;
    let frames = 0;
    let lastFrame = performance.now();
    let fadeUntil = 0;
    let lastApplied = -1;
    let lastReach = -1;
    let running = false;
    let pal = readPalette(stage);
    let palTarget = pal;

    function active() {
      return state.voice === "listening" || state.voice === "speaking" || state.voice === "connecting";
    }

    // Prioridad: una búsqueda en curso tiñe todo de procesamiento aunque Jarvis
    // esté hablando o escuchando mientras tanto.
    function moodOf() {
      if (state.researching) return "thinking";
      if (state.voice === "speaking") return "speaking";
      if (state.voice === "listening" || state.voice === "connecting") return "listening";
      return "idle";
    }

    function applyMood() {
      const next = moodOf();
      if (next === state.mood) return;
      state.mood = next;
      stage.dataset.mood = next;
      if (stage !== root) root.dataset.mood = next;
      palTarget = readPalette(stage);
    }

    // Nivel objetivo: el volumen real si Vapi lo está mandando; si no, un pulso
    // sintético mientras habla (micrófono del navegador, TTS local) y una
    // respiración tenue mientras escucha. Sin voz activa, cero.
    function targetLevel(now) {
      if (!active()) return 0;
      if (now - state.volumeAt < 600) return state.volume;
      if (state.voice === "speaking") {
        return clamp(0.4 + 0.22 * Math.sin(now / 140) + 0.12 * Math.sin(now / 53), 0, 1);
      }
      return 0.12 + 0.05 * Math.sin(now / 400);
    }

    // Cuánto han crecido los circuitos desde las esquinas (0 = recogidos, 1 = tocan
    // el núcleo). Hablando llegan al máximo; escuchando siguen al volumen;
    // procesando fluyen; en reposo se recogen.
    function targetReach(now) {
      switch (state.mood) {
        case "speaking":
          return 1;
        case "thinking":
          return 0.6 + 0.3 * Math.sin(now / 320);
        case "listening":
          return state.voice === "connecting" ? 0.45 + 0.15 * Math.sin(now / 260) : 0.35 + level * 0.65;
        default:
          return 0;
      }
    }

    function tempo() {
      switch (state.mood) {
        case "thinking":
          return 2.4;
        case "speaking":
          return 1.4;
        case "listening":
          return 1;
        default:
          return 0.5;
      }
    }

    function applyRings(value) {
      // Cambios por debajo del 1 % no se ven y repintar el texto cada frame cuesta.
      if (Math.abs(value - lastApplied) < 0.01) return;
      lastApplied = value;
      const isActive = active();
      ringOuter.style.transform = `scale(${(1 + value * 0.22).toFixed(3)})`;
      ringOuter.style.opacity = isActive ? (0.4 + value * 0.6).toFixed(2) : "";
      ringMid.style.transform = `scale(${(1 + value * 0.1).toFixed(3)})`;
      label.style.textShadow =
        `0 0 ${Math.round(10 + value * 22)}px ${rgba(pal.a, (0.8 + value * 0.2).toFixed(2))}, ` +
        `0 0 ${Math.round(2 + value * 6)}px rgba(255, 255, 255, 0.55)`;
    }

    function applyReach(value) {
      if (Math.abs(value - lastReach) < 0.01) return;
      lastReach = value;
      const offset = (1 - value).toFixed(3);
      const opacity = (0.15 + value * 0.85).toFixed(3);
      for (const el of energy) {
        el.style.strokeDashoffset = offset;
        el.style.opacity = value < 0.02 ? "0" : opacity;
      }
      stage.style.setProperty("--reach", value.toFixed(3));
    }

    function frame(now) {
      if (!running) return;
      const dt = Math.min(0.05, (now - lastFrame) / 1000);
      lastFrame = now;
      frames += 1;
      // Suavizado exponencial independiente de los fps: ataque rápido, caída suave.
      level += (targetLevel(now) - level) * (1 - Math.exp(-dt * 12));
      const wantReach = targetReach(now);
      reach += (wantReach - reach) * (1 - Math.exp(-dt * (wantReach > reach ? 6 : 2.5)));
      if (pal !== palTarget) {
        const k = 1 - Math.exp(-dt * 4);
        pal = { a: mixRgb(pal.a, palTarget.a, k), b: mixRgb(pal.b, palTarget.b, k), c: mixRgb(pal.c, palTarget.c, k) };
        const settled = ["a", "b", "c"].every((key) => pal[key].every((v, i) => Math.abs(v - palTarget[key][i]) < 1));
        if (settled) pal = palTarget;
        lastApplied = -1;
      }
      const speed = active() ? 0.35 + level * 0.8 : 0.12;
      angle += speed * dt * (REDUCED_MOTION ? 0.3 : 1);
      const beat = tempo();

      const crossfading = now < fadeUntil;
      if (!state.researching || crossfading) {
        const { ctx, w, h } = fitCanvas(globeCanvas);
        drawGlobe(ctx, w, h, points, links, angle, level, pal);
      }
      if (state.researching || crossfading) {
        net.draw(dt, now / 1000, pal, beat);
      }
      // En reposo la red de fondo va a la mitad de frames: se mueve despacio y
      // apenas gasta; en cuanto hay humor, cada frame.
      if (field && (state.mood !== "idle" || frames % 2 === 0)) {
        field.draw(state.mood === "idle" ? dt * 2 : dt, now / 1000, pal, beat);
      }
      applyRings(level);
      applyReach(clamp(reach, 0, 1));
      global.requestAnimationFrame(frame);
    }

    function start() {
      if (running) return;
      running = true;
      lastFrame = performance.now();
      global.requestAnimationFrame(frame);
    }

    function stop() {
      running = false;
    }

    function setVoice(status) {
      state.voice = String(status || "idle");
      root.dataset.voice = state.voice;
      if (!active()) lastApplied = -1;
      applyMood();
    }

    function setVolume(value) {
      state.volume = clamp(Number(value) || 0, 0, 1);
      state.volumeAt = performance.now();
    }

    // El cruce visual (anillos que se apagan, red que entra) lo hace el CSS a partir
    // de data-researching; aquí solo se mantiene vivo el dibujo de ambos mientras dura.
    function setResearching(on) {
      const next = Boolean(on);
      if (next === state.researching) return;
      state.researching = next;
      root.dataset.researching = next ? "1" : "0";
      fadeUntil = performance.now() + FADE_MS;
      if (next) net.reset();
      applyMood();
    }

    function relayout() {
      layoutConnectors(connectors, stage, root);
    }

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stop();
      else start();
    });
    global.addEventListener("resize", relayout);
    if (global.ResizeObserver) new global.ResizeObserver(relayout).observe(root);

    applyMood();
    relayout();
    applyReach(0);
    start();
    return { setVoice, setVolume, setResearching, relayout, start, stop, state };
  }

  global.createHoloCore = createHoloCore;
})(window);
