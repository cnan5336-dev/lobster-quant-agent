#!/usr/bin/env python3
"""Save a manually entered CLIProxyAPI client key without reading an old key."""
import argparse
import getpass
import os
from pathlib import Path
import secrets
import stat
import sys
import warnings


class SafeError(Exception):
    """Messages are authored constants, never exception/input interpolation."""


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise SafeError('参数无效；仅支持 --replace 和 --help。请勿通过命令参数传入密钥。')


def identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def directory_identity(info):
    # Creating our temporary file legitimately changes directory size/timestamps.
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid


def target_stat(folder_fd):
    try:
        return os.stat('client-key', dir_fd=folder_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def check_target(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise SafeError('已有输入文件的类型、所有权或权限不符合要求；未读取或替换它。')


def save_key(replace=False):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SafeError('请在本机终端运行；禁止通过聊天、管道或命令参数传入密钥。')
    root = Path.home() / '.openclaw'
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid():
        raise SafeError('OpenClaw 私人目录不符合预期；未保存密钥。')
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = folder_fd = temp_fd = None
    temp_name = temp_identity = None
    try:
        root_fd = os.open(root, directory_flags)
        if directory_identity(os.fstat(root_fd)) != directory_identity(root_info):
            raise SafeError('目标目录在操作中发生变化；已停止。')
        try:
            folder_info = os.stat('cliproxy-private', dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            if replace:
                raise SafeError('待替换的输入文件不存在；首次保存请省略 --replace。')
            os.mkdir('cliproxy-private', mode=0o700, dir_fd=root_fd)
            folder_info = os.stat('cliproxy-private', dir_fd=root_fd, follow_symlinks=False)
        if (not stat.S_ISDIR(folder_info.st_mode) or folder_info.st_uid != os.getuid()
                or stat.S_IMODE(folder_info.st_mode) != 0o700):
            raise SafeError('目标目录必须由当前用户所有、权限为 0700 且不是符号链接；已停止。')
        folder_fd = os.open('cliproxy-private', directory_flags, dir_fd=root_fd)
        if directory_identity(os.fstat(folder_fd)) != directory_identity(folder_info):
            raise SafeError('目标目录在操作中发生变化；已停止。')
        initial = target_stat(folder_fd)
        if initial is not None and not replace:
            raise SafeError('输入文件已经存在；默认不覆盖。确认需要重录时，请明确加上 --replace。')
        if initial is None and replace:
            raise SafeError('待替换的输入文件不存在；首次保存请省略 --replace。')
        if initial is not None:
            check_target(initial)
        print('请输入 CLIProxyAPI 的客户端 API key（api-keys）。')
        print('不要输入 OAuth token、管理密码或 DeepSeek 密钥。输入过程不会显示字符。')
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            first = getpass.getpass('输入客户端密钥：')
            second = getpass.getpass('再次输入确认：')
        if (first != second or not first or len(first) > 4096
                or any(ord(char) < 33 or ord(char) > 126 for char in first)):
            raise SafeError('两次输入不一致、为空、过长或含不支持的字符；未保存密钥。')
        temp_name = '.client-key.tmp-' + secrets.token_hex(12)
        temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=folder_fd)
        os.fchmod(temp_fd, 0o600)
        temp_identity = os.fstat(temp_fd)
        pending = memoryview(first.encode('ascii'))
        while pending:
            written = os.write(temp_fd, pending)
            if written <= 0:
                raise SafeError('安全保存未完成；请检查本机状态，不要发送密钥内容。')
            pending = pending[written:]
        os.fsync(temp_fd)
        first = second = pending = None
        current_temp = os.stat(temp_name, dir_fd=folder_fd, follow_symlinks=False)
        if (current_temp.st_dev, current_temp.st_ino) != (temp_identity.st_dev, temp_identity.st_ino):
            raise SafeError('临时文件在操作中发生变化；已停止。')
        if (directory_identity(root.lstat()) != directory_identity(root_info)
                or directory_identity(os.stat('cliproxy-private', dir_fd=root_fd, follow_symlinks=False))
                != directory_identity(folder_info)):
            raise SafeError('目标目录在操作中发生变化；已停止。')
        current = target_stat(folder_fd)
        if replace:
            if current is None or identity(current) != identity(initial):
                raise SafeError('已有输入文件在确认期间发生变化；为避免覆盖新内容，已停止。')
            check_target(current)
            os.replace(temp_name, 'client-key', src_dir_fd=folder_fd, dst_dir_fd=folder_fd)
            temp_name = None
        else:
            if current is not None:
                raise SafeError('输入文件在确认期间被创建；未覆盖它。')
            # Atomic no-clobber publication; rename/replace would overwrite a racing creator.
            os.link(temp_name, 'client-key', src_dir_fd=folder_fd, dst_dir_fd=folder_fd,
                    follow_symlinks=False)
            os.unlink(temp_name, dir_fd=folder_fd)
            temp_name = None
        os.fsync(folder_fd)
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if temp_name is not None and temp_identity is not None and folder_fd is not None:
            try:
                remaining = os.stat(temp_name, dir_fd=folder_fd, follow_symlinks=False)
                if (remaining.st_dev, remaining.st_ino) == (temp_identity.st_dev, temp_identity.st_ino):
                    os.unlink(temp_name, dir_fd=folder_fd)
            except FileNotFoundError:
                pass
        if folder_fd is not None:
            os.close(folder_fd)
        if root_fd is not None:
            os.close(root_fd)
    print('客户端密钥已安全保存为本机 0600 文件；尚未进行接入或模型测试。')
    print('请回复“已重新输入密钥”或“已输入密钥”，不要发送密钥内容。')


def main(argv=None):
    try:
        parser = SafeArgumentParser(description='在本机终端隐藏输入 CLIProxyAPI 客户端密钥。')
        parser.add_argument('--replace', action='store_true', help='明确替换已有的 0600 输入文件；不读取旧密钥')
        args = parser.parse_args(argv)
        save_key(replace=args.replace)
        return 0
    except SafeError as error:
        print(str(error), file=sys.stderr)
    except getpass.GetPassWarning:
        print('终端不支持隐藏输入，已停止；不要改为明文输入。', file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print('输入已取消；请在本机终端重新操作，不要发送密钥内容。', file=sys.stderr)
    except Exception:
        # Never interpolate OS exceptions: filenames or mocked errors may contain secrets.
        print('安全保存未正常完成；请先检查本机文件状态，不要发送密钥内容。', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
