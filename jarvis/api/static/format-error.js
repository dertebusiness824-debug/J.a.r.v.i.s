/** Extrae un mensaje de error legible para la terminal del HUD (nunca "[object Object]"). */
(function (root) {
  function jsonSafe(value) {
    const seen = new WeakSet();
    return JSON.stringify(
      value,
      function (_key, nested) {
        if (nested instanceof Error) {
          return {
            name: nested.name,
            message: nested.message,
            cause: nested.cause,
            error: nested.error,
          };
        }
        if (nested && typeof nested === "object") {
          if (seen.has(nested)) return "[Circular]";
          seen.add(nested);
        }
        return nested;
      },
      2
    );
  }

  function formatError(error) {
    const seen = new WeakSet();

    function asText(value, depth) {
      if (value == null) return "";
      if (typeof value === "string") {
        const trimmed = value.trim();
        if (!trimmed || trimmed === "[object Object]") return "";
        return trimmed;
      }
      if (typeof value === "number" || typeof value === "boolean") return String(value);
      if (depth > 6) return "";
      if (typeof value !== "object") return String(value);
      if (seen.has(value)) return "";
      seen.add(value);

      for (const key of ["message", "error", "reason", "details", "detail", "msg", "cause"]) {
        if (value[key] == null) continue;
        const inner = asText(value[key], depth + 1);
        if (inner) return inner;
      }

      try {
        const json = jsonSafe(value);
        if (json && json !== "{}" && json !== "[]" && json !== "null") return json;
      } catch (_err) {}
      return "";
    }

    return asText(error, 0) || "error desconocido";
  }

  root.formatError = formatError;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { formatError };
  }
})(typeof window !== "undefined" ? window : globalThis);
