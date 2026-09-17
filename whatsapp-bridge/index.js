"use strict";

const axios = require("axios");
const express = require("express");
const qrcode = require("qrcode-terminal");
const { Client, LocalAuth } = require("whatsapp-web.js");

const PORT = Number(process.env.WHATSAPP_BRIDGE_PORT || 3000);
const JARVIS_WEBHOOK =
  process.env.JARVIS_WHATSAPP_WEBHOOK || "http://127.0.0.1:8000/webhooks/whatsapp-local";

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: "./.wwebjs_auth" }),
  puppeteer: {
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  },
});

let ready = false;

client.on("qr", (qr) => {
  console.log("Escanea este QR con WhatsApp (Dispositivos vinculados):");
  qrcode.generate(qr, { small: true });
});

client.on("authenticated", () => {
  console.log("WhatsApp autenticado. Sesión LocalAuth guardada.");
});

client.on("ready", () => {
  ready = true;
  console.log(`Puente WhatsApp listo. Express en :${PORT}`);
});

client.on("auth_failure", (msg) => {
  ready = false;
  console.error("Fallo de autenticación WhatsApp:", msg);
});

client.on("disconnected", (reason) => {
  ready = false;
  console.error("WhatsApp desconectado:", reason);
});

client.on("message", async (message) => {
  if (!message || message.fromMe) return;
  if (message.from === "status@broadcast") return;
  const body = typeof message.body === "string" ? message.body : "";
  if (!body.trim()) return;

  const payload = { from: message.from, body };
  try {
    await axios.post(JARVIS_WEBHOOK, payload, {
      timeout: 60000,
      headers: { "Content-Type": "application/json" },
    });
  } catch (err) {
    console.error("No se pudo reenviar a Jarvis:", err.message);
  }
});

const app = express();
app.use(express.json({ limit: "1mb" }));

app.get("/health", (_req, res) => {
  res.json({ ok: true, ready, provider: "whatsapp-web.js" });
});

app.post("/send", async (req, res) => {
  const to = String((req.body && req.body.to) || "").trim();
  const text = String((req.body && (req.body.message ?? req.body.body)) || "");
  if (!to || !text) {
    return res.status(400).json({ ok: false, error: "Faltan 'to' y/o 'message'." });
  }
  if (!ready) {
    return res.status(503).json({ ok: false, error: "WhatsApp no está listo. Escanea el QR." });
  }
  try {
    const chatId = to.includes("@") ? to : `${to.replace(/\D/g, "")}@c.us`;
    const sent = await client.sendMessage(chatId, text);
    return res.json({
      ok: true,
      channel: "whatsapp-web",
      to: chatId,
      id: sent && sent.id ? sent.id.id || sent.id._serialized : undefined,
    });
  } catch (err) {
    console.error("Error al enviar WhatsApp:", err.message);
    return res.status(500).json({ ok: false, error: err.message });
  }
});

app.listen(PORT, "127.0.0.1", () => {
  console.log(`Express /send escuchando en http://127.0.0.1:${PORT}`);
});

client.initialize();
