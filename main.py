"""
main.py — convenience launcher

  python main.py ui                    → start the Chainlit chatbot (port 8000)
  python main.py server                → start the sales analyst MCP server (stdio)
  python main.py server --transport sse --port 8000  → sales MCP over HTTP/SSE
  python main.py query "..."           → one-shot CLI sales query

  python main.py research-server       → start the cigar research MCP server (stdio)
  python main.py research-server --transport sse --port 8001
  python main.py research "Perdomo BBA Mad. Churchill" "Perdomo"  → research one cigar
  python main.py research --batch      → populate Cigar_Research.xlsx for all inventory
  python main.py research --batch --limit 20  → batch, first 20 uncached items only
  python main.py research --status     → show research cache coverage
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

    elif cmd == "research-server":
        from research_server import main as research_server_main
        research_server_main()

    elif cmd == "research":
        # Pass all remaining args to cigar_researcher's own argparse
        import sys as _sys
        _sys.argv = [_sys.argv[0]] + rest
        import cigar_researcher
        cigar_researcher  # triggers __main__ block via runpy
        import runpy
        runpy.run_module("cigar_researcher", run_name="__main__", alter_sys=True)

    else:
        print(f"Unknown command: {cmd!r}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
