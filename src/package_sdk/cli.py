"""Command-line entry point ``package-sdk`` (TAI-ADR-0062).

package-sdk init|add|workflow|check|test|lock|cache|plan|apply|export|describe|docs|migrate-expr …
package-sdk edit <operation> …    style-preserving edits of package files
package-sdk sandbox [packages…]   package tests by the core's code in-process
package-sdk mcp                   the author's MCP server over stdio (extra: mcp)
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from package_sdk import __version__

USAGE = """usage: package-sdk [--version] <command> [options]

commands:
  init           a new package: manifest, a process with its test, CI, README
  add            a new object of any catalog kind in a package
  workflow       regenerate the CI workflow of a package by the layout of this installation
  check          check packages: schema, references, core validators (--server: the core too)
  test           the test pyramid: check, skill contracts, integration tests, scenarios
  lock           pin the sources of an installation: commit and content hash (packages.lock)
  cache prune    remove git source checkouts no lock refers to (--all: the whole cache)
  plan           build the one installation plan of every kind and save it (--out)
  apply          apply exactly a saved plan, after a human yes
  export         export an object from a server into a package file
  describe       what an installation needs: variables, settings, nodes, ontologies (--env-example)
  docs           generated README sections of a package (--write, --check)
  migrate-expr   translate legacy expressions to CEL
  edit           style-preserving edits of package files (add-step, rename, set, …)
  sandbox        run package tests by the core's code in-process (extra: sandbox)
  image          Dockerfile of an integration image: observer or skills host
  mcp            the author's MCP server over stdio: pkg_check, pkg_test, pkg_plan, … (extra: mcp)
"""


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0
    if args[0] == "--version":
        print(f"package-sdk {__version__}")
        return 0
    command, rest = args[0], args[1:]
    if command == "edit":
        from package_sdk import edit

        return edit.main(rest)
    if command == "test":
        from package_sdk import testing

        return testing.main(rest)
    if command == "sandbox":
        from package_sdk import sandbox

        return sandbox.main(rest)
    if command == "image":
        from package_sdk import image

        return image.main(rest)
    if command == "mcp":
        from package_sdk import mcp

        return mcp.main(rest)
    from package_sdk import commands

    return commands.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
