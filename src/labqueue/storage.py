"""在打开 SQLite 之前验证运行账号的存储边界。"""

import json
import os
import sqlite3
import stat
import subprocess
from pathlib import Path

# 通过标准输入传路径，查询命令固定。SID 分别表示系统、管理员和对象所有者。
_CHECK_ACL = """
$ErrorActionPreference = 'Stop'
$owners = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value,
            'S-1-5-18', 'S-1-5-32-544')
$trusted = $owners + 'S-1-3-4'
foreach ($path in (ConvertFrom-Json ([Console]::In.ReadToEnd()))) {
    $acl = if ([IO.Directory]::Exists($path)) {
        [IO.Directory]::GetAccessControl($path)
    } else {
        [IO.File]::GetAccessControl($path)
    }
    $sd = [Security.AccessControl.RawSecurityDescriptor]::new(
        $acl.GetSecurityDescriptorBinaryForm(), 0)
    if ($sd.Owner.Value -notin $owners -or $null -eq $sd.DiscretionaryAcl) { exit 1 }
    $inheritFiles = $false
    foreach ($ace in $sd.DiscretionaryAcl) {
        if ($ace.AceType -ne 'AccessAllowed' -or $ace.SecurityIdentifier.Value -notin $trusted) {
            exit 1
        }
        if ($ace.AceFlags.HasFlag([Security.AccessControl.AceFlags]::ObjectInherit)) {
            $inheritFiles = $true
        }
    }
    if ([IO.Directory]::Exists($path) -and -not $inheritFiles) { exit 1 }
}
"""


def rollback_open_transaction(db: sqlite3.Connection) -> bool:
    """只在能确认开放事务已回滚时返回真。"""
    try:
        if db.in_transaction:
            db.execute("ROLLBACK")
            return True
    except sqlite3.Error:
        # 原连接可能已关闭，提交结果须从持久状态核对。
        pass
    return False


def default_database() -> Path:
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "LabQueue" / "labqueue.sqlite3"
    return Path.home() / ".local" / "share" / "labqueue" / "labqueue.sqlite3"


def verify_storage(database: Path) -> None:
    directory = database.parent
    paths = [directory]
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = database.with_name(database.name + suffix)
        if os.path.lexists(path):
            paths.append(path)

    for path in paths:
        info = path.lstat()
        expected = stat.S_ISDIR if path == directory else stat.S_ISREG
        attributes = getattr(info, "st_file_attributes", 0)
        if not expected(info.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise PermissionError("数据库存储路径必须是普通目录或文件")
        if path != directory and info.st_nlink != 1:
            raise PermissionError("数据库文件有额外的硬链接")
        if os.name != "nt" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise PermissionError("数据库存储仅允许运行账号访问")

    if os.name == "nt":
        # 使用系统自带的 PowerShell 回读 NTFS ACL，路径作为参数传递。
        powershell = (
            Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", _CHECK_ACL],
            input=json.dumps(list(map(str, paths))).encode("ascii"),
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise PermissionError("无法确认数据库存储的 Windows 访问权限")


def prepare_storage(database: Path) -> None:
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    verify_storage(database)
    # SQLite 在 POSIX 上按主库权限创建 WAL、SHM 和回滚日志。
    try:
        fd = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(fd)
