"""Format CSS and JavaScript assets, or check them without writing."""

import argparse
from pathlib import Path

import cssbeautifier
import jsbeautifier


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    changed = []
    directory = Path(__file__).resolve().parents[1] / "static"
    for path in sorted(directory.iterdir()):
        if path.suffix not in {".css", ".js"}:
            continue
        formatter = cssbeautifier if path.suffix == ".css" else jsbeautifier
        options = formatter.default_options()
        options.indent_size = 2
        original = path.read_text(encoding="utf-8")
        formatted = formatter.beautify(original, options).rstrip() + "\n"
        if formatted != original:
            changed.append(path.name)
            if not args.check:
                path.write_text(formatted, encoding="utf-8", newline="\n")
    print("Assets already formatted." if not changed else ", ".join(changed))
    return int(args.check and bool(changed))


if __name__ == "__main__":
    raise SystemExit(main())
