"""
main.py — convenience launcher

  python main.py ui      → start the Gradio chatbot (default: port 7860)
  python main.py server  → start the MCP server over stdio
  python main.py server --transport sse --port 8000  → MCP over HTTP/SSE
  python main.py query "What are the top products?"  → one-shot CLI query
"""

import sys


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    cmd, rest = sys.argv[1], sys.argv[2:]
    sys.argv = [sys.argv[0]] + rest  # let sub-parsers see only their args

    if cmd == "ui":
        import subprocess, sys as _sys
        raise SystemExit(
            subprocess.call(
                [_sys.executable, "-m", "chainlit", "run", "ui.py"] + rest
            )
        )

    elif cmd == "server":
        from server import main as server_main
        server_main()

    elif cmd == "query":
        from agent import run_query
        question = " ".join(rest) if rest else "Summarise the contents of this file."
        print(run_query(question))

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
