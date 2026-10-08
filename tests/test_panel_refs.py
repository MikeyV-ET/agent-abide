import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))
from chat_widgets import short_ref, ToolCallPanel, ToolRunStack, tool_run_window, InterjectionBlock, is_control_user_line, system_alert_body

def test_short_ref_call_id():
    assert short_ref("call-a6f1a470-70ef-4508") == "id:a6f1a470"

def test_short_ref_bell():
    assert short_ref("bell_32ac0874") == "bell:32ac0874"

def test_tool_panel_title_includes_ref():
    p = ToolCallPanel("call-deadbeef-1234", "tool")
    p.render()
    assert "id:deadbeef" in p.border_title
    body = p.render()
    assert "cite id:deadbeef" in body.plain

def test_collapsed_shows_command_input():
    p = ToolCallPanel("call-deadbeef-1234", "run_terminal_command")
    p.set_command("python3 - << 'PY'\nprint(1)\nprint(2)\nprint(3)\nprint(4)\nprint(5)\nPY")
    body = p.render()
    plain = body.plain
    assert "print(1)" in plain
    assert "+1 lines" in plain or "expand" in plain
    assert "no output yet" not in plain


def test_interjection_ref():
    b = InterjectionBlock("[eric (via tui) (id=bell_32ac0874, ts=Tue)] hello")
    plain = b.render().plain
    assert "bell:32ac0874" in plain
    assert "interjection" in plain.lower()


def test_continue_is_control_not_eric():
    assert is_control_user_line(
        "[continue (id=cont_xwktr3sr, ts=Tue Oct 06 18:34 PDT)] Your turn ended. You may continue, delay, or stand by."
    )
    assert is_control_user_line("[aa.control] delay 600s")
    assert is_control_user_line("[delay 600]")
    assert is_control_user_line("You have hit your session limit · resets 8:40pm")


def test_eric_turn_with_footer_is_not_control():
    text = (
        "<eric (via tui)> drafts vanish on standup\n"
        "[Context left 8.6k till autocompaction | compaction available | tui | Tue Oct 06 18:34 PDT]"
    )
    assert not is_control_user_line(text)
    assert not is_control_user_line("[background] eric_tui in #standup (reply_via=irc outbox): hi")
    assert not is_control_user_line("[IRC #standup from eric_tui] hello")


def test_continue_paints_as_system_alert_not_user_bubble():
    from chat_model import SpeechItem
    from paint_mount import widget_for_item
    from chrome_widgets import SystemAlert
    from chat_widgets import UserMessage
    w = widget_for_item(SpeechItem(kind="user", text="[continue (id=x)] Your turn ended. stand by."))
    assert isinstance(w, SystemAlert)
    w2 = widget_for_item(SpeechItem(kind="user", text="<eric (via tui)> hello"))
    assert isinstance(w2, UserMessage)


def test_localmail_is_control_not_eric_talking_about_mail():
    mail = (
        "[localmail (id=bell_37_sq1oo, ts=Thu Aug 13 16:47 PDT, reply_via=localmail outbox)] "
        "[localmail] Mail from Squiggy: noted"
    )
    assert is_control_user_line(mail)
    assert not is_control_user_line("<eric (via tui)> what's the deal with localmail?")


def test_localmail_paints_as_system_alert():
    from chat_model import SpeechItem
    from paint_mount import widget_for_item
    from chrome_widgets import SystemAlert
    from chat_widgets import UserMessage
    w = widget_for_item(SpeechItem(kind="user", text="[localmail] Mail from Squiggy: hi"))
    assert isinstance(w, SystemAlert)
    assert not isinstance(w, UserMessage)


def test_center_band_is_15_70_15():
    from nav_widgets import layout_center_band, SYSTEM_SIDE_PCT
    from rich.text import Text
    total = 100
    out = layout_center_band(Text("hello"), total=total)
    plain = out.plain
    side = total * SYSTEM_SIDE_PCT // 100
    assert SYSTEM_SIDE_PCT == 15
    assert plain.startswith(" " * side)
    assert plain[side:side + 5] == "hello"


def test_completed_tool_is_one_liner():
    p = ToolCallPanel("call-deadbeef-1234", "run_terminal_command")
    p.set_command("python3 - << 'PY'\nprint(1)\nprint(2)\nprint(3)\nprint(4)\nprint(5)\nPY")
    p.set_status("completed")
    assert p.density == "one"
    plain = p.render().plain
    assert "print(1)" in plain
    assert plain.count("\n") <= 1


def test_running_tool_stays_snippet():
    p = ToolCallPanel("call-deadbeef-1234", "run_terminal_command")
    p.set_command("python3 - << 'PY'\nprint(1)\nprint(2)\nprint(3)\nprint(4)\nprint(5)\nPY")
    assert p.density == "snippet"
    plain = p.render().plain
    assert "print(1)" in plain
    assert "expand" in plain


def test_tool_run_window_keeps_live_and_hides_oldest():
    class P:
        def __init__(self, st):
            self.tool_status = st
    done = [P("completed") for _ in range(10)]
    live = [P("in_progress")]
    hidden, vis = tool_run_window(done + live, max_rows=8, snippet_cost=5)
    assert hidden == 7
    assert len([x for x in vis if x.tool_status == "completed"]) == 3
    assert vis[-1].tool_status == "in_progress"


def test_mount_items_groups_consecutive_tools():
    from chat_model import ToolItem, SpeechItem
    from paint_mount import mount_items
    from chat_widgets import AgentMessage

    class Dummy:
        def __init__(self):
            self.children = []
        def mount(self, w, before=None):
            if before is None:
                self.children.append(w)
            else:
                self.children.insert(self.children.index(before), w)

    s = Dummy()
    items = [
        ToolItem("a", title="t", status="completed"),
        ToolItem("b", title="t", status="completed"),
        SpeechItem(kind="agent", text="hi"),
        ToolItem("c", title="t", status="running"),
    ]
    mount_items(s, items)
    assert isinstance(s.children[0], ToolRunStack)
    assert s.children[0].panel_count() == 2
    assert isinstance(s.children[1], AgentMessage)
    assert isinstance(s.children[2], ToolRunStack)
    assert s.children[2].panel_count() == 1


def test_painted_bubble_hugs_short_human_on_the_right():
    from nav_widgets import layout_painted_bubble
    from rich.text import Text
    out = layout_painted_bubble(Text("hi"), is_human=True, total=80, border="#fe8019")
    lines = out.plain.split("\n")
    assert lines[0].strip().startswith("╭")
    assert lines[0].rstrip().endswith("╮")
    assert lines[0].startswith(" " * 20)
    assert "hi" in out.plain


def test_painted_bubble_agent_sits_left():
    from nav_widgets import layout_painted_bubble
    from rich.text import Text
    out = layout_painted_bubble(Text("hello"), is_human=False, total=80, border="#83a598")
    first = out.plain.split("\n")[0]
    assert first.lstrip().startswith("╭")
    assert first.startswith("╭") or first.startswith(" ")
    # not shoved to the right
    assert len(first) - len(first.lstrip()) < 5


def test_compaction_complete_is_control_not_eric():
    assert is_control_user_line(
        "[Compaction complete. Context reduced from 400379 to 11043 tokens. You are resuming from a compacted context. Follow your boot protocol.]"
    )
    assert is_control_user_line(
        "This session is being continued from a previous conversation that ran out of context. Follow your boot protocol."
    )
    assert not is_control_user_line("<eric (via tui)> you're compacting as well at 205k")


def test_ephact_table_does_not_balloon_at_bubble_width():
    from rich.markdown import Markdown as RichMarkdown
    from chat_widgets import _flatten_to_text, AgentMessage
    body = (
        "| Class | n | 4.6 / 4.7 | Grade | What it does |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| A | 124 | 61 / 63 | pass | Gold return on all 3 tests |\n"
        "| B | 2 | 2 / 0 | fail | Caps the switch loop |\n"
    )
    formatted = AgentMessage._format_ephacts(
        f'<ephact type="table" title="26.2 functional classes">\n{body}</ephact>'
    )
    assert formatted.strip().startswith("**📌")
    assert not formatted.lstrip().startswith(">")
    md = _flatten_to_text(RichMarkdown(formatted), width=56)
    nlines = md.plain.count("\n") + 1
    assert nlines < 16, nlines
