"""发送前的联系人校验；纯数据库查询，不依赖 UI 自动化，界面也可直接使用。"""


class SendError(RuntimeError):
    pass


def display_name(con, talker: str) -> str:
    """Resolve one searchable label; reject ambiguous local labels."""
    if talker == "filehelper":
        raise SendError("文件传输助手不参与自动回复")
    if talker.endswith("@chatroom"):
        raise SendError("群聊尚未启用自动发送")
    row = con.execute(
        "SELECT nickname,remark,alias FROM contacts WHERE wxid=?", (talker,)).fetchone()
    if row is None:
        raise SendError("联系人不在本地通讯录")
    label = (row["remark"] or row["nickname"] or row["alias"] or "").strip()
    if not label:
        raise SendError("联系人没有可搜索的名称")
    duplicate = con.execute(
        "SELECT count(*) FROM contacts WHERE "
        "COALESCE(NULLIF(trim(remark),''),NULLIF(trim(nickname),''),"
        "NULLIF(trim(alias),''),'')=?", (label,)).fetchone()[0]
    if duplicate != 1:
        raise SendError("通讯录中存在同名联系人")
    return label
