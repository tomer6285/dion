"""Continue Watching actions using the existing fzf/questionary backends."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

import questionary
from prompt_toolkit.keys import Keys

from . import ui


HINT = "Enter: Play · Ctrl+D twice: Remove · Ctrl+Z: Undo · Tab: Info · Esc: Cancel"


class ContinueMenu:
    def __init__(self, items, history):
        self.items = list(items)
        self.history = history
        self.undo = []
        self.position = 0
        self.query = ""
        self.message = ""

    def remove(self, index):
        item = self.items[index]
        # Persist first: a failed write must not remove a row or create an undo.
        self.history.set_continue_hidden(item["imdb_id"], True)
        self.undo.append((index, item, self.query, self.position))
        self.items.pop(index)
        self.message = f"Removed {item['title']}. Ctrl+Z to undo."

    def restore(self):
        if not self.undo:
            return
        index, item, query, position = self.undo[-1]
        self.history.set_continue_hidden(item["imdb_id"], False)
        self.undo.pop()
        self.items.insert(index, item)
        self.query, self.position = query, position
        self.message = f"Restored {item['title']}."


def _fzf_action(menu):
    """Keep search, navigation, and confirmation inside fzf until an action completes.

    Only a private confirmation marker is used by the shell bindings. fzf quotes
    placeholders; title text is passed as a printf argument, never as shell code.
    """
    with tempfile.TemporaryDirectory(prefix="dion-continue-") as directory:
        marker = shlex.quote(str(Path(directory) / "confirm"))
        reset = f"rm -f {marker}"
        header = HINT + ("\n" + menu.message if menu.message else "")
        clear = f"execute-silent({reset})+change-header:{header}"
        # print() queues the action and visual cursor position before accepting.
        finish = lambda action: (
            f"printf 'print({action})+print(%s)+accept' \"$FZF_POS\""
        )
        remove = (
            "if [ -z {1} ] || [ {1} = -1 ]; then :; "
            f"elif test -f {marker}; then {finish('remove')}; "
            f"else touch {marker}; "
            "printf 'change-header:Remove %s from Continue Watching? "
            "Ctrl+D again confirms; Esc cancels.' {2..}; fi"
        )
        escape = (
            f"if test -f {marker}; then {reset}; "
            f"printf '%s' 'change-header:{HINT}'; else printf abort; fi"
        )
        bindings = [
            f"ctrl-d:transform:{remove}",
            f"esc:transform:{escape}",
            f"enter:transform:{finish('select')}",
            f"ctrl-z:transform:{finish('undo')}",
            f"tab:transform:{finish('info')}",
            f"change:{clear}", f"focus:{clear}",
            f"load:pos({menu.position + 1})",
        ]
        # Cancel even when a navigation key cannot move (e.g. a single match).
        for key, motion in (("up", "up"), ("down", "down"),
                            ("ctrl-p", "up"), ("ctrl-n", "down"),
                            ("pgup", "page-up"), ("pgdn", "page-down"),
                            ("home", "first"), ("end", "last")):
            bindings.append(f"{key}:execute-silent({reset})+{motion}+change-header:{HINT}")
        rows = [f"{i}\t{ui.format_history_choice(item).replace(chr(10), ' ').replace(chr(13), ' ')}" for i, item in enumerate(menu.items)]
        if not rows:
            rows = ["-1\tNo titles left. Ctrl+Z to undo, Esc to exit."]
        cmd = ["fzf", "--ansi", "--reverse", "--height=50%", "--cycle",
               "--no-multi", "--delimiter=\t", "--with-nth=2..", "--print-query",
               "--prompt=Continue watching > ", f"--header={header}",
               f"--query={menu.query}"]
        for binding in bindings:
            cmd.extend(["--bind", binding])
        result = subprocess.run(cmd, input="\n".join(rows), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=dict(os.environ, SHELL="/bin/sh"))
        # accept returns 1 without a match, but still prints queued actions
        # such as Undo. Only discard genuinely cancelled/empty output.
        if result.returncode == 130 or (result.returncode == 1 and not result.stdout.strip()):
            return "exit", None
        if result.returncode not in (0, 1):
            raise OSError(result.stderr.strip() or "fzf could not start")
        lines = result.stdout.splitlines()
        menu.query = lines[0]
        action = lines[1]
        menu.position = max(0, int(lines[2] or 1) - 1)
        index = int(lines[3].split("\t", 1)[0]) if len(lines) > 3 else -1
        return action, index if index >= 0 else None


def _questionary_action(menu):
    """Add actions to questionary's existing list, search, and scrolling UI."""
    pending = None
    choices = [questionary.Choice(ui.format_history_choice(item, ansi=False), value=i)
               for i, item in enumerate(menu.items)]
    if not choices:
        choices = [questionary.Choice("No titles left. Ctrl+Z to undo, Esc to exit.", value=-1)]

    q = questionary.select(
        "Continue watching", choices, use_jk_keys=False, use_search_filter=True,
        instruction=HINT,
    )
    control = next(c for c in q.application.layout.find_all_controls()
                   if hasattr(c, "get_pointed_at"))
    control.error_message = menu.message or None
    control.search_filter = menu.query
    control.pointed_at = min(menu.position, control.choice_count - 1)

    def current():
        value = control.get_pointed_at().value
        return value if value >= 0 else None

    def finish(event, action):
        menu.query = control.search_filter or ""
        menu.position = control.pointed_at
        event.app.exit(result=(action, current()))

    @q.application.key_bindings.add("c-d", eager=True)
    def remove(event):
        nonlocal pending
        index = current()
        if index is None:
            return
        if pending == index:
            finish(event, "remove")
        else:
            pending = index
            control.error_message = f"Remove {menu.items[index]['title']}? Ctrl+D again confirms; Esc cancels."

    @q.application.key_bindings.add("escape", eager=True)
    def escape(event):
        nonlocal pending
        if pending is not None:
            pending = None
            control.error_message = None
        elif control.search_filter:
            control.search_filter, control.pointed_at = "", 0
        else:
            finish(event, "exit")

    for key, action in (("c-z", "undo"), ("tab", "info"), ("enter", "select")):
        q.application.key_bindings.add(key, eager=True)(
            lambda event, action=action: finish(event, action))

    # Wrap the list's existing handlers so navigation and typing retain their
    # normal behavior while disarming confirmation, including at list boundaries.
    for binding in q.application.key_bindings.bindings:
        if binding.keys in ((Keys.ControlD,), (Keys.Escape,)):
            continue
        handler = binding.handler

        def cancel_then_handle(event, handler=handler):
            nonlocal pending
            pending = None
            control.error_message = None
            return handler(event)

        binding.handler = cancel_then_handle
    return q.unsafe_ask()


def select_continue(items, history, on_info=None):
    menu = ContinueMenu(items, history)
    # fzf's shell action bindings require a POSIX shell. Windows uses questionary.
    use_fzf = ui.has_fzf() and os.name != "nt"
    while True:
        try:
            action, index = (_fzf_action if use_fzf else _questionary_action)(menu)
        except (KeyboardInterrupt, EOFError):
            return None
        except OSError as error:
            if use_fzf:
                use_fzf = False
                menu.message = f"Using fallback selector: {error}"
                continue
            raise
        try:
            if action == "exit":
                return None
            if action == "undo":
                menu.restore()
            elif index is not None:
                if action == "select":
                    return menu.items[index]
                if action == "remove":
                    menu.remove(index)
                elif action == "info" and on_info:
                    ui.console.clear()
                    on_info(menu.items[index])
                    ui.wait_for_any_key()
                    ui.console.clear()
        except OSError as error:
            menu.message = f"Could not save change: {error}. Try again."
