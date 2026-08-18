"""Headless starter shiny hunter.

Runs the full loop: load save -> open the bag -> read the roll -> if it is not the
starter you want, restart the game and roll again. Stops on a shiny.

  python hunt_cli.py --starter treecko
  python hunt_cli.py --starter treecko --force     # prove the loop end-to-end
  python hunt_cli.py --starter mudkip --max 50
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gamma.memory import GameProcess
from gamma.ue import UEGame
from gamma import layouts, versions
from gamma import input as gameinput
from gamma.hunt import StarterHunter, STARTERS


_CACHE = {}


def find_pid(name="PokemonEmerald.exe", min_mb=500):
    """The real game process, not the tiny launcher shim."""
    from gamma.memory import list_named_processes
    try:
        found = list_named_processes(name)
    except Exception:
        return None
    for proc in found:
        if proc["mem_mb"] >= min_mb:
            return proc["pid"]
    return None


def attach(hunter=None):
    """(Re)build every handle against the current game process.

    After a relaunch the PID, the window handle and every UObject address are
    different, so nothing from the previous session may be reused.
    """
    pid = find_pid()
    if not pid:
        return False
    gp = GameProcess(pid=pid).attach()
    game = UEGame(gp)
    # reuse the addresses found on the first attach: they are identical every
    # launch here, and rescanning costs ~10s of every hunt iteration
    game = game.discover_cached(_CACHE.get("ue")) if _CACHE.get("ue") else game.discover()
    _CACHE["ue"] = game.cache()
    hwnd = gameinput.find_window(pid)
    if not hwnd:
        return False
    gi = gameinput.GameInput(hwnd, pid=pid)
    if hunter is None:
        return gp, game, gi
    hunter.game = game
    hunter.gp = gp
    hunter.input = gi
    hunter.player = None
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--starter", default="treecko",
                    choices=[s["id"] for s in STARTERS])
    ap.add_argument("--force", action="store_true",
                    help="write the shiny flag each attempt (proves the loop)")
    ap.add_argument("--max", type=int, default=0, help="stop after N attempts")
    ap.add_argument("--version", default="ea")
    args = ap.parse_args()

    got = attach()
    if not got:
        sys.exit("no running game found - start PokemonEmerald.exe first")
    gp, game, gi = got
    spec = versions.get(args.version)

    h = StarterHunter(game, layouts.BY_VERSION.get(args.version, layouts.UE56), gi,
                      starter=args.starter, force_shiny=args.force,
                      on_event=on_event)
    h.reattach = attach
    h.exe_path = Path(spec.game_dir).parent / "PokemonEmerald.exe"
    if not h.exe_path.exists():
        h.exe_path = Path(spec.exe)

    print(f"hunting {args.starter}  force={args.force}  exe={h.exe_path.name}")
    t0 = time.time()
    stats = h.run(max_attempts=args.max)
    print("\n%s after %d attempts in %.0fs" % (
        "FOUND A SHINY" if stats.found else "stopped: " + (stats.error or stats.status),
        stats.attempts, time.time() - t0))
    gp.close()
    return 0 if stats.found else 1


def on_event(ev):
    k = ev.get("kind")
    st = ev.get("stats", {})
    if k == "result":
        print("  attempt %-3d %-10s frame=%s" % (
            st.get("attempts"), "SHINY" if ev.get("shiny") else "not shiny",
            st.get("shiny_frame")), flush=True)
    elif k in ("reset", "found", "error", "timeout"):
        print("  [%s] %s %s" % (k, st.get("status", ""),
                                ev.get("error") or ev.get("waiting_for") or ""), flush=True)


if __name__ == "__main__":
    sys.exit(main())
