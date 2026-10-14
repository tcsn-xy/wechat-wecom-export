"""两个聊天库的建表 DDL。

设计要点（踩过的坑都体现在这里）
--------------------------------
1. **两个库彻底分开**：``wx_chat`` / ``wecom_chat``，表结构对称但互不引用，
   每张表都带 ``platform`` 列，防止将来被误合表。
2. **群 ID 原样保留**：微信 ``<数字>@chatroom``、企微 ``R:`` / ``S:`` / ``M:``
   前缀一律按原字符串存，**绝不做数值化或截断**。
3. **群昵称按群存**：``chat_members.room_nick`` 是 ``(chat_id, wxid)`` 上的属性，
   绝不能合并成全局 wxid→群昵称表（AGENTS 坑 #1）。
4. **message_0/1.db 是时间分片不是重复**：消息唯一键是
   ``(chat_pk, source_db, source_table, local_id)``，两片拼起来才是全量。
5. **媒体零遗漏靠约束落地**：每条媒体类消息在 ``media_items`` 必须有一行——
   要么 ``data`` 非空且 ``sha1`` 校验过，要么 ``unavailable=1`` 且有 ``reason``。
6. 所有 BLOB 用 ``LONGBLOB``；文本一律 utf8mb4；大字段单独分表 ``message_raw``，
   避免主表行过大影响扫描。
"""
from __future__ import annotations

TABLES = [
    # ------------------------------------------------------------------ #
    """CREATE TABLE IF NOT EXISTS `accounts` (
  `id`            INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `platform`      VARCHAR(16)  NOT NULL,
  `account_key`   VARCHAR(191) NOT NULL COMMENT '微信 wxid / 企微 corp uid',
  `display_name`  VARCHAR(255) DEFAULT NULL,
  `alias`         VARCHAR(191) DEFAULT NULL COMMENT '微信号（登录 ID）',
  `uin`           VARCHAR(64)  DEFAULT NULL,
  `app_version`   VARCHAR(64)  DEFAULT NULL,
  `snapshot_time` DATETIME     DEFAULT NULL,
  `snapshot_note` VARCHAR(255) DEFAULT NULL,
  `imported_at`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_account` (`platform`, `account_key`),
  KEY `idx_account` (`account_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS `datasets` (
  `id`            INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `platform`      VARCHAR(16)  NOT NULL,
  `account_key`   VARCHAR(191) NOT NULL,
  `name`          VARCHAR(255) NOT NULL COMMENT '数据集/账号目录名',
  `kind`          VARCHAR(32)  NOT NULL DEFAULT 'live' COMMENT 'live / backup',
  `path`          VARCHAR(1024) DEFAULT NULL,
  `included`      TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '0=已排除',
  `exclude_reason` VARCHAR(255) DEFAULT NULL,
  `message_rows`  BIGINT       DEFAULT NULL,
  `snapshot_file_time` DATETIME DEFAULT NULL,
  `created_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_dataset` (`platform`, `account_key`, `name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS `contacts` (
  `id`          INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `platform`    VARCHAR(16)  NOT NULL,
  `account_key` VARCHAR(191) NOT NULL,
  `contact_id`  VARCHAR(320) NOT NULL COMMENT 'wxid / 企微 user_id / 208 字符的 v3_ 匿名 id',
  `alias`       VARCHAR(191) DEFAULT NULL COMMENT '微信号，只有好友有',
  `nick_name`   VARCHAR(255) DEFAULT NULL,
  `remark`      VARCHAR(255) DEFAULT NULL,
  `real_name`   VARCHAR(255) DEFAULT NULL,
  `corp_name`   VARCHAR(255) DEFAULT NULL,
  `extra`       JSON         DEFAULT NULL,
  `imported_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_contact` (`platform`, `contact_id`, `account_key`),
  KEY `idx_contact_nick` (`nick_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS `chats` (
  `chat_pk`      INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `platform`     VARCHAR(16)  NOT NULL,
  `account_key`  VARCHAR(191) NOT NULL,
  `chat_id`      VARCHAR(255) NOT NULL COMMENT '原样：<数字>@chatroom / wxid_x / R:xxx / S:x_y',
  `is_room`      TINYINT(1)   NOT NULL DEFAULT 0,
  `kind`         VARCHAR(32)  NOT NULL DEFAULT '单聊' COMMENT '群聊/单聊/系统应用/孤儿',
  `title`        VARCHAR(512) DEFAULT NULL COMMENT '群名或对方显示名',
  `title_extra`  JSON         DEFAULT NULL COMMENT '别名、备注等补充',
  `owner_id`     VARCHAR(320) DEFAULT NULL COMMENT '群主 wxid / user_id',
  `member_count` INT          DEFAULT NULL COMMENT '成员数（来自 ext_buffer 解析）',
  `message_count` BIGINT      DEFAULT NULL,
  `sender_count`  INT         DEFAULT NULL,
  `first_ts`     BIGINT       DEFAULT NULL,
  `last_ts`      BIGINT       DEFAULT NULL,
  `first_time`   DATETIME     DEFAULT NULL,
  `last_time`    DATETIME     DEFAULT NULL,
  `source_datasets` JSON      DEFAULT NULL COMMENT '出现过的源：["message_0.db", ...]',
  `is_orphan`    TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '反查不到会话的 Msg_ 表',
  `raw_meta`     JSON         DEFAULT NULL,
  `imported_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`chat_pk`),
  UNIQUE KEY `uk_chat` (`platform`, `account_key`, `chat_id`),
  KEY `idx_chat_room` (`is_room`),
  KEY `idx_chat_last` (`last_ts`),
  KEY `idx_chat_title` (`title`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS `chat_members` (
  `id`          INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `platform`    VARCHAR(16)  NOT NULL,
  `chat_pk`     INT UNSIGNED NOT NULL,
  `chat_id`     VARCHAR(255) NOT NULL,
  `contact_id`  VARCHAR(320) NOT NULL COMMENT 'wxid / user_id / 企微 sender id',
  `room_nick`   VARCHAR(255) DEFAULT NULL COMMENT '**群昵称，只对本 chat_id 有效**',
  `nick_name`   VARCHAR(255) DEFAULT NULL COMMENT '微信昵称/企微姓名',
  `remark`      VARCHAR(255) DEFAULT NULL,
  `alias`       VARCHAR(191) DEFAULT NULL,
  `shown`       VARCHAR(512) DEFAULT NULL COMMENT '实际显示名：备注→群昵称→昵称→微信号→id',
  `role`        VARCHAR(32)  DEFAULT NULL COMMENT '群主/我/成员',
  `message_count` BIGINT     DEFAULT NULL,
  `first_ts`    BIGINT       DEFAULT NULL,
  `last_ts`     BIGINT       DEFAULT NULL,
  `in_member_list` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否出现在群成员清单里',
  `imported_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_member` (`platform`, `chat_pk`, `contact_id`),
  KEY `idx_member_contact` (`contact_id`),
  KEY `idx_member_shown` (`shown`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # ------------------------------------------------------------------ #
    """CREATE TABLE IF NOT EXISTS `messages` (
  `id`           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `platform`     VARCHAR(16)  NOT NULL,
  `account_key`  VARCHAR(191) NOT NULL,
  `chat_pk`      INT UNSIGNED NOT NULL,
  `chat_id`      VARCHAR(255) NOT NULL,
  `local_id`     BIGINT       NOT NULL COMMENT '表内 local_id',
  `server_id`    VARCHAR(64)  DEFAULT NULL COMMENT 'server_id / msg_id，跨库去重用',
  `sort_seq`     BIGINT       DEFAULT NULL,
  `local_type`   BIGINT       NOT NULL COMMENT '完整 64 位（低位是类型，高位是标志）',
  `kind`         VARCHAR(32)  NOT NULL COMMENT '归一化类型',
  `sender_id`    VARCHAR(320) DEFAULT NULL COMMENT 'wxid / user_id / 208 字符的 v3_ 匿名 id',
  `sender_name`  VARCHAR(512) DEFAULT NULL COMMENT '显示名',
  `real_sender_id` BIGINT     DEFAULT NULL COMMENT '表里的数字 sender（用于反查）',
  `ts`           BIGINT       NOT NULL COMMENT 'unix 秒',
  `msg_time`     DATETIME     DEFAULT NULL,
  `text`         TEXT         COMMENT '展示文本',
  `msg_date`     DATE GENERATED ALWAYS AS (FROM_UNIXTIME(`ts`)) STORED COMMENT '按天查询用',
  `content_type` VARCHAR(64)  DEFAULT NULL COMMENT '企微 content_type / 微信 local_type 低位',
  `source_dataset` VARCHAR(255) DEFAULT NULL COMMENT 'message_0.db / 数据集名',
  `source_db`    VARCHAR(255) DEFAULT NULL COMMENT '源库文件',
  `source_table` VARCHAR(255) DEFAULT NULL COMMENT '源表名 Msg_<md5> / message_table',
  `has_media`    TINYINT(1)   NOT NULL DEFAULT 0,
  `has_link`     TINYINT(1)   NOT NULL DEFAULT 0,
  `extra`        JSON         DEFAULT NULL,
  `imported_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  `source_identity` CHAR(64) CHARACTER SET ascii GENERATED ALWAYS AS
    (SHA2(CAST(JSON_ARRAY(`platform`,`account_key`,`chat_id`,`source_dataset`,`source_db`,`source_table`,`local_id`) AS CHAR),256)) STORED,
  UNIQUE KEY `uk_msg` (`source_identity`),
  KEY `idx_msg_chat_ts` (`chat_pk`, `ts`),
  KEY `idx_msg_ts` (`ts`),
  KEY `idx_msg_sender` (`sender_id`),
  KEY `idx_msg_kind` (`kind`),
  KEY `idx_msg_server` (`server_id`),
  KEY `idx_msg_date` (`msg_date`),
  KEY `idx_msg_name_time` (`msg_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS `message_raw` (
  `message_pk`  BIGINT UNSIGNED NOT NULL,
  `platform`    VARCHAR(16)  NOT NULL,
  `raw_content` LONGTEXT     COMMENT '解压后的原始正文（XML/protobuf/文本原样）',
  `raw_blob`    LONGBLOB,
  `extra_blob`  LONGBLOB,
  `compress_type` INT        DEFAULT NULL COMMENT 'WCDB_CT_message_content',
  `raw_bytes`   INT          DEFAULT NULL,
  `decoded_from` VARCHAR(32) DEFAULT NULL COMMENT 'plain / zstd / protobuf / hex',
  PRIMARY KEY (`message_pk`),
  FULLTEXT KEY `ft_raw` (`raw_content`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS `message_links` (
  `id`          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `platform`    VARCHAR(16)  NOT NULL,
  `message_pk`  BIGINT UNSIGNED NOT NULL,
  `chat_id`     VARCHAR(255) NOT NULL,
  `ts`          BIGINT       NOT NULL,
  `url`         TEXT         NOT NULL,
  `url_hash`    CHAR(40)     NOT NULL COMMENT 'sha1(url)，用于前缀索引',
  `title`       VARCHAR(512) DEFAULT NULL,
  `des`         VARCHAR(1024) DEFAULT NULL,
  `app_type`    VARCHAR(32)  DEFAULT NULL COMMENT '5=链接 33=小程序 51=视频号 …',
  `origin`      VARCHAR(32)  DEFAULT NULL COMMENT 'text/appmsg/href/cdnurl/thumb',
  `extra`       JSON         DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_link` (`message_pk`, `url_hash`),
  KEY `idx_link_url` (`url_hash`),
  KEY `idx_link_ts` (`ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS `media_items` (
  `id`           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `platform`     VARCHAR(16)  NOT NULL,
  `account_key`  VARCHAR(191) NOT NULL DEFAULT '',
  `identity_key` CHAR(64) DEFAULT NULL,
  `message_pk`   BIGINT UNSIGNED DEFAULT NULL COMMENT '关联消息；目录型媒体可为空',
  `chat_id`      VARCHAR(255) DEFAULT NULL,
  `ts`           BIGINT       DEFAULT NULL,
  `msg_kind`     VARCHAR(32)  NOT NULL COMMENT 'image/file/video/voice/sticker/emotion',
  `media_role`   VARCHAR(32)  NOT NULL DEFAULT 'original' COMMENT 'original/thumb/attachment',
  `file_name`    VARCHAR(512) DEFAULT NULL,
  `ext`          VARCHAR(32)  DEFAULT NULL,
  `mime`         VARCHAR(128) DEFAULT NULL,
  `bytes`        BIGINT       DEFAULT NULL,
  `md5`          CHAR(32)     DEFAULT NULL COMMENT '消息里声明的 md5',
  `sha1`         CHAR(40)     DEFAULT NULL COMMENT '实际内容的 sha1（校验用）',
  `content_md5`  CHAR(32)     DEFAULT NULL,
  `width`        INT          DEFAULT NULL,
  `height`       INT          DEFAULT NULL,
  `duration_ms`  BIGINT       DEFAULT NULL,
  `source_kind`  VARCHAR(32)  DEFAULT NULL COMMENT 'attach/hardlink/cache/resource',
  `source_path`  VARCHAR(1024) DEFAULT NULL,
  `source_key`   VARCHAR(255) DEFAULT NULL COMMENT '配对键：md5 / data_index / 路径',
  `unavailable`  TINYINT(1)   NOT NULL DEFAULT 0,
  `reason`       VARCHAR(512) DEFAULT NULL,
  `data`         LONGBLOB     COMMENT '原始字节。unavailable=1 时可为空',
  `is_thumb`     TINYINT(1)   NOT NULL DEFAULT 0,
  `needs_decode` TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '如语音 SILK 未解码',
  `decoded_mime` VARCHAR(128) DEFAULT NULL,
  `decoded_data` LONGBLOB     DEFAULT NULL,
  `decoded_sha1` CHAR(40)     DEFAULT NULL,
  `imported_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_media_identity` (`identity_key`),
  KEY `idx_media_msg` (`message_pk`),
  KEY `idx_media_chat` (`chat_id`, `ts`),
  KEY `idx_media_md5` (`md5`),
  KEY `idx_media_sha1` (`sha1`),
  KEY `idx_media_kind` (`msg_kind`),
  KEY `idx_media_unavail` (`unavailable`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS `blob_parts` (
  `id`          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `table_name`  VARCHAR(64)  NOT NULL,
  `row_id`      BIGINT UNSIGNED NOT NULL,
  `part_no`     INT          NOT NULL,
  `data`        LONGBLOB     NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_part` (`table_name`, `row_id`, `part_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # 企业微信独有：明文 conv_snapshot（会话列表 + 每个会话最后一条消息）
    """CREATE TABLE IF NOT EXISTS `snapshot_conversations` (
  `id`            INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `platform`      VARCHAR(16)  NOT NULL,
  `account_key`   VARCHAR(191) NOT NULL,
  `conv_id`       VARCHAR(255) NOT NULL,
  `name`          VARCHAR(512) DEFAULT NULL,
  `peer_id`       VARCHAR(255) DEFAULT NULL,
  `last_ts`       BIGINT       DEFAULT NULL,
  `last_time`     DATETIME     DEFAULT NULL,
  `sender_name`   VARCHAR(512) DEFAULT NULL,
  `sender_id`     VARCHAR(320) DEFAULT NULL,
  `content_type`  VARCHAR(64)  DEFAULT NULL,
  `last_text`     TEXT,
  `imported_at`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_snapshot_conv` (`platform`, `account_key`, `conv_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # ------------------------------------------------------------------ #
    """CREATE TABLE IF NOT EXISTS `import_sources` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `platform`      VARCHAR(16)  NOT NULL,
  `account_key`   VARCHAR(191) NOT NULL,
  `dataset`       VARCHAR(255) NOT NULL COMMENT '数据集名',
  `source_db`     VARCHAR(255) NOT NULL,
  `source_table`  VARCHAR(255) NOT NULL,
  `chat_id`       VARCHAR(255) DEFAULT NULL,
  `source_rows`   BIGINT       NOT NULL DEFAULT 0,
  `inserted_rows` BIGINT       NOT NULL DEFAULT 0,
  `missing_rows`  BIGINT       NOT NULL DEFAULT 0,
  `min_ts`        BIGINT       DEFAULT NULL,
  `max_ts`        BIGINT       DEFAULT NULL,
  `media_ok`      BIGINT       NOT NULL DEFAULT 0,
  `media_missing` BIGINT       NOT NULL DEFAULT 0,
  `link_rows`     BIGINT       NOT NULL DEFAULT 0,
  `note`          VARCHAR(512) DEFAULT NULL,
  `checked_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  -- ⚠️ **不要**在这张表上建唯一键。同一个 `Msg_<md5>` 表名会**同时**出现在
  -- message_0.db 与 message_1.db，而且是**同一个 chat_id**（分片不是重复）。
  -- 无论唯一键是 (platform,db,table) 还是带上 chat_id，都会把其中一行挤掉
  -- ——实测 453 张源表只剩 452 行，message_0.db 少算 1 张。
  -- 幂等由 `wx_import._save_source` 先 DELETE 再 INSERT 保证。
  KEY `idx_source_lookup` (`platform`, `source_db`(60), `source_table`(60), `chat_id`(60)),
  KEY `idx_source_chat` (`chat_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS `import_reconciliation` (
  `id`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `platform`   VARCHAR(16)  NOT NULL,
  `scope`      VARCHAR(32)  NOT NULL COMMENT 'total/db/chat/media/link',
  `name`       VARCHAR(255) NOT NULL COMMENT '库名或会话 ID',
  `metric`     VARCHAR(64)  NOT NULL,
  `source_value` BIGINT     DEFAULT NULL,
  `dest_value`   BIGINT     DEFAULT NULL,
  `diff`       BIGINT       DEFAULT NULL,
  `status`     VARCHAR(16)  NOT NULL COMMENT 'ok/warn/error',
  `detail`     VARCHAR(1024) DEFAULT NULL,
  `checked_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_recon_scope` (`scope`),
  KEY `idx_recon_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]

VIEWS = [
    """CREATE OR REPLACE VIEW `v_chat_summary` AS
SELECT c.`chat_pk`, c.`chat_id`, c.`title`, c.`kind`, c.`is_room`, c.`is_orphan`,
       c.`member_count`, c.`message_count`, c.`first_time`, c.`last_time`,
       (SELECT COUNT(*) FROM `media_items` m WHERE m.`chat_id` = c.`chat_id`) AS media_rows,
       (SELECT COUNT(*) FROM `media_items` m WHERE m.`chat_id` = c.`chat_id`
                                               AND m.`unavailable` = 0) AS media_ok
FROM `chats` c""",

    """CREATE OR REPLACE VIEW `v_media_coverage` AS
SELECT `msg_kind`, `media_role`,
       COUNT(*) AS total_rows,
       SUM(CASE WHEN `unavailable` = 0 THEN 1 ELSE 0 END) AS with_blob,
       SUM(CASE WHEN `unavailable` = 1 THEN 1 ELSE 0 END) AS unavailable,
       SUM(CASE WHEN `is_thumb` = 1 THEN 1 ELSE 0 END)     AS thumbs,
       SUM(CASE WHEN `needs_decode` = 1 THEN 1 ELSE 0 END) AS needs_decode,
       ROUND(SUM(COALESCE(`bytes`, 0)) / 1024 / 1024, 1)   AS total_mb
FROM `media_items`
GROUP BY `msg_kind`, `media_role`""",

    """CREATE OR REPLACE VIEW `v_chunked_media` AS
SELECT m.`id` AS media_id, m.`chat_id`, m.`msg_kind`, m.`file_name`, m.`bytes`,
       COUNT(b.`id`) AS parts,
       ROUND(SUM(LENGTH(b.`data`)) / 1024 / 1024, 1) AS stored_mb
FROM `media_items` m
JOIN `blob_parts` b ON b.`table_name` = 'media_items' AND b.`row_id` = m.`id`
GROUP BY m.`id`, m.`chat_id`, m.`msg_kind`, m.`file_name`, m.`bytes`""",

    """CREATE OR REPLACE VIEW `v_message_kinds` AS
SELECT `kind`, COUNT(*) AS n,
       MIN(`msg_time`) AS first_time, MAX(`msg_time`) AS last_time
FROM `messages` GROUP BY `kind` ORDER BY n DESC""",

    """CREATE OR REPLACE VIEW `v_errors` AS
SELECT `scope`, `name`, `metric`, `source_value`, `dest_value`, `diff`, `detail`
FROM `import_reconciliation` WHERE `status` <> 'ok'""",
]


def create_all(connection, db: str):
    """建库（如不存在）+ 建表 + 建视图。"""
    from .conn import database_name
    database_name(db)
    with connection.cursor() as cur:
        cur.execute("CREATE DATABASE IF NOT EXISTS `%s` "
                    "DEFAULT CHARACTER SET utf8mb4 "
                    "COLLATE utf8mb4_0900_ai_ci" % db)
        cur.execute("USE `%s`" % db)
    connection.commit()
    with connection.cursor() as cur:
        for ddl in TABLES:
            cur.execute(ddl)
    connection.commit()
    with connection.cursor() as cur:
        migrate(connection)
        for ddl in VIEWS:
            cur.execute(ddl)
    connection.commit()


def drop_db(connection, db: str):
    from .conn import database_name
    database_name(db)
    with connection.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS `%s`" % db)
    connection.commit()


def migrate(connection):
    """Repeatable additive migrations; callers select the target database."""
    additions = {
        "message_raw": {"raw_blob": "LONGBLOB", "extra_blob": "LONGBLOB"},
        "media_items": {"account_key": "VARCHAR(191) NOT NULL DEFAULT ''",
                        "identity_key": "CHAR(64) DEFAULT NULL", "decoded_sha1":"CHAR(40) DEFAULT NULL",
                        "content_md5":"CHAR(32) DEFAULT NULL"},
    }
    with connection.cursor() as cur:
        import re
        cur.execute("SELECT id,dataset,source_db,source_table FROM import_sources "
                    "WHERE platform=%s AND chat_id=%s",("wechat","wechat-snapshot"))
        for row in cur.fetchall():
            if (re.fullmatch(r"(?:biz_)?message_\d+\.db",row['dataset']) and
                    re.fullmatch(r"Msg_[a-fA-F0-9]{32}",row['source_db'])):
                cur.execute("UPDATE import_sources SET dataset=%s,source_db=%s,source_table=%s,chat_id=%s WHERE id=%s",
                            ("wechat-snapshot",row['dataset'],row['source_db'],row['source_table'],row['id']))
        cur.execute("SELECT id,platform,account_key,dataset,source_db,source_table,chat_id FROM import_sources ORDER BY id DESC")
        seen=set()
        for row in cur.fetchall():
            key=tuple(row[k] for k in ('platform','account_key','dataset','source_db','source_table','chat_id'))
            if key in seen:cur.execute("DELETE FROM import_sources WHERE id=%s",(row['id'],))
            seen.add(key)
        for table,key,column,definition in (
                ("contacts","uk_contact","contact_id","platform,contact_id,account_key"),
                ("snapshot_conversations","uk_snapshot_conv","conv_id","platform,account_key,conv_id")):
            cur.execute("SHOW INDEX FROM `%s` WHERE Key_name=%%s" % table,(key,))
            if any(r["Column_name"]==column and r["Sub_part"] is not None for r in cur.fetchall()):
                cur.execute("ALTER TABLE `%s` DROP INDEX `%s`, ADD UNIQUE KEY `%s` (%s)" % (table,key,key,definition))
        for table, fields in additions.items():
            cur.execute("SHOW COLUMNS FROM `%s`" % table)
            existing = {r["Field"] for r in cur.fetchall()}
            for name, ddl in fields.items():
                if name not in existing:
                    cur.execute("ALTER TABLE `%s` ADD COLUMN `%s` %s" % (table, name, ddl))
        cur.execute("SHOW INDEX FROM media_items WHERE Key_name='uk_media_identity'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE media_items ADD UNIQUE KEY uk_media_identity (identity_key)")
        from .conn import media_identity
        cur.execute("SHOW COLUMNS FROM messages LIKE 'source_identity'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE messages ADD COLUMN source_identity CHAR(64) CHARACTER SET ascii "
                        "GENERATED ALWAYS AS (SHA2(CAST(JSON_ARRAY(platform,account_key,chat_id,source_dataset,source_db,source_table,local_id) AS CHAR),256)) STORED, "
                        "DROP INDEX uk_msg, ADD UNIQUE KEY uk_msg (source_identity)")
        cur.execute("SELECT i.id,i.platform,i.message_pk,i.msg_kind,i.source_key,i.md5,i.source_path,"
                    "COALESCE(m.account_key,i.account_key) account_key FROM media_items i "
                    "LEFT JOIN messages m ON m.id=i.message_pk WHERE i.identity_key IS NULL "
                    "ORDER BY i.unavailable,i.id")
        legacy = cur.fetchall()
        for row in legacy:
            if not row['account_key']:
                cur.execute("SELECT DISTINCT account_key FROM messages WHERE platform=%s",(row['platform'],))
                owners=cur.fetchall()
                if len(owners)!=1:
                    raise ValueError("旧媒体不能唯一对应账号，请从快照重建独立候选库")
                row['account_key']=owners[0]['account_key']
            key = media_identity(row)
            cur.execute("SELECT id FROM media_items WHERE identity_key=%s", (key,))
            if cur.fetchone():
                cur.execute("DELETE FROM blob_parts WHERE table_name=%s AND row_id=%s",
                            ("media_items", row["id"]))
                cur.execute("DELETE FROM media_items WHERE id=%s", (row["id"],))
            else:
                cur.execute("UPDATE media_items SET identity_key=%s,account_key=%s WHERE id=%s",
                            (key, row["account_key"], row["id"]))
        import hashlib
        cur.execute("SELECT id FROM media_items WHERE (data IS NOT NULL AND content_md5 IS NULL) "
                    "OR (decoded_data IS NOT NULL AND decoded_sha1 IS NULL)")
        inline=cur.fetchall()
        for entry in inline:
            cur.execute("SELECT data,content_md5,decoded_data,decoded_sha1 FROM media_items WHERE id=%s",(entry['id'],))
            row=cur.fetchone()
            md5=row['content_md5'] or (hashlib.md5(row['data']).hexdigest() if row['data'] is not None else None)
            sha=row['decoded_sha1'] or (hashlib.sha1(row['decoded_data']).hexdigest() if row['decoded_data'] is not None else None)
            cur.execute("UPDATE media_items SET content_md5=%s,decoded_sha1=%s WHERE id=%s",(md5,sha,entry['id']))
        cur.execute("SELECT id FROM media_items WHERE unavailable=0 AND data IS NULL AND content_md5 IS NULL")
        chunked=cur.fetchall()
        for row in chunked:
            cur.execute("SELECT part_no FROM blob_parts WHERE table_name=%s AND row_id=%s ORDER BY part_no",('media_items',row['id']))
            parts=cur.fetchall();digest=hashlib.md5()
            for part in parts:
                cur.execute("SELECT data FROM blob_parts WHERE table_name=%s AND row_id=%s AND part_no=%s",('media_items',row['id'],part['part_no']))
                digest.update(cur.fetchone()['data'])
            if parts:
                cur.execute("UPDATE media_items SET content_md5=%s WHERE id=%s",(digest.hexdigest(),row['id']))
        cur.execute("CREATE TABLE IF NOT EXISTS schema_versions (id INT PRIMARY KEY, version INT NOT NULL)")
        cur.execute("INSERT INTO schema_versions VALUES (1,2) ON DUPLICATE KEY UPDATE version=2")
        cur.execute("CREATE TABLE IF NOT EXISTS import_runs (id BIGINT AUTO_INCREMENT PRIMARY KEY, "
                    "platform VARCHAR(16) NOT NULL, scope JSON NOT NULL, media_enabled BOOL NOT NULL, "
                    "status VARCHAR(16) NOT NULL, detail TEXT, started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    connection.commit()
