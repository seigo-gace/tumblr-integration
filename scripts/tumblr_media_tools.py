"""Install pinned FFmpeg v8 binaries only into TGtumbler's app-owned private data.

Not a system package operation. Never rewrites system PATH, Python or Gserver
shared packages. Archive SHA-256 is pinned to Git LFS source at a fixed commit.
No Tumblr API calls or media downloads. The binary license needs normal FFmpeg
redistribution compliance if these files are ever shared (they are not committed).
"""
import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import zipfile

SOURCE_COMMIT="df95abcb0ce6efff710dda5ef28a2f6f1dc21493"
SOURCES={
 "x86_64":("linux","ca75b05e887c7a97676632f673031875847be83daa9794298fed9cef8cac14ad",142008975),
 "aarch64":("linux_arm64","e03efe471c03b999f10988d5db62ae3bd94837463291b3c7755528b100e97d6f",131816005),
}
MAX_DOWNLOAD=155*1024*1024

class InstallError(ValueError):
    pass

def _private_dir(path):
    path=Path(path).expanduser()
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise InstallError("TOOLS_DIRECTORY_UNSAFE")
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    if path.stat().st_uid!=os.getuid() or path.stat().st_mode & 0o077:
        raise InstallError("TOOLS_DIRECTORY_UNSAFE")
    return path

def _check(executable):
    if (not executable.is_file() or executable.is_symlink()
            or executable.stat().st_uid!=os.getuid()
            or executable.stat().st_mode & 0o077):
        raise InstallError("TOOLS_EXECUTABLE_UNSAFE")
    try:
        result=subprocess.run([str(executable),"-version"],capture_output=True,timeout=12,check=False)
    except (OSError,subprocess.TimeoutExpired):
        raise InstallError("TOOLS_EXECUTION_FAILED") from None
    if result.returncode or b"ffmpeg version" not in result.stdout.lower() and b"ffprobe version" not in result.stdout.lower():
        raise InstallError("TOOLS_EXECUTION_FAILED")

def install(folder):
    if platform.system()!="Linux":
        raise InstallError("TOOLS_PLATFORM_UNSUPPORTED")
    key=platform.machine().lower()
    if key not in SOURCES:
        raise InstallError("TOOLS_ARCH_UNSUPPORTED")
    label,expected_hash,expected_size=SOURCES[key]
    root=_private_dir(folder)
    if all((root/x).exists() for x in ("ffmpeg","ffprobe")):
        for name in ("ffmpeg","ffprobe"):_check(root/name)
        return "ALREADY_VERIFIED"
    if shutil.disk_usage(root).free<1024*1024*1024:
        raise InstallError("TOOLS_DISK_INSUFFICIENT")
    url=("https://media.githubusercontent.com/media/zackees/ffmpeg_bins/"
         +SOURCE_COMMIT+"/v8.0/"+label+".zip")
    with tempfile.TemporaryDirectory(prefix=".ffmpeg-stage-",dir=root) as stage:
        stage=Path(stage)
        os.chmod(stage,0o700)
        archive=stage/"tool.zip"
        digest=hashlib.sha256()
        count=0
        req=urllib.request.Request(url,headers={"User-Agent":"TGtumbler-MediaTools/1.0"})
        try:
            with urllib.request.urlopen(req,timeout=45) as response,open(archive,"xb") as dest:
                if response.url!=url or response.status!=200:
                    raise InstallError("TOOLS_DOWNLOAD_REDIRECT")
                while True:
                    chunk=response.read(65536)
                    if not chunk:break
                    count+=len(chunk)
                    if count>MAX_DOWNLOAD:
                        raise InstallError("TOOLS_DOWNLOAD_EXCESSIVE")
                    digest.update(chunk)
                    dest.write(chunk)
        except (OSError,TimeoutError) as exc:
            raise InstallError("TOOLS_DOWNLOAD_FAILED") from None
        if count!=expected_size or digest.hexdigest()!=expected_hash:
            raise InstallError("TOOLS_HASH_MISMATCH")
        with zipfile.ZipFile(archive) as zipped:
            for name in ("ffmpeg","ffprobe"):
                entry=label+"/"+name
                try:
                    info=zipped.getinfo(entry)
                except KeyError:
                    raise InstallError("TOOLS_ARCHIVE_INVALID") from None
                if info.file_size<100000 or info.file_size>200*1024*1024:
                    raise InstallError("TOOLS_ARCHIVE_INVALID")
                temp=stage/name
                with zipped.open(info) as src,open(temp,"xb") as dest:
                    shutil.copyfileobj(src,dest,65536)
                os.chmod(temp,0o700)
                with open(temp,"rb") as binary:
                    if binary.read(4)!=b"\x7fELF":
                        raise InstallError("TOOLS_EXECUTABLE_INVALID")
                _check(temp)
        for name in ("ffmpeg","ffprobe"):
            os.replace(stage/name,root/name)
        return "INSTALLED_VERIFIED"

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--app-data",required=True)
    args=p.parse_args()
    root=Path(args.app_data).expanduser()
    if not root.is_dir() or root.is_symlink() or root.stat().st_uid!=os.getuid():
        raise InstallError("APP_DATA_UNSAFE")
    try:
        result=install(root/"media-tools")
        print("MEDIA_TOOLS="+result)
        print("VERIFY=PASS")
    except (InstallError,zipfile.BadZipFile,zipfile.LargeZipFile) as exc:
        print("MEDIA_TOOLS=NOT_VERIFIED")
        print("ERROR_TYPE="+type(exc).__name__)
        raise SystemExit(48) from None

if __name__=="__main__":
    main()
