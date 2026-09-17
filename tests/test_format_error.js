const { formatError } = require("../jarvis/api/static/format-error.js");

function assert(cond, msg) {
  if (!cond) {
    console.error("FAIL:", msg);
    process.exit(1);
  }
}

const nested = { error: { message: "Daily: meeting token invalid" } };
assert(formatError(nested) === "Daily: meeting token invalid", nested);

const wrapped = new Error("[object Object]");
wrapped.error = { msg: "assistant not found" };
assert(formatError(wrapped) === "assistant not found", wrapped);

const objMessage = { message: { error: "mic denied" } };
assert(formatError(objMessage) === "mic denied", objMessage);

const eventLike = { type: "error", error: { message: { code: 401, detail: "unauthorized" } } };
const dumped = formatError(eventLike);
assert(!String(dumped).includes("[object Object]"), dumped);
assert(dumped.includes("unauthorized") || dumped.includes("401"), dumped);

const plain = { foo: 1, bar: "x" };
assert(formatError(plain).includes('"foo"'), plain);

assert(formatError("timeout") === "timeout", "string");
assert(formatError(null) === "error desconocido", "null");

const bogus = new Error("[object Object]");
assert(formatError(bogus) !== "[object Object]", bogus);

console.log("ok");
