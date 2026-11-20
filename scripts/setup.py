"""Install this checkout and initialize an empty bridge for the current user."""
import argparse
import os
import subprocess
import sys
import venv
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-only", action="store_true", help="Skip optional MCP, A2A and Claude integration libraries")
    parser.add_argument("--venv", type=Path, help="Directory for the isolated Python environment")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.exit(1, "Use Python 3.11 or newer.\n")
    root = Path(__file__).resolve().parents[1]
    environment = (args.venv or root / ".venv").resolve()
    venv.EnvBuilder(with_pip=True).create(environment)
    bin_dir = environment / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / ("python.exe" if os.name == "nt" else "python")
    # A wheel install also makes the bundled skills available outside the clone.
    subprocess.run([str(python), "-m", "pip", "install", "." if args.core_only else ".[mcp,a2a,claude]"],
                   cwd=root, check=True)
    command = bin_dir / ("mhb.exe" if os.name == "nt" else "mhb")
    subprocess.run([str(command), "init"], check=True)
    subprocess.run([str(command), "self-test"], check=True)
    print(f"Bridge installed. Add {bin_dir} to your PATH or activate {environment}.")


if __name__ == "__main__":
    main()
