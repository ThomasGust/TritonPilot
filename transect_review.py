"""Click-to-run launcher for the Transect Session Review applet.

Open this file in VSCode and hit Run (or ``python transect_review.py``) to launch
the offline review tool. It auto-loads the most recent recording; use the
**Session** dropdown (or Open folder… / Open file…) to pick another.

The implementation lives in ``tools/transect_review.py``; this is just a top-level
entry point so the Run button works without remembering ``python -m``.
"""

from tools.transect_review import main


if __name__ == "__main__":
    main()
