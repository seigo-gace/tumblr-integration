"""Pinned, isolated gallery-dl and yt-dlp installation for the existing app.

Only PyPI wheel downloads and private application paths. No global pip, sudo,
Tumblr requests, automatic authentication, browser cookies or provider probing.
"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import venv

WHEELS=(
 ("gallery_dl-1.32.19-py3-none-any.whl",
  "f6b1eb5e9a7409a988b754335d99dbb1873e130d133ed5d164d32e5e645d1c50"),
 ("yt_dlp-2026.10.26-py3-none-any.whl",
  "a05c5b5d8562f7e8a62f5334348b406af4930d56d3a65f8021d14af0e22339ae"),
)
HOST="files.pythonhosted.org"

class InstallerError(ValueError):
    pass

def install(app_data):
    root=Path(app_data).expanduser()
    if (not root.is_dir() or root.is_symlink() or root.stat().st_uid!=os.getuid()
            or root.stat().st_mode & 0o077):
        raise InstallerError("APP_DATA_UNSAFE")
    target=root/"media-extractors"
    if target.exists():
        if target.is_symlink() or not target.is_dir() or target.stat().st_mode & 0o077:
            raise InstallerError("EXTRACTOR_DIRECTORY_UNSAFE")
        binary=target/"bin"
        if (binary/"gallery-dl").is_file() and (binary/"yt-dlp").is_file():
            for name in ("gallery-dl","yt-dlp"):
                executable=binary/name
                if executable.is_symlink() or executable.stat().st_mode & 0o077:
                    raise InstallerError("EXTRACTOR_BINARY_UNSAFE")
                check=subprocess.run([str(executable),"--version"],capture_output=True,timeout=15)
                if check.returncode:
                    raise InstallerError("EXTRACTOR_CHECK_FAILED")
            return "ALREADY_VERIFIED"
        raise InstallerError("EXTRACTOR_PARTIAL_INSTALL")
    if shutil.disk_usage(root).free<1024*1024*1024:
        raise InstallerError("EXTRACTOR_DISK_LOW")
    with tempfile.TemporaryDirectory(prefix=".extractors-",dir=root) as tmp:
        stage=Path(tmp)
        os.chmod(stage,0o700)
        venv_dir=stage/"venv"
        venv.EnvBuilder(with_pip=True,clear=True).create(venv_dir)
        pip=venv_dir/"bin"/"pip"
        if not pip.is_file():
            raise InstallerError("EXTRACTOR_PIP_MISSING")
        # The pinned first-party wheels are exact checked bytes. Resolve URLs
        # from the official PyPI JSON listing rather than untrusted search links.
        for filename,expected in WHEELS:
            pkg="gallery-dl" if filename.startswith("gallery") else "yt-dlp"
            version="1.32.19" if pkg=="gallery-dl" else "2026.10.26"
            import json
            from urllib.parse import urlsplit
            with urllib.request.urlopen(
                urllib.request.Request("https://pypi.org/pypi/"+pkg+"/"+version+"/json",
                     headers={"User-Agent":"TGtumbler-MediaInstaller/1.0"}),timeout=20) as response:
                metadata=json.load(response)
            matching=[x for x in metadata.get("urls",[]) if x.get("filename")==filename]
            if len(matching)!=1:
                raise InstallerError("EXTRACTOR_WHEEL_UNAVAILABLE")
            wheel_info=matching[0]
            url=wheel_info["url"]
            parsed=urlsplit(url)
            if parsed.scheme!="https" or parsed.hostname!=HOST:
                raise InstallerError("EXTRACTOR_UNTRUSTED_SOURCE")
            if wheel_info["digests"]["sha256"]!=expected:
                raise InstallerError("EXTRACTOR_REMOTE_HASH_MISMATCH")
            local=stage/filename
            size=0
            digest=hashlib.sha256()
            with urllib.request.urlopen(
                urllib.request.Request(url,headers={"User-Agent":"TGtumbler-MediaInstaller/1.0"}),timeout=40) as response,open(local,"xb") as output:
                final=urlsplit(response.url)
                if final.scheme!="https" or final.hostname!=HOST or response.status!=200:
                    raise InstallerError("EXTRACTOR_DOWNLOAD_REDIRECT")
                while True:
                    chunk=response.read(65536)
                    if not chunk:break
                    size+=len(chunk)
                    if size>8*1024*1024:
                        raise InstallerError("EXTRACTOR_WHEEL_TOO_LARGE")
                    output.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest()!=expected:
                raise InstallerError("EXTRACTOR_HASH_MISMATCH")
        # Official pinned wheels first. The gallery-dl 'requests' dependency is
        # resolved by pip in this private venv (no shared Gserver changes).
        cmd=[str(pip),"install","--disable-pip-version-check","--no-input",
             "--no-cache-dir",str(stage/WHEELS[0][0]),str(stage/WHEELS[1][0])]
        result=subprocess.run(cmd,capture_output=True,timeout=150,check=False)
        if result.returncode:
            raise InstallerError("EXTRACTOR_INSTALL_FAILED")
        for name,expected in (("gallery-dl",b"1.32.19"),("yt-dlp",b"2026.10.26")):
            tool=venv_dir/"bin"/name
            if not tool.is_file():
                raise InstallerError("EXTRACTOR_BINARY_MISSING")
            check=subprocess.run([str(tool),"--version"],capture_output=True,timeout=20)
            if check.returncode or expected not in check.stdout:
                raise InstallerError("EXTRACTOR_VERSION_INVALID")
        # venv script symlinks refer to stage paths. Rebuild scripts with the
        # final environment prefix after atomic promotion.
        final=target
        os.rename(venv_dir,final)
        for name,module in (("gallery-dl","gallery_dl"),("yt-dlp","yt_dlp")):
            tool=final/"bin"/name
            tool.write_text("#!"+str(final/"bin"/"python")+"\nimport runpy\n"
                            "runpy.run_module('"+module+"',run_name='__main__')\n")
            os.chmod(tool,0o700)
        return "INSTALLED_VERIFIED"

if __name__=="__main__":
    if len(sys.argv)!=2:raise SystemExit(2)
    try:
        state=install(sys.argv[1])
        print("EXTRACTORS="+state)
        print("VERIFY=PASS")
    except (InstallerError,OSError,subprocess.TimeoutExpired) as e:
        print("EXTRACTORS=FAILED")
        print("ERROR_TYPE="+type(e).__name__)
        raise SystemExit(48) from None
