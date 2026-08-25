"""Frozen Windows GUI entry point.

This small launcher must stay import-light.  In particular, freeze_support has to
run before pyannote/torch can create worker processes in the packaged build.
"""

from __future__ import annotations

from multiprocessing import freeze_support


def main() -> None:
    freeze_support()

    from app.gui.entrypoint import entrypoint

    entrypoint()


if __name__ == "__main__":
    main()
