/** Convierte los mensajes del SDK de Vapi en líneas "Tú: …" / "JARVIS: …" para la terminal.
 *
 * Vapi solo entrega al navegador los tipos marcados en "Client Messages" del
 * asistente. La fuente principal es `transcript` (una línea por transcripción
 * `final`); si el asistente del panel no la tiene activada pero sí
 * `conversation-update`, las líneas salen de ahí. Cada llamada empieza con
 * `reset()`; lo ya impreso no se repite aunque llegue por las dos vías.
 */
(function (root) {
  const USER_ROLES = new Set(["user", "human", "customer"]);
  const ASSISTANT_ROLES = new Set(["assistant", "bot", "ai", "jarvis"]);

  function speaker(role) {
    const key = String(role || "").toLowerCase();
    if (USER_ROLES.has(key)) return "user";
    if (ASSISTANT_ROLES.has(key)) return "assistant";
    return null; // system, tool, function: no se leen en la terminal
  }

  function createTranscriptFeed() {
    let seen = new Set();
    let conversationCursor = 0;
    let transcriptsSeen = 0;
    let printed = 0;

    function reset() {
      seen = new Set();
      conversationCursor = 0;
      transcriptsSeen = 0;
      printed = 0;
    }

    function line(role, text) {
      const who = speaker(role);
      const clean = String(text || "")
        .replace(/\s+/g, " ")
        .trim();
      if (!who || !clean) return null;
      const key = who + "|" + clean.toLowerCase();
      if (seen.has(key)) return null;
      seen.add(key);
      printed += 1;
      return { role: who, text: clean, line: (who === "user" ? "Tú:" : "JARVIS:") + " " + clean };
    }

    function fromTranscript(msg) {
      transcriptsSeen += 1;
      // Las parciales se reescriben palabra a palabra: solo la final es una frase.
      if (msg.transcriptType && msg.transcriptType !== "final") return [];
      const item = line(msg.role, msg.transcript);
      return item ? [item] : [];
    }

    function fromConversation(msg) {
      // Respaldo: llega el historial completo, se imprime solo lo nuevo. Si el
      // asistente sí envía `transcript`, esta vía se ignora para no duplicar.
      if (transcriptsSeen > 0) return [];
      const convo = Array.isArray(msg.conversation)
        ? msg.conversation
        : Array.isArray(msg.messages)
          ? msg.messages
          : [];
      const out = [];
      for (let i = conversationCursor; i < convo.length; i++) {
        const entry = convo[i] || {};
        const text = typeof entry.content === "string" ? entry.content : entry.message;
        const item = line(entry.role, text);
        if (item) out.push(item);
      }
      conversationCursor = convo.length;
      return out;
    }

    function ingest(msg) {
      if (!msg || typeof msg !== "object") return [];
      const type = String(msg.type || "");
      if (type === "transcript") return fromTranscript(msg);
      if (type === "conversation-update") return fromConversation(msg);
      return [];
    }

    function stats() {
      return { transcripts: transcriptsSeen, printed };
    }

    return { ingest, reset, stats };
  }

  root.createTranscriptFeed = createTranscriptFeed;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { createTranscriptFeed };
  }
})(typeof window !== "undefined" ? window : globalThis);
