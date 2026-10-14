"""MySQL 接入层：连接、建表、批量装载、对账。

两个独立的库，结构对称，**绝不混表**：

* ``wx_chat``    —— 个人微信（``platform=wechat``）
* ``wecom_chat`` —— 企业微信（``platform=wecom``）

用法::

    from mysql_store import conn, schema
    c = conn.connect("wx_chat")
    schema.create_all(c, "wx_chat")
"""
