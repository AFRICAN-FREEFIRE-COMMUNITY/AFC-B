"""
tools/shoot.py - screenshot AFC pages at an exact viewport, with an optional signed-in session.

WHY THIS EXISTS
    The verification rule is that every UI change is walked on a DESKTOP and a ~390x844 MOBILE
    viewport before it is called done. The browser extension could not resize a maximized window
    (it reports success and changes nothing), so there was no way to honour the mobile half of that
    rule. Headless Chrome, driven over CDP, sets the viewport exactly and does not care what the
    developer's own browser window is doing.

    It is not a replacement for walking a flow by hand. It is the thing that makes "and the same
    page at 390px" a command rather than a negotiation.

USAGE
    python tools/shoot.py <out-dir> <url> [<url> ...] [--token=<auth_token>] [--width=390]
                                                      [--height=844] [--mobile] [--wait=3]
                                                      [--click="Tab" | --click="Open>>Choice"]
                                                      [--audit=<js, printed AFTER the clicks>]

    Every URL is shot at the given viewport and written to <out-dir>/<slug>.png. `--token` sets the
    same `auth_token` cookie AuthContext reads, so an admin page renders signed in.

HOW IT WORKS
    Launches `chrome.exe --headless=new --remote-debugging-port=<free port>` against a THROWAWAY
    profile directory, talks CDP over a websocket (websocket-client, already in backend/.venv), and
    uses Emulation.setDeviceMetricsOverride for the viewport. A throwaway profile matters: it keeps
    the developer's own cookies, extensions and proxy settings out of the picture, which is the
    whole reason this is more trustworthy than the extension for a layout check.

WHY --audit EXISTS
    Because this tool kept reporting success while doing nothing. It clicked a different option
    in a scrolling listbox and photographed the wrong page; it matched a control that was
    display:none at the other breakpoint and reported the text as missing; it 'clicked' a
    checkbox whose <label> it could not see and changed nothing. A screenshot cannot catch any
    of those. --audit runs JS AFTER the clicks and PRINTS the result, so a run asserts a fact
    about the rendered DOM ("3 linked, 9 plain") instead of leaving a human to squint at a
    picture. Do NOT write the answer into the page: React re-renders and wipes it. Return it.

WHY IT LIVES IN THIS REPO
    It shoots FRONTEND pages but runs on this repo's virtualenv, which already has websocket-client.
    It sat untracked at WEBSITE/tools/ for a day, which meant the one tool that makes the mandatory
    mobile pass possible was one `rm` from being lost. Version-controlled here instead.

CONNECTS TO
    Nothing in the app. It is a developer tool, invoked by hand or by an agent doing a verification
    pass. The pages it shoots are the ordinary frontend routes on the dev server:

        backend/.venv/Scripts/python.exe tools/shoot.py <outdir> <url>...             --width=390 --height=844 --mobile --token=<auth_token> --wait=9 --click="Settings" 
"""
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import websocket  # websocket-client, from backend/.venv

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_chrome(port, profile_dir):
    """Headless Chrome on a throwaway profile. --headless=new is the modern engine; the old one
    renders some flex layouts differently, which would defeat the point of a layout check."""
    args = [
        CHROME,
        "--headless=new",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        # Loopback is the whole point here; never let a system proxy get between us and the dev
        # server (a proxy that does not bypass localhost is exactly what broke the extension path).
        "--no-proxy-server",
        # Chrome 111+ rejects a CDP websocket whose Origin it does not recognise. websocket-client
        # always sends one, so without this every connect is a 403 handshake.
        "--remote-allow-origins=*",
        "--hide-scrollbars",
        "about:blank",
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1):
                return proc
        except Exception:
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("headless Chrome did not open its debugging port")


class CDP:
    """The smallest CDP client that does this job. One websocket, one incrementing id."""

    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=45)
        self.n = 0

    def send(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv())
            # CDP interleaves events with replies; anything without our id is an event.
            if message.get("id") == self.n:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def slugify(url):
    path = urllib.parse.urlparse(url).path.strip("/") or "root"
    return re.sub(r"[^a-zA-Z0-9]+", "-", path).strip("-")


def _clear_target(cdp):
    """Drop the data-shoot-target marker so a later click in the same run cannot match it."""
    cdp.send("Runtime.evaluate", expression=(
        "document.querySelectorAll('[data-shoot-target]')"
        ".forEach(function(n) { n.removeAttribute('data-shoot-target'); })"))


def main():
    args = [a for a in sys.argv[1:]]
    opts = {a.split("=", 1)[0]: (a.split("=", 1)[1] if "=" in a else True)
            for a in args if a.startswith("--")}
    rest = [a for a in args if not a.startswith("--")]
    if len(rest) < 2:
        print(__doc__)
        sys.exit(2)

    out_dir, urls = rest[0], rest[1:]
    width = int(opts.get("--width", 1440))
    height = int(opts.get("--height", 900))
    mobile = bool(opts.get("--mobile"))
    wait = float(opts.get("--wait", 3))
    token = opts.get("--token")
    os.makedirs(out_dir, exist_ok=True)

    port = free_port()
    profile_dir = tempfile.mkdtemp(prefix="afc-shoot-")
    proc = start_chrome(port, profile_dir)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
            targets = json.load(response)
        page = next(t for t in targets if t["type"] == "page")
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Network.enable")
        cdp.send("Runtime.enable")
        cdp.send("Emulation.setDeviceMetricsOverride", width=width, height=height,
                 deviceScaleFactor=2 if mobile else 1, mobile=mobile)
        if mobile:
            # A real phone UA, so anything that branches on it behaves the way it would on a phone.
            cdp.send("Emulation.setUserAgentOverride", userAgent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"))
            cdp.send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)

        for url in urls:
            if token:
                host = urllib.parse.urlparse(url).hostname
                cdp.send("Network.setCookie", name="auth_token", value=token,
                         domain=host, path="/")
            cdp.send("Page.navigate", url=url)
            time.sleep(wait)
            # --eval runs one expression before the shot, which is how a tab gets opened or a
            # dialog triggered. Client-side tabs keep their state in React rather than the URL, so
            # without this there is no way to photograph the second tab of a page.
            if opts.get("--eval"):
                cdp.send("Runtime.evaluate", expression=opts["--eval"], awaitPromise=True)
                time.sleep(1.5)
            # --click finds an element by its exact text and clicks it with REAL mouse events.
            # A synthetic .click() is not enough for the shadcn/Radix controls this codebase uses:
            # tabs and selects listen for pointerdown, so a click() alone leaves the tab unchanged
            # and you photograph the page you were already on without noticing.
            # A SEQUENCE, separated by ">>", so a control that only reveals its choices once it is
            # open can be driven: --click="Pick an event>>FFWS Africa 2026". A Radix Select renders
            # its options in a portal that does not exist until the trigger is pressed, so a single
            # click can never reach them, and the run silently photographs the unchanged page.
            for _step in [t.strip() for t in opts.get("--click", "").split(">>") if t.strip()]:
                target = json.dumps(_step)
                box = cdp.send("Runtime.evaluate", returnByValue=True, expression=f"""
                    (function() {{
                      var want = {target};
                      // TABS FIRST. A sidebar link and a tab can share a word ("Settings"),
                      // and matching the link navigates away and silently photographs a
                      // different page. Ask for the tab, then fall back to anything clickable.
                      // Exact first, then STARTS-WITH. A tab can carry a count badge inside the
                      // trigger, so "Approvals" renders as textContent "Approvals1" and an exact
                      // match silently photographs the tab you were already on.
                      // VISIBLE candidates only. A responsive layout keeps the other
                      // breakpoint's controls in the DOM as display:none, so at 390px the
                      // desktop tab strip still matches by text, measures zero, and the run
                      // reports 'nothing reads exactly ...' while the real target sits
                      // unexamined in a later pool. An <option> is exempt: its box belongs to
                      // a dropdown the OS draws, so it legitimately has no size of its own.
                      function shown(n) {{
                        if (n.tagName === 'OPTION') return true;
                        var r = n.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                      }}
                      function pick(nodes) {{
                        var vis = nodes.filter(shown);
                        return vis.find(function(n) {{ return n.textContent.trim() === want; }})
                            || vis.find(function(n) {{ return n.textContent.trim().startsWith(want); }});
                      }}
                      var el = pick([...document.querySelectorAll('[role="tab"]')])
                            || pick([...document.querySelectorAll('[role="option"]')])
                            || pick([...document.querySelectorAll('button,a')])
                            // A checkbox/radio row is usually a <label> wrapping the input, and
                            // clicking the label is exactly how a person toggles it.
                            || pick([...document.querySelectorAll('label,summary')])
                            // A native <select> tab strip (this codebase collapses tabs into
                            // one on a phone viewport). Last, so a real button always wins.
                            || pick([...document.querySelectorAll('option')]);
                      if (!el) return null;
                      // MARK IT, do not measure yet. Measured below, once the scroll settles.
                      el.setAttribute('data-shoot-target', '1');
                      el.scrollIntoView({{block: 'center'}});
                      return true;
                    }})()""")["result"].get("value")
                # An <option> cannot be clicked by coordinate: the dropdown is drawn by the OS,
                # not the page. Select it the way React expects instead, then skip the click.
                if box:
                    picked = cdp.send(
                        "Runtime.evaluate", returnByValue=True, expression="""
                        (function() {
                          var el = document.querySelector('[data-shoot-target]');
                          if (!el || el.tagName !== 'OPTION') return false;
                          el.removeAttribute('data-shoot-target');
                          var sel = el.closest('select');
                          if (!sel) return false;
                          var proto = Object.getPrototypeOf(sel);
                          var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                          setter.call(sel, el.value);
                          sel.dispatchEvent(new Event('input', {bubbles: true}));
                          sel.dispatchEvent(new Event('change', {bubbles: true}));
                          return true;
                        })()""")["result"].get("value")
                    if picked:
                        time.sleep(2)
                        continue
                if box:
                    # Re-measure AFTER the scroll, and check the element is really the topmost
                    # thing at its own centre. Without this a covered or freshly scrolled target
                    # is clicked at a stale point, which silently picks a DIFFERENT option and
                    # then photographs and audits the wrong page while reporting success.
                    time.sleep(0.4)
                    box = cdp.send(
                        "Runtime.evaluate", returnByValue=True, expression="""
                        (function() {
                          var el = document.querySelector('[data-shoot-target]');
                          if (!el) return null;
                          var r = el.getBoundingClientRect();
                          if (!r.width || !r.height) return null;
                          var x = r.left + r.width / 2, y = r.top + r.height / 2;
                          var hit = document.elementFromPoint(x, y);
                          var ok = !!hit && (el === hit || el.contains(hit) || hit.contains(el));
                          return {x: x, y: y, ok: ok,
                                  got: hit ? (hit.textContent || '').trim().slice(0, 40) : null};
                        })()""")["result"].get("value")
                    if box and not box.get("ok"):
                        print(f"   !! {_step!r} is not the topmost element at its own centre "
                              f"(found {box.get('got')!r}); clicking anyway")
                if not box:
                    print(f"   !! no VISIBLE element reads {_step!r} "
                          f"(hidden at this viewport, or not rendered yet)")
                elif box.get("ok"):
                    for kind in ("mousePressed", "mouseReleased"):
                        cdp.send("Input.dispatchMouseEvent", type=kind, x=box["x"], y=box["y"],
                                 button="left", clickCount=1)
                    _clear_target(cdp)
                    time.sleep(2)
                else:
                    # The target is not reachable by coordinate (on a phone viewport a Radix
                    # listbox puts its later options past the fold, so elementFromPoint at the
                    # option's centre is null). Drive the element itself with the pointer events
                    # Radix actually listens for. Coordinates stay the default because they are
                    # what proves a control is genuinely reachable by a person.
                    cdp.send("Runtime.evaluate", expression="""
                        (function() {
                          var el = document.querySelector('[data-shoot-target]');
                          if (!el) return;
                          var opts = {bubbles: true, cancelable: true, pointerId: 1,
                                      pointerType: 'mouse', button: 0, isPrimary: true};
                          ['pointerover', 'pointerenter', 'pointermove', 'pointerdown',
                           'mousedown', 'pointerup', 'mouseup', 'click'].forEach(function(t) {
                            var C = t.indexOf('pointer') === 0 ? PointerEvent : MouseEvent;
                            el.dispatchEvent(new C(t, opts));
                          });
                        })()""")
                    _clear_target(cdp)
                    time.sleep(2)
            # --audit runs AFTER the clicks and PRINTS its result, which is the difference that
            # matters: --eval runs before them, so it cannot see a table that only exists once a
            # control has been driven. Use it to assert a fact about the rendered DOM instead of
            # squinting at a screenshot, e.g. how many competitor cells are links:
            #   --audit='[...document.querySelectorAll("table tr")].filter(r=>r.querySelector("td")).length'
            # Do NOT write the answer into the page (React re-renders and wipes it); return it.
            if opts.get("--audit"):
                res = cdp.send("Runtime.evaluate", returnByValue=True, awaitPromise=True,
                               expression=opts["--audit"])
                val = res.get("result", {}).get("value")
                if val is None and "exceptionDetails" in res:
                    val = f"(threw) {res['exceptionDetails'].get('text')}"
                print(f"   AUDIT {val}")
            # The console, so a red error on the page is reported rather than silently photographed.
            errors = cdp.send("Runtime.evaluate", expression=(
                "JSON.stringify({h1: document.querySelector('h1')?.innerText || '',"
                " scrollW: document.documentElement.scrollWidth,"
                " clientW: document.documentElement.clientWidth,"
                " text: document.body.innerText.slice(0, 160)})"),
                returnByValue=True)
            info = json.loads(errors["result"]["value"])
            overflow = info["scrollW"] > info["clientW"] + 1
            shot = cdp.send("Page.captureScreenshot", format="png", captureBeyondViewport=True)
            name = f"{slugify(url)}-{'mobile' if mobile else 'desktop'}.png"
            with open(os.path.join(out_dir, name), "wb") as handle:
                handle.write(base64.b64decode(shot["data"]))
            flag = "  HORIZONTAL SCROLL" if overflow else ""
            print(f"{name}  {info['scrollW']}x  h1={info['h1'][:40]!r}{flag}")
            if not info["h1"] and "can’t be reached" in info["text"]:
                print(f"   !! {info['text'][:80]}")
        cdp.close()
    finally:
        proc.kill()
        shutil.rmtree(profile_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
