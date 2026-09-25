"""Check recipient resolution rejects self, groups, and ambiguous contacts."""

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from auto_reply.wx import uia_send
from auto_reply.wx.uia_send import SendError, display_name


def rejects(con, talker):
    try:
        display_name(con, talker)
    except SendError:
        return
    raise AssertionError(f"unexpectedly accepted {talker}")


def main():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE contacts(wxid TEXT, nickname TEXT, remark TEXT, alias TEXT)")
    con.executemany("INSERT INTO contacts VALUES(?,?,?,?)", [
        ("a", "Alice", "", ""),
        ("b", "Bob", "", ""),
        ("c", "Alice", "", ""),
        ("g@chatroom", "Group", "", ""),
    ])
    assert display_name(con, "b") == "Bob"
    for talker in ("a", "c", "g@chatroom", "filehelper", "missing"):
        rejects(con, talker)

    class Pattern:
        IsReadOnly = False
        Value = ""

        def SetValue(self, text):
            self.Value = text

    class Input:
        Name = "Bob"

        def __init__(self):
            self.pattern = Pattern()
            self.keys = []

        def GetValuePattern(self):
            return self.pattern

        def SetFocus(self):
            pass

        def SendKeys(self, keys, **_):
            self.keys.append(keys)

    class Focus:
        AutomationId = "chat_input_field"
        Name = "Bob"

    box = Input()
    with patch.object(uia_send.auto, "GetFocusedControl", return_value=Focus()):
        uia_send._press_enter_once(box, "Bob", "hello")
    assert box.keys == ["{Enter}"]

    box = Input()
    box.pattern.Value = "existing draft"
    try:
        uia_send._press_enter_once(box, "Bob", "hello")
    except SendError:
        pass
    else:
        raise AssertionError("existing draft was overwritten")
    assert not box.keys

    chat = object()
    with (patch.object(uia_send, "display_name", return_value="Bob"),
          patch.object(uia_send, "_window", return_value=(123, 456)),
          patch.object(uia_send, "_ensure_accessibility"),
          patch.object(uia_send, "_activate", return_value=chat),
          patch.object(uia_send, "_open_exact_chat", return_value=box) as opened,
          patch.object(uia_send.ui, "find", return_value=box) as found):
        session = uia_send.SendSession(con, "b")
        assert session.chat()[-1] is box
        assert session.chat()[-1] is box
        assert opened.call_count == 1 and found.call_count == 1
    box.Name = "Alice"
    with (patch.object(uia_send, "_window", return_value=(123, 456)),
          patch.object(uia_send, "_ensure_accessibility"),
          patch.object(uia_send, "_activate", return_value=chat),
          patch.object(uia_send.ui, "find", return_value=box)):
        try:
            session.chat()
        except SendError:
            pass
        else:
            raise AssertionError("changed conversation was reused")

    dll = Path("C:/test/Weixin.dll")
    regions = [SimpleNamespace(path=str(dll), addr=addr)
               for addr in ("0x1000", "0x3000", "0x2000")]
    process = SimpleNamespace(memory_maps=lambda grouped: regions)
    with (patch.object(uia_send.psutil, "Process", return_value=process),
          patch.object(uia_send.probe_gate, "process_byte",
                       side_effect=lambda pid, addr: {0x1000: 77, 0x1001: 90}[addr])):
        assert uia_send._verified_module_base(123) == (dll, 0x1000)

    regions.append(SimpleNamespace(path=str(dll.parent.with_name("other") / dll.name),
                                   addr="0x4000"))
    with patch.object(uia_send.psutil, "Process", return_value=process):
        try:
            uia_send._verified_module_base(123)
        except SendError:
            pass
        else:
            raise AssertionError("mixed Weixin.dll paths were accepted")
    print("[+] UIA 发送对象唯一性与特殊会话过滤通过")


if __name__ == "__main__":
    main()
