"""PROTOTYPE — drive the dashboard headless and record what happened.

Scenario, on a fresh fleet (./mkfleet.sh):
1. list workspaces, highlight `voip`, press `r`: the confirm dialog shows the plan with SHAs;
2. while the dialog is open, the upstream moves (a collaborator pushes);
3. press `y`. With `-y --no-fetch` the SHA confirmed is the SHA reset to.
4. same on `stock`, but with OW3_TUI_REFETCH=1 (the second run fetches again): it is not.
Screenshots land in ./shots/.
"""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import tui  # noqa: E402
from ow3 import HOME  # noqa: E402

SHOTS = HERE / "shots"
SHOTS.mkdir(exist_ok=True)


def upstream_push(repo: str, msg: str) -> str:
    up = HOME / "upstream" / repo
    with open(up / "f", "a") as f:
        f.write(msg + "\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "p", "GIT_AUTHOR_EMAIL": "p@p",
           "GIT_COMMITTER_NAME": "p", "GIT_COMMITTER_EMAIL": "p@p"}
    subprocess.run(["git", "-C", up, "commit", "-qam", msg], check=True, env=env)
    return subprocess.run(["git", "-C", up, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def head(ws: str, repo: str) -> str:
    return subprocess.run(["git", "-C", HOME / "ws" / ws / repo, "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


async def until(pilot, cond, what):
    for _ in range(200):
        await pilot.pause(0.05)
        if cond():
            return
    raise TimeoutError(what)


async def run_case(pilot, app, ws: str, idx: int, refetch: bool, tag: str):
    os.environ.pop("OW3_TUI_REFETCH", None)
    if refetch:
        os.environ["OW3_TUI_REFETCH"] = "1"
    app.query_one("#ws").index = idx
    await until(pilot, lambda: f"{ws}  " in str(app.query_one("#status").render()), "status")
    app.save_screenshot(str(SHOTS / f"{tag}-1-status.svg"))
    await pilot.press("r")
    await until(pilot, lambda: isinstance(app.screen, tui.Confirm), "dialog")
    plan = app.screen.plan
    app.save_screenshot(str(SHOTS / f"{tag}-2-confirm.svg"))
    confirmed = re.search(r"git -C community reset origin/main\s+# origin/main = (\w+)", plan).group(1)
    pushed = upstream_push("community", f"pushed while {ws}'s dialog was open")
    await pilot.press("y")
    await until(pilot, lambda: any("[exit" in str(l) for l in app.query_one("#log").lines), "exit")
    await pilot.pause(0.5)
    app.save_screenshot(str(SHOTS / f"{tag}-3-done.svg"))
    got = head(ws, "community")
    print(f"[{tag}] second run {'refetches' if refetch else '--no-fetch'}: "
          f"confirmed {confirmed}, upstream now {pushed[:10]}, community HEAD {got[:10]} -> "
          f"{'MATCHES what was confirmed' if got.startswith(confirmed) else 'DIFFERS from what was confirmed'}")


async def main():
    app = tui.Dashboard()
    async with app.run_test(size=(132, 40)) as pilot:
        await until(pilot, lambda: len(app.query_one("#ws").children) == 3, "workspaces")
        await run_case(pilot, app, "voip", 0, refetch=False, tag="a-no-fetch")
        await run_case(pilot, app, "stock", 1, refetch=True, tag="b-refetch")
        print("log pane:")
        for line in app.query_one("#log").lines:
            print("   ", line.text)


asyncio.run(main())
