COMMANDS = {}        # 命令名 -> (帮助文本, 处理函数)
COMMAND_GROUP = {}   # 命令名(含别名) -> 所属子集
PRIMARY_COMMANDS = set()   # 主命令名（不含别名），纠错提示优先用

# 子集定义：名称 -> (中文名, 别名)
NAMESPACES = {
    'play': ('播放控制', ('播放',)),
    'pl':   ('自定义歌单', ('歌单',)),
    'set':  ('设置', ('设置项', 'setting', 'settings')),
    'sys':  ('系统', ('系统项', 'system')),
}


def register_command(name, help_text, handler, aliases=(), group='basic'):
    entry = (help_text, handler)
    COMMANDS[name] = entry
    COMMAND_GROUP[name] = group
    PRIMARY_COMMANDS.add(name)
    for alias in aliases:
        COMMANDS[alias] = entry
        COMMAND_GROUP[alias] = group


def command(name, help_text, aliases=(), group='basic'):
    """装饰器：一键注册新指令。

    group: basic = 表层命令；其余为子集名（play / pl / set / sys）
    """
    def decorator(func):
        register_command(name, help_text, func, aliases, group)
        return func
    return decorator


def resolve_namespace(word):
    """把 'play' / '播放' 这类词解析成子集名；不是子集返回 None。"""
    word = (word or '').lower()
    if word in NAMESPACES:
        return word
    for ns, (cn, aliases) in NAMESPACES.items():
        names = [cn] + list(aliases)
        if word in [a.lower() for a in names]:
            return ns
    return None


def namespace_name(ns):
    """子集的中文显示名。"""
    if ns in NAMESPACES:
        return NAMESPACES[ns][0]
    return ns or ''


def group_commands(ns):
    """返回某个子集下的 (命令名, 帮助文本) 列表（不含别名重复项）。"""
    seen = set()
    out = []
    for name, entry in COMMANDS.items():
        if COMMAND_GROUP.get(name) != ns:
            continue
        if id(entry) in seen:
            continue
        seen.add(id(entry))
        out.append((name, entry[0]))
    return out


def basic_commands():
    return group_commands('basic')
