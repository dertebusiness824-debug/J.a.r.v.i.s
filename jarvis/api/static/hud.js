/* HoloCore: el núcleo holográfico de J.A.R.V.I.S.

   Componente autónomo. La página le pasa el estado de voz (idle, connecting,
   listening, speaking), el nivel de volumen y si hay una investigación en curso;
   él se ocupa de la esfera de partículas, de los anillos que reaccionan al sonido
   y de la red neuronal que sustituye a los anillos en modo investigación. No sabe
   nada de Vapi ni del backend: así se puede probar y cambiar por separado.

   Rendimiento en móvil: un solo requestAnimationFrame, canvas con el DPR limitado
   a 2, sin shadowBlur ni filtros por frame, y todo se detiene con la pestaña
   oculta. La rotación de los anillos va por CSS y el escalado por transform en un
   contenedor aparte, para que ninguna de las dos pise a la otra. */
(function (global) {
  "use strict";

  const TAU = Math.PI * 2;
  // En pantallas 3x el canvas triplica los píxeles sin ganar nitidez que se vea.
  const DPR_CAP = 2;
  const GLOBE_POINTS = 220;
  const GLOBE_NEIGHBOURS = 2;
  const NET_NODES = 34;
  const NET_NEIGHBOURS = 3;
  // Lo que dura el cruce entre anillos y red (debe coincidir con duration-700).
  const FADE_MS = 750;
  const REDUCED_MOTION = Boolean(
    global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches
  );

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
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

  function drawGlobe(ctx, w, h, points, links, angle, level) {
    ctx.clearRect(0, 0, w, h);
    const cx = w / 2;
    const cy = h / 2;
    const radius = Math.min(w, h) * (0.355 + level * 0.02);
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
      ctx.strokeStyle = `rgba(56, 189, 248, ${((0.05 + depth * 0.22) * linkBoost).toFixed(3)})`;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }

    for (let i = 0; i < projected.length; i++) {
      const p = projected[i];
      const front = (p.z + 1) / 2;
      ctx.fillStyle =
        front > 0.5
          ? `rgba(165, 243, 252, ${(0.35 + front * 0.6).toFixed(3)})`
          : `rgba(59, 130, 246, ${(0.15 + front * 0.4).toFixed(3)})`;
      ctx.beginPath();
      ctx.arc(p.x, p.y, 0.8 + front * 1.6, 0, TAU);
      ctx.fill();
    }
  }

  // Red neuronal del modo investigación: nodos a la deriva dentro del disco,
  // enlaces que titilan y pulsos de "electricidad" que viajan de nodo a nodo.
  function createNet(canvas) {
    let nodes = [];
    let links = [];
    let pulses = [];

    function reset() {
      nodes = [];
      for (let i = 0; i < NET_NODES; i++) {
        const r = 0.42 * Math.sqrt(Math.random());
        const a = Math.random() * TAU;
        nodes.push({
          x: Math.cos(a) * r,
          y: Math.sin(a) * r,
          vx: (Math.random() - 0.5) * 0.05,
          vy: (Math.random() - 0.5) * 0.05,
          flash: 0,
          z: 0,
        });
      }
      links = nearestPairs(nodes, NET_NEIGHBOURS);
      pulses = [];
    }

    function draw(dt, t) {
      const { ctx, w, h } = fitCanvas(canvas);
      ctx.clearRect(0, 0, w, h);
      const cx = w / 2;
      const cy = h / 2;
      const scale = Math.min(w, h);

      for (const n of nodes) {
        n.x += n.vx * dt;
        n.y += n.vy * dt;
        if (Math.hypot(n.x, n.y) > 0.44) {
          n.vx *= -1;
          n.vy *= -1;
        }
        n.flash = Math.max(0, n.flash - dt * 2.5);
      }

      ctx.lineWidth = 1;
      for (let i = 0; i < links.length; i++) {
        const a = nodes[links[i][0]];
        const b = nodes[links[i][1]];
        const flicker = 0.18 + 0.12 * Math.sin(t * 3 + i * 1.7);
        ctx.strokeStyle = `rgba(59, 130, 246, ${flicker.toFixed(3)})`;
        ctx.beginPath();
        ctx.moveTo(cx + a.x * scale, cy + a.y * scale);
        ctx.lineTo(cx + b.x * scale, cy + b.y * scale);
        ctx.stroke();
      }

      // Unos seis pulsos por segundo, con la probabilidad ligada al tiempo real
      // para que la densidad no dependa de los fps del dispositivo.
      if (links.length && Math.random() < dt * 6) {
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
        pulse.pos += pulse.speed * dt;
        const a = nodes[pulse.from];
        const b = nodes[pulse.to];
        if (pulse.pos >= 1) {
          b.flash = 1;
          pulses.splice(i, 1);
          continue;
        }
        const tail = Math.max(0, pulse.pos - 0.18);
        const x1 = cx + (a.x + (b.x - a.x) * tail) * scale;
        const y1 = cy + (a.y + (b.y - a.y) * tail) * scale;
        const x2 = cx + (a.x + (b.x - a.x) * pulse.pos) * scale;
        const y2 = cy + (a.y + (b.y - a.y) * pulse.pos) * scale;
        const glow = ctx.createLinearGradient(x1, y1, x2, y2);
        glow.addColorStop(0, "rgba(34, 211, 238, 0)");
        glow.addColorStop(1, "rgba(165, 243, 252, 0.95)");
        ctx.strokeStyle = glow;
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
        ctx.fillStyle = "rgba(236, 254, 255, 1)";
        ctx.beginPath();
        ctx.arc(x2, y2, 1.8, 0, TAU);
        ctx.fill();
      }

      // El halo es un segundo círculo tenue: shadowBlur sería mucho más caro en móvil.
      for (const n of nodes) {
        const x = cx + n.x * scale;
        const y = cy + n.y * scale;
        const r = 2.2 + n.flash * 2.6;
        ctx.fillStyle = `rgba(34, 211, 238, ${(0.12 + n.flash * 0.25).toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(x, y, r * 2.6, 0, TAU);
        ctx.fill();
        ctx.fillStyle = n.flash > 0.4 ? "rgba(236, 254, 255, 1)" : "rgba(103, 232, 249, 0.95)";
        ctx.beginPath();
        ctx.arc(x, y, r, 0, TAU);
        ctx.fill();
      }
    }

    reset();
    return { reset, draw };
  }

  function createHoloCore(root) {
    const part = (name) => root.querySelector(`[data-hud="${name}"]`);
    const globeCanvas = part("globe");
    const netCanvas = part("net");
    const ringOuter = part("ring-outer");
    const ringMid = part("ring-mid");
    const label = part("label");

    const state = { voice: "idle", volume: 0, volumeAt: 0, researching: false };
    const points = spherePoints(GLOBE_POINTS);
    const links = nearestPairs(points, GLOBE_NEIGHBOURS);
    const net = createNet(netCanvas);

    let level = 0;
    let angle = 0;
    let lastFrame = performance.now();
    let fadeUntil = 0;
    let lastApplied = -1;
    let running = false;

    function active() {
      return state.voice === "listening" || state.voice === "speaking" || state.voice === "connecting";
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

    function applyRings(value) {
      // Cambios por debajo del 1 % no se ven y repintar el texto cada frame cuesta.
      if (Math.abs(value - lastApplied) < 0.01) return;
      lastApplied = value;
      const isActive = active();
      ringOuter.style.transform = `scale(${(1 + value * 0.16).toFixed(3)})`;
      ringOuter.style.opacity = isActive ? (0.45 + value * 0.55).toFixed(2) : "";
      ringMid.style.transform = `scale(${(1 + value * 0.07).toFixed(3)})`;
      label.style.textShadow =
        `0 0 ${Math.round(10 + value * 22)}px rgba(6, 182, 212, ${(0.8 + value * 0.2).toFixed(2)}), ` +
        `0 0 ${Math.round(2 + value * 6)}px rgba(255, 255, 255, 0.55)`;
    }

    function frame(now) {
      if (!running) return;
      const dt = Math.min(0.05, (now - lastFrame) / 1000);
      lastFrame = now;
      // Suavizado exponencial independiente de los fps: ataque rápido, caída suave.
      level += (targetLevel(now) - level) * (1 - Math.exp(-dt * 12));
      const speed = active() ? 0.35 + level * 0.8 : 0.12;
      angle += speed * dt * (REDUCED_MOTION ? 0.3 : 1);

      const crossfading = now < fadeUntil;
      if (!state.researching || crossfading) {
        const { ctx, w, h } = fitCanvas(globeCanvas);
        drawGlobe(ctx, w, h, points, links, angle, level);
      }
      if (state.researching || crossfading) {
        net.draw(dt, now / 1000);
      }
      applyRings(level);
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
    }

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stop();
      else start();
    });

    start();
    return { setVoice, setVolume, setResearching, start, stop, state };
  }

  global.createHoloCore = createHoloCore;
})(window);
