#!/usr/bin/env python3
"""Stamp local script/stylesheet references in WebUI pages with a content hash,
e.g. src="js/course-order.js?v=1a2b3c4d", so browsers fetch a changed file
instead of reusing a cached copy that no longer matches the page. Run before
deploying; only rewrites the pages given.

  python3 scripts/stamp_assets.py WebUI/courses.html WebUI/event.html ...
"""

import hashlib
import re
import sys
from pathlib import Path

REF = re.compile(r'''((?:src|href)=")((?:js|css)/[^"?#]+\.(?:js|css))(?:\?v=[0-9a-f]+)?(")''')


def stamp(page: Path) -> bool:
    text = page.read_text()
    root = page.parent

    def sub(m):
        asset = root / m.group(2)
        if not asset.is_file():
            return m.group(0)
        h = hashlib.sha256(asset.read_bytes()).hexdigest()[:8]
        return f'{m.group(1)}{m.group(2)}?v={h}{m.group(3)}'

    new = REF.sub(sub, text)
    if new != text:
        page.write_text(new)
        return True
    return False


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 1
    for p in sys.argv[1:]:
        changed = stamp(Path(p))
        print(f"{p}: {'stamped' if changed else 'unchanged'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
