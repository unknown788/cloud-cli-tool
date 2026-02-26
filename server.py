"""
server.py

Entry point for the CloudLaunch API server.

Usage:
    python server.py                    # default: 0.0.0.0:8000, auto-reload off
    python server.py --port 8080        # custom port
    python server.py --reload           # enable auto-reload for development

Or directly via uvicorn:
    uvicorn api.app:app --reload --port 8000
"""

import uvicorn

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Start the CloudLaunch API server.")
    parser.add_argument("--host",   default="0.0.0.0",  help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port",   default=8000, type=int, help="Bind port (default: 8000)")
    parser.add_argument("--reload", action="store_true",   help="Enable auto-reload (dev mode)")
    args = parser.parse_args()

    print(f"\n  🚀  CloudLaunch API starting on http://{args.host}:{args.port}")
    print(f"  📖  Interactive docs → http://localhost:{args.port}/docs\n")

    uvicorn.run(
        "api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
