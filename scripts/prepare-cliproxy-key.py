#!/usr/bin/env python3
"""Accept a user-entered proxy client key without echo, argv or shell history."""
import getpass
import os
from pathlib import Path
import stat
import sys
import warnings


def main():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit('请在本机终端运行此程序；禁止通过聊天、管道或命令参数传入密钥。')
    root = Path.home() / '.openclaw'
    folder = root / 'cliproxy-private'
    if root.is_symlink() or not root.is_dir():
        raise SystemExit('OpenClaw 私人目录不符合预期，未做任何修改。')
    if folder.exists() or folder.is_symlink():
        info = folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise SystemExit('目标目录权限不符合预期，未读取或覆盖已有文件。')
    else:
        folder.mkdir(mode=0o700)
    target = folder / 'client-key'
    if target.exists() or target.is_symlink():
        raise SystemExit('输入文件已经存在；本程序不会读取或覆盖它。请让助手先核对接入状态。')
    print('这里只保存你手动提供的 CLIProxyAPI 客户端密钥。')
    print('不会读取代理现有密钥、改变 DeepSeek、启动模型请求或启用代理。')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        first = getpass.getpass('输入客户端密钥（屏幕不会显示）：')
        second = getpass.getpass('再次输入确认（屏幕不会显示）：')
    if first != second or not first or any(ord(c) < 33 or ord(c) == 127 for c in first):
        raise SystemExit('两次输入不一致、为空或含空白/控制字符；未保存密钥。')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(target, flags, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(first)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    first = second = None
    print('密钥已保存为本机私有 0600 文件；接入及模型测试尚未执行。')
    print('请回复“已输入密钥”，不要发送密钥内容。')


if __name__ == '__main__':
    try:
        main()
    except getpass.GetPassWarning:
        raise SystemExit('终端不支持隐藏输入，已停止；不要改为明文输入。')
