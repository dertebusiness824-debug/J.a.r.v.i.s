const { createTranscriptFeed } = require("../jarvis/api/static/transcripts.js");

function assert(cond, msg) {
  if (!cond) {
    console.error("FAIL:", msg);
    process.exit(1);
  }
}

function lines(items) {
  return items.map((item) => item.line);
}

// Solo las transcripciones finales se imprimen, con el rol del snippet: user → Tú, resto → JARVIS.
let feed = createTranscriptFeed();
assert(feed.ingest({ type: "transcript", role: "user", transcriptType: "partial", transcript: "hola" }).length === 0, "parcial");
let out = feed.ingest({ type: "transcript", role: "user", transcriptType: "final", transcript: "Hola  Jarvis" });
assert(JSON.stringify(lines(out)) === JSON.stringify(["Tú: Hola Jarvis"]), lines(out));
assert(out[0].role === "user" && out[0].text === "Hola Jarvis", out[0]);
out = feed.ingest({ type: "transcript", role: "assistant", transcriptType: "final", transcript: "Te escucho, maestro." });
assert(lines(out)[0] === "JARVIS: Te escucho, maestro.", lines(out));

// Sin transcriptType (SDKs antiguos) también cuenta como final; la misma frase no se repite.
out = feed.ingest({ type: "transcript", role: "assistant", transcript: "Te escucho, maestro." });
assert(out.length === 0, "duplicado");
out = feed.ingest({ type: "transcript", role: "user", transcript: "Investiga a Ada" });
assert(lines(out)[0] === "Tú: Investiga a Ada", lines(out));

// Otros tipos de mensaje no producen líneas ni rompen.
assert(feed.ingest({ type: "speech-update", status: "started" }).length === 0, "speech-update");
assert(feed.ingest({ type: "function-call", functionCall: { name: "web_search" } }).length === 0, "function-call");
assert(feed.ingest(null).length === 0, "null");
assert(feed.ingest("texto").length === 0, "string");
assert(feed.ingest({ type: "transcript", role: "user", transcriptType: "final", transcript: "   " }).length === 0, "vacío");
assert(feed.ingest({ type: "transcript", role: "system", transcriptType: "final", transcript: "x" }).length === 0, "system");

// Respaldo: el asistente del panel solo envía conversation-update. Se imprime lo nuevo de cada historial.
feed = createTranscriptFeed();
out = feed.ingest({
  type: "conversation-update",
  conversation: [
    { role: "system", content: "Eres J.A.R.V.I.S." },
    { role: "assistant", content: "Sistemas en línea." },
    { role: "user", content: "¿Qué hora es?" },
  ],
});
assert(JSON.stringify(lines(out)) === JSON.stringify(["JARVIS: Sistemas en línea.", "Tú: ¿Qué hora es?"]), lines(out));
out = feed.ingest({
  type: "conversation-update",
  conversation: [
    { role: "system", content: "Eres J.A.R.V.I.S." },
    { role: "assistant", content: "Sistemas en línea." },
    { role: "user", content: "¿Qué hora es?" },
    { role: "assistant", content: "Son las doce, maestro." },
  ],
});
assert(JSON.stringify(lines(out)) === JSON.stringify(["JARVIS: Son las doce, maestro."]), lines(out));

// La forma `messages` (role bot/user, campo message) también vale.
feed = createTranscriptFeed();
out = feed.ingest({
  type: "conversation-update",
  messages: [
    { role: "bot", message: "Sistemas en línea.", time: 1 },
    { role: "user", message: "hola", time: 2 },
    { role: "tool_calls", toolCalls: [{ function: { name: "web_search" } }] },
  ],
});
assert(JSON.stringify(lines(out)) === JSON.stringify(["JARVIS: Sistemas en línea.", "Tú: hola"]), lines(out));

// Con las dos vías activas mandan las transcripciones: el historial no duplica.
feed = createTranscriptFeed();
feed.ingest({ type: "transcript", role: "assistant", transcriptType: "final", transcript: "Sistemas en línea." });
out = feed.ingest({
  type: "conversation-update",
  conversation: [
    { role: "assistant", content: "Sistemas en línea." },
    { role: "user", content: "hola" },
  ],
});
assert(out.length === 0, "conversation-update ignorado cuando hay transcripts");
assert(feed.stats().transcripts === 1 && feed.stats().printed === 1, JSON.stringify(feed.stats()));

// reset() arranca una llamada nueva: lo dicho antes vuelve a poder imprimirse.
feed.reset();
assert(feed.stats().printed === 0, "reset");
out = feed.ingest({ type: "transcript", role: "assistant", transcriptType: "final", transcript: "Sistemas en línea." });
assert(out.length === 1, "tras reset se imprime de nuevo");

console.log("ok");
