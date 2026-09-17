from __future__ import annotations

import argparse

from jarvis.agent_core import extract_answer
from jarvis.supervisor import run_jarvis


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis v2.0 — Supervisor multi-agente")
    parser.add_argument("query", nargs="*", default=["¿Cuánto es 17 * 24?"])
    args = parser.parse_args()
    result = run_jarvis(" ".join(args.query), session_id="cli")
    print(extract_answer(result) or result)


if __name__ == "__main__":
    main()
