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

  python main.py social-server         → start the social intel MCP server (stdio, port 8002)
  python main.py social-server --transport sse --port 8002
  python main.py social "Perdomo BBA Mad. Churchill" "Perdomo"  → research one cigar's reputation
  python main.py social --batch        → populate Cigar_Social.xlsx for all inventory
  python main.py social --batch --limit 20  → batch, first 20 uncached items only
  python main.py social --buzz         → refresh Cigar_Buzz.xlsx (new/upcoming cigars)
  python main.py social --status       → show cache and API configuration status

  python main.py order-server          → start the ordering agent MCP server (stdio, port 8003)
  python main.py order-server --transport sse --port 8003
  python main.py order                         → 30-day horizon, $5,000 budget, 10% new cigars
  python main.py order --horizon 7             → 7-day horizon (~$1,167 default budget)
  python main.py order --horizon 90            → 90-day horizon (~$15,000 default budget)
  python main.py order --budget 3000           → explicit $3,000 budget
  python main.py order --new-cigar-pct 20      → 20% of budget for new cigars, 80% restock
  python main.py order --new-cigar-budget 500  → $500 for new cigars, rest for restock
  python main.py order --refresh               → refresh buzz feed then run ordering analysis
  python main.py order --slots 5               → recommend 5 new SKUs (default 3)
  python main.py order --craziness 7           → more adventurous branching (default 5)
  python main.py order --max-price 22          → filter out cigars above $22/stick
  python main.py order --json                  → output raw JSON

  python main.py inventory-server      → start the inventory agent MCP server (stdio, port 8004)
  python main.py inventory-server --transport sse --port 8004
  python main.py inventory --low-stock          → items selling but low/out of stock
  python main.py inventory --stockout-risk      → items likely to run out in 30 days
  python main.py inventory --slow-movers        → excess stock candidates for discounting
  python main.py inventory --discontinue        → dead stock candidates to discontinue
  python main.py inventory --profitable         → top profitable items to push
  python main.py inventory --all                → run all five analyses
  python main.py inventory --category "Cigars" → filter by category (default: Cigars)
  python main.py inventory --summarize          → add Claude natural-language interpretation
  python main.py inventory --json               → output raw JSON

  python main.py verify-inventory      → build Smoke_Shoppe_Inventory_Verified.xlsx
                                         (zeros items never in sales + clamps negatives)
  python main.py verify-inventory --summary   → print stats only, no file written
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
        # Default to 0.0.0.0 so the reverse proxy can reach us.
        # Override with --host or --port in `rest` if needed.
        base_args = ["--host", "0.0.0.0", "--port", "8000"]
        # Let explicit flags in `rest` win over the defaults
        if "--host" in rest:
            base_args = [a for a in base_args if a not in ("--host", "0.0.0.0")]
        if "--port" in rest:
            base_args = [a for a in base_args if a not in ("--port", "8000")]
        raise SystemExit(
            subprocess.call(
                [_sys.executable, "-m", "chainlit", "run", "ui.py"] + base_args + rest
            )
        )

    elif cmd == "server":
        from sales_server import main as server_main
        server_main()

    elif cmd == "query":
        from sales_agent import run_query
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

    elif cmd == "social-server":
        from social_intel_server import main as social_server_main
        social_server_main()

    elif cmd == "social":
        import sys as _sys
        _sys.argv = [_sys.argv[0]] + rest
        import runpy
        runpy.run_module("social_intel_agent", run_name="__main__", alter_sys=True)

    elif cmd == "order-server":
        from ordering_server import main as order_server_main
        order_server_main()

    elif cmd == "order":
        import sys as _sys
        _sys.argv = [_sys.argv[0]] + rest
        import runpy
        runpy.run_module("ordering_agent", run_name="__main__", alter_sys=True)

    elif cmd == "inventory-server":
        from inventory_server import main as inventory_server_main
        inventory_server_main()

    elif cmd == "inventory":
        import sys as _sys
        _sys.argv = [_sys.argv[0]] + rest
        import runpy
        runpy.run_module("inventory_agent", run_name="__main__", alter_sys=True)

    elif cmd == "verify-inventory":
        from inventory_verifier import main as verify_main
        verify_main()

    else:
        print(f"Unknown command: {cmd!r}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
